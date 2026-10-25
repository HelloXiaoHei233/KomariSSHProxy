"""Authenticated MCP transport and the desktop application's MCP runtime."""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import replace
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from config import AgentConfig
from mcp_files import FileOperations
from terminal_exchange import send_and_read
from ssh_assist import SharedSSHServer
from ssh_bridge import load_or_create_host_key
from terminal_sessions import (
    KEYS, MAX_CONNECTIONS, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MAX_READ_BYTES,
    OperationLog, TerminalError, TerminalManager, utc_now,
)


ACTOR: ContextVar[str] = ContextVar("mcp_actor", default="mcp")
CLIENT: ContextVar[str] = ContextVar("mcp_client", default="local")
INSTRUCTIONS = """这是有状态的交互终端桥，不是 SSH exec 或命令执行 API。
先 list_terminals，再 connect_terminal；每次连接都会新建独立窗口，并报告该服务器窗口数。
后续操作必须使用 connection_id，不能用服务器 UUID 代替。
send_and_read 是唯一终端输入输出工具；text 可省略，不自动回车，pwd"Enter" 可一次输入并执行。
"Ctrl+C" 中断前台程序；方向键、Tab 等也支持引号语法。
需要原样输入带引号的键名（例如 echo "Enter"）时传 parse_keys=false。未知键名和其他文本原样保留。
返回原始 PTY 流（含 ANSI，stdout/stderr 合并），没有可靠的命令退出码或完成边界。
wait.mode=time 等待 wait_ms；idle 等待收到新输出后连续 idle_ms 无新字节；match 等待新输出出现 pattern 后 after_ms。
match 支持 regex=true；所有等待都有独立的 max_wait_ms 上限，0 立即刷新；关闭或取消提前返回，重连中不当作关闭。
read.mode=all/new/tail_bytes/tail_lines，分别返回全部保留、本次输入前位置之后的新增、末 count 字节/行。
不填 text、wait 使用默认零毫秒即可刷新；new 不填 text 时从本次调用开始观察。条件只观察本次调用后的输出。
默认仅返回 text；需要无损字节时传 include_base64=true 获取 data_base64。
长等待可自选唯一 wait_id，另一次 send_and_read 传 cancel_wait_id 停止它并让原调用返回最后输出；不会中断远端程序。
协议取消可能由客户端/SDK关闭响应，输出仍保留，可零毫秒刷新。原生跨POST取消使用同一Mcp-Client-Id请求头隔离客户端。
每窗口保留最近 4 MiB，dropped_bytes 表示游标之前的输出已淘汰；不同读取者互不消费数据。
用户可以通过界面或协助 SSH 同时输入。操作前查看 get_connection 的协作者和最近活动。
协助 SSH 只监听本机，由用户在界面启用；退出 SSH 连接仅退出协助，输入 exit/Ctrl+D 可能关闭共享 shell。
远端开启 ECHO 时才能看到输入回显；程序关闭 ECHO 时不会额外回显密码或按键。
任务结束请 disconnect_terminal。关闭 MCP 会断开所有 MCP 终端及协助 SSH。
reconnecting 表示连接断开，正在自动重连；请保留 connection_id 并等待恢复，不要新建窗口或重复发送输入。
超过粘性时限或远端会话已结束时才终止连接。发送失败的输入不会自动重放，恢复后先检查输出。
file_operation 使用 SSH SFTP 共用的面板原生文件接口，无需先打开终端。支持列目录、读文件、上传下载、重命名、权限和属主。
local_path 指 MCP 服务所在电脑的路径。upload 遇到同名文件失败，不支持上传替换或合成替换流程。
面板没有原子无覆盖参数；本服务串行化同节点文件操作，但不能消除外部写入者的竞争。
"""


