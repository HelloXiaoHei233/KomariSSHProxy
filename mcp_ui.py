"""MCP settings, independent terminal windows and shared SSH assistance."""
from __future__ import annotations

import codecs
from dataclasses import replace
import json
from pathlib import Path
import queue
import re
import threading
from typing import Any, Callable

from PyQt6 import QtCore, QtGui, QtWidgets

from config import AgentConfig, generate_mcp_password
from mcp_server import McpRuntime, agent_instructions, validate_password, validate_public_url
from terminal_sessions import ACTIVE_STATES, KEYS, STATUS_LABELS, TerminalError
from ui_controls import AppCheckBox, AppComboBox, AppLineEdit, AppPlainTextEdit, AppSpinBox, FramelessWindow, TitleBar


def button(text: str, kind: str = "") -> QtWidgets.QPushButton:
    widget = QtWidgets.QPushButton(text)
    widget.setAutoDefault(False)
    if kind:
        widget.setObjectName(kind)
    return widget


def number(value: int, minimum: int, maximum: int) -> QtWidgets.QSpinBox:
    widget = AppSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


def readonly() -> QtWidgets.QPlainTextEdit:
    widget = AppPlainTextEdit()
    widget.setReadOnly(True)
    font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(10)
    widget.setFont(font)
    return widget


def table(headers: list[str]) -> QtWidgets.QTableWidget:
    widget = QtWidgets.QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().hide()
    widget.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
    widget.setAlternatingRowColors(True)
    widget.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
    widget.verticalHeader().setDefaultSectionSize(34)
    return widget


def password_row(value: str):
    edit = AppLineEdit(value)
    edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
    edit.setMinimumWidth(160)
    show = button("显示")
    show.setCheckable(True)
    show.toggled.connect(lambda checked: edit.setEchoMode(
        QtWidgets.QLineEdit.EchoMode.Normal if checked else QtWidgets.QLineEdit.EchoMode.Password))
    regenerate = button("随机生成")
    regenerate.clicked.connect(lambda: edit.setText(generate_mcp_password()))
    container = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    layout.addWidget(show)
    layout.addWidget(regenerate)
    return container, edit


# 错误标红、重连中标黄；其余状态沿用默认文字颜色。
STATUS_COLORS = {"error": "#ff7d7d", "reconnecting": "#e6b422"}


def fill_table(widget: QtWidgets.QTableWidget, rows: list[list[Any]], *,
               status_column: int | None = None,
               status_values: list[str] | None = None) -> None:
    selected = widget.item(widget.currentRow(), 0)
    identity = selected.data(QtCore.Qt.ItemDataRole.UserRole) if selected else None
    widget.setRowCount(len(rows))
    for index, row in enumerate(rows):
        raw_status = ""
        if status_values is not None and index < len(status_values):
            raw_status = status_values[index]
        for column, value in enumerate(row[1:]):
            item = QtWidgets.QTableWidgetItem(str(value if value is not None else "—"))
            item.setToolTip(item.text())
            item.setData(QtCore.Qt.ItemDataRole.UserRole, row[0])
            if status_column is not None and column == status_column:
                color = STATUS_COLORS.get(raw_status)
                if color:
                    item.setForeground(QtGui.QColor(color))
            widget.setItem(index, column, item)
        if row[0] == identity:
            widget.selectRow(index)


def selected_id(widget: QtWidgets.QTableWidget) -> str | None:
    item = widget.item(widget.currentRow(), 0)
    return item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None


def update_combo(widget: QtWidgets.QComboBox, items: list[tuple[str, str]]) -> None:
    previous_items = [(widget.itemText(i), widget.itemData(i)) for i in range(widget.count())]
    if items == previous_items:
        return
    selected = widget.currentData()
    widget.clear()
    for label, identity in items:
        widget.addItem(label, identity)
    if (index := widget.findData(selected)) >= 0:
        widget.setCurrentIndex(index)


