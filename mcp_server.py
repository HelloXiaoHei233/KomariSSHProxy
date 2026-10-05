"""Authenticated MCP transport and the desktop application's MCP runtime."""
from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
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
from ssh_assist import SharedSSHServer
from ssh_bridge import load_or_create_host_key
from terminal_sessions import (
    KEYS, MAX_CONNECTIONS, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MAX_READ_BYTES,
    OperationLog, TerminalError, TerminalManager, utc_now,
)


ACTOR: ContextVar[str] = ContextVar("mcp_actor", default="mcp")
INSTRUCTIONS = """这是有状态的交互终端桥，不是 SSH exec 或命令执行 API。
先 list_terminals，再 connect_terminal；每次连接都会新建独立窗口，并报告该服务器窗口数。
后续操作必须使用 connection_id，不能用服务器 UUID 代替。
send_input 只发送原文，不自动回车。输入命令后必须单独 send_key(key="Enter") 才会执行。
Ctrl+C 使用 send_key(key="Ctrl+C")，其含义是中断前台程序，不是断开终端。
方向键、Tab、Escape、Ctrl+D 等同样通过 send_key 发送。也可在 send_input 中显式发送控制字符。
read_output 返回原始 PTY 流（含 ANSI，stdout/stderr 合并），没有可靠的命令退出码或完成边界。
先读前 N 行/字节；持续读取时将 next_cursor 传作下一次 cursor，可用 wait_ms 等待新输出。
max_lines 与 max_bytes 同时设置时以先达到的限制为准；tail=true 读取末尾，且不能传 cursor。
游标单位是原始字节。text 为 UTF-8 预览；多字节边界和二进制内容以 data_base64 为准。
每窗口保留最近 4 MiB，dropped_bytes 表示游标之前的输出已淘汰；不同读取者互不消费数据。
用户可以通过界面或协助 SSH 同时输入。操作前查看 get_connection 的协作者和最近活动。
协助 SSH 只监听本机，由用户在界面启用；退出 SSH 连接仅退出协助，输入 exit/Ctrl+D 可能关闭共享 shell。
远端开启 ECHO 时才能看到输入回显；程序关闭 ECHO 时不会额外回显密码或按键。
任务结束请 disconnect_terminal。关闭 MCP 会断开所有 MCP 终端及协助 SSH。
远端短暂掉线时窗口会进入 reconnecting 并在粘性时限内自动重挂原终端，超时才真正断开。
SFTP 不是 MCP 工具：它由本地 SSH 代理端口和协助端口提供，可用 sftp / scp 传输节点文件。
"""


