from __future__ import annotations

import io
import json
import socket
import threading
import time
from datetime import datetime
from typing import Callable

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


LogCallback = Callable[[str], None]
StateCallback = Callable[[str, bool], None]


def require_paramiko() -> None:
    if paramiko is None:
        raise RuntimeError("缺少 paramiko，请先运行: pip install -r requirements.txt")
    if ABNF is None:
        raise RuntimeError("缺少 websocket-client，请先运行: pip install -r requirements.txt")


class TerminalBridge:
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

    def run(self, channel) -> None:
        self.channel = channel
        client = KomariClient(self.config)
        self.log(f"连接远端终端: {self.uuid}")
        self.ws = client.connect_terminal(self.uuid, self.config.terminal_2fa_code)
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
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            if self.channel:
                self.channel.close()
        except Exception:
            pass

    def send_resize(self, cols: int, rows: int) -> None:
        self.cols = max(20, int(cols or self.cols))
        self.rows = max(8, int(rows or self.rows))
        payload = json.dumps({"type": "resize", "cols": self.cols, "rows": self.rows})
        self._send_text(payload)
        self.log(f"窗口大小同步: {self.cols}x{self.rows}")

    def _ssh_to_ws(self) -> None:
        self.channel.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                data = self.channel.recv(8192)
            except socket.timeout:
                continue
            except Exception as exc:
                self.log(f"SSH 读取结束: {exc}")
                break
            if not data:
                break
            self._send_binary(data)
            self.log(f"IN  SSH -> WS  {len(data)} bytes  {preview_bytes(data)}")

    def _ws_to_ssh(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.ws.recv()
            except WebSocketTimeoutException:
                continue
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.log(f"WebSocket 读取结束: {exc}")
                break
            if message is None:
                break
            if isinstance(message, str):
                data = message.encode("utf-8", errors="replace")
            else:
                data = bytes(message)
            if not data:
                continue
            try:
                self.channel.sendall(data)
                self.log(f"OUT WS  -> SSH {len(data)} bytes  {preview_bytes(data)}")
            except Exception as exc:
                self.log(f"SSH 写入结束: {exc}")
                break
        self.stop_event.set()

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(10):
            payload = json.dumps(
                {"type": "heartbeat", "timestamp": datetime.utcnow().isoformat() + "Z"}
            )
            self._send_text(payload)

    def _send_binary(self, data: bytes) -> None:
        if not self.ws:
            return
        with self._ws_lock:
            self.ws.send(data, opcode=ABNF.OPCODE_BINARY)

    def _send_text(self, text: str) -> None:
        if not self.ws:
            return
        with self._ws_lock:
            try:
                self.ws.send(text)
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.log(f"WebSocket 写入失败: {exc}")


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
            server = ProxyServerInterface(self, address)
            with self._lock:
                self.active_transports.append(transport)
            transport.start_server(server=server)
            channel = transport.accept(20)
            if channel is None:
                self.log(f"SSH 客户端未打开 channel: {address}")
                return
            if not server.shell_event.wait(20):
                self.log(f"SSH 客户端未请求 shell: {address}")
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
            self.parent.log(
                f"SFTP 请求已拒绝: {self.address}。当前远端只暴露网页终端 WebSocket，没有文件传输通道。"
            )
            return False
        self.parent.log(f"未知子系统请求已拒绝: {self.address} / {subsystem}")
        return False


def ensure_config_host_key(config: AgentConfig) -> None:
    require_paramiko()
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
