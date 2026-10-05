from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from PyQt6 import QtCore, QtGui, QtTest, QtWidgets
from websocket import ABNF

from config import AgentConfig
from mcp_server import McpRuntime
from mcp_ui import McpPage, TerminalPreview
from test_mcp import FakeFactory, SERVERS
import ui


def process_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for Qt state")


class QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        # Windows' offscreen Qt plugin does not discover system fonts automatically.
        font_root = Path(os.environ.get("SystemRoot", "")) / "Fonts"
        for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
            if (font_root / filename).is_file():
                QtGui.QFontDatabase.addApplicationFont(str(font_root / filename))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="komari-mcp-ui-test-")
        self.config = AgentConfig(mcp_port=0)
        self.factory = FakeFactory()
        self.runtime = McpRuntime(self.config, Path(self.temp.name), client_factory=self.factory)
        self.runtime.manager.update_catalog(self.config, SERVERS, {"test-node-a": {"online": True}})
        self.page = McpPage(self.runtime, self.config, lambda _: None)
        self.page.resize(1120, 660)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.shutdown()
        self.page.close()
        self.runtime.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_key_shortcuts_enter_tab_arrows_and_ime(self):
        preview = TerminalPreview()
        keys, raw = [], []
        preview.key_requested.connect(keys.append)
        preview.input_requested.connect(raw.append)
        for key, modifiers in ((QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier),
                               (QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.NoModifier),
                               (QtCore.Qt.Key.Key_Tab, QtCore.Qt.KeyboardModifier.NoModifier),
                               (QtCore.Qt.Key.Key_Up, QtCore.Qt.KeyboardModifier.NoModifier)):
            QtTest.QTest.keyClick(preview, key, modifiers)
        self.assertEqual(keys, ["Ctrl+C", "Enter", "Tab", "Up"])
        event = QtGui.QInputMethodEvent()
        event.setCommitString("中文")
        self.app.sendEvent(preview, event)
        self.assertEqual(raw, ["中文".encode("utf-8")])

    def test_new_windows_remain_independent_and_hide_does_not_disconnect(self):
        one = self.runtime.manager.connect("test-node-a")["connection"]["connection_id"]
        two = self.runtime.manager.connect("test-node-a")["connection"]["connection_id"]
        process_until(lambda: len(self.page.windows) == 2)
        self.assertIsNot(self.page.windows[one], self.page.windows[two])
        window = self.page.windows[one]
        window.input.setText("pwd")
        window.send_text()
        self.page.queue_input(one, b"\r", "Enter")
        process_until(lambda: len([item for item in self.factory.sockets[0].frames if item[0] == ABNF.OPCODE_BINARY]) == 2)
        self.assertEqual([data for opcode, data in self.factory.sockets[0].frames if opcode == ABNF.OPCODE_BINARY], [b"pwd", b"\r"])
        window.close()
        self.assertEqual(self.runtime.manager.get(one).status, "connected")
        self.assertTrue(self.page.windows[two].isVisible())

    def test_start_copy_and_stop_controls(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.page.port.setValue(port)
        self.assertFalse(self.page.public.isChecked())
        self.page.start_btn.click()
        process_until(lambda: self.runtime.http.running and not self.page._busy)
        self.page.copy_agent_instructions()
        copied = self.app.clipboard().text()
        # 复制内容必须是可直接粘贴的配置，不含任何额外说明。
        self.assertEqual(json.loads(copied), {"mcpServers": {"komari-terminal": {
            "type": "http", "url": f"http://127.0.0.1:{port}/mcp",
            "headers": {"Authorization": f"Bearer {self.config.mcp_password}"}}}})
        self.assertFalse(self.page.port.isEnabled())
        self.page.start_btn.click()
        process_until(lambda: not self.runtime.http.running and not self.page._busy)
        self.assertTrue(self.page.port.isEnabled())

    def test_shared_controls_keep_step_buttons_and_popup_selection(self):
        ui.apply_style(self.app, "light")
        self.page.port.setValue(12300)
        self.app.processEvents()
        spin = self.page.port
        QtTest.QTest.mouseClick(spin, QtCore.Qt.MouseButton.LeftButton,
                               pos=QtCore.QPoint(spin.width() - 12, spin.height() // 4))
        self.assertEqual(spin.value(), 12301)
        QtTest.QTest.mouseClick(spin, QtCore.Qt.MouseButton.LeftButton,
                               pos=QtCore.QPoint(spin.width() - 12, spin.height() * 3 // 4))
        self.assertEqual(spin.value(), 12300)
        combo = self.page.servers
        combo.addItem("Another example", "test-node-b")
        combo.setFocus()
        QtTest.QTest.keyClick(combo, QtCore.Qt.Key.Key_Down)
        self.assertEqual(combo.currentData(), "test-node-b")
        combo.showPopup()
        self.app.processEvents()
        self.assertTrue(combo.view().isVisible())
        combo.hidePopup()

    def test_status_column_is_chinese_and_error_is_red(self):
        identity = self.runtime.manager.connect("test-node-a")["connection"]["connection_id"]
        self.runtime.manager.get(identity).close(reason="synthetic", error=True)
        self.page.refresh()
        self.app.processEvents()
        item = self.page.connections.item(0, 0)
        self.assertEqual(item.text(), "错误")
        self.assertEqual(item.foreground().color().name(), "#ff7d7d")

    def test_reconnecting_status_is_yellow_and_purge_removes_history(self):
        identity = self.runtime.manager.connect("test-node-a")["connection"]["connection_id"]
        session = self.runtime.manager.get(identity)
        with session._lock:
            session.status = "reconnecting"
        self.page.refresh()
        self.app.processEvents()
        item = self.page.connections.item(0, 0)
        self.assertEqual(item.text(), "重连中")
        self.assertEqual(item.foreground().color().name(), "#e6b422")
        session.close(reason="synthetic")
        self.page.purge_closed()
        self.assertEqual(self.runtime.manager.list_connections(include_closed=True)["total"], 0)

    def test_main_window_navigation_and_theme_snapshots(self):
        root = Path(self.temp.name) / "main-window-logs"
        root.mkdir()
        with patch.object(ui.MainWindow, "ensure_local_config"), patch("ui.CONFIG_PATH", root / "unused.ini"), patch("ui.prepare_log_root", return_value=root):
            window = ui.MainWindow()
        try:
            window.mcp_runtime.manager.client_factory = FakeFactory()
            window.on_clients_loaded(SERVERS, {"test-node-a": {"online": True}})
            window.mcp_page.auto_open.setChecked(False)
            window.mcp_runtime.manager.connect("test-node-a", label="诊断任务")
            window.mcp_runtime.manager.connect("test-node-a", label="日志观察")
            window.stack.setCurrentWidget(window.server_page)
            window.server_page.mcp_btn.click()
            self.assertIs(window.stack.currentWidget(), window.mcp_page)
            window.show()
            self.app.processEvents()
            snapshot_directory = os.environ.get("KOMARI_UI_SNAPSHOT_DIR")
            for theme in ("light", "dark"):
                window.set_theme_mode(theme, persist=False)
                self.app.processEvents()
                if snapshot_directory:
                    destination = Path(snapshot_directory)
                    destination.mkdir(parents=True, exist_ok=True)
                    self.assertTrue(window.grab().save(str(destination / f"mcp-{theme}.png")))
                    window.mcp_page.tabs.setCurrentIndex(1)
                    self.app.processEvents()
                    self.assertTrue(window.grab().save(str(destination / f"assist-{theme}.png")))
                    window.mcp_page.tabs.setCurrentIndex(0)
                self.assertLessEqual(window.width(), 1240)
            with patch.object(window, "refresh_clients"):
                window.mcp_page.back_requested.emit()
            self.assertIs(window.stack.currentWidget(), window.server_page)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
