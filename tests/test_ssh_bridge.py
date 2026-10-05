"""SSH 反代桥的粘性会话与日志测试。

复用 ``tests/test_mcp.py`` 里的假 WebSocket，直接驱动 ``TerminalBridge``，
不启动真实监听端口，覆盖控制帧拦截、request_id 重挂、中文日志与主动关闭通知。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import queue
import socket
import threading
import time
import unittest
from unittest.mock import patch

from websocket import ABNF

from config import AgentConfig
from ssh_bridge import TerminalBridge
from test_mcp import FakeFactory


def wait_for(predicate, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


class FakeChannel:
    """最小 SSH 通道：记录写入内容，并按需回送数据。"""

    def __init__(self):
        self.incoming: queue.Queue = queue.Queue()
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, value):
        pass

    def recv(self, size):
        try:
            return self.incoming.get(timeout=0.05)
        except queue.Empty:
            raise socket.timeout()

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True


class TerminalBridgeStickyTests(unittest.TestCase):
    def setUp(self):
        self.factory = FakeFactory()
        self.patcher = patch("ssh_bridge.KomariClient", self.factory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.logs: list[str] = []
        self.channel = FakeChannel()
        self.config = AgentConfig(mcp_port=0, ssh_sticky_minutes=5)
        self.bridge = TerminalBridge(self.config, "test-node-a", 120, 36, self.logs.append)
        self.thread = threading.Thread(target=self.bridge.run, args=(self.channel,), daemon=True)
        self.thread.start()
        wait_for(lambda: bool(self.factory.sockets))

    def tearDown(self):
        self.bridge.stop()
        self.thread.join(timeout=5)

    def _attach(self, request_id: str) -> None:
        self.factory.sockets[0].incoming.put(json.dumps({"request_id": request_id}))
        wait_for(lambda: self.bridge.remote_request_id == request_id)

    def test_request_id_frame_is_not_forwarded_to_the_shell(self):
        # 面板的控制帧不能被当成终端输入，否则会在命令行里显示成一串 JSON。
        self._attach("ssh-1")
        self.assertNotIn(b"request_id", bytes(self.channel.sent))

    def test_remote_drop_reattaches_and_is_narrated_in_chinese(self):
        self._attach("ssh-1")
        self.factory.sockets[0].abort()
        wait_for(lambda: self.bridge.reconnect_attempts == 1 and len(self.factory.sockets) == 2)
        self.assertEqual(self.factory.attach_requests, ["ssh-1"])
        self.assertTrue(any("会话断开，正在重建连接" in line for line in self.logs))
        self.assertTrue(any("当前会话已重建" in line for line in self.logs))

    def test_stop_tells_the_panel_to_release_the_session(self):
        self._attach("ssh-2")
        self.bridge.stop()
        self.assertIn((ABNF.OPCODE_TEXT, json.dumps({"type": "close"})),
                      self.factory.sockets[0].frames)

    def test_disabled_sticky_ends_the_bridge_without_reattaching(self):
        self.bridge.config = AgentConfig(mcp_port=0, ssh_sticky_minutes=0)
        self._attach("ssh-3")
        self.factory.sockets[0].abort()
        wait_for(lambda: self.bridge.stop_event.is_set())
        self.assertEqual(self.factory.attach_requests, [])
        self.assertTrue(any("远端会话断开" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
