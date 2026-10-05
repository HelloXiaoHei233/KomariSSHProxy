from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import paramiko
import requests
from websocket import ABNF, WebSocket, WebSocketTimeoutException

from config import AgentConfig, render_ini
from mcp_server import McpRuntime, TerminalError, TOOLS, validate_public_url
from terminal_sessions import MAX_HISTORY, MAX_INPUT_BYTES, OperationLog, OutputBuffer, TerminalManager


SERVERS = [{"uuid": "test-node-a", "name": "Example node", "ipv4": "192.0.2.10", "os": "Linux",
            "session_token": "not-public", "ssh_password": "not-public"}]


def wait_for(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


class FakeSocket:
    def __init__(self):
        self.incoming = queue.Queue()
        self.frames: list[tuple[int, object]] = []
        self.closed = False
        self.fail_send = False

    def settimeout(self, value):
        pass

    def send(self, data, opcode):
        if self.closed or self.fail_send:
            raise OSError("synthetic connection failure")
        self.frames.append((opcode, data))
        if opcode == ABNF.OPCODE_BINARY:
            self.incoming.put(data)
        return len(data)

    def recv(self):
        try:
            return self.incoming.get(timeout=0.05)
        except queue.Empty:
            raise WebSocketTimeoutException()

    def recv_data(self):
        data = self.recv()
        return (ABNF.OPCODE_CLOSE if data is None else ABNF.OPCODE_TEXT if isinstance(data, str) else ABNF.OPCODE_BINARY, data)

    def abort(self):
        self.incoming.put(None)

    def close(self, **kwargs):
        self.closed = True
        self.abort()


class FakeFactory:
    def __init__(self):
        self.sockets: list[FakeSocket] = []
        self.attach_requests: list[str] = []
        self.fail_attach = 0

    def __call__(self, config):
        factory = self
        class Client:
            def connect_terminal(self, uuid, code="", timeout=20, request_id=""):
                if request_id:
                    factory.attach_requests.append(request_id)
                    if factory.fail_attach > 0:
                        factory.fail_attach -= 1
                        raise OSError("synthetic reattach failure")
                ws = FakeSocket()
                factory.sockets.append(ws)
                return ws

            def get_clients(self):
                return SERVERS

            def get_latest_status(self):
                return {"test-node-a": {"online": True, "session_token": "not-public"}}
        return Client()


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="komari-mcp-test-")
        self.config = AgentConfig(mcp_port=0, assist_port=0)
        self.factory = FakeFactory()
        self.runtime = McpRuntime(self.config, Path(self.temp.name), client_factory=self.factory)
        self.runtime.manager.update_catalog(self.config, SERVERS, {"test-node-a": {"online": True}})

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def connect(self):
        return self.runtime.dispatch("connect_terminal", {"server_uuid": "test-node-a"})


class BufferTests(unittest.TestCase):
    def test_independent_cursors_and_line_limit(self):
        buffer = OutputBuffer()
        buffer.append(b"one\r\ntwo\nthree\n")
        first = buffer.read(max_lines=1)
        self.assertEqual(first["data"], b"one\r\n")
        self.assertEqual(buffer.read(max_lines=1)["data"], first["data"])
        self.assertEqual(buffer.read(first["next_cursor"], max_lines=1)["data"], b"two\n")

    def test_eviction_reports_absolute_offsets(self):
        buffer = OutputBuffer(capacity=8)
        buffer.append(b"0123456789")
        result = buffer.read(0, max_bytes=3)
        self.assertEqual((result["data"], result["cursor"], result["next_cursor"], result["dropped_bytes"]), (b"234", 2, 5, 2))
        self.assertEqual(buffer.bounds()["retained_bytes"], 8)

    def test_tail_and_bytes(self):
        buffer = OutputBuffer()
        buffer.append(b"a\nb\nc\n")
        self.assertEqual(buffer.read(tail=True, max_lines=2)["data"], b"b\nc\n")
        self.assertEqual(buffer.read(tail=True, max_bytes=3)["data"], b"\nc\n")
        with self.assertRaises(TerminalError):
            buffer.read(0, tail=True)
        with self.assertRaises(TerminalError):
            buffer.read(99)

    def test_wait_wakes_for_output_and_close(self):
        buffer = OutputBuffer()
        result = []
        thread = threading.Thread(target=lambda: result.append(buffer.read(0, wait_ms=30000)))
        thread.start()
        buffer.append(b"new")
        thread.join(1)
        self.assertEqual(result[0]["data"], b"new")
        thread = threading.Thread(target=lambda: result.append(buffer.read(3, wait_ms=30000)))
        thread.start()
        buffer.close()
        thread.join(1)
        self.assertTrue(result[-1]["closed"])


