"""Independent, shared Komari PTY sessions. No SSH exec emulation is involved."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from websocket import ABNF, WebSocketTimeoutException

from config import AgentConfig
from komari_client import KomariClient, TERMINAL_WAITING_MESSAGE, normalize_latest_status


MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_READ_BYTES = 256 * 1024
MAX_INPUT_BYTES = 64 * 1024
MAX_CONNECTIONS = 32
MAX_HISTORY = 32
ACTIVE_STATES = {"connecting", "connected", "reconnecting"}
RECONNECTING_MESSAGE = "连接断开，正在自动重连..."
STATUS_LABELS = {
    "connecting": "连接中",
    "connected": "已连接",
    "reconnecting": "重连中",
    "disconnected": "已断开",
    "error": "错误",
}
# 关闭原因的中文说明；日志流的 message 字段直接引用，未知原因原样透出。
CLOSE_REASONS = {
    "requested": "用户主动断开",
    "idle_timeout": "空闲超时",
    "remote_closed": "远端关闭会话",
    "disconnect_all": "全部断开",
    "shutdown": "程序退出",
}
KEYS: dict[str, bytes] = {
    "Enter": b"\r", "Tab": b"\t", "Escape": b"\x1b", "Backspace": b"\x7f",
    "Delete": b"\x1b[3~", "Up": b"\x1b[A", "Down": b"\x1b[B",
    "Right": b"\x1b[C", "Left": b"\x1b[D", "Home": b"\x1b[H",
    "End": b"\x1b[F", "PageUp": b"\x1b[5~", "PageDown": b"\x1b[6~",
    "Shift+Tab": b"\x1b[Z",
    **{f"Ctrl+{chr(64 + number)}": bytes([number]) for number in range(1, 27)},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TerminalError(RuntimeError):
    """An actionable, credential-free error safe to show to clients."""


def connection_error(exc: Exception) -> str:
    # WebSocket/HTTP exceptions can contain cookies, URLs with 2FA, or response bodies.
    status = getattr(exc, "status_code", None)
    suffix = f", HTTP {status}" if isinstance(status, int) else ""
    return f"远端连接失败 ({type(exc).__name__}{suffix})；请检查节点在线状态、面板凭据、2FA 和网络。"


class OperationLog:
    """A bounded live cursor stream and up to 16 MiB of exportable JSONL history."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "mcp-operations.jsonl"
        self._lock = threading.RLock()
        self._entries: deque[dict[str, Any]] = deque(maxlen=8000)
        self._sequence = 0
        self._closed = False
        self._handler = RotatingFileHandler(
            self.path, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def add(self, action: str, *, actor: str = "app", connection_id: str = "",
            **details: Any) -> None:
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            entry = dict(cursor=self._sequence, time=utc_now(), action=action,
                         actor=actor, connection_id=connection_id, **details)
            self._entries.append(entry)
            self._handler.handle(logging.LogRecord(
                "mcp.operations", logging.INFO, "", 0,
                json.dumps(entry, ensure_ascii=False), (), None
            ))

    def read(self, cursor: int = 0, limit: int = 200) -> dict[str, Any]:
        with self._lock:
            first = self._entries[0]["cursor"] if self._entries else self._sequence + 1
            entries = [dict(item) for item in self._entries if item["cursor"] > cursor][:limit]
            return dict(entries=entries, next_cursor=entries[-1]["cursor"] if entries else cursor,
                        dropped_entries=max(0, first - cursor - 1), latest_cursor=self._sequence)

    def export_text(self) -> str:
        with self._lock:
            if not self._closed:
                self._handler.flush()
            paths = [Path(str(self.path) + f".{i}") for i in (3, 2, 1)] + [self.path]
            return "".join(path.read_text(encoding="utf-8") for path in paths if path.exists())

    def export(self, path: Path) -> None:
        path.write_text(self.export_text(), encoding="utf-8")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._handler.close()


class OutputBuffer:
    """Raw byte offsets remain absolute after eviction; readers never consume each other."""

    def __init__(self, capacity: int = MAX_OUTPUT_BYTES):
        self.capacity = capacity
        self.condition = threading.Condition()
        self.data = bytearray()
        self.base = 0
        self.end = 0
        self.closed = False
        self.last_append_at = 0.0

    def append(self, data: bytes) -> None:
        with self.condition:
            if self.closed or not data:
                return
            self.last_append_at = time.monotonic()
            self.end += len(data)
            self.data.extend(data)
            excess = max(0, len(self.data) - self.capacity)
            if excess:
                del self.data[:excess]
                self.base += excess
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def bounds(self) -> dict[str, int]:
        with self.condition:
            return dict(buffer_start=self.base, output_end=self.end,
                        retained_bytes=len(self.data), capacity_bytes=self.capacity)

    def read(self, cursor: int | None = None, *, max_bytes: int = 65536,
             max_lines: int | None = None, tail: bool = False, wait_ms: int = 0) -> dict[str, Any]:
        if not 1 <= max_bytes <= MAX_READ_BYTES:
            raise TerminalError(f"max_bytes 必须为 1 到 {MAX_READ_BYTES}。")
        if max_lines is not None and not 1 <= max_lines <= 10000:
            raise TerminalError("max_lines 必须为 1 到 10000，或 null。")
        if not 0 <= wait_ms <= 30000:
            raise TerminalError("wait_ms 必须为 0 到 30000。")
        if tail and cursor is not None:
            raise TerminalError("tail=true 时请省略 cursor。")
        with self.condition:
            requested = self.base if cursor is None else cursor
            if requested < 0 or requested > self.end:
                raise TerminalError(f"cursor 超出输出范围，请使用 0 到 {self.end} 之间的字节游标。")
            deadline = time.monotonic() + wait_ms / 1000
            while not self.closed and not tail and requested == self.end and wait_ms:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            start = max(self.base, self.end - max_bytes) if tail else max(self.base, requested)
            raw = bytes(self.data[start - self.base:start - self.base + max_bytes])
            if max_lines is not None:
                # LF terminates a PTY line; CR alone is a cursor movement, not a new line.
                if tail:
                    pieces = raw.split(b"\n")
                    line_count = len(pieces) - (1 if raw.endswith(b"\n") else 0)
                    if line_count > max_lines:
                        drop = b"\n".join(pieces[:line_count - max_lines]) + b"\n"
                        start += len(drop)
                        raw = raw[len(drop):]
                else:
                    position = -1
                    for _ in range(max_lines):
                        position = raw.find(b"\n", position + 1)
                        if position < 0:
                            break
                    if position >= 0:
                        raw = raw[:position + 1]
            next_cursor = start + len(raw)
            return dict(data=raw, cursor=start, next_cursor=next_cursor,
                        buffer_start=self.base, output_end=self.end, bytes_returned=len(raw),
                        dropped_bytes=max(0, self.base - requested) if not tail else 0,
                        has_more=next_cursor < self.end, closed=self.closed)


SERVER_FIELDS = (
    "uuid", "name", "os", "arch", "cpu_name", "cpu_cores", "mem_total", "disk_total",
    "ipv4", "ipv6", "region", "version", "virtualization", "group", "created_at",
)
STATUS_FIELDS = (
    "online", "cpu", "ram", "ram_total", "swap", "swap_total", "disk", "disk_total",
    "net_in", "net_out", "net_total_up", "net_total_down", "uptime", "updated_at",
    "load", "load1", "load5", "load15", "connections", "process",
)


def public_fields(data: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: data[name] for name in names if name in data
            and isinstance(data[name], (str, int, float, bool, type(None)))}


class TerminalSession:
    def __init__(self, manager: "TerminalManager", server: dict[str, Any], config: AgentConfig,
                 label: str, actor: str, number: int, cols: int, rows: int):
        self.manager = manager
        self.server = server
        self.config = config
        self.connection_id = str(uuid4())
        self.label = label or f"{server.get('name') or server['uuid']} · {number}"
        self.number = number
        self.cols, self.rows = cols, rows
        self.created_at = utc_now()
        self.connected_at: str | None = None
        self.disconnected_at: str | None = None
        self.last_input_at: str | None = None
        self.last_output_at: str | None = None
        self.last_heartbeat_at: str | None = None
        self.last_activity = time.monotonic()
        self.created_by = actor
        self.last_input_by: str | None = None
        self.status = "connecting"
        self.close_reason = ""
        self.bytes_sent = 0
        self.bytes_received = 0
        self.output = OutputBuffer()
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._ws: Any = None
        self._ws_reattached = False
        self._threads: list[threading.Thread] = []
        self._collaborators: dict[str, dict[str, Any]] = {}
        # 粘性会话：面板在 request_id 存活期内允许重挂同一终端，因此掉线后可以续接。
        self.remote_request_id = ""
        self.reconnect_attempts = 0
        self.reconnecting_since: str | None = None

    def connect(self, two_fa_code: str) -> None:
        client = self.manager.client_factory(self.config)
        ws = None
        try:
            ws = client.connect_terminal(self.server["uuid"], two_fa_code, timeout=20)
            ws.settimeout(2)
            with self._lock:
                if self._stop.is_set():
                    raise TerminalError("连接已被用户取消。")
                self._ws = ws
                self._ws_reattached = False
                self.status = "connected"
                self.connected_at = utc_now()
                self.last_activity = time.monotonic()
                self.remote_request_id = ""
                self.reconnect_attempts = 0
                self.reconnecting_since = None
            self.resize(self.cols, self.rows, actor=self.created_by)
            for target, name in ((self._receive_loop, "mcp-terminal-output"),
                                 (self._heartbeat_loop, "mcp-terminal-heartbeat")):
                thread = threading.Thread(target=target, name=name, daemon=True)
                self._threads.append(thread)
                thread.start()
            self.manager.log.add("terminal_connected", actor=self.created_by,
                                 connection_id=self.connection_id, server_uuid=self.server["uuid"])
            self.manager.changed(self)
        except Exception as exc:
            if ws is not None and ws is not self._ws:
                self._close_socket(ws)
            reason = str(exc) if isinstance(exc, TerminalError) else connection_error(exc)
            self.close(reason=reason, error=True)
            raise TerminalError(reason) from None
        finally:
            session = getattr(client, "session", None)
            if session is not None:
                session.close()
            # The temporary 2FA argument is never retained in the session's metadata.
            self.config = replace(self.config, terminal_2fa_code="")

    @staticmethod
    def _close_socket(ws: Any) -> None:
        try:
            ws.abort()
        except Exception:
            pass
        try:
            ws.close(timeout=0)
        except Exception:
            pass
        try:
            # websocket-client's close() returns early after a peer CLOSE frame.
            ws.shutdown()
        except Exception:
            pass

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                ws = self._ws
            if ws is None:
                # 重连期间由 _reconnect 持有控制权；这里等待它交接新的连接。
                if self._stop.wait(0.2):
                    return
                continue
            try:
                opcode, data = ws.recv_data()
            except WebSocketTimeoutException:
                continue
            except Exception as exc:
                if self._stop.is_set():
                    return
                if not self._handle_disconnect(connection_error(exc), ws):
                    return
                continue
            if opcode == ABNF.OPCODE_CLOSE or data is None:
                if not self._handle_disconnect("remote_closed", ws):
                    return
                continue
            if opcode not in {ABNF.OPCODE_TEXT, ABNF.OPCODE_BINARY} or not data:
                continue
            raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
            control = self._control_frame(raw) if opcode == ABNF.OPCODE_TEXT else None
            with self._lock:
                # Check after parsing and keep state changes atomic with socket replacement.
                # A delayed close/request_id from the old socket must not affect its successor.
                if ws is not self._ws:
                    continue
                if self._ws_reattached and opcode == ABNF.OPCODE_TEXT and raw == TERMINAL_WAITING_MESSAGE:
                    continue
                if control is not None:
                    if set(control) == {"request_id"}:
                        self._remember_request_id(str(control["request_id"]))
                        continue
                    if control.get("type") == "close":
                        self.close(reason="remote_closed")
                        return
                self.bytes_received += len(raw)
                self.last_output_at = utc_now()
                self.last_activity = time.monotonic()
                self.output.append(raw)

    @staticmethod
    def _control_frame(raw: bytes) -> dict[str, Any] | None:
        """识别面板插入终端流的 JSON 控制帧，避免把协议帧当成终端输出。"""
        if not raw.startswith(b"{"):
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _remember_request_id(self, request_id: str) -> None:
        request_id = request_id.strip()
        if not request_id:
            return
        with self._lock:
            if self.remote_request_id == request_id:
                return
            self.remote_request_id = request_id
        self.manager.log.add("terminal_attached", connection_id=self.connection_id,
                             sticky=bool(getattr(self.config, "mcp_sticky_minutes", 0)),
                             message="已取得面板会话标识，粘性重连已就绪。")

    def _handle_disconnect(self, reason: str, ws: Any) -> bool:
        """远端掉线后的统一出口：可粘性重连返回 True，否则终止会话并返回 False。"""
        minutes = int(getattr(self.config, "mcp_sticky_minutes", 0) or 0)
        with self._lock:
            if self._stop.is_set():
                return False
            if ws is not self._ws or self.status == "reconnecting":
                return True  # Another reader/writer already handled this socket.
            request_id = self.remote_request_id
            if not minutes or not request_id:
                self.close(reason=reason, error=True)
                return False
            self.status = "reconnecting"
            self.reconnecting_since = utc_now()
            self.close_reason = reason
            stale, self._ws = self._ws, None
        self._close_socket(stale)
        self.manager.log.add("terminal_reconnecting", connection_id=self.connection_id,
                             reason=reason, window_minutes=minutes,
                             message=RECONNECTING_MESSAGE)
        self.manager.changed(self)
        thread = threading.Thread(target=self._reconnect, args=(minutes, request_id),
                                  name="mcp-terminal-reconnect", daemon=True)
        with self._lock:
            self._threads.append(thread)
            thread.start()
        return True

    def _reconnect(self, minutes: int, request_id: str) -> bool:
        """在粘性窗口内退避重连并重挂原终端；只有一个线程负责重挂。"""
        deadline = time.monotonic() + minutes * 60
        delay = 1.0
        while not self._stop.is_set():
            if time.monotonic() >= deadline:
                self.close(reason=f"粘性重连超时（{minutes} 分钟）", error=True)
                return False
            if self._stop.wait(min(delay, max(0, deadline - time.monotonic()))):
                return False
            if time.monotonic() >= deadline:
                continue
            delay = min(delay * 2, 15.0)
            client = None
            ws = None
            try:
                client = self.manager.client_factory(self.config)
                ws = client.connect_terminal(self.server["uuid"], "", timeout=min(20, max(0.1, deadline - time.monotonic())),
                                             request_id=request_id)
                ws.settimeout(2)
            except Exception as exc:
                if ws is not None:
                    self._close_socket(ws)
                with self._lock:
                    self.reconnect_attempts += 1
                status = getattr(exc, "status_code", None)
                if isinstance(status, int) and status in {401, 403, 404}:
                    # 面板已回收该会话（例如远端 shell 已退出），重连没有意义。
                    self.close(reason="远端会话已结束，无法重连", error=True)
                    return False
                continue
            finally:
                session = getattr(client, "session", None)
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass  # Cleanup must not strand the terminal in reconnecting.
            with self._lock:
                if self._stop.is_set():
                    self._close_socket(ws)
                    return False
                self._ws = ws
                self._ws_reattached = True
                self.status = "connected"
                self.reconnect_attempts += 1
                self.reconnecting_since = None
                self.close_reason = ""
                self.last_activity = time.monotonic()
            self.manager.log.add("terminal_reconnected", connection_id=self.connection_id,
                                 attempts=self.reconnect_attempts,
                                 message=f"当前会话已重建，终端继续可用（第 {self.reconnect_attempts} 次尝试）。")
            self.manager.changed(self)
            try:
                self.resize(self.cols, self.rows, actor="sticky")
            except TerminalError:
                pass  # 尺寸同步失败会走正常掉线处理，由下一轮重连接管。
            return True
        return False

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(10):
            if self.status != "connected":
                continue  # 重连期间不发心跳；重连成功后自动恢复。
            try:
                self._send_control({"type": "heartbeat", "timestamp": utc_now().replace("+00:00", "Z")})
                with self._lock:
                    self.last_heartbeat_at = utc_now()
            except TerminalError:
                continue

    def _send_control(self, payload: dict[str, Any]) -> None:
        with self._send_lock:
            with self._lock:
                self._check_connected()
                ws = self._ws
            try:
                ws.send(json.dumps(payload), opcode=ABNF.OPCODE_TEXT)
            except Exception as exc:
                if self._handle_disconnect(connection_error(exc), ws):
                    raise TerminalError(RECONNECTING_MESSAGE + " 请等待原连接恢复。") from None
                raise TerminalError("终端控制帧发送失败，连接已断开。") from None

    def _check_connected(self) -> None:
        with self._lock:
            if self.status == "reconnecting" and not self._stop.is_set():
                raise TerminalError(RECONNECTING_MESSAGE + " 请保留 connection_id，等待恢复后再操作。")
            if self._stop.is_set() or self.status != "connected":
                raise TerminalError(f"终端未连接（{self.status}）；请新建连接后重试。")

    def send(self, raw: bytes, *, actor: str, key: str | None = None,
             cancel_event: threading.Event | None = None) -> dict[str, Any]:
        if not raw or len(raw) > MAX_INPUT_BYTES:
            raise TerminalError(f"输入必须为 1 到 {MAX_INPUT_BYTES} 字节。")
        initial_output = self.output.bounds()["output_end"]

        def cancelled_receipt():
            return dict(connection_id=self.connection_id, bytes_sent=0, input_output_start=initial_output,
                        appended_enter=False, message="输入已取消，未发送。")

        while not self._send_lock.acquire(timeout=0.025):
            if cancel_event is not None and cancel_event.is_set():
                return cancelled_receipt()
        try:
            if cancel_event is not None and cancel_event.is_set():
                return cancelled_receipt()
            with self._lock:
                self._check_connected()
                ws = self._ws
                output_start = self.output.bounds()["output_end"]
            try:
                ws.send(raw, opcode=ABNF.OPCODE_BINARY)
            except Exception as exc:
                if self._handle_disconnect(connection_error(exc), ws):
                    raise TerminalError(RECONNECTING_MESSAGE + " 本次输入未自动重放；恢复后请先检查输出。") from None
                raise TerminalError("输入发送失败，连接已断开；请检查输出后再决定是否重试。") from None
            with self._lock:
                self.bytes_sent += len(raw)
                self.last_input_at = utc_now()
                self.last_input_by = actor
                self.last_activity = time.monotonic()
        finally:
            self._send_lock.release()
        details: dict[str, Any] = {"bytes": len(raw)}
        if key is not None:
            details["key"] = key
        self.manager.log.add("send_key" if key else "send_input", actor=actor,
                             connection_id=self.connection_id, **details)
        return dict(connection_id=self.connection_id, bytes_sent=len(raw),
                    input_output_start=output_start,
                    appended_enter=False, message="已发送指定输入，未自动追加回车。")

    def resize(self, cols: int, rows: int, *, actor: str) -> dict[str, Any]:
        if not 20 <= cols <= 500 or not 8 <= rows <= 300:
            raise TerminalError("终端尺寸范围：cols 20–500，rows 8–300。")
        self._send_control(dict(type="resize", cols=cols, rows=rows))
        with self._lock:
            self.cols, self.rows = cols, rows
        self.manager.log.add("resize_terminal", actor=actor, connection_id=self.connection_id,
                             cols=cols, rows=rows)
        return dict(connection_id=self.connection_id, cols=cols, rows=rows)

    def close(self, *, reason: str = "disconnected", actor: str = "app", error: bool = False) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            self.status = "error" if error else "disconnected"
            self.close_reason = reason
            self.disconnected_at = utc_now()
            self.reconnecting_since = None
            ws = self._ws
            self._ws = None
            request_id = self.remote_request_id
        if ws is not None and request_id:
            # 主动告知面板结束会话，避免它把已停用的终端再保留整个粘性窗口等待重挂。
            try:
                ws.send(json.dumps({"type": "close"}), opcode=ABNF.OPCODE_TEXT)
            except Exception:
                pass
        self.output.close()
        if ws is not None:
            self._close_socket(ws)
        self.manager.log.add("terminal_closed", actor=actor, connection_id=self.connection_id,
                             reason=reason, status=self.status,
                             message=f"会话已结束（{CLOSE_REASONS.get(reason, reason)}）。")
        self.manager.changed(self)

    def add_collaborator(self, identity: str, details: dict[str, Any]) -> None:
        with self._lock:
            self._collaborators[identity] = details
        self.manager.changed(self)

    def remove_collaborator(self, identity: str) -> None:
        with self._lock:
            self._collaborators.pop(identity, None)
        self.manager.changed(self)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            parsed = urlsplit(self.config.base_url)
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if parsed.port:
                host += f":{parsed.port}"
            endpoint = urlunsplit(("wss" if parsed.scheme == "https" else "ws", host,
                                   f"/api/admin/client/{self.server['uuid']}/terminal", "", ""))
            result = dict(
                connection_id=self.connection_id, window_number=self.number, label=self.label,
                server=dict(self.server), status=self.status, transport="komari-websocket-pty",
                status_message=RECONNECTING_MESSAGE if self.status == "reconnecting" else STATUS_LABELS.get(self.status, self.status),
                remote_endpoint=endpoint, created_by=self.created_by, created_at=self.created_at,
                connected_at=self.connected_at, disconnected_at=self.disconnected_at,
                last_input_at=self.last_input_at, last_output_at=self.last_output_at,
                last_input_by=self.last_input_by, last_heartbeat_at=self.last_heartbeat_at,
                idle_seconds=round(time.monotonic() - self.last_activity, 1),
                cols=self.cols, rows=self.rows, bytes_sent=self.bytes_sent,
                bytes_received=self.bytes_received, close_reason=self.close_reason,
                reconnect_attempts=self.reconnect_attempts,
                reconnecting_since=self.reconnecting_since,
                sticky_minutes=int(getattr(self.config, "mcp_sticky_minutes", 0) or 0),
                collaborators=[dict(item) for item in self._collaborators.values()],
            )
        result.update(self.output.bounds())
        return result


class TerminalManager:
    def __init__(self, config: AgentConfig, log: OperationLog,
                 client_factory: Callable = KomariClient):
        self.config = replace(config)
        self.log = log
        self.client_factory = client_factory
        self._lock = threading.RLock()
        self._sessions: dict[str, TerminalSession] = {}
        self._servers: dict[str, dict[str, Any]] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._numbers: dict[str, int] = {}
        self.catalog_updated_at: str | None = None
        self.on_event: Callable[[dict[str, Any]], None] | None = None
        self.accepting_connections = True
        self._shutdown = threading.Event()
        self._reaper = threading.Thread(target=self._reap_idle, name="mcp-idle-check", daemon=True)
        self._reaper.start()

    def update_catalog(self, config: AgentConfig, clients: list[dict[str, Any]],
                       statuses: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self.config = replace(config)
            self._servers = {str(item["uuid"]): public_fields(item, SERVER_FIELDS)
                             for item in clients if item.get("uuid")}
            self._status = {key: public_fields(value, STATUS_FIELDS) for key, value in statuses.items()}
            self.catalog_updated_at = utc_now()

    def refresh_catalog(self) -> None:
        with self._lock:
            config = replace(self.config)
        client = self.client_factory(config)
        try:
            clients = client.get_clients()
            status = normalize_latest_status(client.get_latest_status())
            self.update_catalog(config, clients, status)
        except Exception as exc:
            raise TerminalError(connection_error(exc)) from None
        finally:
            session = getattr(client, "session", None)
            if session is not None:
                session.close()

    def list_terminals(self, query: str = "") -> dict[str, Any]:
        with self._lock:
            servers = []
            terms = query.casefold().split()
            for uuid, server in self._servers.items():
                summary = public_fields(server, ("uuid", "name", "ipv4", "ipv6", "os", "group", "region"))
                searchable = " ".join(str(value) for value in summary.values()).casefold()
                if terms and not any(term in searchable for term in terms):
                    continue
                ids = [key for key, value in self._sessions.items()
                       if value.server["uuid"] == uuid and value.status in ACTIVE_STATES]
                servers.append(dict(summary, online=self._status.get(uuid, {}).get("online"),
                                    open_window_count=len(ids), connection_ids=ids))
            return dict(terminals=servers, total=len(servers), catalog_updated_at=self.catalog_updated_at,
                        status_note="节点在线状态为最近一次面板快照；refresh=true 可刷新。")

    def sessions(self) -> list[TerminalSession]:
        with self._lock:
            return list(self._sessions.values())

    def get_terminal(self, server_uuid: str) -> dict[str, Any]:
        with self._lock:
            server = self._servers.get(server_uuid)
            if server is None:
                raise TerminalError("找不到服务器 UUID；请先调用 list_terminals。")
            ids = [key for key, value in self._sessions.items()
                   if value.server["uuid"] == server_uuid and value.status in ACTIVE_STATES]
            return dict(server=dict(server), status=dict(self._status.get(server_uuid, {})),
                        connection_ids=ids, open_window_count=len(ids),
                        catalog_updated_at=self.catalog_updated_at)

    def get(self, connection_id: str) -> TerminalSession:
        with self._lock:
            value = self._sessions.get(connection_id)
        if value is None:
            raise TerminalError("找不到 connection_id；请调用 list_connections 获取当前窗口 ID。")
        return value

    def list_connections(self, include_closed: bool = False) -> dict[str, Any]:
        values = [session.snapshot() for session in self.sessions()
                  if include_closed or session.status in ACTIVE_STATES]
        return dict(connections=values, total=len(values),
                    open_window_count=sum(item["status"] in ACTIVE_STATES for item in values))

    def purge_closed(self) -> int:
        """清除已断开/出错的窗口记录，避免界面列表无限堆积。"""
        with self._lock:
            closed = [key for key, value in self._sessions.items()
                      if value.status not in ACTIVE_STATES]
            for key in closed:
                self._sessions.pop(key, None)
        return len(closed)

    def connect(self, server_uuid: str, *, label: str = "", cols: int = 120, rows: int = 36,
                two_fa_code: str = "", actor: str = "mcp") -> dict[str, Any]:
        if not 20 <= cols <= 500 or not 8 <= rows <= 300:
            raise TerminalError("终端尺寸范围：cols 20–500，rows 8–300。")
        with self._lock:
            if self._shutdown.is_set() or not self.accepting_connections:
                raise TerminalError("MCP 正在停止，暂不接受新终端。")
            server = self._servers.get(server_uuid)
            if server is None:
                raise TerminalError("找不到服务器 UUID；请先调用 list_terminals（需要时 refresh=true）。")
            active = [value for value in self._sessions.values() if value.status in ACTIVE_STATES]
            if len(active) >= MAX_CONNECTIONS:
                raise TerminalError(f"已达到 {MAX_CONNECTIONS} 个活动窗口上限，请先断开不用的窗口。")
            before = sum(item.server["uuid"] == server_uuid for item in active)
            self._numbers[server_uuid] = self._numbers.get(server_uuid, 0) + 1
            session = TerminalSession(self, dict(server), replace(self.config), label, actor,
                                      self._numbers[server_uuid], cols, rows)
            self._sessions[session.connection_id] = session
        self.changed(session)
        self.log.add("terminal_connecting", actor=actor, connection_id=session.connection_id,
                     server_uuid=server_uuid, existing_windows=before)
        session.connect(two_fa_code)
        current = sum(item.server["uuid"] == server_uuid and item.status in ACTIVE_STATES
                      for item in self.sessions())
        return dict(connection=session.snapshot(), already_open_windows=before,
                    open_window_count=current,
                    message=f"已新建独立终端窗口，该服务器当前已打开 {current} 个窗口（连接前 {before} 个）。已有窗口继续运行。")

    def disconnect(self, connection_id: str, *, actor: str = "mcp") -> dict[str, Any]:
        session = self.get(connection_id)
        session.close(reason="requested", actor=actor)
        return dict(connection=session.snapshot())

    def disconnect_all(self, *, actor: str = "app") -> None:
        for session in self.sessions():
            session.close(reason="disconnect_all", actor=actor)

    def changed(self, session: TerminalSession) -> None:
        if session.status not in ACTIVE_STATES:
            with self._lock:
                closed = sorted((value for value in self._sessions.values() if value.status not in ACTIVE_STATES),
                                key=lambda value: value.disconnected_at or value.created_at)
                for expired in closed[:-MAX_HISTORY]:
                    self._sessions.pop(expired.connection_id, None)
        callback = self.on_event
        if callback is not None and not self._shutdown.is_set():
            try:
                callback(session.snapshot())
            except RuntimeError:
                pass  # The Qt receiver can have been destroyed during application exit.

    def _reap_idle(self) -> None:
        while not self._shutdown.wait(2):
            minutes = getattr(self.config, "mcp_idle_minutes", 0)
            if minutes:
                for session in self.sessions():
                    if session.status == "connected" and time.monotonic() - session.last_activity > minutes * 60:
                        session.close(reason="idle_timeout")

    def shutdown(self) -> None:
        self.accepting_connections = False
        self._shutdown.set()
        self.disconnect_all()
        deadline = time.monotonic() + 3
        threads = [self._reaper] + [thread for session in self.sessions() for thread in session._threads]
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
