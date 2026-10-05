"""Loopback SSH access to an existing MCP PTY, with independent output cursors."""
from __future__ import annotations

import base64
import hashlib
import hmac
import socket
import threading
import time
from typing import Any
from uuid import uuid4

import paramiko

from sftp_backend import sftp_subsystem_args
from terminal_sessions import ACTIVE_STATES, TerminalError, TerminalManager, utc_now


def close_quietly(resource) -> None:
    if resource is not None:
        try:
            resource.close()
        except Exception:
            pass  # A peer can reset the connection while Paramiko sends its final EOF.


class AssistInterface(paramiko.ServerInterface):
    def __init__(self, owner: "SharedSSHServer", peer: str):
        self.owner = owner
        self.peer = peer
        self.shell_event = threading.Event()
        self.channel_id: int | None = None
        self.size = (120, 36)
        self.term = "xterm-256color"
        self.auth_attempts = 0

    def check_auth_password(self, username: str, password: str):
        self.auth_attempts += 1
        valid = hmac.compare_digest(username.encode("utf-8"), self.owner.username.encode("utf-8"))
        valid = hmac.compare_digest(hashlib.sha256(password.encode("utf-8")).digest(),
                                    self.owner.password_digest) and valid
        if valid and self.auth_attempts <= 5 and not self.owner.stopping.is_set():
            return paramiko.AUTH_SUCCESSFUL
        self.owner.manager.log.add("assist_auth_rejected", actor=self.peer,
                                   connection_id=self.owner.connection_id)
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, chanid: int):
        if kind != "session":
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        if self.channel_id is None:
            # 第一个会话通道承载共享 shell，其余通道留给 SFTP 等子系统。
            self.channel_id = chanid
        return paramiko.OPEN_SUCCEEDED

    def check_channel_shell_request(self, channel) -> bool:
        if channel.get_id() != self.channel_id:
            return False
        self.shell_event.set()
        return True

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        self.term = term.decode("ascii", errors="replace")[:80] if isinstance(term, bytes) else str(term)[:80]
        self.size = (max(20, min(500, width or 120)), max(8, min(300, height or 36)))
        return channel.get_id() == self.channel_id

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        self.size = (max(20, min(500, width or 120)), max(8, min(300, height or 36)))
        return channel.get_id() == self.channel_id

    def check_channel_exec_request(self, channel, command) -> bool:
        return False

    def check_channel_subsystem_request(self, channel, name) -> bool:
        subsystem = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
        if subsystem.lower() == "sftp":
            self.owner.manager.log.add("sftp_subsystem_requested", actor=self.peer,
                                       connection_id=self.owner.connection_id,
                                       message="协助连接请求了 SFTP 子系统。")
            return super().check_channel_subsystem_request(channel, subsystem.lower())
        return False