def schema(properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


CONNECTION = {"type": "string", "minLength": 1, "maxLength": 128,
              "description": "connect_terminal 返回的独立窗口 connection_id。"}
MILLISECONDS = {"type": "integer", "minimum": 0, "maximum": 600000}
WAIT_SCHEMA = {"oneOf": [
    schema({"mode": {"const": "time"}, "wait_ms": MILLISECONDS, "max_wait_ms": MILLISECONDS},
           ("mode", "wait_ms", "max_wait_ms")),
    schema({"mode": {"const": "idle"}, "idle_ms": MILLISECONDS, "max_wait_ms": MILLISECONDS},
           ("mode", "idle_ms", "max_wait_ms")),
    schema({"mode": {"const": "match"}, "pattern": {"type": "string", "minLength": 1, "maxLength": 2048},
            "regex": {"type": "boolean", "default": False}, "after_ms": MILLISECONDS,
            "max_wait_ms": MILLISECONDS}, ("mode", "pattern", "after_ms", "max_wait_ms")),
]}
READ_SCHEMA = {"oneOf": [
    schema({"mode": {"enum": ["all", "new"]}}, ("mode",)),
    schema({"mode": {"const": "tail_bytes"}, "count": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_BYTES}}, ("mode", "count")),
    schema({"mode": {"const": "tail_lines"}, "count": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_BYTES}}, ("mode", "count")),
]}
PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
MODE = {"type": "string", "pattern": "^[0-7]{3,4}$"}
OFFSET = {"type": "integer", "minimum": 0}
FILE_VARIANTS = {
    "roots": ({}, ()),
    "list": ({"path": PATH, "offset": OFFSET, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ("path",)),
    "stat": ({"path": PATH}, ("path",)),
    "read": ({"path": PATH, "offset": OFFSET, "length": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BYTES}, "include_base64": {"type": "boolean"}}, ("path",)),
    "upload": ({"path": PATH, "local_path": PATH}, ("path", "local_path")),
    "download": ({"path": PATH, "local_path": PATH}, ("path", "local_path")),
    "mkdir": ({"path": PATH, "mode": MODE}, ("path",)),
    "rename": ({"path": PATH, "destination": PATH}, ("path", "destination")),
    "chmod": ({"path": PATH, "mode": MODE}, ("path", "mode")),
    "chown": ({"path": PATH, "uid": OFFSET, "gid": OFFSET}, ("path",)),
    "delete": ({"path": PATH, "recursive": {"type": "boolean", "default": False}}, ("path",)),
}
FILE_SCHEMA = {"type": "object", "oneOf": [schema({
    "server_uuid": CONNECTION, "operation": {"const": action}, **fields},
    ("server_uuid", "operation", *required)) for action, (fields, required) in FILE_VARIANTS.items()]}
for _variant in FILE_SCHEMA["oneOf"]:
    if _variant["properties"]["operation"]["const"] == "chown":
        _variant["anyOf"] = [{"required": ["uid"]}, {"required": ["gid"]}]
