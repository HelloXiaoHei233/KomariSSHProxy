from __future__ import annotations

import io
import json
import socket
import threading
import time
from datetime import datetime
from typing import Any, Callable

try:
    import paramiko
except ImportError:  # pragma: no cover - handled at runtime in the UI.
    paramiko = None

try:
    from websocket import ABNF, WebSocketTimeoutException
except ImportError:  # pragma: no cover
    ABNF = None
    WebSocketTimeoutException = None

from config import AgentConfig, CONFIG_PATH
from komari_client import KomariClient
from sftp_backend import sftp_subsystem_args


LogCallback = Callable[[str], None]
StateCallback = Callable[[str, bool], None]


def require_paramiko() -> None:
    if paramiko is None:
        raise RuntimeError("缺少 paramiko，请先运行: pip install -r requirements.txt")
    if ABNF is None:
        raise RuntimeError("缺少 websocket-client，请先运行: pip install -r requirements.txt")


def bridge_error(exc: Exception) -> str:
    """把底层异常收敛成不含 Cookie、URL 与 2FA 的简短原因。"""
    status = getattr(exc, "status_code", None)
    suffix = f"，HTTP {status}" if isinstance(status, int) else ""
    return f"{type(exc).__name__}{suffix}"


class TerminalBridge:
    """把本地 SSH shell 桥接到面板 WebSocket PTY。

    与 MCP 终端一致，这里同样支持粘性会话：面板在终端建立后会下发 ``request_id``，
    掉线时用它在粘性窗口内重挂同一个终端，期间本地 SSH 通道保持不动。
    """

    def __init__(
        self,
        config: AgentConfig,
        uuid: str,
        cols: int,
        rows: int,
        log: LogCallback,
    ):
        self.config = config
        self.uuid = uuid
        self.cols = cols
        self.rows = rows
        self.log = log
        self.stop_event = threading.Event()
        self.ws = None
        self.channel = None
        self._ws_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self.remote_request_id = ""
        self.reconnect_attempts = 0
        self._close_notified = False

    def run(self, channel) -> None:
        self.channel = channel
        client = KomariClient(self.config)
        self.log(f"连接远端终端: {self.uuid}")
        self.ws = client.connect_terminal(self.uuid, self.config.terminal_2fa_code)
        self.ws.settimeout(2)
        self.log("远端 WebSocket 已连接")
        self.send_resize(self.cols, self.rows)

        ws_thread = threading.Thread(target=self._ws_to_ssh, name="ws-to-ssh", daemon=True)
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="terminal-heartbeat", daemon=True
        )
        ws_thread.start()
        heartbeat_thread.start()
        try:
            self._ssh_to_ws()
        finally:
            self.stop()
            ws_thread.join(timeout=2)
            heartbeat_thread.join(timeout=2)
            self.log("终端桥接已断开")

    def stop(self) -> None:
        self.stop_event.set()
        with self._ws_lock:
            ws, self.ws = self.ws, None
        if ws is not None and self.remote_request_id and not self._close_notified:
            # 主动告知面板结束会话，避免它把已停用的终端再保留整个粘性窗口等待重挂。
            self._close_notified = True
            try:
                ws.send(json.dumps({"type": "close"}), opcode=ABNF.OPCODE_TEXT)
            except Exception:
                pass
        self._close_socket(ws)
        channel = self.channel
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass

    @staticmethod
    def _close_socket(ws) -> None:
        if ws is None:
            return
        try:
            ws.abort()
        except Exception:
            pass
        try:
            ws.close(timeout=0)
        except Exception:
            pass
        try:
            # websocket-client 收到对端 CLOSE 后会提前返回，需要再 shutdown 一次。
            ws.shutdown()
        except Exception:
            pass

    def send_resize(self, cols: int, rows: int) -> None:
        self.cols = max(20, int(cols or self.cols))
        self.rows = max(8, int(rows or self.rows))
        if self.ws is None:
            return  # 重连期间只记住新尺寸，重挂成功后由 _reconnect 统一同步。
        payload = json.dumps({"type": "resize", "cols": self.cols, "rows": self.rows})
        self._send_text(payload)
        self.log(f"窗口大小同步: {self.cols}x{self.rows}")

    def _ssh_to_ws(self) -> None:
        self.channel.settimeout(0.5)
        while not self.stop_event.is_set():
            if self.ws is None:
                # 重连期间不读取通道，让按键留在 SSH 缓冲区，重挂后按原顺序送达。
                if self.stop_event.wait(0.2):
                    return
                continue
            try:
                data = self.channel.recv(8192)
            except socket.timeout:
                continue
            except Exception as exc:
                self.log(f"SSH 读取结束: {exc}")
                break
            if not data:
                break
            if self._send_binary(data):
                self.log(f"IN  SSH -> WS  {len(data)} bytes  {preview_bytes(data)}")

    def _ws_to_ssh(self) -> None:
        while not self.stop_event.is_set():
            with self._ws_lock:
                ws = self.ws
            if ws is None:
                # 重连期间由 _reconnect 持有控制权；这里等待它交接新的连接。
                if self.stop_event.wait(0.2):
                    return
                continue
            try:
                opcode, message = ws.recv_data()
            except WebSocketTimeoutException:
                continue
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                if not self._handle_disconnect(bridge_error(exc)):
                    break
                continue
            if opcode == ABNF.OPCODE_CLOSE or message is None:
                if not self._handle_disconnect("远端已关闭会话"):
                    break
                continue
            if opcode not in {ABNF.OPCODE_TEXT, ABNF.OPCODE_BINARY} or not message:
                continue
            raw = message.encode("utf-8", errors="replace") if isinstance(message, str) else bytes(message)
            if opcode == ABNF.OPCODE_TEXT:
                control = self._control_frame(raw)
                if control is not None:
                    if set(control) == {"request_id"}:
                        self._remember_request_id(str(control["request_id"]))
                        continue
                    if control.get("type") == "close":
                        if not self._handle_disconnect("远端已关闭会话"):
                            break
                        continue
                    continue  # 其余协议帧（例如尺寸回执）不属于终端输出。
            try:
                self.channel.sendall(raw)
                self.log(f"OUT WS  -> SSH {len(raw)} bytes  {preview_bytes(raw)}")
            except Exception as exc:
                self.log(f"SSH 写入结束: {exc}")
                break
        self.stop_event.set()

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
        self.remote_request_id = request_id
        if int(getattr(self.config, "ssh_sticky_minutes", 0) or 0):
            self.log("已取得面板会话标识：远端掉线时会自动重挂同一终端")

    def _handle_disconnect(self, reason: str) -> bool:
        """远端掉线后的统一出口：可粘性重连返回 True，否则结束桥接并返回 False。"""
        if self.stop_event.is_set():
            return False
        if not self._reconnect_lock.acquire(blocking=False):
            return True  # 已有线程在重连，这里直接让出，避免重复重挂。
        try:
            minutes = int(getattr(self.config, "ssh_sticky_minutes", 0) or 0)
            request_id = self.remote_request_id
            if not minutes or not request_id:
                self.log(f"远端会话断开: {reason}")
                return False
            return self._reconnect(reason, minutes, request_id)
        finally:
            self._reconnect_lock.release()

    def _reconnect(self, reason: str, minutes: int, request_id: str) -> bool:
        """在粘性窗口内退避重连并重挂原终端；成功返回 True。"""
        with self._ws_lock:
            stale, self.ws = self.ws, None
        self._close_socket(stale)
        self.log("会话断开，正在重建连接")
        deadline = time.monotonic() + minutes * 60
        delay = 1.0
        while not self.stop_event.is_set():
            if time.monotonic() >= deadline:
                self.log(f"会话重建超时（{minutes} 分钟），终端已结束")
                return False
            if self.stop_event.wait(delay):
                return False
            delay = min(delay * 2, 15.0)
            client = KomariClient(self.config)
            try:
                ws = client.connect_terminal(self.uuid, "", timeout=20, request_id=request_id)
            except Exception as exc:
                self.reconnect_attempts += 1
                status = getattr(exc, "status_code", None)
                if isinstance(status, int) and status in {401, 403, 404}:
                    self.log("远端会话已结束，无法重连")
                    return False
                continue
            finally:
                session = getattr(client, "session", None)
                if session is not None:
                    session.close()
            ws.settimeout(2)
            with self._ws_lock:
                if self.stop_event.is_set():
                    self._close_socket(ws)
                    return False
                self.ws = ws
            self.reconnect_attempts += 1
            self.log(f"当前会话已重建，终端继续可用（第 {self.reconnect_attempts} 次尝试）")
            self.send_resize(self.cols, self.rows)
            return True
        return False

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(10):
            if self.ws is None:
                continue  # 重连期间不发心跳，重挂成功后自动恢复。
            payload = json.dumps(
                {"type": "heartbeat", "timestamp": datetime.utcnow().isoformat() + "Z"}
            )
            if not self._send_text(payload) and not self._handle_disconnect("心跳发送失败"):
                return

    def _send_binary(self, data: bytes) -> bool:
        with self._ws_lock:
            ws = self.ws
            if ws is None:
                return False
            try:
                ws.send(data, opcode=ABNF.OPCODE_BINARY)
                return True
            except Exception:
                return False  # 掉线判定统一交给读取侧，避免两处各自重连。

    def _send_text(self, text: str) -> bool:
        with self._ws_lock:
            ws = self.ws
            if ws is None:
                return False
            try:
                ws.send(text, opcode=ABNF.OPCODE_TEXT)
                return True
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.log(f"WebSocket 写入失败: {exc}")
                return False