class SharedSSHServer:
    def __init__(self, manager: TerminalManager, connection_id: str, port: int,
                 username: str, password: str, host_key: paramiko.PKey):
        self.manager = manager
        self.connection_id = connection_id
        self.port = port
        self.username = username
        self.password_digest = hashlib.sha256(password.encode("utf-8")).digest()
        self.host_key = host_key
        self.fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(host_key.asbytes()).digest()).decode().rstrip("=")
        self.stopping = threading.Event()
        self._lock = threading.RLock()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._sockets: set[socket.socket] = set()
        self._transports: set[paramiko.Transport] = set()
        self._handlers: set[threading.Thread] = set()
        self._peers: dict[str, dict[str, Any]] = {}
        self.started_at: str | None = None

    @property
    def running(self) -> bool:
        return self._listener is not None and not self.stopping.is_set()

    def start(self) -> None:
        self.manager.get(self.connection_id)._check_connected()
        if not 0 <= self.port <= 65535:
            raise TerminalError("协助 SSH 端口无效。")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(8)
            listener.settimeout(0.5)
        except OSError:
            listener.close()
            raise TerminalError("协助 SSH 端口无法监听；请检查端口是否被占用。") from None
        self.port = listener.getsockname()[1]
        self._listener = listener
        self.started_at = utc_now()
        self._thread = threading.Thread(target=self._accept, name="mcp-assist-listener", daemon=True)
        self._thread.start()
        self.manager.log.add("assist_started", connection_id=self.connection_id,
                             host="127.0.0.1", port=self.port, username=self.username)

    def _accept(self) -> None:
        target_closed = False
        try:
            while not self.stopping.is_set():
                try:
                    # 粘性会话重连期间保持监听，避免一次网络抖动就踢掉协助者。
                    connected = self.manager.get(self.connection_id).status in ACTIVE_STATES
                except TerminalError:
                    connected = False
                if not connected:
                    target_closed = True
                    break
                try:
                    client, address = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with self._lock:
                    if self.stopping.is_set() or len(self._sockets) >= 8:
                        client.close()
                        continue
                    self._sockets.add(client)
                    thread = threading.Thread(target=self._handle, args=(client, address),
                                              name="mcp-assist-client", daemon=True)
                    self._handlers.add(thread)
                    thread.start()
        finally:
            if target_closed and not self.stopping.is_set():
                # Stop accepting immediately, but let existing readers drain the PTY tail.
                with self._lock:
                    listener, self._listener = self._listener, None
                    handlers = list(self._handlers)
                if listener is not None:
                    listener.close()
                deadline = time.monotonic() + 2
                for handler in handlers:
                    handler.join(timeout=max(0, deadline - time.monotonic()))
            self._close_resources()

    def _handle(self, client: socket.socket, address: tuple) -> None:
        identity = str(uuid4())
        actor = f"ssh-assist:{address[0]}:{address[1]}"
        transport = None
        channel = None
        sender = None
        detached = threading.Event()
        session = None
        try:
            session = self.manager.get(self.connection_id)
            transport = paramiko.Transport(client)
            transport.banner_timeout = 10
            transport.auth_timeout = 15
            transport.add_server_key(self.host_key)
            # 协助者与 AI 共享同一个终端，文件通道也指向同一个节点。
            transport.set_subsystem_handler(
                "sftp", *sftp_subsystem_args(session.config, session.server["uuid"], self._sftp_log))
            with self._lock:
                if self.stopping.is_set():
                    return
                self._transports.add(transport)
            interface = AssistInterface(self, actor)
            transport.start_server(server=interface)
            channel = transport.accept(15)
            if channel is None:
                return
            deadline = time.monotonic() + 15
            while not interface.shell_event.wait(0.2):
                if self.stopping.is_set() or not transport.is_active() or time.monotonic() > deadline:
                    return
            session._check_connected()
            channel.settimeout(0.5)
            details = dict(id=identity, kind="ssh-assist", peer_host=address[0], peer_port=address[1],
                           username=self.username, connected_at=utc_now(), last_input_at=None,
                           last_output_at=None, bytes_sent=0, bytes_received=0,
                           client_version=transport.remote_version,
                           cipher_in=transport.remote_cipher, cipher_out=transport.local_cipher,
                           terminal_type=interface.term, cols=interface.size[0], rows=interface.size[1])
            cursor = session.output.read(tail=True, max_bytes=65536)["cursor"]
            with self._lock:
                self._peers[identity] = details
            session.add_collaborator(identity, details)
            self.manager.log.add("assist_attached", actor=actor, connection_id=self.connection_id,
                                 collaborator_id=identity)

            def output_loop() -> None:
                nonlocal cursor
                try:
                    while not detached.is_set() and not self.stopping.is_set():
                        result = session.output.read(cursor, max_bytes=8192, wait_ms=500)
                        if result["dropped_bytes"]:
                            channel.sendall(b"\r\n[Output buffer overflow; showing retained output]\r\n")
                        raw = result["data"]
                        if raw:
                            # A timed-out sendall has unknown partial progress: detach, never replay.
                            channel.sendall(raw)
                            details["bytes_received"] += len(raw)
                            details["last_output_at"] = utc_now()
                        cursor = result["next_cursor"]
                        if result["closed"] and not result["has_more"]:
                            break
                except (OSError, EOFError):
                    pass
                finally:
                    detached.set()
                    close_quietly(channel)

            sender = threading.Thread(target=output_loop, name="mcp-assist-output", daemon=True)
            sender.start()
            last_size = None
            while not self.stopping.is_set() and not detached.is_set() and session.status in ACTIVE_STATES:
                if interface.size != last_size:
                    last_size = interface.size
                    session.resize(*last_size, actor=actor)
                    details["cols"], details["rows"] = last_size
                try:
                    raw = channel.recv(8192)
                except socket.timeout:
                    continue
                if not raw:
                    break
                try:
                    session.send(raw, actor=actor)
                except TerminalError:
                    continue  # 重连中暂不可发送；连接恢复后输入继续。
                details["bytes_sent"] += len(raw)
                details["last_input_at"] = utc_now()
        except Exception as exc:
            if not self.stopping.is_set():
                self.manager.log.add("assist_client_closed", actor=actor,
                                     connection_id=self.connection_id, reason=type(exc).__name__)
        finally:
            if (sender is not None and session is not None and session.status != "connected"
                    and not self.stopping.is_set()):
                sender.join(timeout=2)
            detached.set()
            close_quietly(channel)
            close_quietly(transport)
            close_quietly(client)
            if sender is not None:
                sender.join(timeout=1)
            if session is not None:
                session.remove_collaborator(identity)
            with self._lock:
                was_attached = self._peers.pop(identity, None) is not None
                self._transports.discard(transport)
                self._sockets.discard(client)
                self._handlers.discard(threading.current_thread())
            if was_attached:
                self.manager.log.add("assist_detached", actor=actor, connection_id=self.connection_id,
                                     collaborator_id=identity)

    def _close_resources(self) -> None:
        with self._lock:
            already_stopped = self.stopping.is_set()
            self.stopping.set()
            listener, self._listener = self._listener, None
            transports = list(self._transports)
            sockets = list(self._sockets)
        if listener is not None:
            listener.close()
        for transport in transports:
            close_quietly(transport)
        for client in sockets:
            close_quietly(client)
        if not already_stopped:
            self.manager.log.add("assist_stopped", connection_id=self.connection_id)

    def stop(self) -> None:
        self._close_resources()
        deadline = time.monotonic() + 2
        with self._lock:
            threads = list(self._handlers) + ([self._thread] if self._thread else [])
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))

    def _sftp_log(self, line: str) -> None:
        """把协助端口的 SFTP 操作写进 MCP 操作日志流。"""
        self.manager.log.add("sftp_operation", connection_id=self.connection_id, message=line)

    def info(self) -> dict[str, Any]:
        with self._lock:
            return dict(running=self.running, host="127.0.0.1", port=self.port,
                        username=self.username, connection_id=self.connection_id,
                        host_key_fingerprint=self.fingerprint, started_at=self.started_at,
                        collaborators=[dict(item) for item in self._peers.values()],
                        connected_clients=len(self._peers), max_clients=8,
                        disconnect_note="关闭 SSH 连接仅退出协助；在共享 shell 输入 exit/Ctrl+D 会影响整个终端。")