TOOLS: dict[str, dict[str, Any]] = {
    "get_help": dict(description="完整交互规则、鉴权及调用示例。", inputSchema=schema()),
    "get_service_info": dict(description="MCP 监听、HTTP 请求活动、协助 SSH、连接数和限制；不含密码。", inputSchema=schema()),
    "list_terminals": dict(description="仅列 UUID、名称和在线状态；query 按名称、UUID、IP、系统、分组、地区检索，空格分隔多个关键词取并集。IP等详情请指定UUID调用get_terminal。", inputSchema=schema({
        "query": {"type": "string", "maxLength": 256, "default": ""},
        "refresh": {"type": "boolean", "default": False, "description": "true 从面板重新获取服务器和状态。"}})),
    "get_terminal": dict(description="按唯一 UUID 查看单台服务器完整信息及性能快照，用于筛查同名节点；refresh=true 重新从面板取数。", inputSchema=schema({
        "server_uuid": CONNECTION, "refresh": {"type": "boolean", "default": False}}, ("server_uuid",))),
    "list_connections": dict(description="检查所有 MCP 窗口的详细状态、时间、计数、端点及协助者。", inputSchema=schema({
        "include_closed": {"type": "boolean", "default": False}})),
    "get_connection": dict(description="查看指定窗口和服务器的完整连接详情。", inputSchema=schema({"connection_id": CONNECTION}, ("connection_id",))),
    "connect_terminal": dict(description="新建独立终端窗口；重复连接同一服务器也不会复用或关闭原窗口。返回当前窗口数。", inputSchema=schema({
        "server_uuid": {"type": "string", "minLength": 1, "maxLength": 128},
        "label": {"type": "string", "maxLength": 100, "default": ""},
        "cols": {"type": "integer", "minimum": 20, "maximum": 500, "default": 120},
        "rows": {"type": "integer", "minimum": 8, "maximum": 300, "default": 36},
        "two_fa_code": {"type": "string", "maxLength": 32, "default": "", "description": "面板需要时填写；仅用于本次连接，不存储。"},
    }, ("server_uuid",))),
    "disconnect_terminal": dict(description="断开指定窗口及其协助者，原始流可在历史记录保留期间读取。重复断开安全。", inputSchema=schema({"connection_id": CONNECTION}, ("connection_id",))),
    "send_and_read": dict(description='可选输入并等待后读取；空输入、零等待立即刷新。支持固定时间、输出静默、关键词/正则后延时和独立最大等待。', inputSchema=schema({
        "connection_id": CONNECTION, "text": {"type": "string", "maxLength": MAX_INPUT_BYTES, "default": ""},
        "parse_keys": {"type": "boolean", "default": True,
                       "description": "将双引号包裹的已知键名转换为按键；false 保留全部原文。"},
        "wait": WAIT_SCHEMA, "read": READ_SCHEMA,
        "include_base64": {"type": "boolean", "default": False},
        "wait_id": CONNECTION, "cancel_wait_id": CONNECTION,
    }, ("connection_id",))),
    "resize_terminal": dict(description="调整共享 PTY 尺寸；会影响同一窗口的所有协助者。", inputSchema=schema({
        "connection_id": CONNECTION, "cols": {"type": "integer", "minimum": 20, "maximum": 500},
        "rows": {"type": "integer", "minimum": 8, "maximum": 300},
    }, ("connection_id", "cols", "rows"))),
    "file_operation": dict(description="面板原生文件管理：roots/list/stat/read/upload/download/mkdir/rename/chmod/chown/delete。路径是节点绝对虚拟路径，local_path 是MCP主机绝对路径；上传和重命名拒绝同名目标，不提供替换。", inputSchema=FILE_SCHEMA),
    "read_operation_logs": dict(description="按游标读取可导出的操作日志流；不记录密码或输入正文。", inputSchema=schema({
        "cursor": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
    })),
}
for _name, _definition in TOOLS.items():
    _read_only = _name.startswith(("get_", "list_", "read_"))
    _definition["annotations"] = {"readOnlyHint": _read_only,
                                  "destructiveHint": not _read_only,
                                  "idempotentHint": _read_only or _name == "disconnect_terminal",
                                  "openWorldHint": True}


def public_help() -> str:
    examples = [
        {"name": "list_terminals", "arguments": {}},
        {"name": "connect_terminal", "arguments": {"server_uuid": "<SERVER_UUID>", "label": "诊断"}},
        {"name": "send_and_read", "arguments": {"connection_id": "<CONNECTION_ID>", "text": 'pwd"Enter"',
            "wait": {"mode": "idle", "idle_ms": 300, "max_wait_ms": 10000}, "read": {"mode": "new"}}},
        {"name": "send_and_read", "arguments": {"connection_id": "<CONNECTION_ID>", "read": {"mode": "tail_lines", "count": 100}}},
        {"name": "send_and_read", "arguments": {"connection_id": "<CONNECTION_ID>", "text": '"Ctrl+C"'}},
        {"name": "file_operation", "arguments": {"server_uuid": "<SERVER_UUID>", "operation": "list", "path": "/tmp"}},
        {"name": "disconnect_terminal", "arguments": {"connection_id": "<CONNECTION_ID>"}},
    ]
    return ("Komari SSH Proxy · MCP 操作帮助\n\n"
            "端点：POST /mcp（Streamable HTTP，由官方 MCP SDK 协商协议版本）。\n"
            "鉴权：Authorization: Bearer <MCP设置中的密码>，每个请求都必须携带。密码不能放进 URL。\n"
            "请求头：Content-Type: application/json；Accept: application/json, text/event-stream。\n"
            "需要客户端支持静态 Bearer/自定义请求头；本服务不提供 OAuth 登录流程。\n"
            "服务和公网访问默认关闭；本机 127.0.0.1，启用公网后监听 0.0.0.0。\n"
            "跨公网请通过 HTTPS 反向代理或加密隧道连接；填写对外 URL 以允许该域名。\n"
            "GET / 是公开的脱敏帮助；GET /logs 是需要 Bearer 鉴权的 JSONL 操作日志导出。\n\n"
            + INSTRUCTIONS + "\n工具调用示例（通过 MCP 客户端 tools/call）：\n"
            + json.dumps(examples, ensure_ascii=False, indent=2)
            + "\n\n原始 2025 协议初始化示例：\n"
            + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "your-agent", "version": "1.0"}}}, ensure_ascii=False, indent=2)
            + "\n初始化后发送 notifications/initialized。后续请求携带协商的 MCP-Protocol-Version。\n"
            "无 HTTP 会话 ID；终端使用 connection_id 保留状态。协议通知使用 202 空响应。\n"
            "远端掉线时窗口进入 reconnecting 并在粘性时限内自动重挂；文件管理由 file_operation 提供。\n"
            "输入错误会返回明确参数路径和期望格式；网络错误不会回显 Cookie、2FA 或密码。\n\n"
            "全部工具与严格参数格式（禁止未声明字段）：\n"
            + json.dumps(TOOLS, ensure_ascii=False, indent=2))