class LocalSSHProxyServer:
    def __init__(
        self,
        config: AgentConfig,
        uuid: str,
        server_name: str,
        log: LogCallback,
        state: StateCallback,
    ):
        self.config = config
        self.uuid = uuid
        self.server_name = server_name
        self.log = log
        self.state = state
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.listen_socket: socket.socket | None = None
        self.host_key = None
        self.active_bridges: list[TerminalBridge] = []
        self.active_transports = []
        self._lock = threading.Lock()
        self._session_counter = 0

    @property
    def bind_host(self) -> str:
        return "0.0.0.0" if self.config.public_access else "127.0.0.1"

    @property
    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        require_paramiko()
        if self.is_running:
            return
        self.stop_event.clear()
        self.host_key = load_or_create_host_key(self.config)
        self.thread = threading.Thread(target=self._serve, name="local-ssh-proxy", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            if self.listen_socket:
                self.listen_socket.close()
        except Exception:
            pass
        with self._lock:
            for bridge in list(self.active_bridges):
                bridge.stop()
            for transport in list(self.active_transports):
                try:
                    transport.close()
                except Exception:
                    pass
        if self.thread:
            self.thread.join(timeout=2)
        self.state("已停止", False)

    def _serve(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_host, int(self.config.ssh_port)))
            sock.listen(64)
            sock.settimeout(0.5)
            self.listen_socket = sock
            url = f"ssh://{self.config.ssh_username}@{self.bind_host}:{self.config.ssh_port}"
            self.log(f"本地 SSH 已监听: {url}")
            self.state(f"运行中 {url}", True)
        except Exception as exc:
            self.log(f"启动 SSH 监听失败: {exc}")
            self.state(f"启动失败: {exc}", False)
            return

        while not self.stop_event.is_set():
            try:
                client, addr = self.listen_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client,
                args=(client, addr),
                name=f"ssh-client-{addr[0]}:{addr[1]}",
                daemon=True,
            ).start()
        self.log("SSH 监听已退出")

    def _wait_for_shell(self, server, transport, timeout: float = 20) -> bool:
        """等待 shell 请求；纯子系统连接（例如只用 SFTP）不应让线程空转 20 秒。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.shell_event.wait(0.2):
                return True
            if self.stop_event.is_set() or not transport.is_active():
                return False
        return False

    def _hold_transport(self, transport) -> None:
        """子系统（例如 SFTP）仍占用连接时保持会话，避免 shell 结束就断开文件传输。"""
        while not self.stop_event.is_set() and transport.is_active():
            time.sleep(0.2)

    def _handle_client(self, client: socket.socket, addr) -> None:
        with self._lock:
            self._session_counter += 1
            session_id = self._session_counter
        address = f"#{session_id} {addr[0]}:{addr[1]}"
        self.log(f"SSH 客户端接入: {address}")
        transport = None
        bridge = None
        try:
            transport = paramiko.Transport(client)
            transport.set_keepalive(15)
            transport.add_server_key(self.host_key)
            # SFTP 子系统在独立通道上运行，与 shell 通道互不影响。
            transport.set_subsystem_handler(
                "sftp", *sftp_subsystem_args(self.config, self.uuid, self.log))
            server = ProxyServerInterface(self, address)
            with self._lock:
                self.active_transports.append(transport)
            transport.start_server(server=server)
            channel = transport.accept(20)
            if channel is None:
                # 没有会话通道时，这条连接仍可能只用于子系统（例如 SFTP）。
                self.log(f"SSH 客户端未打开 channel: {address}")
                self._hold_transport(transport)
                return
            if not self._wait_for_shell(server, transport):
                self.log(f"SSH 客户端未请求 shell: {address}")
                self._hold_transport(transport)
                return
            bridge = TerminalBridge(
                self.config,
                self.uuid,
                server.cols,
                server.rows,
                self.log,
            )
            server.bridge = bridge
            with self._lock:
                self.active_bridges.append(bridge)
            bridge.run(channel)
            # shell 结束后，同一连接上可能仍有 SFTP 在传输文件。
            self._hold_transport(transport)
        except Exception as exc:
            self.log(f"SSH 会话异常 {address}: {exc}")
        finally:
            if bridge:
                bridge.stop()
                with self._lock:
                    if bridge in self.active_bridges:
                        self.active_bridges.remove(bridge)
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass
                with self._lock:
                    if transport in self.active_transports:
                        self.active_transports.remove(transport)
            try:
                client.close()
            except Exception:
                pass
            self.log(f"SSH 客户端断开: {address}")


class ProxyServerInterface(paramiko.ServerInterface if paramiko else object):
    def __init__(self, parent: LocalSSHProxyServer, address: str):
        self.parent = parent
        self.address = address
        self.shell_event = threading.Event()
        self.cols = parent.config.terminal_cols
        self.rows = parent.config.terminal_rows
        self.bridge: TerminalBridge | None = None

    def check_auth_password(self, username: str, password: str):
        if username == self.parent.config.ssh_username and password == self.parent.config.ssh_password:
            self.parent.log(f"SSH 登录成功: {self.address} / {username}")
            return paramiko.AUTH_SUCCESSFUL
        self.parent.log(f"SSH 登录失败: {self.address} / {username}")
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, chanid: int):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(
        self,
        channel,
        term: bytes,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
        modes: bytes,
    ) -> bool:
        self.cols = max(20, int(width or self.cols))
        self.rows = max(8, int(height or self.rows))
        return True

    def check_channel_window_change_request(
        self,
        channel,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
    ) -> bool:
        self.cols = max(20, int(width or self.cols))
        self.rows = max(8, int(height or self.rows))
        if self.bridge:
            self.bridge.send_resize(self.cols, self.rows)
        return True

    def check_channel_shell_request(self, channel) -> bool:
        self.shell_event.set()
        return True

    def check_channel_subsystem_request(self, channel, name: str) -> bool:
        subsystem = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
        if subsystem.lower() == "sftp":
            # 交给 paramiko 的默认实现启动已注册的子系统处理器。
            self.parent.log(f"SFTP 子系统请求: {self.address}")
            return super().check_channel_subsystem_request(channel, subsystem.lower())
        self.parent.log(f"未知子系统请求已拒绝: {self.address} / {subsystem}")
        return False


def ensure_config_host_key(config: AgentConfig) -> None:
    require_paramiko()
    if config.ssh_host_key_pem.strip():
        return
    key = paramiko.RSAKey.generate(2048)
    output = io.StringIO()
    key.write_private_key(output)
    config.ssh_host_key_pem = output.getvalue().strip() + "\n"
    config.save(CONFIG_PATH)


def load_or_create_host_key(config: AgentConfig):
    require_paramiko()
    if not config.ssh_host_key_pem.strip():
        ensure_config_host_key(config)
    try:
        return paramiko.RSAKey.from_private_key(io.StringIO(config.ssh_host_key_pem))
    except Exception as exc:
        raise RuntimeError("SSH Host Key 解析失败，请删除 ini 中的 ssh_host_key_pem 后重启程序重新生成。") from exc


def preview_bytes(data: bytes, limit: int = 80) -> str:
    text = data[:limit].decode("utf-8", errors="replace")
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if len(data) > limit:
        text += "..."
    return text