def schema(properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


CONNECTION = {"type": "string", "minLength": 1, "maxLength": 128,
              "description": "connect_terminal 返回的独立窗口 connection_id。"}
TOOLS: dict[str, dict[str, Any]] = {
    "get_help": dict(description="完整交互规则、鉴权及调用示例。", inputSchema=schema()),
    "get_service_info": dict(description="MCP 监听、HTTP 请求活动、协助 SSH、连接数和限制；不含密码。", inputSchema=schema()),
    "list_terminals": dict(description="列出面板所有服务器终端、最近在线状态及各服务器窗口数。", inputSchema=schema({
        "refresh": {"type": "boolean", "default": False, "description": "true 从面板重新获取服务器和状态。"}})),
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
    "send_input": dict(description="按原样输入文本，绝不自动追加回车。输入命令后必须另调用 send_key(Enter)。", inputSchema=schema({
        "connection_id": CONNECTION, "text": {"type": "string", "minLength": 1, "maxLength": MAX_INPUT_BYTES},
    }, ("connection_id", "text"))),
    "send_key": dict(description="显式发送 Enter、Ctrl+C、Tab、方向键等终端按键。Ctrl+C 中断前台程序，连接仍保留。", inputSchema=schema({
        "connection_id": CONNECTION, "key": {"type": "string", "enum": list(KEYS)},
        "repeat": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
    }, ("connection_id", "key"))),
    "resize_terminal": dict(description="调整共享 PTY 尺寸；会影响同一窗口的所有协助者。", inputSchema=schema({
        "connection_id": CONNECTION, "cols": {"type": "integer", "minimum": 20, "maximum": 500},
        "rows": {"type": "integer", "minimum": 8, "maximum": 300},
    }, ("connection_id", "cols", "rows"))),
    "read_output": dict(description="按独立字节游标读取前 N 行/字节或末尾输出；不消费他人输出。可长轮询等待。", inputSchema=schema({
        "connection_id": CONNECTION, "cursor": {"type": ["integer", "null"], "minimum": 0, "default": None},
        "max_lines": {"type": ["integer", "null"], "minimum": 1, "maximum": 10000, "default": 200},
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BYTES, "default": 65536},
        "tail": {"type": "boolean", "default": False},
        "wait_ms": {"type": "integer", "minimum": 0, "maximum": 30000, "default": 0},
    }, ("connection_id",))),
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
        {"name": "send_input", "arguments": {"connection_id": "<CONNECTION_ID>", "text": "pwd"}},
        {"name": "send_key", "arguments": {"connection_id": "<CONNECTION_ID>", "key": "Enter"}},
        {"name": "read_output", "arguments": {"connection_id": "<CONNECTION_ID>", "max_lines": 100, "max_bytes": 65536}},
        {"name": "send_key", "arguments": {"connection_id": "<CONNECTION_ID>", "key": "Ctrl+C"}},
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
            "远端掉线时窗口进入 reconnecting 并在粘性时限内自动重挂；SFTP 由本地 SSH 与协助端口提供，不是 MCP 工具。\n"
            "输入错误会返回明确参数路径和期望格式；网络错误不会回显 Cookie、2FA 或密码。\n\n"
            "全部工具与严格参数格式（禁止未声明字段）：\n"
            + json.dumps(TOOLS, ensure_ascii=False, indent=2))


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
        try:
            await self.app(scope, receive, send)
        finally:
            ACTOR.reset(token)
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

        async def list_tools(context, params):
            return types.ListToolsResult(tools=[types.Tool(name=name, **definition) for name, definition in TOOLS.items()])

        async def call_tool(context, params):
            try:
                result = await asyncio.to_thread(self.runtime.dispatch, params.name, params.arguments)
                return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                            structuredContent=result, isError=False)
            except TerminalError as exc:
                result = {"error": str(exc), "help": "调用 get_help 或访问 GET / 查看完整参数说明。"}
                return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                            structuredContent=result, isError=True)

        server = Server("komari-terminal", version="1.1.0", instructions=INSTRUCTIONS,
                        on_list_tools=list_tools, on_call_tool=call_tool)
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
                                max_read_bytes=MAX_READ_BYTES, max_input_bytes=MAX_INPUT_BYTES,
                                idle_disconnect_minutes=self.manager.config.mcp_idle_minutes,
                                sticky_reconnect_minutes=self.manager.config.mcp_sticky_minutes),
                    sftp=dict(transport="ssh-subsystem",
                              endpoints=["local-ssh-proxy", "assist-ssh"],
                              note="通过面板文件接口读写节点文件，支持 sftp 与 scp。"),
                    input_requires_explicit_enter=True)

    def dispatch(self, name: str, arguments: Any = None) -> dict[str, Any]:
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
            reason = {"additionalProperties": "含未声明字段"}.get(error.validator)
            if error.validator == "required":
                missing = [field for field in error.validator_value if field not in error.instance]
                reason = "缺少必填字段：" + ", ".join(missing)
            if reason is None:
                reason = f"不满足 {error.validator}={json.dumps(error.validator_value, ensure_ascii=False)}"
            self.log.add("invalid_arguments", actor=ACTOR.get(), tool=name, field=path)
            raise TerminalError(f"参数 {path}：{reason}；请参考该工具 inputSchema。")
        args = dict(args)
        for field, value in args.items():
            field_type = definition["inputSchema"]["properties"][field].get("type")
            if isinstance(value, float) and (field_type == "integer" or isinstance(field_type, list) and "integer" in field_type):
                # JSON Schema defines 2.0 as an integer; normalize before slicing/range/byte repeat.
                args[field] = int(value)
        actor = ACTOR.get()
        try:
            if name == "get_help":
                return {"help": public_help()}
            if name == "get_service_info":
                return self.info()
            if name == "list_terminals":
                if args.get("refresh", False):
                    self.manager.refresh_catalog()
                return self.manager.list_terminals()
            if name == "list_connections":
                return self.manager.list_connections(**args)
            if name == "get_connection":
                return {"connection": self.manager.get(args["connection_id"]).snapshot()}
            if name == "connect_terminal":
                return self.manager.connect(**args, actor=actor)
            if name == "disconnect_terminal":
                return self.manager.disconnect(**args, actor=actor)
            if name == "send_input":
                return self.manager.get(args["connection_id"]).send(args["text"].encode("utf-8"), actor=actor)
            if name == "send_key":
                return self.manager.send_key(**args, actor=actor)
            if name == "resize_terminal":
                options = dict(args)
                return self.manager.get(options.pop("connection_id")).resize(**options, actor=actor)
            if name == "read_output":
                options = {"max_lines": 200, **args}
                result = self.manager.read_output(**options)
                self.log.add("read_output", actor=actor, connection_id=args["connection_id"],
                             bytes=result["bytes_returned"], next_cursor=result["next_cursor"])
                return result
            if name == "read_operation_logs":
                return self.log.read(**args)
        except TerminalError:
            raise
        except Exception as exc:
            self.log.add("tool_failed", actor=actor, tool=name, reason=type(exc).__name__)
            raise TerminalError(f"操作失败（{type(exc).__name__}），请检查连接详情和操作日志。") from None
        raise TerminalError("工具尚未实现。")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.stop()
            self.manager.shutdown()
            self.log.close()