QUOTED_KEY = re.compile(r'"(' + "|".join(re.escape(key) for key in KEYS) + r')"')


def encode_terminal_input(text: str, parse_keys: bool = True) -> bytes:
    if parse_keys:
        text = QUOTED_KEY.sub(lambda match: KEYS[match[1]].decode("ascii"), text)
    return text.encode("utf-8")


def validate_password(password: str) -> None:
    if not 16 <= len(password) <= 128 or any(not 33 <= ord(char) <= 126 for char in password):
        raise TerminalError("密码需为 16–128 位可打印 ASCII 字符，不含空格或换行；可点击随机生成。")


def validate_public_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(char.isspace() for char in value) or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
    except ValueError:
        raise TerminalError("对外地址需为完整 http(s) URL，不含用户名、密码、查询参数或空白。") from None
    path = parsed.path.rstrip("/")
    if not path.endswith("/mcp"):
        path += "/mcp"
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise TerminalError("对外地址中的域名无效。") from None
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port is not None and parsed.port != default_port:
        host += f":{parsed.port}"
    return f"{parsed.scheme}://{host}{path}"


def agent_instructions(url: str, password: str) -> str:
    """只返回可直接粘贴的 MCP 客户端配置，不附加任何说明文字。"""
    configuration = {"mcpServers": {"komari-terminal": {
        "type": "http", "url": url, "headers": {"Authorization": f"Bearer {password}"}}}}
    return json.dumps(configuration, ensure_ascii=False, indent=2)