class TerminalTests(RuntimeCase):
    def test_repeated_connect_is_independent_and_counted(self):
        first, second = self.connect(), self.connect()
        one = first["connection"]["connection_id"]
        two = second["connection"]["connection_id"]
        self.assertNotEqual(one, two)
        self.assertEqual(second["already_open_windows"], 1)
        self.assertEqual(second["open_window_count"], 2)
        self.assertFalse(self.factory.sockets[0].closed)
        self.runtime.dispatch("disconnect_terminal", {"connection_id": one})
        self.assertTrue(self.factory.sockets[0].closed)
        self.assertFalse(self.factory.sockets[1].closed)
        self.runtime.dispatch("disconnect_terminal", {"connection_id": one})
        self.assertEqual(self.runtime.dispatch("list_connections")["total"], 1)

    def test_input_never_adds_enter_and_shortcuts_are_raw(self):
        identity = self.connect()["connection"]["connection_id"]
        self.runtime.dispatch("send_input", {"connection_id": identity, "text": "pwd"})
        self.runtime.dispatch("send_key", {"connection_id": identity, "key": "Enter"})
        self.runtime.dispatch("send_key", {"connection_id": identity, "key": "Ctrl+C"})
        self.runtime.dispatch("send_key", {"connection_id": identity, "key": "Up"})
        binary = [data for opcode, data in self.factory.sockets[0].frames if opcode == ABNF.OPCODE_BINARY]
        self.assertEqual(binary, [b"pwd", b"\r", b"\x03", b"\x1b[A"])
        self.assertEqual(self.runtime.manager.get(identity).status, "connected")

    def test_unicode_bytes_and_raw_pty_are_lossless(self):
        identity = self.connect()["connection"]["connection_id"]
        raw = "\x1b[31m你好\r\n".encode("utf-8") + b"\xff"
        self.factory.sockets[0].incoming.put(raw)
        wait_for(lambda: self.runtime.manager.get(identity).bytes_received == len(raw))
        first = self.runtime.dispatch("read_output", {"connection_id": identity, "max_bytes": 7})
        second = self.runtime.dispatch("read_output", {"connection_id": identity, "cursor": first["next_cursor"]})
        self.assertEqual(base64.b64decode(first["data_base64"]) + base64.b64decode(second["data_base64"]), raw)

    def test_validation_is_strict_and_does_not_echo_input(self):
        for name, args in (
            ("connect_terminal", {"server_uuid": "test-node-a", "cols": True}),
            ("read_output", {"connection_id": "x", "max_bytes": "10"}),
            ("list_connections", {"extra": "private input"}),
            ("send_input", {"text": "private input"}),
            ("send_input", []),
            ("send_key", {"connection_id": "x", "key": "not-a-key"}),
            ("read_output", {"connection_id": "x", "wait_ms": 30001}),
        ):
            with self.subTest(name=name, args=args), self.assertRaises(TerminalError) as caught:
                self.runtime.dispatch(name, args)
            self.assertNotIn("private input", str(caught.exception))
        with self.assertRaises(TerminalError):
            self.runtime.dispatch("unknown")

    def test_utf8_input_size_limit_applies_to_bytes(self):
        identity = self.connect()["connection"]["connection_id"]
        with self.assertRaises(TerminalError):
            self.runtime.dispatch("send_input", {"connection_id": identity, "text": "中" * (MAX_INPUT_BYTES // 2)})

    def test_log_and_metadata_never_contain_credentials_or_input(self):
        identity = self.connect()["connection"]["connection_id"]
        sensitive = self.config.mcp_password + "-typed-input"
        self.runtime.dispatch("send_input", {"connection_id": identity, "text": sensitive})
        self.runtime.dispatch("list_terminals", {"refresh": True})
        metadata = json.dumps(self.runtime.info()) + json.dumps(self.runtime.dispatch("list_terminals"))
        self.assertNotIn("not-public", metadata)
        logs = self.runtime.log.export_text()
        self.assertNotIn(sensitive, logs)
        self.assertNotIn(self.config.mcp_password, logs + metadata)
        self.assertIn("send_input", logs)
        self.assertIn('"bytes"', logs)

    def test_close_preserves_output_history_and_rejects_send(self):
        identity = self.connect()["connection"]["connection_id"]
        self.runtime.manager.get(identity).output.append(b"retained")
        self.runtime.dispatch("disconnect_terminal", {"connection_id": identity})
        self.assertEqual(self.runtime.dispatch("read_output", {"connection_id": identity})["text"], "retained")
        with self.assertRaises(TerminalError):
            self.runtime.dispatch("send_input", {"connection_id": identity, "text": "pwd"})

    def test_cancel_during_connection_closes_late_socket(self):
        entered, release = threading.Event(), threading.Event()
        late = FakeSocket()
        class Client:
            def connect_terminal(self, *args, **kwargs):
                entered.set()
                release.wait(3)
                return late
        self.runtime.manager.client_factory = lambda _: Client()
        failures = []
        def connect():
            try:
                self.connect()
            except TerminalError as exc:
                failures.append(exc)
        thread = threading.Thread(target=connect)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.runtime.manager.disconnect_all()
        release.set()
        thread.join(2)
        self.assertTrue(late.closed)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.runtime.manager.list_connections()["total"], 0)

    def test_failed_send_closes_connection(self):
        identity = self.connect()["connection"]["connection_id"]
        self.factory.sockets[0].fail_send = True
        with self.assertRaises(TerminalError):
            self.runtime.dispatch("send_input", {"connection_id": identity, "text": "pwd"})
        self.assertEqual(self.runtime.manager.get(identity).status, "error")

    def test_empty_data_frames_do_not_close_terminal(self):
        identity = self.connect()["connection"]["connection_id"]
        self.factory.sockets[0].incoming.put(b"")
        self.factory.sockets[0].incoming.put(b"still connected")
        wait_for(lambda: self.runtime.manager.get(identity).bytes_received == 15)
        self.assertEqual(self.runtime.manager.get(identity).status, "connected")

    def test_heartbeat_matches_komari_protocol(self):
        identity = self.connect()["connection"]["connection_id"]
        session = self.runtime.manager.get(identity)
        owner = threading.current_thread()
        original_wait = session._stop.wait
        answers = iter([False, True])
        def test_wait(timeout):
            return next(answers) if threading.current_thread() is owner else original_wait(timeout)
        with patch.object(session._stop, "wait", side_effect=test_wait):
            session._heartbeat_loop()
        heartbeat = json.loads(self.factory.sockets[0].frames[-1][1])
        self.assertEqual(heartbeat["type"], "heartbeat")
        self.assertTrue(heartbeat["timestamp"].endswith("Z"))

    def test_integer_valued_json_floats_are_accepted(self):
        identity = self.connect()["connection"]["connection_id"]
        self.runtime.manager.get(identity).output.append(b"one\ntwo\nthree\n")
        result = self.runtime.dispatch("read_output", {"connection_id": identity, "max_lines": 2.0, "max_bytes": 100.0})
        self.assertEqual(result["text"], "one\ntwo\n")
        self.runtime.dispatch("send_key", {"connection_id": identity, "key": "Tab", "repeat": 2.0})
        self.assertEqual(self.factory.sockets[0].frames[-1], (ABNF.OPCODE_BINARY, b"\t\t"))
        self.runtime.dispatch("read_operation_logs", {"limit": 2.0})

    def test_closed_window_history_is_bounded_immediately(self):
        ids = []
        for _ in range(MAX_HISTORY + 2):
            identity = self.connect()["connection"]["connection_id"]
            ids.append(identity)
            self.runtime.manager.disconnect(identity)
        retained = self.runtime.manager.list_connections(include_closed=True)["connections"]
        self.assertEqual(len(retained), MAX_HISTORY)
        self.assertNotIn(ids[0], {item["connection_id"] for item in retained})
        self.assertEqual(self.runtime.manager.get(ids[-1]).status, "disconnected")

    def test_websocket_shutdown_after_peer_close(self):
        from terminal_sessions import TerminalSession
        left, right = socket.socketpair()
        try:
            ws = WebSocket()
            ws.sock = left
            ws.connected = False  # recv_data_frame/send_close sets this before the close callback.
            TerminalSession._close_socket(ws)
            self.assertIsNone(ws.sock)
            self.assertEqual(left.fileno(), -1)
        finally:
            left.close()
            right.close()

    def test_control_frame_is_not_written_to_terminal_output(self):
        identity = self.connect()["connection"]["connection_id"]
        session = self.runtime.manager.get(identity)
        self.factory.sockets[0].incoming.put(json.dumps({"request_id": "remote-1"}))
        wait_for(lambda: session.remote_request_id == "remote-1")
        output = self.runtime.dispatch("read_output", {"connection_id": identity})
        self.assertEqual(output["text"], "")

    def test_sticky_session_reattaches_after_remote_drop(self):
        self.runtime.manager.config = replace(self.runtime.manager.config, mcp_sticky_minutes=5)
        identity = self.connect()["connection"]["connection_id"]
        session = self.runtime.manager.get(identity)
        self.factory.sockets[0].incoming.put(json.dumps({"request_id": "remote-1"}))
        wait_for(lambda: session.remote_request_id == "remote-1")
        seen: list[str] = []
        self.runtime.manager.on_event = lambda snap: seen.append(snap["status"])
        self.factory.sockets[0].abort()
        wait_for(lambda: session.status == "connected" and len(self.factory.sockets) == 2)
        self.assertEqual(self.factory.attach_requests, ["remote-1"])
        self.assertEqual(session.reconnect_attempts, 1)
        self.assertIn("reconnecting", seen)
        self.assertIsNone(session.reconnecting_since)
        # 操作日志流必须用中文写清楚掉线与重挂，供界面与导出的 JSONL 直接阅读。
        messages = [entry.get("message", "") for entry in self.runtime.log.read(0, 500)["entries"]]
        self.assertTrue(any("会话断开，正在重建连接" in text for text in messages))
        self.assertTrue(any("当前会话已重建" in text for text in messages))

    def test_sticky_disabled_closes_instead_of_reattaching(self):
        self.runtime.manager.config = replace(self.runtime.manager.config, mcp_sticky_minutes=0)
        identity = self.connect()["connection"]["connection_id"]
        session = self.runtime.manager.get(identity)
        self.factory.sockets[0].incoming.put(json.dumps({"request_id": "remote-2"}))
        wait_for(lambda: session.remote_request_id == "remote-2")
        self.factory.sockets[0].abort()
        wait_for(lambda: session.status == "error")
        self.assertEqual(self.factory.attach_requests, [])

    def test_reattach_retries_transient_failures(self):
        self.runtime.manager.config = replace(self.runtime.manager.config, mcp_sticky_minutes=5)
        identity = self.connect()["connection"]["connection_id"]
        session = self.runtime.manager.get(identity)
        self.factory.sockets[0].incoming.put(json.dumps({"request_id": "remote-3"}))
        wait_for(lambda: session.remote_request_id == "remote-3")
        self.factory.fail_attach = 1
        self.factory.sockets[0].abort()
        wait_for(lambda: session.status == "connected" and len(self.factory.sockets) == 2, timeout=8)
        self.assertEqual(self.factory.attach_requests, ["remote-3", "remote-3"])
        self.assertEqual(session.reconnect_attempts, 2)

    def test_deliberate_disconnect_notifies_panel_and_purge_clears_history(self):
        identity = self.connect()["connection"]["connection_id"]
        session = self.runtime.manager.get(identity)
        self.factory.sockets[0].incoming.put(json.dumps({"request_id": "remote-4"}))
        wait_for(lambda: session.remote_request_id == "remote-4")
        self.runtime.manager.disconnect(identity)
        text_frames = [data for opcode, data in self.factory.sockets[0].frames
                       if opcode == ABNF.OPCODE_TEXT]
        self.assertIn(json.dumps({"type": "close"}), text_frames)
        self.assertEqual(self.runtime.manager.purge_closed(), 1)
        self.assertEqual(self.runtime.manager.list_connections(include_closed=True)["total"], 0)

    def test_remote_connection_errors_are_sanitized(self):
        sensitive = self.config.mcp_password
        class Client:
            def connect_terminal(self, *args, **kwargs):
                raise OSError(sensitive)
        self.runtime.manager.client_factory = lambda _: Client()
        with self.assertRaises(TerminalError) as caught:
            self.connect()
        self.assertNotIn(sensitive, str(caught.exception) + self.runtime.log.export_text())


class ConfigTests(unittest.TestCase):
    def test_mcp_settings_persist_but_exposure_does_not(self):
        config = AgentConfig(mcp_public_access=True, public_access=True, mcp_port=23456,
                             mcp_idle_minutes=30, assist_port=23457, ssh_host_key_pem="fixture")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ini"
            config.save(path)
            loaded = AgentConfig.load(path)
        self.assertEqual(loaded.mcp_password, config.mcp_password)
        self.assertEqual(loaded.assist_password, config.assist_password)
        self.assertEqual((loaded.mcp_port, loaded.assist_port, loaded.mcp_idle_minutes), (23456, 23457, 30))
        self.assertFalse(loaded.mcp_public_access)
        self.assertNotIn("mcp_public_access", render_ini(config))

    def test_host_key_is_not_regenerated(self):
        from ssh_bridge import ensure_config_host_key
        config = AgentConfig(ssh_host_key_pem="already-configured")
        with patch("ssh_bridge.paramiko.RSAKey.generate") as generate:
            ensure_config_host_key(config)
            generate.assert_not_called()

    def test_public_url_rejects_credentials_and_query(self):
        self.assertEqual(validate_public_url("https://mcp.example.com"), "https://mcp.example.com/mcp")
        self.assertEqual(validate_public_url("https://MCP.example.com:443"), "https://mcp.example.com/mcp")
        for url in ("https://user:password@example.com", "https://example.com/?token=x", "not-a-url", "https://example.com:99999"):
            with self.subTest(url=url), self.assertRaises(TerminalError):
                validate_public_url(url)


class HTTPTests(RuntimeCase):
    def setUp(self):
        super().setUp()
        self.runtime.start(self.config)
        self.http = requests.Session()
        self.http.trust_env = False
        self.url = self.runtime.http.url
        self.headers = {"Authorization": f"Bearer {self.config.mcp_password}",
                        "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                        "MCP-Protocol-Version": "2025-11-25"}

    def tearDown(self):
        self.http.close()
        super().tearDown()

    def rpc(self, method, params=None, **kwargs):
        data = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            data["params"] = params
        return self.http.post(self.url, headers=self.headers, json=data, timeout=5, **kwargs)

    def test_help_public_and_operations_authenticated(self):
        response = self.http.get(self.url.removesuffix("mcp"), timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertIn("send_key", response.text)
        self.assertIn("不自动回车", response.text)
        self.assertNotIn(self.config.mcp_password, response.text)
        self.assertEqual(self.http.post(self.url, json={}, timeout=5).status_code, 401)
        self.assertEqual(self.http.get(self.url.removesuffix("mcp") + "logs", timeout=5).status_code, 401)
        logs = self.http.get(self.url.removesuffix("mcp") + "logs", headers=self.headers, timeout=5)
        self.assertEqual(logs.status_code, 200)
        self.assertNotIn(self.config.mcp_password, logs.text)

    def test_origin_host_and_public_allowlist(self):
        response = self.http.post(self.url, headers={**self.headers, "Origin": "https://untrusted.example"}, json={}, timeout=5)
        self.assertEqual(response.status_code, 403)
        response = self.http.post(self.url, headers={**self.headers, "Host": "untrusted.example"}, json={}, timeout=5)
        self.assertEqual(response.status_code, 421)
        guard = self.runtime.http.build_app(self.config.mcp_password)
        self.runtime.http.public_access = True
        self.runtime.http.public_url = "https://mcp.example.com/mcp"
        self.assertTrue(guard.trusted_host("mcp.example.com"))
        self.assertTrue(guard.trusted_host("mcp.example.com:443"))
        self.assertTrue(guard.trusted_host(f"192.0.2.10:{self.runtime.http.port}"))
        self.assertFalse(guard.trusted_host("untrusted.example"))
        self.assertTrue(guard.trusted_origin("https://mcp.example.com", "mcp.example.com"))

    def test_legacy_initialize_notifications_and_schema_errors(self):
        response = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "test-client", "version": "1"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["protocolVersion"], "2025-11-25")
        notification = self.http.post(self.url, headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=5)
        self.assertEqual(notification.status_code, 202)
        self.assertEqual(notification.content, b"")
        listing = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual({tool["name"] for tool in listing}, set(TOOLS))
        result = self.rpc("tools/call", {"name": "send_input", "arguments": {"text": "sensitive-input"}}).json()["result"]
        self.assertTrue(result["isError"])
        self.assertNotIn("sensitive-input", json.dumps(result))

    def test_malformed_json_wrong_type_and_protocol(self):
        response = self.http.post(self.url, headers=self.headers, data="{", timeout=5)
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.text.lower())
        response = self.http.post(self.url, headers={**self.headers, "Content-Type": "text/plain"}, data="{}", timeout=5)
        self.assertEqual(response.status_code, 415)
        response = self.http.post(self.url, headers={**self.headers, "MCP-Protocol-Version": "invalid"}, json={}, timeout=5)
        self.assertEqual(response.status_code, 400)

    def test_failed_auth_does_not_lock_out_correct_password(self):
        for _ in range(10):
            self.assertEqual(self.http.post(self.url, json={}, timeout=5).status_code, 401)
        self.assertEqual(self.http.post(self.url, json={}, timeout=5).status_code, 429)
        self.assertEqual(self.rpc("tools/list").status_code, 200)

    def test_official_sdk_client_connects_and_interacts(self):
        async def scenario():
            import httpx2
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            async with httpx2.AsyncClient(headers={"Authorization": self.headers["Authorization"]}, trust_env=False) as http:
                async with streamable_http_client(self.url, http_client=http) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=5) as client:
                        await client.initialize()
                        listing = await client.list_tools()
                        self.assertEqual(len(listing.tools), len(TOOLS))
                        result = await client.call_tool("connect_terminal", {"server_uuid": "test-node-a"})
                        self.assertFalse(result.is_error)
                        payload = json.loads(result.content[0].text)
                        identity = payload["connection"]["connection_id"]
                        await client.call_tool("send_input", {"connection_id": identity, "text": "pwd"})
                        self.assertNotIn((ABNF.OPCODE_BINARY, b"pwd\r"), self.factory.sockets[0].frames)
                        await client.call_tool("send_key", {"connection_id": identity, "key": "Enter"})
                        output = await client.call_tool("read_output", {"connection_id": identity, "wait_ms": 1000})
                        self.assertFalse(output.is_error)
                        bad = await client.call_tool("send_key", {"connection_id": identity, "key": "Wrong"})
                        self.assertTrue(bad.is_error)
                        await client.call_tool("disconnect_terminal", {"connection_id": identity})
        asyncio.run(scenario())


class SharedSSHTests(RuntimeCase):
    def setUp(self):
        super().setUp()
        self.runtime.start(self.config)
        key = paramiko.RSAKey.generate(2048)
        pem = io.StringIO()
        key.write_private_key(pem)
        self.config.ssh_host_key_pem = pem.getvalue()
        self.identity = self.connect()["connection"]["connection_id"]
        self.runtime.start_assist(self.identity, self.config)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        super().tearDown()

    def ssh(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=self.runtime.assist.port, username=self.config.assist_username,
                       password=self.config.assist_password, allow_agent=False, look_for_keys=False,
                       timeout=4, banner_timeout=4, auth_timeout=4)
        self.clients.append(client)
        channel = client.invoke_shell(term="xterm-256color", width=100, height=30)
        channel.settimeout(3)
        return client, channel

    def test_shared_output_input_and_detach_isolation(self):
        client, channel = self.ssh()
        wait_for(lambda: len(self.runtime.manager.get(self.identity).snapshot()["collaborators"]) == 1)
        self.runtime.dispatch("send_input", {"connection_id": self.identity, "text": "AI text"})
        self.assertIn(b"AI text", channel.recv(4096))
        channel.sendall(b"\x03human\r")
        wait_for(lambda: (ABNF.OPCODE_BINARY, b"\x03human\r") in self.factory.sockets[0].frames)
        details = self.runtime.info()["assist"]["collaborators"][0]
        self.assertEqual(details["username"], self.config.assist_username)
        self.assertIn("cipher_in", details)
        channel.close()
        client.close()
        wait_for(lambda: not self.runtime.manager.get(self.identity).snapshot()["collaborators"])
        self.assertFalse(self.factory.sockets[0].closed)
        self.assertEqual(self.runtime.manager.get(self.identity).status, "connected")

    def test_multiple_assist_readers_and_mcp_have_independent_cursors(self):
        _, one = self.ssh()
        _, two = self.ssh()
        wait_for(lambda: self.runtime.assist.info()["connected_clients"] == 2)
        self.factory.sockets[0].incoming.put(b"shared output\r\n")
        self.assertIn(b"shared output", one.recv(4096))
        self.assertIn(b"shared output", two.recv(4096))
        output = self.runtime.dispatch("read_output", {"connection_id": self.identity})
        self.assertIn("shared output", output["text"])
        self.runtime.stop_assist()
        self.assertFalse(self.factory.sockets[0].closed)

    def test_closing_target_stops_assist_listener(self):
        self.ssh()
        self.runtime.dispatch("disconnect_terminal", {"connection_id": self.identity})
        wait_for(lambda: not self.runtime.assist.running)

    def test_remote_exit_drains_last_output(self):
        _, channel = self.ssh()
        wait_for(lambda: self.runtime.assist.info()["connected_clients"] == 1)
        session = self.runtime.manager.get(self.identity)
        data = b"tail-output\r\n" * 6000
        session.output.append(data)
        session.close(reason="remote_closed")
        received = bytearray()
        while True:
            part = channel.recv(8192)
            if not part:
                break
            received.extend(part)
        self.assertEqual(bytes(received), data)

    def test_assist_requires_password_and_rejects_exec(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            with self.assertRaises(paramiko.AuthenticationException):
                client.connect("127.0.0.1", port=self.runtime.assist.port, username=self.config.assist_username,
                               password="wrong", allow_agent=False, look_for_keys=False, timeout=3)
        finally:
            client.close()
        valid, _ = self.ssh()
        with self.assertRaises(paramiko.SSHException):
            valid.exec_command("pwd", timeout=3)


if __name__ == "__main__":
    unittest.main()