class TerminalPreview(AppPlainTextEdit):
    input_requested = QtCore.pyqtSignal(bytes)
    key_requested = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self._preedit = QtWidgets.QLabel(self.viewport())
        self._preedit.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._preedit.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._preedit.setAutoFillBackground(True)
        self._preedit.hide()

    def inputMethodQuery(self, query):
        queries = QtCore.Qt.InputMethodQuery
        if query == queries.ImEnabled:
            return True
        if query in {queries.ImSurroundingText, queries.ImCurrentSelection,
                     queries.ImTextBeforeCursor, queries.ImTextAfterCursor}:
            return ""
        if query in {queries.ImCursorPosition, queries.ImAnchorPosition, queries.ImAbsolutePosition}:
            return 0
        if query == queries.ImCursorRectangle:
            cursor = QtGui.QTextCursor(self.document())
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            return self.cursorRect(cursor).translated(self.viewport().pos())
        return super().inputMethodQuery(query)

    def event(self, event) -> bool:
        if event.type() == QtCore.QEvent.Type.KeyPress and event.key() in {QtCore.Qt.Key.Key_Tab, QtCore.Qt.Key.Key_Backtab}:
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()
        ctrl = bool(modifiers & QtCore.Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        if ctrl and shift and key == QtCore.Qt.Key.Key_C:
            self.copy()
        elif ctrl and key == QtCore.Qt.Key.Key_V:
            text = QtWidgets.QApplication.clipboard().text()
            if text:
                self.input_requested.emit(text.encode("utf-8"))
        elif ctrl and QtCore.Qt.Key.Key_A <= key <= QtCore.Qt.Key.Key_Z:
            self.key_requested.emit("Ctrl+" + chr(key))
        else:
            names = {
                QtCore.Qt.Key.Key_Return: "Enter", QtCore.Qt.Key.Key_Enter: "Enter",
                QtCore.Qt.Key.Key_Tab: "Tab", QtCore.Qt.Key.Key_Backtab: "Shift+Tab",
                QtCore.Qt.Key.Key_Escape: "Escape", QtCore.Qt.Key.Key_Backspace: "Backspace",
                QtCore.Qt.Key.Key_Delete: "Delete", QtCore.Qt.Key.Key_Up: "Up", QtCore.Qt.Key.Key_Down: "Down",
                QtCore.Qt.Key.Key_Left: "Left", QtCore.Qt.Key.Key_Right: "Right",
                QtCore.Qt.Key.Key_Home: "Home", QtCore.Qt.Key.Key_End: "End",
                QtCore.Qt.Key.Key_PageUp: "PageUp", QtCore.Qt.Key.Key_PageDown: "PageDown",
            }
            if key in names:
                self.key_requested.emit(names[key])
            elif event.text():
                prefix = b"\x1b" if modifiers & QtCore.Qt.KeyboardModifier.AltModifier else b""
                self.input_requested.emit(prefix + event.text().encode("utf-8"))
        event.accept()

    def inputMethodEvent(self, event) -> None:
        if event.commitString():
            self.input_requested.emit(event.commitString().encode("utf-8"))
        self._preedit.setText(event.preeditString())
        if event.preeditString():
            palette = self.palette()
            palette.setColor(QtGui.QPalette.ColorRole.Window, palette.color(QtGui.QPalette.ColorRole.Base))
            palette.setColor(QtGui.QPalette.ColorRole.WindowText, palette.color(QtGui.QPalette.ColorRole.Text))
            self._preedit.setPalette(palette)
            self._preedit.setFont(self.font())
            self._preedit.adjustSize()
            rect = self.inputMethodQuery(QtCore.Qt.InputMethodQuery.ImCursorRectangle)
            point = self.viewport().mapFrom(self, rect.topLeft())
            self._preedit.move(max(0, min(point.x(), self.viewport().width() - self._preedit.width())),
                               max(0, min(point.y(), self.viewport().height() - self._preedit.height())))
        self._preedit.setVisible(bool(event.preeditString()))
        event.accept()


class TerminalWindow(FramelessWindow):
    def __init__(self, page: "McpPage", connection_id: str):
        super().__init__()
        self.page = page
        self.connection_id = connection_id
        self.cursor = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._escape_pending = ""
        self.resize(960, 620)
        self.setMinimumSize(680, 440)
        self.setObjectName("McpTerminalWindow")
        self.setWindowIcon(page.window().windowIcon())
        self.title_bar = TitleBar(self, show_theme=False)
        self.windowTitleChanged.connect(self.title_bar.title.setText)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.output = TerminalPreview()
        self.output.input_requested.connect(lambda raw: page.queue_input(connection_id, raw))
        self.output.key_requested.connect(lambda key: page.queue_input(connection_id, KEYS[key], key))
        details = button("连接详情")
        details.clicked.connect(lambda: page.show_connection_details(connection_id))
        export = button("导出保留的输出")
        export.clicked.connect(self.export_output)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(details)
        top.addWidget(export)
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addLayout(top)
        layout.addWidget(self.output, 1)
        shell = QtWidgets.QFrame()
        shell.setObjectName("AppShell")
        shell_layout = QtWidgets.QVBoxLayout(shell)
        shell_layout.setContentsMargins(1, 1, 1, 1)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self.title_bar)
        shell_layout.addWidget(content, 1)
        root = QtWidgets.QWidget()
        root.setObjectName("Root")
        root_layout = QtWidgets.QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.addWidget(shell)
        self.setCentralWidget(root)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)

    def poll(self) -> None:
        try:
            session = self.page.runtime.manager.get(self.connection_id)
            state = session.snapshot()
            self.setWindowTitle(f"{state['label']} · {self.connection_id[:8]}")
            label = STATUS_LABELS.get(state["status"], state["status"])
            self.status.setText(f"{label} · {state['server'].get('name', '')} · 协助者 {len(state['collaborators'])} · 收 {state['bytes_received']} B / 发 {state['bytes_sent']} B")
            self.status.setStyleSheet(f"color: {STATUS_COLORS[state['status']]};"
                                      if state["status"] in STATUS_COLORS else "")
            result = session.output.read(self.cursor, max_bytes=32768)
            if result["dropped_bytes"]:
                self.output.appendPlainText(f"\n[较早的 {result['dropped_bytes']} 字节已滚动淘汰]\n")
                self.decoder.reset()
            self.cursor = result["next_cursor"]
            if result["data"]:
                text = self._escape_pending + self.decoder.decode(result["data"])
                self._escape_pending = ""
                # Preserve incomplete escape sequences across WebSocket/read chunk boundaries.
                last_escape = text.rfind("\x1b")
                if last_escape >= 0 and re.fullmatch(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07]*|)", text[last_escape:]):
                    self._escape_pending = text[last_escape:][-4096:]
                    text = text[:last_escape]
                text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])", "", text)
                text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                bar = self.output.verticalScrollBar()
                follow = bar.value() >= bar.maximum() - 5
                cursor = QtGui.QTextCursor(self.output.document())
                cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
                cursor.insertText(text)
                if follow:
                    bar.setValue(bar.maximum())
        except TerminalError as exc:
            self.status.setText(str(exc))
            self.timer.stop()

    def export_output(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出保留的终端原始输出", f"terminal-{self.connection_id[:8]}.txt", "Text (*.txt);;Raw bytes (*.bin)")
        if not path:
            return
        try:
            output = self.page.runtime.manager.get(self.connection_id).output
            with output.condition:
                data = bytes(output.data)
            Path(path).write_bytes(data)
            self.page.runtime.log.add("terminal_output_exported", actor="gui", connection_id=self.connection_id, bytes=len(data))
        except (OSError, TerminalError) as exc:
            self.page.show_error(str(exc))

    def showEvent(self, event) -> None:
        self.timer.start()
        self.poll()
        super().showEvent(event)
        self.output.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    def closeEvent(self, event) -> None:
        self.timer.stop()
        event.accept()


class McpPage(QtWidgets.QWidget):
    back_requested = QtCore.pyqtSignal()
    refresh_requested = QtCore.pyqtSignal()
    session_changed = QtCore.pyqtSignal(object)
    task_done = QtCore.pyqtSignal(object, object)
    task_failed = QtCore.pyqtSignal(str)

    def __init__(self, runtime: McpRuntime, config: AgentConfig, save_config: Callable[[AgentConfig], None]):
        super().__init__()
        self.runtime = runtime
        self.config = config
        self.save_config = save_config
        self.windows: dict[str, TerminalWindow] = {}
        self._shown: set[str] = set()
        self._closed = False
        self._busy = False
        self._log_cursor = 0
        self._inputs: queue.Queue = queue.Queue(maxsize=1024)
        self.task_done.connect(self._task_done)
        self.task_failed.connect(self.show_error)
        self.session_changed.connect(self.on_session_changed)
        runtime.manager.on_event = self.session_changed.emit
        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        threading.Thread(target=self._input_worker, name="mcp-gui-input", daemon=True).start()
        self.refresh()

    def _build(self) -> None:
        title = QtWidgets.QLabel("MCP协助")
        title.setObjectName("PageTitle")
        back = button("返回服务器")
        back.clicked.connect(self.back_requested.emit)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(back)
        self.notice = QtWidgets.QLabel("MCP 已关闭 · 默认仅允许本机访问")
        self.notice.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        card = QtWidgets.QFrame()
        card.setObjectName("Card")
        grid = QtWidgets.QGridLayout(card)
        self.port = number(self.config.mcp_port, 1, 65535)
        self.password_container, self.password = password_row(self.config.mcp_password)
        self.start_btn = button("启动 MCP", "Primary")
        self.start_btn.clicked.connect(self.toggle_mcp)
        self.public = AppCheckBox("允许公网访问")
        self.public.setChecked(False)
        self.public_url = AppLineEdit(self.config.mcp_public_url)
        self.public_url.setPlaceholderText("远程连接地址，例如 https://mcp.example.com/mcp")
        self.copy_btn = button("复制 Agent 连接指令")
        self.copy_btn.clicked.connect(self.copy_agent_instructions)
        self.idle = number(self.config.mcp_idle_minutes, 0, 10080)
        self.idle.setSuffix(" 分钟")
        self.idle.setSpecialValueText("不自动断开")
        self.sticky = number(self.config.mcp_sticky_minutes, 0, 10080)
        self.sticky.setSuffix(" 分钟")
        self.sticky.setSpecialValueText("关闭粘性重连")
        grid.addWidget(QtWidgets.QLabel("MCP 端口"), 0, 0)
        grid.addWidget(self.port, 0, 1)
        grid.addWidget(QtWidgets.QLabel("鉴权密码"), 0, 2)
        grid.addWidget(self.password_container, 0, 3)
        grid.addWidget(self.start_btn, 0, 4)
        grid.addWidget(self.public, 1, 0, 1, 2)
        grid.addWidget(QtWidgets.QLabel("对外 URL"), 1, 2)
        grid.addWidget(self.public_url, 1, 3)
        grid.addWidget(self.copy_btn, 1, 4)
        grid.addWidget(QtWidgets.QLabel("空闲断开"), 2, 0)
        grid.addWidget(self.idle, 2, 1)
        grid.addWidget(QtWidgets.QLabel("粘性重连"), 3, 0)
        grid.addWidget(self.sticky, 3, 1)
        self.endpoint = QtWidgets.QLabel()
        self.endpoint.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.endpoint.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.endpoint.setWordWrap(True)
        grid.addWidget(self.endpoint, 2, 2, 1, 3)
        self.tabs = QtWidgets.QTabWidget()
        self._build_terminals()
        self._build_assistance()
        self._build_logs()
        self.service_details = readonly()
        self.tabs.addTab(self.service_details, "连接详情")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.addLayout(header)
        layout.addWidget(self.notice)
        layout.addWidget(card)
        layout.addWidget(self.tabs, 1)

    def _build_terminals(self) -> None:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        row = QtWidgets.QHBoxLayout()
        self.servers = AppComboBox()
        self.servers.setMinimumWidth(180)
        self.label = AppLineEdit()
        self.label.setPlaceholderText("窗口备注（可选）")
        self.two_fa = AppLineEdit()
        self.two_fa.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.two_fa.setPlaceholderText("终端 2FA（可选）")
        self.two_fa.setMaximumWidth(180)
        self.connect_btn = button("新建终端窗口", "Primary")
        self.connect_btn.clicked.connect(self.connect_terminal)
        refresh = button("刷新服务器")
        refresh.clicked.connect(self.refresh_requested.emit)
        for widget in (self.servers, self.label, self.two_fa, self.connect_btn, refresh):
            row.addWidget(widget)
        self.connections = table(["状态", "服务器", "窗口备注", "窗口 ID", "连接时间", "最近输入", "协助者"])
        self.connections.doubleClicked.connect(lambda: self.open_selected())
        actions = QtWidgets.QHBoxLayout()
        # 双击列表行即可打开窗口；“连接详情”与选项卡功能重复，因此不再单独放置按钮。
        for caption, handler, kind in (
            ("Ctrl+C 中断", self.interrupt_selected, "Warning"),
            ("断开选中终端", self.disconnect_selected, "Danger"),
            ("断开全部终端", lambda: self.run_task(lambda: self.runtime.manager.disconnect_all(actor="gui")), "Danger"),
            ("清除已断开", self.purge_closed, ""),
        ):
            control = button(caption, kind)
            control.clicked.connect(handler)
            actions.addWidget(control)
        actions.addStretch(1)
        self.auto_open = AppCheckBox("新连接自动显示窗口")
        self.auto_open.setChecked(self.config.mcp_auto_open)
        self.auto_open.toggled.connect(self.save_auto_open)
        actions.addWidget(self.auto_open)
        self.counts = QtWidgets.QLabel()
        self.counts.setObjectName("Muted")
        layout.addLayout(row)
        layout.addWidget(self.connections, 1)
        layout.addLayout(actions)
        layout.addWidget(self.counts)
        self.tabs.addTab(page, "终端窗口")

    def _build_assistance(self) -> None:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        grid = QtWidgets.QGridLayout()
        self.assist_target = AppComboBox()
        self.assist_port = number(self.config.assist_port, 1, 65535)
        self.assist_user = AppLineEdit(self.config.assist_username)
        self.assist_password_container, self.assist_password = password_row(self.config.assist_password)
        self.assist_btn = button("启用协助模式", "Primary")
        self.assist_btn.clicked.connect(self.toggle_assist)
        copy = button("复制 SSH 连接命令")
        copy.clicked.connect(self.copy_ssh_command)
        grid.addWidget(QtWidgets.QLabel("共享终端窗口"), 0, 0)
        grid.addWidget(self.assist_target, 0, 1, 1, 3)
        grid.addWidget(self.assist_btn, 0, 4)
        grid.addWidget(QtWidgets.QLabel("本机端口"), 1, 0)
        grid.addWidget(self.assist_port, 1, 1)
        grid.addWidget(QtWidgets.QLabel("用户名"), 1, 2)
        grid.addWidget(self.assist_user, 1, 3)
        grid.addWidget(copy, 1, 4)
        grid.addWidget(QtWidgets.QLabel("协助 SSH 密码"), 2, 0)
        grid.addWidget(self.assist_password_container, 2, 1, 1, 4)
        note = QtWidgets.QLabel("协助 SSH 仅监听 127.0.0.1，与选中窗口共享输入和输出。退出 SSH 连接只退出协助；在共享 shell 输入 exit / Ctrl+D 可能关闭 AI 终端。新协助者会收到最近 64 KiB 输出。")
        note.setWordWrap(True)
        self.assist_status = QtWidgets.QLabel()
        self.assist_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.assist_status.setWordWrap(True)
        self.assist_connections = table(["用户", "来源", "SSH 客户端", "连接时间", "输入字节", "输出字节"])
        layout.addLayout(grid)
        layout.addWidget(note)
        layout.addWidget(self.assist_status)
        layout.addWidget(self.assist_connections, 1)
        self.tabs.addTab(page, "协助 SSH")

    def _build_logs(self) -> None:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        actions = QtWidgets.QHBoxLayout()
        export = button("导出操作日志")
        export.clicked.connect(self.export_logs)
        clear = button("清空显示")
        clear.clicked.connect(lambda: self.logs.clear())
        self.pause_logs = AppCheckBox("暂停滚动")
        actions.addWidget(QtWidgets.QLabel("记录动作与数据量；不记录输入正文、密码、Cookie 或 2FA。"), 1)
        actions.addWidget(self.pause_logs)
        actions.addWidget(clear)
        actions.addWidget(export)
        self.logs = readonly()
        self.logs.setMaximumBlockCount(4000)
        layout.addLayout(actions)
        layout.addWidget(self.logs, 1)
        layout.addWidget(QtWidgets.QLabel("导出包含保留的完整操作日志（最多 16 MiB）；退出程序后临时日志会清理，请提前导出。"))
        self.tabs.addTab(page, "操作日志流")

    def collect_config(self) -> AgentConfig:
        config = replace(self.config)
        config.mcp_port = self.port.value()
        config.mcp_password = self.password.text()
        config.mcp_public_access = self.public.isChecked()
        config.mcp_public_url = validate_public_url(self.public_url.text().strip())
        config.mcp_idle_minutes = self.idle.value()
        config.mcp_sticky_minutes = self.sticky.value()
        config.mcp_auto_open = self.auto_open.isChecked()
        config.assist_port = self.assist_port.value()
        config.assist_username = self.assist_user.text().strip()
        config.assist_password = self.assist_password.text()
        validate_password(config.mcp_password)
        validate_password(config.assist_password)
        return config

    def persist_settings(self) -> AgentConfig:
        config = self.collect_config()
        self.save_config(config)
        self.config = config
        self.runtime.update_config(config)
        return config

    def load_config(self, config: AgentConfig) -> None:
        self.config = config
        with QtCore.QSignalBlocker(self.auto_open):
            self.auto_open.setChecked(config.mcp_auto_open)
        if not self.runtime.http.running:
            self.password.setText(config.mcp_password)
            self.port.setValue(config.mcp_port)
            self.public_url.setText(config.mcp_public_url)
            self.idle.setValue(config.mcp_idle_minutes)
            self.sticky.setValue(config.mcp_sticky_minutes)
        if not self.runtime.assist or not self.runtime.assist.running:
            self.assist_port.setValue(config.assist_port)
            self.assist_user.setText(config.assist_username)
            self.assist_password.setText(config.assist_password)

    def save_auto_open(self, enabled: bool) -> None:
        # Save this preference independently of unfinished service settings.
        config = replace(self.config, mcp_auto_open=enabled)
        try:
            self.save_config(config)
        except OSError:
            with QtCore.QSignalBlocker(self.auto_open):
                self.auto_open.setChecked(self.config.mcp_auto_open)
            self.show_error("无法保存窗口显示设置，请检查程序目录的写入权限。")
            return
        self.config = config
        self.runtime.update_config(config)

    def toggle_mcp(self) -> None:
        if self._busy:
            return
        if self.runtime.http.running:
            self._busy = True
            self.run_task(self.runtime.stop, lambda _: self._release_busy("MCP 已关闭，终端与协助连接已断开。"))
        else:
            try:
                config = self.persist_settings()
            except (TerminalError, OSError) as exc:
                self.show_error(str(exc))
                return
            self._busy = True
            self.run_task(lambda: self.runtime.start(config), lambda _: self._release_busy("MCP 已启动。"))
        self.refresh()

    def _release_busy(self, text: str) -> None:
        self._busy = False
        self.notice.setText(text)
        self.refresh()

    def toggle_assist(self) -> None:
        if self._busy:
            return
        if self.runtime.assist and self.runtime.assist.running:
            self._busy = True
            self.run_task(self.runtime.stop_assist, lambda _: self._release_busy("协助模式已关闭，AI 终端继续运行。"))
        else:
            connection_id = self.assist_target.currentData()
            if not connection_id:
                self.show_error("请先选择一个已连接的终端窗口。")
                return
            try:
                config = self.persist_settings()
            except (TerminalError, OSError) as exc:
                self.show_error(str(exc))
                return
            self._busy = True
            self.run_task(lambda: self.runtime.start_assist(connection_id, config), lambda _: self._release_busy("协助模式已启用。"))

    def connect_terminal(self) -> None:
        server_uuid = self.servers.currentData()
        if not server_uuid:
            self.show_error("请先加载并选择服务器。")
            return
        code, label = self.two_fa.text(), self.label.text()
        self.two_fa.clear()
        self.run_task(lambda: self.runtime.manager.connect(server_uuid, label=label, two_fa_code=code, actor="gui"),
                      lambda result: self.notice.setText(result["message"]))

    def purge_closed(self) -> None:
        """清除列表里已断开/出错的记录，避免窗口历史无限堆积。"""
        removed = self.runtime.manager.purge_closed()
        self.notice.setText(f"已清除 {removed} 条已断开记录。" if removed else "没有可清除的已断开记录。")
        self.refresh()

    def require_selected(self) -> str | None:
        identity = selected_id(self.connections)
        if not identity:
            self.show_error("请先选择一个终端窗口。")
        return identity

    def open_selected(self) -> None:
        if identity := self.require_selected():
            self.open_window(identity)

    def open_window(self, identity: str) -> None:
        if identity not in self.windows:
            self.windows[identity] = TerminalWindow(self, identity)
        window = self.windows[identity]
        window.show()
        window.raise_()
        window.activateWindow()
        window.output.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    def interrupt_selected(self) -> None:
        if identity := self.require_selected():
            self.queue_input(identity, KEYS["Ctrl+C"], "Ctrl+C")

    def disconnect_selected(self) -> None:
        if identity := self.require_selected():
            self.run_task(lambda: self.runtime.manager.disconnect(identity, actor="gui"))

    def show_connection_details(self, identity: str | None) -> None:
        if not identity:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("终端连接详情")
        dialog.resize(820, 620)
        layout = QtWidgets.QVBoxLayout(dialog)
        view = readonly()
        layout.addWidget(view)
        timer = QtCore.QTimer(dialog)
        def update() -> None:
            try:
                text = json.dumps(self.runtime.manager.get(identity).snapshot(), ensure_ascii=False, indent=2)
                if view.toPlainText() != text:
                    scroll = view.verticalScrollBar().value()
                    view.setPlainText(text)
                    view.verticalScrollBar().setValue(scroll)
            except TerminalError as exc:
                view.setPlainText(str(exc))
                timer.stop()
        timer.timeout.connect(update)
        timer.start(1000)
        update()
        dialog.exec()

    def copy_agent_instructions(self) -> None:
        if not self.runtime.http.running:
            self.show_error("请先启动 MCP，再复制可用的连接指令。")
            return
        QtWidgets.QApplication.clipboard().setText(agent_instructions(self.runtime.http.url, self.config.mcp_password))
        self.notice.setText("已复制 Agent 连接配置（含鉴权密码）；公网连接请使用设置中的对外 URL。")

    def copy_ssh_command(self) -> None:
        if not self.runtime.assist or not self.runtime.assist.running:
            self.show_error("请先启用协助模式。")
            return
        info = self.runtime.assist.info()
        # Password stays out of the command line and shell history.
        QtWidgets.QApplication.clipboard().setText(f"ssh -tt -p {info['port']} {info['username']}@127.0.0.1")
        self.notice.setText("已复制 SSH 命令；连接时输入协助 SSH 密码。")

    def export_logs(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 MCP 操作日志", "mcp-operations.jsonl", "JSON Lines (*.jsonl);;Text (*.txt)")
        if path:
            try:
                self.runtime.log.add("logs_exported", actor="gui")
                self.runtime.log.export(Path(path))
                self.notice.setText("操作日志已导出。")
            except OSError as exc:
                self.show_error(str(exc))

    def queue_input(self, identity: str, raw: bytes, key: str | None = None) -> None:
        try:
            self._inputs.put_nowait((identity, raw, key))
        except queue.Full:
            self.show_error("输入队列已满，请等待终端响应后重试。")

    def _input_worker(self) -> None:
        while not self._closed:
            try:
                identity, raw, key = self._inputs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.runtime.manager.get(identity).send(raw, actor="gui", key=key)
            except TerminalError as exc:
                self.task_failed.emit(str(exc))
            finally:
                self._inputs.task_done()

    def run_task(self, function: Callable, callback: Callable | None = None) -> None:
        def work() -> None:
            try:
                result = function()
                if not self._closed:
                    self.task_done.emit(callback, result)
            except Exception as exc:
                if not self._closed:
                    self.task_failed.emit(str(exc) if isinstance(exc, (TerminalError, OSError)) else f"操作失败（{type(exc).__name__}）。")
        threading.Thread(target=work, name="mcp-gui-task", daemon=True).start()

    def _task_done(self, callback, result) -> None:
        if callback and not self._closed:
            callback(result)
        if not self._closed:
            self.refresh()

    def show_error(self, message: str) -> None:
        self._busy = False
        self.notice.setText(message)

    def on_session_changed(self, session: dict) -> None:
        identity = session["connection_id"]
        if session["status"] == "connected" and identity not in self._shown:
            self._shown.add(identity)
            if self.auto_open.isChecked() and not self._closed:
                self.open_window(identity)
        if not self._closed:
            self.refresh()

    def refresh(self) -> None:
        if self._closed:
            return
        running = self.runtime.http.running
        assisting = bool(self.runtime.assist and self.runtime.assist.running)
        self.start_btn.setText("处理中…" if self._busy else "停止 MCP 并断开终端" if running else "启动 MCP")
        self.start_btn.setEnabled(not self._busy)
        self.assist_btn.setText("关闭协助模式" if assisting else "启用协助模式")
        self.assist_btn.setEnabled(running and not self._busy)
        for widget in (self.port, self.password_container, self.public, self.public_url, self.idle,
                       self.sticky):
            widget.setEnabled(not running and not self._busy)
        for widget in (self.assist_target, self.assist_port, self.assist_user, self.assist_password_container):
            widget.setEnabled(not assisting and not self._busy)
        self.copy_btn.setEnabled(running)
        self.connect_btn.setEnabled(running and bool(self.servers.count()) and not self._busy)
        host = "0.0.0.0" if self.runtime.http.public_access else "127.0.0.1"
        self.endpoint.setText(f"监听 {host}:{self.runtime.http.port} · {self.runtime.http.url}" if running
                              else "服务已关闭 · 公网访问默认关闭 · 跨公网建议使用 HTTPS")
        catalog = self.runtime.manager.list_terminals()["terminals"]
        update_combo(self.servers, [(f"{item.get('name') or item['uuid']} · {item['open_window_count']} 个窗口", item["uuid"]) for item in catalog])
        sessions = self.runtime.manager.list_connections(include_closed=True)["connections"]
        retained_ids = {item["connection_id"] for item in sessions}
        for identity in list(self.windows):
            if identity not in retained_ids:
                expired = self.windows.pop(identity)
                expired.close()
                expired.deleteLater()
        self._shown.intersection_update(retained_ids)
        ordered = list(reversed(sessions))
        rows = [[item["connection_id"], STATUS_LABELS.get(item["status"], item["status"]),
                 item["server"].get("name", ""), item["label"],
                 item["connection_id"][:8], item["connected_at"] or item["created_at"],
                 item["last_input_at"], len(item["collaborators"])] for item in ordered]
        fill_table(self.connections, rows, status_column=0,
                   status_values=[item["status"] for item in ordered])
        active = [item for item in sessions if item["status"] in ACTIVE_STATES]
        self.counts.setText(f"活动窗口 {len(active)} / 32 · 历史记录 {len(sessions) - len(active)} · 时间以 UTC 显示 · 重复连接始终新建窗口")
        update_combo(self.assist_target, [(f"{item['label']} · {item['connection_id'][:8]}", item["connection_id"])
                                         for item in active if item["status"] == "connected"])
        info = self.runtime.info()
        assist = info["assist"]
        self.assist_status.setText(f"共享 {assist['connection_id'][:8]} · 127.0.0.1:{assist['port']} · 主机指纹 {assist['host_key_fingerprint']}" if assisting else "协助模式未启用")
        fill_table(self.assist_connections, [[item["id"], item["username"], f"{item['peer_host']}:{item['peer_port']}",
                    item["client_version"], item["connected_at"], item["bytes_sent"], item["bytes_received"]]
                   for item in assist.get("collaborators", [])])
        if self.tabs.currentWidget() is self.service_details:
            scroll = self.service_details.verticalScrollBar().value()
            self.service_details.setPlainText(json.dumps(info, ensure_ascii=False, indent=2))
            self.service_details.verticalScrollBar().setValue(scroll)
        if not self.pause_logs.isChecked():
            result = self.runtime.log.read(self._log_cursor, 500)
            if result["dropped_entries"]:
                self.logs.appendPlainText(f"[较早的 {result['dropped_entries']} 条记录已从实时缓存淘汰，可导出保留的日志文件]")
            if result["entries"]:
                self.logs.appendPlainText("\n".join(json.dumps(item, ensure_ascii=False) for item in result["entries"]))
                self._log_cursor = result["next_cursor"]

    def shutdown(self) -> None:
        self._closed = True
        self.timer.stop()
        self.runtime.manager.on_event = None
        for window in self.windows.values():
            window.close()
            window.deleteLater()
        self.windows.clear()