class HttpGuard:
    def __init__(self, app, owner: "MCPHttpServer", password: str):
        self.app = app
        self.owner = owner
        self.password_digest = hashlib.sha256(password.encode("ascii")).digest()
        self.failures: dict[str, deque[float]] = {}

    def trusted_host(self, host: str) -> bool:
        try:
            parsed = urlsplit("//" + host)
            if not parsed.hostname or parsed.username is not None or parsed.path or parsed.query or parsed.fragment:
                return False
            port = parsed.port
            if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
                return port in {None, self.owner.port}
            if not self.owner.public_access:
                return False
            configured = urlsplit(self.owner.public_url)
            if configured.hostname and parsed.hostname.lower() == configured.hostname.lower():
                default_port = 443 if configured.scheme == "https" else 80
                return (port or default_port) == (configured.port or default_port)
            ipaddress.ip_address(parsed.hostname)
            return port in {None, self.owner.port}
        except ValueError:
            return False

    def trusted_origin(self, origin: str, host: str) -> bool:
        if not origin:
            return True
        allowed = {f"http://127.0.0.1:{self.owner.port}", f"http://localhost:{self.owner.port}"}
        if self.owner.public_access:
            parsed = urlsplit(self.owner.public_url)
            if parsed.hostname:
                allowed.add(f"{parsed.scheme}://{parsed.netloc}")
            if self.trusted_host(host):
                try:
                    ipaddress.ip_address(urlsplit("//" + host).hostname or "")
                    allowed.update({f"http://{host}", f"https://{host}"})
                except ValueError:
                    pass
        return origin in allowed

    async def __call__(self, scope, receive, send) -> None:
        from starlette.responses import JSONResponse, PlainTextResponse

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1")
        origin = headers.get(b"origin", b"").decode("latin-1")

        async def fail(status: int, message: str, extra: dict | None = None) -> None:
            await JSONResponse({"error": message, "help": "/", "endpoint": "/mcp"}, status_code=status,
                               headers={"Cache-Control": "no-store", **(extra or {})})(scope, receive, send)

        if not self.trusted_host(host):
            await fail(421, "Host 未获允许；公网域名请先填入 MCP 设置的对外 URL。")
            return
        if not self.trusted_origin(origin, host):
            await fail(403, "Origin 未获允许；请使用配置的来源或不发送 Origin 的 MCP 客户端。")
            return
        if scope["path"] == "/" and scope["method"] in {"GET", "HEAD"}:
            await PlainTextResponse(public_help(), headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        peer = str((scope.get("client") or ("unknown", 0))[0])
        recent = self.failures.setdefault(peer, deque(maxlen=10))
        now = time.monotonic()
        while recent and now - recent[0] >= 60:
            recent.popleft()
        authorization = headers.get(b"authorization", b"")
        scheme, _, credential = authorization.partition(b" ")
        if (scheme.lower() != b"bearer" or not credential or
                not hmac.compare_digest(hashlib.sha256(credential).digest(), self.password_digest)):
            # A shared proxy/NAT peer must never lock out clients with the correct password.
            if len(recent) >= 10:
                await fail(429, "鉴权失败次数过多，请 60 秒后重试。", {"Retry-After": "60"})
                return
            recent.append(now)
            if len(self.failures) > 512:
                self.failures.pop(next(iter(self.failures)))
            self.owner.runtime.log.add("mcp_auth_rejected", actor=f"http:{peer}")
            await fail(401, "需要 Authorization: Bearer <MCP密码>。", {"WWW-Authenticate": 'Bearer realm="Komari MCP"'})
            return
        if scope["path"] == "/logs" and scope["method"] == "GET":
            self.owner.runtime.log.add("logs_exported", actor=f"mcp:{peer}")
            await PlainTextResponse(self.owner.runtime.log.export_text(), media_type="application/x-ndjson",
                                    headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="mcp-operations.jsonl"'})(scope, receive, send)
            return
        if scope["path"] != "/mcp":
            await fail(404, "未知路径；GET / 查看完整帮助，MCP 请求请使用 /mcp。")
            return
        if (scope["method"] == "POST" and
                headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower() != b"application/json"):
            await fail(415, "Content-Type 必须为 application/json，正文为 UTF-8 JSON。")
            return
        identity = self.owner.request_started(scope, headers)
        token = ACTOR.set(f"mcp:{peer}")
        client_token = CLIENT.set(hashlib.sha256(
            (identity + ":" + headers.get(b"mcp-client-id", b"").decode("latin-1")).encode()).hexdigest())
        try:
            await self.app(scope, receive, send)
        finally:
            ACTOR.reset(token)
            CLIENT.reset(client_token)
            self.owner.request_finished(identity)


class MCPHttpServer:
    def __init__(self, runtime: "McpRuntime"):
        self.runtime = runtime
        self.port = 0
        self.public_access = False
        self.public_url = ""
        self.started_at: str | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._error = ""
        self._lock = threading.Lock()
        self._clients: dict[str, dict[str, Any]] = {}
        self._executor: ThreadPoolExecutor | None = None

    @property
    def running(self) -> bool:
        return bool(self._server and self._server.started and not self._server.should_exit
                    and self._thread and self._thread.is_alive())

    @property
    def url(self) -> str:
        return self.public_url if self.public_access and self.public_url else f"http://127.0.0.1:{self.port}/mcp"

    def build_app(self, password: str):
        try:
            from mcp import types
            from mcp.server.lowlevel import Server
            from mcp.server.transport_security import TransportSecuritySettings
            from importlib.metadata import version
            if tuple(int(part) for part in version("mcp").split(".")[:2]) < (2, 2):
                raise ImportError("MCP SDK 2.2+ required")
        except ImportError:
            raise TerminalError("MCP 需要新版依赖，请运行 python -m pip install -r requirements.txt 后重启。") from None

        with self._lock:
            if self._executor is None:
                # A fixed pool reserves capacity beyond the 16 wait slots even
                # on low-core machines, where asyncio's default pool is small.
                self._executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="mcp-tool")
            executor = self._executor

        async def list_tools(context, params):
            return types.ListToolsResult(tools=[types.Tool(name=name, **definition) for name, definition in TOOLS.items()])

        async def call_tool(context, params):
            cancel_event = threading.Event()
            key = (CLIENT.get(), type(context.request_id).__name__, str(context.request_id))
            registered = False
            try:
                if key in requests_in_flight:
                    raise TerminalError("该客户端的 requestId 正在使用；请使用唯一 ID 或独立 Mcp-Client-Id。")
                requests_in_flight[key] = cancel_event
                registered = True
                ctx = copy_context()
                result = await asyncio.get_running_loop().run_in_executor(executor, lambda: ctx.run(
                    self.runtime.dispatch, params.name, params.arguments, cancel_event=cancel_event))
                return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                            structuredContent=result, isError=False)
            except TerminalError as exc:
                result = {"error": str(exc), "help": "调用 get_help 或访问 GET / 查看完整参数说明。"}
                return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                            structuredContent=result, isError=True)
            finally:
                cancel_event.set()
                if registered:
                    requests_in_flight.pop(key, None)

        requests_in_flight = {}

        async def cancel_tool(context, params):
            key = (CLIENT.get(), type(params.request_id).__name__, str(params.request_id))
            event = requests_in_flight.get(key)
            if event is not None:
                event.set()

        server = Server("komari-terminal", version="1.2.0", instructions=INSTRUCTIONS,
                        on_list_tools=list_tools, on_call_tool=call_tool)
        server.add_notification_handler("notifications/cancelled", types.CancelledNotificationParams, cancel_tool)
        # HttpGuard validates both Host and Origin, including configured reverse-proxy domains.
        app = server.streamable_http_app(json_response=True, stateless_http=True,
                                         max_request_body_size=1024 * 1024,
                                         transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        return HttpGuard(app, self, password)

    def start(self, config: AgentConfig) -> None:
        import uvicorn

        if self._thread and self._thread.is_alive():
            raise TerminalError("MCP 服务正在运行或尚未停止，请稍后重试。")
        validate_password(config.mcp_password)
        self.public_url = validate_public_url(config.mcp_public_url)
        self.public_access = config.mcp_public_access
        self.port = config.mcp_port
        if not 0 <= self.port <= 65535:
            raise TerminalError("MCP 端口无效。")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("0.0.0.0" if self.public_access else "127.0.0.1", self.port))
            listener.listen(64)
            self.port = listener.getsockname()[1]
            application = self.build_app(config.mcp_password)
            self._server = uvicorn.Server(uvicorn.Config(
                application, loop="asyncio", http="h11", ws="none", log_config=None,
                log_level="critical", access_log=False, proxy_headers=False,
                timeout_keep_alive=5, timeout_graceful_shutdown=2, limit_concurrency=64,
            ))
        except Exception:
            listener.close()
            raise
        self._socket = listener
        self._error = ""

        def run() -> None:
            try:
                self._server.run(sockets=[listener])
            except BaseException as exc:
                self._error = type(exc).__name__
                self.runtime.log.add("mcp_server_error", reason=self._error)
            finally:
                listener.close()
                with self._lock:
                    executor, self._executor = self._executor, None
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)

        self._thread = threading.Thread(target=run, name="mcp-http", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 8
        while not self.running and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.running:
            self.stop()
            raise TerminalError(f"MCP 启动失败（{self._error or '启动超时'}）。")
        self.started_at = utc_now()
        self.runtime.log.add("mcp_started", host="0.0.0.0" if self.public_access else "127.0.0.1", port=self.port)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if not self._thread or not self._thread.is_alive():
            with self._lock:
                executor, self._executor = self._executor, None
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    def request_started(self, scope: dict, headers: dict) -> str:
        peer = scope.get("client") or ("unknown", 0)
        user_agent = headers.get(b"user-agent", b"").decode("latin-1")[:160]
        identity = hashlib.sha256(f"{peer[0]}:{user_agent}".encode()).hexdigest()[:16]
        with self._lock:
            entry = self._clients.setdefault(identity, dict(id=identity, kind="http-request-activity",
                peer_host=peer[0], user_agent=user_agent, first_seen=utc_now(), total_requests=0, active_requests=0))
            entry.update(last_seen=utc_now(), last_peer_port=peer[1], last_method=scope["method"],
                         protocol_version=headers.get(b"mcp-protocol-version", b"").decode("latin-1")[:32])
            entry["total_requests"] += 1
            entry["active_requests"] += 1
            for key in list(self._clients):
                if len(self._clients) <= 128:
                    break
                if not self._clients[key]["active_requests"]:
                    del self._clients[key]
        return identity

    def request_finished(self, identity: str) -> None:
        with self._lock:
            if identity in self._clients:
                self._clients[identity]["active_requests"] -= 1

    def info(self) -> dict[str, Any]:
        with self._lock:
            return dict(running=self.running, host="0.0.0.0" if self.public_access else "127.0.0.1",
                        port=self.port, endpoint=self.url if self.port else None, public_access=self.public_access,
                        auth="pre-shared Bearer password", transport="Streamable HTTP (stateless)",
                        started_at=self.started_at, recent_http_clients=[dict(item) for item in self._clients.values()],
                        note="HTTP 客户端列表表示最近请求活动，不代表持久连接。")


class McpRuntime:
    def __init__(self, config: AgentConfig, log_directory: Path, *, client_factory=None):
        self.config = replace(config)
        self.log = OperationLog(log_directory)
        options = {"client_factory": client_factory} if client_factory else {}
        self.manager = TerminalManager(config, self.log, **options)
        self.http = MCPHttpServer(self)
        self.assist: SharedSSHServer | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._validators: dict = {}
        self.files = FileOperations(self.manager)
        self._wait_lock = threading.Lock()
        self._waits: dict[tuple[str, str, str], threading.Event] = {}
        self._wait_slots = threading.BoundedSemaphore(16)

    def update_config(self, config: AgentConfig) -> None:
        self.config = replace(config)
        self.manager.config = replace(config)

    def start(self, config: AgentConfig) -> None:
        with self._lock:
            if self._closed:
                raise TerminalError("程序正在退出。")
            self.update_config(config)
            self.http.start(config)
            self.manager.accepting_connections = True

    def stop(self) -> None:
        with self._lock:
            self.manager.accepting_connections = False
            self.stop_assist()
            self.manager.disconnect_all()
            self.http.stop()
            self.log.add("mcp_stopped")

    def start_assist(self, connection_id: str, config: AgentConfig) -> None:
        with self._lock:
            if not self.http.running:
                raise TerminalError("请先启动 MCP。")
            validate_password(config.assist_password)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", config.assist_username) is None:
                raise TerminalError("协助 SSH 用户名需以英文字母或下划线开头，仅含字母、数字、下划线、点或连字符，最多 64 位。")
            self.manager.get(connection_id)._check_connected()
            if self.assist and self.assist.running:
                raise TerminalError("请先关闭当前协助 SSH，再切换目标窗口。")
            self.stop_assist()
            server = SharedSSHServer(self.manager, connection_id, config.assist_port,
                                     config.assist_username, config.assist_password,
                                     load_or_create_host_key(config))
            server.start()
            self.assist = server

    def stop_assist(self) -> None:
        if self.assist:
            self.assist.stop()

    def info(self) -> dict[str, Any]:
        return dict(mcp=self.http.info(), assist=self.assist.info() if self.assist else {"running": False},
                    terminals=self.manager.list_connections()["open_window_count"],
                    limits=dict(max_connections=MAX_CONNECTIONS, output_bytes_per_window=MAX_OUTPUT_BYTES,
                                max_read_bytes=MAX_OUTPUT_BYTES, max_input_bytes=MAX_INPUT_BYTES,
                                max_concurrent_waits=16, max_wait_ms=600000,
                                idle_disconnect_minutes=self.manager.config.mcp_idle_minutes,
                                sticky_reconnect_minutes=self.manager.config.mcp_sticky_minutes),
                    sftp=dict(transport="panel-native-file-api",
                              endpoints=["local-ssh-proxy", "assist-ssh", "MCP file_operation"],
                              note="通过面板文件接口读写节点文件，支持 sftp 与 scp。"),
                    input_requires_explicit_enter=True, quoted_keys_in_send_and_read=True)

    def dispatch(self, name: str, arguments: Any = None, *, cancel_event=None) -> dict[str, Any]:
        from jsonschema import Draft202012Validator

        definition = TOOLS.get(name)
        if definition is None:
            raise TerminalError("未知工具；请先 tools/list 获取可用工具名称。")
        args = {} if arguments is None else arguments
        validator = self._validators.setdefault(name, Draft202012Validator(definition["inputSchema"]))
        error = next(validator.iter_errors(args), None)
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path) or "arguments"
            # Do not interpolate ValidationError.message: it can repeat a password or input text.
            reason = {"additionalProperties": "含未声明字段", "oneOf": "不满足该模式的参数要求；请检查必填字段及类型"}.get(error.validator)
            if error.validator == "required":
                missing = [field for field in error.validator_value if field not in error.instance]
                reason = "缺少必填字段：" + ", ".join(missing)
            if reason is None:
                reason = f"不满足 {error.validator}={json.dumps(error.validator_value, ensure_ascii=False)}"
            self.log.add("invalid_arguments", actor=ACTOR.get(), tool=name, field=path)
            raise TerminalError(f"参数 {path}：{reason}；请参考该工具 inputSchema。")
        def normalize(value):
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value
        args = normalize(args)
        actor = ACTOR.get()
        try:
            if name == "get_help":
                return {"help": public_help()}
            if name == "get_service_info":
                return self.info()
            if name == "list_terminals":
                if args.get("refresh", False):
                    self.manager.refresh_catalog()
                result = self.manager.list_terminals(query=args.get("query", ""))
                fields = {"uuid", "name", "online"}
                return dict(terminals=[{key: value for key, value in item.items() if key in fields}
                                      for item in result["terminals"]], total=result["total"])
            if name == "get_terminal":
                if args.get("refresh", False):
                    self.manager.refresh_catalog()
                return self.manager.get_terminal(args["server_uuid"])
            if name == "list_connections":
                return self.manager.list_connections(**args)
            if name == "get_connection":
                return {"connection": self.manager.get(args["connection_id"]).snapshot()}
            if name == "connect_terminal":
                return self.manager.connect(**args, actor=actor)
            if name == "disconnect_terminal":
                return self.manager.disconnect(**args, actor=actor)
            if name == "send_and_read":
                return self._exchange(args, actor, cancel_event)
            if name == "file_operation":
                return self.files.run(**args, actor=actor)
            if name == "resize_terminal":
                options = dict(args)
                return self.manager.get(options.pop("connection_id")).resize(**options, actor=actor)
            if name == "read_operation_logs":
                return self.log.read(**args)
        except TerminalError:
            raise
        except Exception as exc:
            self.log.add("tool_failed", actor=actor, tool=name, reason=type(exc).__name__)
            raise TerminalError(f"操作失败（{type(exc).__name__}），请检查连接详情和操作日志。") from None
        raise TerminalError("工具尚未实现。")

    def _exchange(self, args, actor, cancel_event):
        options = dict(args)
        identity = options.pop("connection_id")
        session = self.manager.get(identity)
        raw = encode_terminal_input(options.pop("text", ""), options.pop("parse_keys", True))
        if len(raw) > MAX_INPUT_BYTES:
            raise TerminalError(f"输入不能超过 {MAX_INPUT_BYTES} 字节。")
        cancel_id = options.pop("cancel_wait_id", None)
        wait_id = options.pop("wait_id", None)
        event = cancel_event or threading.Event()
        key = (CLIENT.get(), identity, wait_id)
        waiting = options.get("wait", {}).get("max_wait_ms", 0) > 0
        if waiting and not self._wait_slots.acquire(blocking=False):
            raise TerminalError("同时等待已达 16 个；请先取消一个等待，或使用零毫秒刷新。")
        registered = False
        try:
            with self._wait_lock:
                if wait_id and key in self._waits:
                    raise TerminalError("wait_id 正在使用，请为每次等待选择唯一 ID。")
                if cancel_id:
                    target = self._waits.get((CLIENT.get(), identity, cancel_id))
                    if target is None:
                        raise TerminalError("找不到该 cancel_wait_id 的活动等待。")
                    target.set()
                if wait_id:
                    self._waits[key] = event
                    registered = True
            result = send_and_read(session, raw, actor=actor, cancel_event=event, **options)
            if wait_id:
                result["wait_id"] = wait_id
            self.log.add("send_and_read", actor=actor, connection_id=identity,
                         bytes=result["bytes_returned"], wait_reason=result["wait_reason"])
            return result
        finally:
            if registered:
                with self._wait_lock:
                    self._waits.pop(key, None)
            if waiting:
                self._wait_slots.release()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.stop()
            self.manager.shutdown()
            self.log.close()
