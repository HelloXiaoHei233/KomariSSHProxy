from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import threading
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable

from PyQt6 import QtCore, QtGui, QtWidgets

from config import APP_DISPLAY_NAME, AgentConfig, CONFIG_PATH
from komari_client import KomariClient, KomariError, normalize_latest_status
from oauth_login import OAuthCallbackServer
from panel_auth import PanelLoginError, checked_config, login_error_message, login_with_password
from request_parser import apply_parsed_request, describe_parsed, parse_request_text
from ssh_bridge import LocalSSHProxyServer, ensure_config_host_key
from ui_controls import AppCheckBox, AppLineEdit, AppPlainTextEdit, AppSpinBox, AppTextEdit, FramelessWindow, TitleBar


LOG_VIEW_MAX_BLOCKS = 4000


def prepare_log_root() -> Path:
    temp_root = Path(tempfile.gettempdir())
    now = datetime.now().timestamp()
    for stale in temp_root.glob("komari-ssh-proxy-*"):
        if stale.is_dir() and process_from_pid_file_is_running(stale / "pid"):
            continue
        try:
            age = now - stale.stat().st_mtime
        except OSError:
            continue
        if stale.is_dir() and age > 24 * 60 * 60:
            shutil.rmtree(stale, ignore_errors=True)
    root = Path(tempfile.mkdtemp(prefix="komari-ssh-proxy-"))
    (root / "pid").write_text(str(os.getpid()), encoding="utf-8")
    return root


def process_from_pid_file_is_running(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            try:
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class AppSignals(QtCore.QObject):
    login_success = QtCore.pyqtSignal(object, object)
    login_failed = QtCore.pyqtSignal(str)
    oauth_session = QtCore.pyqtSignal(object, int)
    oauth_failed = QtCore.pyqtSignal(str, int)
    oauth_link_ready = QtCore.pyqtSignal(object, int)
    oauth_link_failed = QtCore.pyqtSignal(str, int)
    clients_loaded = QtCore.pyqtSignal(object, object)
    clients_failed = QtCore.pyqtSignal(str)
    status_loaded = QtCore.pyqtSignal(object)
    ssh_log = QtCore.pyqtSignal(str, str)
    ssh_state = QtCore.pyqtSignal(str, str, bool)


@dataclass
class ProxyRuntime:
    server: LocalSSHProxyServer
    client: dict[str, Any]
    config: AgentConfig
    running: bool = False
    state_text: str = "启动中"


class ProxyLogStore:
    def __init__(self, uuid: str, log_root: Path):
        self.uuid = uuid
        self.path = log_root / f"{safe_filename(uuid)}.log"
        self.tail: deque[str] = deque(maxlen=LOG_VIEW_MAX_BLOCKS * 2)
        self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._write_count = 0

    def append(self, line: str) -> None:
        self.tail.append(line)
        self._file.write(line + "\n")
        self._write_count += 1
        if self._write_count % 200 == 0:
            self._file.flush()

    def filtered_tail(self, predicate: Callable[[str], bool]) -> list[str]:
        return [line for line in self.tail if predicate(line)][-LOG_VIEW_MAX_BLOCKS:]

    def export_to(self, path: str, predicate: Callable[[str], bool] | None = None) -> None:
        self._file.flush()
        predicate = predicate or (lambda line: True)
        with self.path.open("r", encoding="utf-8", errors="replace") as src:
            with open(path, "w", encoding="utf-8", newline="\n") as dst:
                for line in src:
                    text = line.rstrip("\n")
                    if predicate(text):
                        dst.write(text + "\n")

    def clear(self) -> None:
        self._file.close()
        self.path.write_text("", encoding="utf-8")
        self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._write_count = 0
        self.tail.clear()

    def close(self) -> None:
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass


class ClickableLabel(QtWidgets.QLabel):
    clicked = QtCore.pyqtSignal()

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ServerTableWidget(QtWidgets.QTableWidget):
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
            bar = self.horizontalScrollBar()
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                bar.setValue(bar.value() - delta)
                event.accept()
                return
        super().wheelEvent(event)


class CredentialsPage(QtWidgets.QWidget):
    save_requested = QtCore.pyqtSignal(object)
    password_requested = QtCore.pyqtSignal(object, str, str, str)
    oauth_requested = QtCore.pyqtSignal(object)
    oauth_prepare_requested = QtCore.pyqtSignal(object)
    oauth_copy_requested = QtCore.pyqtSignal()
    oauth_callback_requested = QtCore.pyqtSignal(str)
    auth_changed = QtCore.pyqtSignal()
    back_requested = QtCore.pyqtSignal()

    def __init__(self, config: AgentConfig):
        super().__init__()
        self.current_config = config
        self._busy = False
        self._oauth_available = False
        self.base_url = line_edit(config.base_url)
        self.user_agent = line_edit(config.user_agent)
        self.session_token = line_edit(config.session_token)
        self.login_username = line_edit("")
        self.login_password = line_edit("")
        self.login_password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.login_2fa = line_edit("")
        self.login_2fa.setPlaceholderText("已启用 2FA 时填写当前验证码")
        self.login_2fa.setMaxLength(32)
        self.cf_clearance = line_edit(config.cf_clearance)
        self.language = line_edit(config.language)
        self.sensorsdata = text_edit(config.sensorsdata2015jssdkcross, 86)
        self.extra_cookies = text_edit(config.extra_cookies, 76)
        self.request_input = text_edit("", 112)
        self.request_input.setPlaceholderText(
            "粘贴浏览器复制出来的 PowerShell / curl 请求，例如 Invoke-WebRequest 或 curl 'https://...'"
        )
        self.parse_result = QtWidgets.QLabel("等待粘贴请求")
        self.parse_result.setObjectName("Muted")
        self.parse_result.setWordWrap(True)
        self.status = QtWidgets.QLabel("")
        self.save_btn = primary_button("保存并登录")
        self.save_btn.clicked.connect(self._save)
        self.back_btn = soft_button("返回服务器列表")
        self.back_btn.clicked.connect(self.back_requested.emit)
        self.back_btn.hide()

        title = QtWidgets.QLabel("登录凭据")
        title.setObjectName("HeroTitle")
        subtitle = QtWidgets.QLabel(
            f"支持账号密码、OAuth 和 Cookie 登录。验证成功后，会话保存到 {CONFIG_PATH.name}；密码与验证码不保存。"
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.addRow("面板地址", self.base_url)
        form.addRow("User-Agent", self.user_agent)

        self.login_tabs = QtWidgets.QTabWidget()
        password_page = QtWidgets.QWidget()
        password_form = QtWidgets.QFormLayout(password_page)
        password_form.addRow("账号", self.login_username)
        password_form.addRow("密码", self.login_password)
        password_form.addRow("2FA 验证码", self.login_2fa)
        self.login_tabs.addTab(password_page, "账号密码")
        oauth_page = QtWidgets.QWidget()
        oauth_layout = QtWidgets.QVBoxLayout(oauth_page)
        oauth_hint = QtWidgets.QLabel(
            "软件自动解析面板 OAuth 跳转，直接打开服务商授权页。也可生成链接，复制到其他浏览器。\n"
            "授权后将地址栏中的完整面板回调链接粘贴到下方；显示 Invalid state 时仍可继续。"
        )
        oauth_hint.setWordWrap(True)
        oauth_layout.addWidget(oauth_hint)
        self.oauth_link = line_edit("")
        self.oauth_link.setReadOnly(True)
        self.oauth_link.setPlaceholderText("点击生成链接或直接打开 OAuth 授权")
        self.oauth_refresh_btn = soft_button("生成 / 刷新授权链接")
        self.oauth_refresh_btn.clicked.connect(lambda: self.oauth_prepare_requested.emit(self.to_config()))
        self.oauth_copy_btn = soft_button("复制授权链接")
        self.oauth_copy_btn.clicked.connect(self.oauth_copy_requested.emit)
        callback_row = QtWidgets.QHBoxLayout()
        callback_row.addWidget(self.oauth_link, 1)
        callback_row.addWidget(self.oauth_refresh_btn)
        callback_row.addWidget(self.oauth_copy_btn)
        oauth_layout.addLayout(callback_row)
        self.oauth_status = QtWidgets.QLabel("")
        self.oauth_status.setWordWrap(True)
        oauth_layout.addWidget(self.oauth_status)
        self.oauth_result = line_edit("")
        self.oauth_result.setPlaceholderText("粘贴授权后 /api/oauth_callback?code=... 的完整链接")
        self.oauth_result.setMaxLength(16384)
        self.oauth_submit_btn = soft_button("提交回调链接")
        self.oauth_submit_btn.clicked.connect(lambda: self.oauth_callback_requested.emit(self.oauth_result.text()))
        result_row = QtWidgets.QHBoxLayout()
        result_row.addWidget(self.oauth_result, 1)
        result_row.addWidget(self.oauth_submit_btn)
        oauth_layout.addLayout(result_row)
        oauth_layout.addStretch(1)
        self.login_tabs.addTab(oauth_page, "OAuth")
        cookie_page = QtWidgets.QWidget()
        cookie_layout = QtWidgets.QVBoxLayout(cookie_page)
        cookie_form = QtWidgets.QFormLayout()
        cookie_form.addRow("session_token", self.session_token)
        cookie_form.addRow("cf_clearance", self.cf_clearance)
        cookie_form.addRow("language", self.language)
        cookie_form.addRow("sensorsdata2015jssdkcross", self.sensorsdata)
        cookie_form.addRow("额外 Cookie", self.extra_cookies)

        parser_card = QtWidgets.QWidget()
        parser_layout = QtWidgets.QVBoxLayout(parser_card)
        parser_layout.setContentsMargins(18, 16, 18, 16)
        parser_layout.setSpacing(10)
        parser_title = QtWidgets.QLabel("智能识别请求")
        parser_title.setObjectName("SectionTitle")
        parser_hint = QtWidgets.QLabel("从开发者工具复制 PowerShell 或 curl 请求，粘贴后可自动提取地址、UA 和 Cookie。")
        parser_hint.setObjectName("Muted")
        parser_hint.setWordWrap(True)
        parser_actions = QtWidgets.QHBoxLayout()
        paste_btn = soft_button("一键粘贴")
        paste_btn.clicked.connect(self.paste_request)
        parse_btn = primary_button("识别并填充")
        parse_btn.clicked.connect(self.parse_request)
        clear_btn = soft_button("清空")
        clear_btn.clicked.connect(self.request_input.clear)
        parser_actions.addWidget(self.parse_result, 1)
        parser_actions.addWidget(clear_btn)
        parser_actions.addWidget(paste_btn)
        parser_actions.addWidget(parse_btn)
        parser_layout.addWidget(parser_title)
        parser_layout.addWidget(parser_hint)
        parser_layout.addWidget(self.request_input)
        parser_layout.addLayout(parser_actions)
        cookie_layout.addWidget(parser_card)
        cookie_layout.addLayout(cookie_form)
        self.login_tabs.addTab(cookie_page, "Cookie")
        self.login_tabs.setCurrentIndex(2 if config.session_token else 0)
        self.login_tabs.currentChanged.connect(self._login_mode_changed)

        card = card_frame()
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(28, 26, 28, 26)
        card_layout.setSpacing(18)
        title_row = QtWidgets.QHBoxLayout()
        title_row.addWidget(title)
        title_row.addWidget(self.back_btn)
        title_row.addStretch(1)
        card_layout.addLayout(title_row)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(6)
        card_layout.addLayout(form)
        card_layout.addWidget(self.login_tabs)
        card_layout.addSpacing(8)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(self.status, 1)
        actions.addWidget(self.save_btn)
        card_layout.addLayout(actions)

        content = QtWidgets.QWidget()
        content.setObjectName("ScrollContent")
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(28, 28, 28, 28)
        content_layout.addWidget(card)
        content_layout.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("TransparentScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        self._login_mode_changed(self.login_tabs.currentIndex())
        for widget in (self.base_url, self.user_agent, self.session_token, self.cf_clearance, self.language):
            widget.textChanged.connect(self.auth_changed.emit)
        for widget in (self.sensorsdata, self.extra_cookies):
            widget.textChanged.connect(self.auth_changed.emit)

    def _login_mode_changed(self, index: int) -> None:
        # The long Cookie form must not push the password/OAuth actions off screen.
        page = self.login_tabs.widget(index)
        page.ensurePolished()
        page.layout().invalidate()
        width = max(1, self.login_tabs.contentsRect().width() - 4)
        page_height = max(page.minimumSizeHint().height(), page.layout().totalHeightForWidth(width),
                          page.sizeHint().height())
        height = page_height + self.login_tabs.tabBar().sizeHint().height() + 8
        self.login_tabs.setFixedHeight(height)
        self.set_busy(self._busy, self.status.text())

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        for widget in (self.save_btn, self.login_tabs, self.base_url, self.user_agent, self.back_btn):
            widget.setDisabled(busy)
        oauth = self.login_tabs.currentIndex() == 1
        label = "用默认浏览器打开 OAuth 授权" if oauth else "登录并保存会话"
        self.save_btn.setText("登录中..." if busy else label)
        if oauth and not self._oauth_available:
            self.save_btn.setDisabled(True)
        self.oauth_refresh_btn.setEnabled(self._oauth_available and not busy)
        self.oauth_copy_btn.setEnabled(bool(self.oauth_link.text()) and not busy)
        self.oauth_submit_btn.setEnabled(bool(self.oauth_link.text()) and not busy)
        self.status.setText(message)

    def set_oauth_authorization(self, url: str, message: str, available: bool) -> None:
        self._oauth_available = available
        self.oauth_link.setText(url)
        self.oauth_status.setText(message)
        self._login_mode_changed(self.login_tabs.currentIndex())

    def set_can_go_back(self, enabled: bool) -> None:
        self.back_btn.setVisible(enabled)

    def paste_request(self) -> None:
        clipboard = QtWidgets.QApplication.clipboard()
        self.request_input.setPlainText(clipboard.text())
        self.parse_request()

    def parse_request(self) -> None:
        parsed = parse_request_text(self.request_input.toPlainText())
        cfg = apply_parsed_request(self.to_config(), parsed)
        self.load_config(cfg)
        self.parse_result.setText(describe_parsed(parsed))

    def _save(self) -> None:
        cfg = self.to_config()
        mode = self.login_tabs.currentIndex()
        if mode == 0:
            self.password_requested.emit(cfg, self.login_username.text(),
                                         self.login_password.text(), self.login_2fa.text())
        elif mode == 1:
            self.oauth_requested.emit(cfg)
        else:
            self.save_requested.emit(cfg)

    def to_config(self) -> AgentConfig:
        cfg = replace(self.current_config)
        cfg.base_url = self.base_url.text().strip()
        cfg.user_agent = self.user_agent.text().strip()
        cfg.session_token = self.session_token.text().strip()
        cfg.cf_clearance = self.cf_clearance.text().strip()
        cfg.language = self.language.text().strip() or "zh-CN"
        cfg.sensorsdata2015jssdkcross = self.sensorsdata.toPlainText().strip()
        cfg.extra_cookies = self.extra_cookies.toPlainText().strip()
        return cfg

    def load_config(self, config: AgentConfig) -> None:
        self.current_config = config
        self.base_url.setText(config.base_url)
        self.user_agent.setText(config.user_agent)
        self.session_token.setText(config.session_token)
        self.cf_clearance.setText(config.cf_clearance)
        self.language.setText(config.language)
        self.sensorsdata.setPlainText(config.sensorsdata2015jssdkcross)
        self.extra_cookies.setPlainText(config.extra_cookies)

    def clear_login_secrets(self) -> None:
        self.login_password.clear()
        self.login_2fa.clear()
        self.request_input.clear()
        self.oauth_result.clear()


class ServerListPage(QtWidgets.QWidget):
    refresh_requested = QtCore.pyqtSignal()
    edit_credentials_requested = QtCore.pyqtSignal()
    mcp_requested = QtCore.pyqtSignal()
    server_selected = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.clients: list[dict[str, Any]] = []
        self.status_by_uuid: dict[str, dict[str, Any]] = {}
        self.active_proxies: dict[str, dict[str, Any]] = {}

        title = QtWidgets.QLabel("选择服务器")
        title.setObjectName("PageTitle")
        self.subtitle = QtWidgets.QLabel("正在登录并获取服务器列表...")
        self.subtitle.setObjectName("Muted")
        self.refresh_btn = soft_button("刷新")
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        self.edit_btn = soft_button("凭据")
        self.edit_btn.clicked.connect(self.edit_credentials_requested.emit)
        self.mcp_btn = soft_button("MCP")
        self.mcp_btn.clicked.connect(self.mcp_requested.emit)

        self.table = ServerTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["状态", "名称", "系统", "CPU", "内存", "流量", "IP"])
        header_view = self.table.horizontalHeader()
        header_view.setStretchLastSection(False)
        header_view.setMinimumSectionSize(56)
        header_view.setSectionsMovable(False)
        header_view.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        header_view.sectionDoubleClicked.connect(self._resize_column_to_contents)
        if hasattr(header_view, "sectionHandleDoubleClicked"):
            header_view.sectionHandleDoubleClicked.connect(self._resize_column_to_contents)
        self.table.setColumnWidth(0, 92)
        self.table.setColumnWidth(1, 170)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 180)
        self.table.setColumnWidth(4, 110)
        self.table.setColumnWidth(5, 180)
        self.table.setColumnWidth(6, 160)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.table.doubleClicked.connect(self._select_current)

        self.open_btn = primary_button("进入 SSH 反代")
        self.open_btn.clicked.connect(self._select_current)

        header = QtWidgets.QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.mcp_btn)
        header.addWidget(self.edit_btn)
        header.addWidget(self.refresh_btn)

        card = card_frame()
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(18, 18, 18, 18)
        card_layout.addWidget(self.table)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(14)
        layout.addLayout(header)
        layout.addWidget(self.subtitle)
        layout.addWidget(card, 1)
        layout.addWidget(self.open_btn, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

    def set_loading(self, text: str) -> None:
        self.subtitle.setText(text)
        self.refresh_btn.setDisabled(True)

    def set_clients(self, clients: list[dict[str, Any]], status_by_uuid: dict[str, dict[str, Any]]) -> None:
        self.refresh_btn.setDisabled(False)
        self.clients = clients
        self.status_by_uuid = status_by_uuid
        self.subtitle.setText(f"已加载 {len(clients)} 台服务器")
        self.table.setRowCount(len(clients))
        status_width = self.table.fontMetrics().horizontalAdvance("状态") + 38
        for row, client in enumerate(clients):
            uuid = str(client.get("uuid", ""))
            status = status_by_uuid.get(uuid, {})
            online = bool(status.get("online"))
            proxy = self.active_proxies.get(uuid)
            status_text = f"代理中({proxy.get('port')})" if proxy else ("在线" if online else "离线")
            status_width = max(status_width, self.table.fontMetrics().horizontalAdvance(status_text) + 38)
            values = [
                status_text,
                str(client.get("name") or "-"),
                f"{client.get('os') or '-'} / {client.get('arch') or '-'}",
                str(client.get("cpu_name") or "-"),
                fmt_bytes(client.get("mem_total")),
                traffic_text(client, status),
                ip_text(client),
            ]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if col == 0:
                    if proxy:
                        item.setForeground(QtGui.QColor("#e6b422"))
                    else:
                        item.setForeground(QtGui.QColor("#57d68d" if online else "#ff7d7d"))
                item.setToolTip(
                    f"{client.get('name') or '-'}\nUUID: {uuid}\nIP: {ip_text(client)}"
                )
                self.table.setItem(row, col, item)
            self.table.setRowHeight(row, 42)
        self.table.setColumnWidth(0, max(76, min(150, status_width)))

    def set_active_proxies(self, active_proxies: dict[str, dict[str, Any]]) -> None:
        self.active_proxies = active_proxies
        if self.clients:
            self.set_clients(self.clients, self.status_by_uuid)

    def show_error(self, message: str) -> None:
        self.refresh_btn.setDisabled(False)
        self.subtitle.setText(message)

    def _select_current(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.clients):
            return
        self.server_selected.emit(self.clients[row])

    def _resize_column_to_contents(self, column: int) -> None:
        self.table.resizeColumnToContents(column)
        self.table.setColumnWidth(column, max(76, self.table.columnWidth(column) + 14))


class ProxyPage(QtWidgets.QWidget):
    back_requested = QtCore.pyqtSignal()
    start_requested = QtCore.pyqtSignal(object)
    stop_requested = QtCore.pyqtSignal()
    restart_requested = QtCore.pyqtSignal(object)
    refresh_status_requested = QtCore.pyqtSignal()
    clear_log_requested = QtCore.pyqtSignal()
    export_log_requested = QtCore.pyqtSignal(str)
    log_level_changed = QtCore.pyqtSignal(str)
    runtime_list_requested = QtCore.pyqtSignal()
    settings_changed = QtCore.pyqtSignal()

    def __init__(self, config: AgentConfig):
        super().__init__()
        self.client: dict[str, Any] | None = None
        self.log_lines: list[str] = []

        self.back_btn = soft_button("返回")
        self.back_btn.clicked.connect(self.back_requested.emit)
        self.title = QtWidgets.QLabel("SSH 反代")
        self.title.setObjectName("PageTitle")
        self.state_label = ClickableLabel("未启动")
        self.state_label.setObjectName("StatusPill")
        self.state_label.clicked.connect(self.runtime_list_requested.emit)

        self.cpu_bar = metric_bar()
        self.ram_bar = metric_bar()
        self.disk_bar = metric_bar()
        self.traffic_label = QtWidgets.QLabel("-")
        self.detail_label = QtWidgets.QLabel("-")
        self.detail_label.setObjectName("Muted")

        self.log_view = AppPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(mono_font(10))
        self.log_view.setMaximumBlockCount(LOG_VIEW_MAX_BLOCKS)
        self.export_btn = soft_button("导出 TXT")
        self.export_btn.clicked.connect(self.export_log)
        self.clear_log_btn = soft_button("清空日志")
        self.clear_log_btn.clicked.connect(self.clear_log_requested.emit)

        self.port = AppSpinBox()
        self.port.setObjectName("StepperSpinBox")
        self.port.setRange(1, 65535)
        self.port.setValue(config.ssh_port)
        self.username = line_edit(config.ssh_username)
        self.password = line_edit(config.ssh_password)
        self.password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.password_toggle = soft_button("显示")
        self.password_toggle.setCheckable(True)
        self.password_toggle.clicked.connect(self.toggle_password_visible)
        self.public_access = AppCheckBox("允许公网访问")
        self.public_access.setChecked(False)
        self.two_fa_code = line_edit(config.terminal_2fa_code)
        self.cols = AppSpinBox()
        self.cols.setRange(20, 400)
        self.cols.setValue(config.terminal_cols)
        self.rows = AppSpinBox()
        self.rows.setRange(8, 120)
        self.rows.setValue(config.terminal_rows)
        self.sticky = AppSpinBox()
        self.sticky.setObjectName("StepperSpinBox")
        self.sticky.setRange(0, 10080)
        self.sticky.setSuffix(" 分钟")
        self.sticky.setSpecialValueText("关闭粘性重连")
        self.sticky.setValue(config.ssh_sticky_minutes)
        self.log_level_buttons = self._create_log_level_buttons()

        self._current_proxy_running = False
        self.action_btn = primary_button("启动")
        self.action_btn.clicked.connect(self._handle_proxy_action)
        self.restart_btn = warn_button("重启端口")
        self.restart_btn.clicked.connect(lambda: self.restart_requested.emit(self.to_config_patch()))
        self.restart_btn.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.restart_btn.setMinimumWidth(0)
        self.restart_btn.setMaximumWidth(0)
        self.restart_btn.hide()
        self._restart_visible = False
        self._restart_anim = QtCore.QPropertyAnimation(self.restart_btn, b"maximumWidth", self)
        self._restart_anim.setDuration(220)
        self._restart_anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self._wire_setting_changes()

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.back_btn)
        top.addWidget(self.title)
        top.addWidget(self.state_label)
        top.addStretch(1)
        refresh_btn = soft_button("刷新状态")
        refresh_btn.clicked.connect(self.refresh_status_requested.emit)
        top.addWidget(refresh_btn)

        metrics = QtWidgets.QGridLayout()
        metrics.setHorizontalSpacing(16)
        metrics.setVerticalSpacing(10)
        metrics.addWidget(metric_card("CPU", self.cpu_bar), 0, 0)
        metrics.addWidget(metric_card("内存", self.ram_bar), 0, 1)
        metrics.addWidget(metric_card("磁盘", self.disk_bar), 0, 2)
        traffic_card = card_frame()
        traffic_layout = QtWidgets.QVBoxLayout(traffic_card)
        traffic_layout.addWidget(QtWidgets.QLabel("流量"))
        traffic_layout.addWidget(self.traffic_label)
        traffic_layout.addWidget(self.detail_label)
        metrics.addWidget(traffic_card, 0, 3)

        log_card = card_frame()
        log_layout = QtWidgets.QVBoxLayout(log_card)
        log_head = QtWidgets.QHBoxLayout()
        log_title = QtWidgets.QLabel("实时指令流")
        log_title.setObjectName("SectionTitle")
        log_head.addWidget(log_title)
        log_head.addStretch(1)
        log_head.addWidget(self.clear_log_btn)
        log_head.addWidget(self.export_btn)
        log_layout.addLayout(log_head)
        log_layout.addWidget(self.log_view, 1)

        settings_card = card_frame()
        settings_layout = QtWidgets.QFormLayout(settings_card)
        settings_layout.setContentsMargins(18, 18, 18, 18)
        settings_layout.setVerticalSpacing(12)
        settings_layout.addRow("端口", self.port)
        settings_layout.addRow("用户名", self.username)
        password_box = QtWidgets.QHBoxLayout()
        password_box.setSpacing(8)
        password_box.addWidget(self.password, 1)
        password_box.addWidget(self.password_toggle)
        settings_layout.addRow("密码", password_box)
        settings_layout.addRow("2FA 验证码", self.two_fa_code)
        size_box = QtWidgets.QHBoxLayout()
        size_box.addWidget(self.cols)
        size_box.addWidget(QtWidgets.QLabel("列"))
        size_box.addWidget(self.rows)
        size_box.addWidget(QtWidgets.QLabel("行"))
        settings_layout.addRow("终端尺寸", size_box)
        settings_layout.addRow("粘性重连", self.sticky)
        settings_layout.addRow("", self.public_access)
        settings_layout.addRow("", self.log_level_buttons)
        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(8)
        self._action_row = actions
        actions.addWidget(self.action_btn, 1)
        actions.addWidget(self.restart_btn, 1)
        settings_layout.addRow("", actions)

        lower = QtWidgets.QHBoxLayout()
        lower.addWidget(log_card, 1)
        lower.addWidget(settings_card)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(14)
        layout.addLayout(top)
        layout.addLayout(metrics)
        layout.addLayout(lower, 1)

    def _create_log_level_buttons(self) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setObjectName("Segmented")
        layout = QtWidgets.QHBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        label = QtWidgets.QLabel("日志等级")
        label.setObjectName("SegmentLabel")
        layout.addWidget(label)
        self._log_button_group = QtWidgets.QButtonGroup(self)
        self._log_button_group.setExclusive(True)
        for text, value in (("详细", "detail"), ("精简", "simple"), ("关闭", "off")):
            button = QtWidgets.QPushButton(text)
            button.setObjectName("SegmentButton")
            button.setCheckable(True)
            button.setProperty("log_level", value)
            button.clicked.connect(lambda checked=False, level=value: self.log_level_changed.emit(level))
            self._log_button_group.addButton(button)
            layout.addWidget(button)
            if value == "detail":
                button.setChecked(True)
        return frame

    def set_client(self, client: dict[str, Any], config: AgentConfig) -> None:
        self.client = client
        self.title.setText(str(client.get("name") or client.get("uuid") or "SSH 反代"))
        blockers = [
            QtCore.QSignalBlocker(self.port),
            QtCore.QSignalBlocker(self.username),
            QtCore.QSignalBlocker(self.password),
            QtCore.QSignalBlocker(self.public_access),
            QtCore.QSignalBlocker(self.two_fa_code),
            QtCore.QSignalBlocker(self.cols),
            QtCore.QSignalBlocker(self.rows),
            QtCore.QSignalBlocker(self.sticky),
        ]
        self.port.setValue(config.ssh_port)
        self.username.setText(config.ssh_username)
        self.password.setText(config.ssh_password)
        self.public_access.setChecked(config.public_access)
        self.two_fa_code.setText(config.terminal_2fa_code)
        self.cols.setValue(config.terminal_cols)
        self.rows.setValue(config.terminal_rows)
        self.sticky.setValue(config.ssh_sticky_minutes)
        del blockers
        self.log_lines.clear()
        self.log_view.clear()
        self.set_restart_visible(False, animated=False)

    def _wire_setting_changes(self) -> None:
        self.port.valueChanged.connect(self.settings_changed.emit)
        self.username.textChanged.connect(self.settings_changed.emit)
        self.password.textChanged.connect(self.settings_changed.emit)
        self.public_access.toggled.connect(self.settings_changed.emit)
        self.two_fa_code.textChanged.connect(self.settings_changed.emit)
        self.cols.valueChanged.connect(self.settings_changed.emit)
        self.rows.valueChanged.connect(self.settings_changed.emit)
        self.sticky.valueChanged.connect(self.settings_changed.emit)

    def toggle_password_visible(self) -> None:
        visible = self.password_toggle.isChecked()
        self.password.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal
            if visible
            else QtWidgets.QLineEdit.EchoMode.Password
        )
        self.password_toggle.setText("隐藏" if visible else "显示")

    def update_metrics(self, status_by_uuid: dict[str, dict[str, Any]]) -> None:
        if not self.client:
            return
        uuid = str(self.client.get("uuid", ""))
        status = status_by_uuid.get(uuid, {})
        cpu = float_or_zero(status.get("cpu"))
        ram_used = float_or_zero(status.get("ram"))
        disk_used = float_or_zero(status.get("disk"))
        mem_total = float_or_zero(self.client.get("mem_total"))
        disk_total = float_or_zero(self.client.get("disk_total"))
        self.cpu_bar.setValue(clamp_percent(cpu))
        self.cpu_bar.setFormat(f"{cpu:.1f}%")
        ram_percent = ram_used / mem_total * 100 if mem_total else 0
        disk_percent = disk_used / disk_total * 100 if disk_total else 0
        self.ram_bar.setValue(clamp_percent(ram_percent))
        self.ram_bar.setFormat(f"{ram_percent:.1f}%")
        self.disk_bar.setValue(clamp_percent(disk_percent))
        self.disk_bar.setFormat(f"{disk_percent:.1f}%")
        self.traffic_label.setText(traffic_text(self.client, status))
        online = bool(status.get("online"))
        self.detail_label.setText(
            f"{'在线' if online else '离线'} / 上传 {fmt_bytes(status.get('net_out'))}/s / 下载 {fmt_bytes(status.get('net_in'))}/s"
        )

    def set_proxy_state(self, text: str, current_running: bool, any_running: bool) -> None:
        self._current_proxy_running = current_running
        self.state_label.setText(text)
        self.state_label.setEnabled(any_running)
        self.state_label.setToolTip("点击查看正在运行的 SSH 代理" if any_running else "")
        self.action_btn.setText("停止" if current_running else "启动")
        self.action_btn.setObjectName("Danger" if current_running else "Primary")
        self.action_btn.style().unpolish(self.action_btn)
        self.action_btn.style().polish(self.action_btn)
        self.action_btn.update()
        if not current_running:
            self.set_restart_visible(False)

    def _handle_proxy_action(self) -> None:
        if self._current_proxy_running:
            self.stop_requested.emit()
        else:
            self.start_requested.emit(self.to_config_patch())

    def set_restart_visible(self, visible: bool, animated: bool = True) -> None:
        if visible == self._restart_visible and self.restart_btn.isVisible() == visible:
            return
        self._restart_visible = visible
        self._restart_anim.stop()
        target = self.restart_target_width() if visible else 0
        if visible:
            self.restart_btn.show()
        if not animated:
            self.restart_btn.setMaximumWidth(target)
            self.restart_btn.setVisible(visible)
            return
        self._restart_anim.setStartValue(self.restart_btn.maximumWidth())
        self._restart_anim.setEndValue(target)
        try:
            self._restart_anim.finished.disconnect()
        except TypeError:
            pass
        if not visible:
            self._restart_anim.finished.connect(self.restart_btn.hide)
        self._restart_anim.start()

    def restart_target_width(self) -> int:
        row_width = self._action_row.parentWidget().width() if self._action_row.parentWidget() else 0
        if row_width <= 0:
            row_width = self.action_btn.width() * 2 + self._action_row.spacing()
        return max(96, int((row_width - self._action_row.spacing()) / 2))

    def append_log(self, full_line: str) -> None:
        self.append_log_batch([full_line])

    def append_log_batch(self, lines: list[str]) -> None:
        if not lines:
            return
        self.log_lines.extend(lines)
        if len(self.log_lines) > LOG_VIEW_MAX_BLOCKS:
            del self.log_lines[: len(self.log_lines) - LOG_VIEW_MAX_BLOCKS]
        self.log_view.appendPlainText("\n".join(lines))
        self.log_view.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def set_log_lines(self, lines: list[str]) -> None:
        self.log_lines = list(lines[-LOG_VIEW_MAX_BLOCKS:])
        self.log_view.setPlainText("\n".join(self.log_lines))
        self.log_view.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def set_log_level(self, level: str) -> None:
        level = level if level in {"detail", "simple", "off"} else "detail"
        for button in self._log_button_group.buttons():
            if button.property("log_level") == level:
                blocker = QtCore.QSignalBlocker(button)
                button.setChecked(True)
                del blocker
                break

    def current_log_level(self) -> str:
        checked = self._log_button_group.checkedButton()
        if checked:
            return str(checked.property("log_level") or "detail")
        return "detail"

    def export_log(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出指令流",
            self.default_log_filename(),
            "Text Files (*.txt);;All Files (*)",
        )
        if not path:
            return
        self.export_log_requested.emit(path)

    def default_log_filename(self) -> str:
        name = str((self.client or {}).get("name") or (self.client or {}).get("uuid") or "terminal-stream")
        return f"{safe_filename(name)}-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"

    def to_config_patch(self) -> dict[str, Any]:
        return {
            "ssh_port": int(self.port.value()),
            "ssh_username": self.username.text().strip() or "root",
            "ssh_password": self.password.text(),
            "public_access": self.public_access.isChecked(),
            "terminal_2fa_code": self.two_fa_code.text().strip(),
            "terminal_cols": int(self.cols.value()),
            "terminal_rows": int(self.rows.value()),
            "ssh_sticky_minutes": int(self.sticky.value()),
        }


class PortConflictDialog(QtWidgets.QDialog):
    def __init__(self, server_name: str, port: int, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumWidth(460)

        card = card_frame()
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        title = QtWidgets.QLabel("端口正在使用")
        title.setObjectName("SectionTitle")
        message = QtWidgets.QLabel(
            f"端口 {port} 正在反代「{server_name}」。\n"
            f"需要更改端口号以继续，或关闭「{server_name}」的反代以继续。"
        )
        message.setWordWrap(True)
        message.setObjectName("Muted")

        actions = QtWidgets.QHBoxLayout()
        actions.addStretch(1)
        cancel_btn = soft_button("更改端口")
        close_btn = danger_button(f"关闭 {server_name} 并继续")
        cancel_btn.clicked.connect(self.reject)
        close_btn.clicked.connect(self.accept)
        actions.addWidget(cancel_btn)
        actions.addWidget(close_btn)

        layout.addWidget(title)
        layout.addWidget(message)
        layout.addLayout(actions)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(card)


class RunningProxiesDialog(QtWidgets.QDialog):
    stop_requested = QtCore.pyqtSignal(str)

    def __init__(self, summaries: dict[str, dict[str, Any]], parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumWidth(560)

        card = card_frame()
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("正在运行的 SSH 代理")
        title.setObjectName("SectionTitle")
        close_btn = soft_button("关闭")
        close_btn.clicked.connect(self.reject)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_btn)
        layout.addLayout(header)

        for uuid, summary in summaries.items():
            row = surface_frame()
            row_layout = QtWidgets.QGridLayout(row)
            row_layout.setContentsMargins(14, 12, 14, 12)
            row_layout.setHorizontalSpacing(12)
            row_layout.setVerticalSpacing(6)
            name = QtWidgets.QLabel(str(summary.get("name") or uuid))
            name.setObjectName("SectionTitle")
            endpoint = QtWidgets.QLabel(
                f"{summary.get('bind_host')}:{summary.get('port')} / {summary.get('username')}"
            )
            endpoint.setObjectName("Muted")
            password = QtWidgets.QLabel(f"密码: {summary.get('password')}")
            password.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            stop_btn = danger_button("停止")
            stop_btn.clicked.connect(lambda checked=False, item_uuid=uuid: self.stop_item(item_uuid))
            row_layout.addWidget(name, 0, 0)
            row_layout.addWidget(endpoint, 1, 0)
            row_layout.addWidget(password, 2, 0)
            row_layout.addWidget(stop_btn, 0, 1, 3, 1)
            layout.addWidget(row)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(card)

    def stop_item(self, uuid: str) -> None:
        self.stop_requested.emit(uuid)
        self.accept()


class MainWindow(FramelessWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setMinimumSize(980, 640)
        self.resize(1180, 760)
        self.signals = AppSignals()
        self._closed = False
        self._login_busy = False
        self._authenticated = False
        self._checking_history = False
        self._oauth_submission_generation: int | None = None
        self._oauth_request_id = 0
        self._oauth_starting: int | None = None
        self.oauth_callback = OAuthCallbackServer(self.signals.oauth_session.emit)
        config_exists = CONFIG_PATH.exists()
        self.config = AgentConfig.load() if config_exists else AgentConfig()
        self.ensure_local_config()
        self.proxy_servers: dict[str, ProxyRuntime] = {}
        self.log_root = prepare_log_root()
        self.proxy_logs: dict[str, ProxyLogStore] = {}
        self.pending_log_lines: dict[str, list[str]] = {}
        self.proxy_log_levels: dict[str, str] = {}
        self.draft_configs: dict[str, AgentConfig] = {}
        self.selected_client: dict[str, Any] | None = None
        self.status_by_uuid: dict[str, dict[str, Any]] = {}

        from mcp_server import McpRuntime
        from mcp_ui import McpPage

        self.mcp_runtime = McpRuntime(self.config, self.log_root / "mcp")
        self.stack = QtWidgets.QStackedWidget()
        self.credentials_page = CredentialsPage(self.config)
        self.server_page = ServerListPage()
        self.proxy_page = ProxyPage(self.config)
        self.mcp_page = McpPage(self.mcp_runtime, self.config, self.save_mcp_config)
        self.stack.addWidget(self.credentials_page)
        self.stack.addWidget(self.server_page)
        self.stack.addWidget(self.proxy_page)
        self.stack.addWidget(self.mcp_page)

        self.title_bar = TitleBar(self)
        shell = QtWidgets.QFrame()
        shell.setObjectName("AppShell")
        shell_layout = QtWidgets.QVBoxLayout(shell)
        shell_layout.setContentsMargins(1, 1, 1, 1)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self.title_bar)
        shell_layout.addWidget(self.stack, 1)

        root = QtWidgets.QWidget()
        root.setObjectName("Root")
        root_layout = QtWidgets.QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.addWidget(shell)
        self.setCentralWidget(root)
        self.title_bar.theme_requested.connect(self.cycle_theme_mode)
        self.set_theme_mode(self.config.app_theme, persist=False)

        self.status_timer = QtCore.QTimer(self)
        self.status_timer.setInterval(3000)
        self.status_timer.timeout.connect(self.refresh_status)
        self.log_flush_timer = QtCore.QTimer(self)
        self.log_flush_timer.setInterval(100)
        self.log_flush_timer.timeout.connect(self.flush_visible_logs)
        self.oauth_timer = QtCore.QTimer(self)
        self.oauth_timer.setInterval(1000)
        self.oauth_timer.timeout.connect(self.refresh_oauth_link)
        self.oauth_timer.start()

        self._wire()
        if config_exists and self.config.has_login_credentials():
            self._checking_history = True
            self.stack.setCurrentWidget(self.server_page)
            self.server_page.set_loading(f"检测到 {CONFIG_PATH.name}，正在自动登录...")
            self._sync_oauth_callback()
            self.try_login(self.config, save=False)
        else:
            self.stack.setCurrentWidget(self.credentials_page)
            self._sync_oauth_callback()

    def ensure_local_config(self) -> None:
        try:
            ensure_config_host_key(self.config)
        except Exception:
            pass
        if not CONFIG_PATH.exists():
            self.config.save()

    def _wire(self) -> None:
        self.credentials_page.save_requested.connect(lambda cfg: self.try_login(cfg, save=True))
        self.credentials_page.password_requested.connect(
            lambda cfg, username, password, code: self.try_login(
                cfg, save=True, credentials=(username, password, code)))
        self.credentials_page.oauth_requested.connect(self.start_oauth_login)
        self.credentials_page.oauth_prepare_requested.connect(
            lambda config: self.start_oauth_login(config, open_browser=False, refresh=True))
        self.credentials_page.oauth_copy_requested.connect(self.copy_oauth_link)
        self.credentials_page.oauth_callback_requested.connect(self.submit_oauth_callback)
        self.credentials_page.auth_changed.connect(self._sync_oauth_callback)
        self.credentials_page.back_requested.connect(self.back_to_servers_from_credentials)
        self.server_page.refresh_requested.connect(self.refresh_clients)
        self.server_page.edit_credentials_requested.connect(self.show_credentials)
        self.server_page.server_selected.connect(self.open_proxy_page)
        self.server_page.mcp_requested.connect(self.open_mcp_page)
        self.mcp_page.back_requested.connect(self.back_to_servers)
        self.mcp_page.refresh_requested.connect(self.refresh_clients)
        self.proxy_page.back_requested.connect(self.back_to_servers)
        self.proxy_page.start_requested.connect(self.start_proxy)
        self.proxy_page.stop_requested.connect(self.stop_proxy)
        self.proxy_page.restart_requested.connect(self.start_proxy)
        self.proxy_page.refresh_status_requested.connect(self.refresh_status)
        self.proxy_page.clear_log_requested.connect(self.clear_current_log)
        self.proxy_page.export_log_requested.connect(self.export_current_log)
        self.proxy_page.log_level_changed.connect(self.set_current_log_level)
        self.proxy_page.runtime_list_requested.connect(self.show_running_proxies)
        self.proxy_page.settings_changed.connect(self.on_proxy_settings_changed)

        self.signals.login_success.connect(self.on_login_success)
        self.signals.login_failed.connect(self.on_login_failed)
        self.signals.oauth_session.connect(self.on_oauth_session)
        self.signals.oauth_failed.connect(self.on_oauth_failed)
        self.signals.oauth_link_ready.connect(self.on_oauth_link_ready)
        self.signals.oauth_link_failed.connect(self.on_oauth_link_failed)
        self.signals.clients_loaded.connect(self.on_clients_loaded)
        self.signals.clients_failed.connect(self.server_page.show_error)
        self.signals.status_loaded.connect(self.on_status_loaded)
        self.signals.ssh_log.connect(self.on_ssh_log)
        self.signals.ssh_state.connect(self.on_ssh_state)

    def cycle_theme_mode(self) -> None:
        app = QtWidgets.QApplication.instance()
        system_theme = _effective_theme(app, "system") if app is not None else "light"
        order = theme_cycle_order(system_theme)
        current = self.config.app_theme if self.config.app_theme in order else "system"
        next_mode = order[(order.index(current) + 1) % len(order)]
        self.set_theme_mode(next_mode, persist=True)

    def set_theme_mode(self, mode: str, persist: bool) -> None:
        mode = mode if mode in {"system", "dark", "light"} else "system"
        self.config.app_theme = mode
        self.title_bar.set_theme_mode(mode)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            apply_style(app, mode)
        if persist:
            self.config.save()

    def start_oauth_login(self, config: AgentConfig, *, open_browser: bool = True,
                          refresh: bool = False) -> None:
        if (self._closed or self._login_busy or self._authenticated or self._checking_history
                or self._oauth_starting is not None or self._oauth_submission_generation is not None):
            return
        try:
            checked_config(config)
            self._sync_oauth_callback()
            if not self.oauth_callback.url:
                raise PanelLoginError("本机回调监听未启动，请检查登录状态。")
            url, _ = self.oauth_callback.authorization
            if url and not refresh:
                if open_browser:
                    self.open_oauth_url(url)
                return
        except Exception as exc:
            self.credentials_page.status.setText(login_error_message(exc))
            return
        self._oauth_request_id += 1
        request_id = self._oauth_request_id
        self._oauth_starting = request_id
        generation = self.oauth_callback.generation
        self.credentials_page.oauth_result.clear()
        self.credentials_page.set_oauth_authorization("", "正在解析面板的 OAuth 授权跳转...", True)
        self.credentials_page.set_busy(True, "正在获取授权链接...")

        def task() -> None:
            try:
                url = self.oauth_callback.begin()
                if not self._closed:
                    self.signals.oauth_link_ready.emit(
                        {"url": url, "generation": generation, "open_browser": open_browser}, request_id)
            except Exception as exc:
                if not self._closed:
                    self.signals.oauth_link_failed.emit(login_error_message(exc), request_id)

        threading.Thread(target=task, name="oauth-authorize", daemon=True).start()

    def on_oauth_link_ready(self, result: dict, request_id: int) -> None:
        if request_id != self._oauth_starting:
            return
        self._oauth_starting = None
        if self._closed or self._login_busy or self._authenticated or self._checking_history:
            return
        self.credentials_page.set_busy(False)
        self._sync_oauth_callback()
        if (result["generation"] != self.oauth_callback.generation
                or result["url"] != self.oauth_callback.authorization[0]):
            return
        if result["open_browser"]:
            self.open_oauth_url(result["url"])

    def on_oauth_link_failed(self, message: str, request_id: int) -> None:
        if request_id != self._oauth_starting:
            return
        self._oauth_starting = None
        if self._closed or self._login_busy or self._authenticated or self._checking_history:
            return
        self.credentials_page.set_busy(False, message)
        self._sync_oauth_callback()

    def open_oauth_url(self, url: str) -> None:
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            self.credentials_page.status.setText("无法打开默认浏览器，请复制授权链接到浏览器中打开。")

    def copy_oauth_link(self) -> None:
        url, _ = self.oauth_callback.authorization
        if not url or url != self.credentials_page.oauth_link.text():
            self._sync_oauth_callback()
            self.credentials_page.status.setText("授权链接已失效，请重新生成。")
            return
        QtWidgets.QApplication.clipboard().setText(url)

    def refresh_oauth_link(self) -> None:
        if (self._closed or self._login_busy or self._authenticated or self._checking_history
                or self._oauth_starting is not None or self._oauth_submission_generation is not None):
            return
        if self.credentials_page.oauth_link.text() != self.oauth_callback.authorization[0]:
            self._sync_oauth_callback()

    def _sync_oauth_callback(self) -> None:
        if self._closed or self._authenticated or self._checking_history:
            if self.oauth_callback.url:
                self.oauth_callback.stop()
            message = "正在验证历史 Cookie，回调监听已关闭。" if self._checking_history else "已登录，回调监听已关闭。"
            self.credentials_page.set_oauth_authorization("", message, False)
            return
        try:
            generation = self.oauth_callback.generation
            self.oauth_callback.configure(self.credentials_page.to_config())
            self.oauth_callback.start()
            if generation != self.oauth_callback.generation:
                self.credentials_page.oauth_result.clear()
            url, provider = self.oauth_callback.authorization
            providers = {"github": "GitHub", "generic": "通用 OAuth / OIDC", "qq": "QQ 聚合登录"}
            message = (f"已解析 {providers.get(provider, 'OAuth')} 授权链接，五分钟内有效。授权后粘贴完整面板回调链接。"
                       if url else "本机回调监听已开启。点击生成授权链接，或用默认浏览器直接开始授权。")
            self.credentials_page.set_oauth_authorization(url, message, True)
        except OSError:
            self.credentials_page.set_oauth_authorization("", "无法启动本机回调监听，请检查本机网络或稍后重试。", False)

    def submit_oauth_callback(self, callback_url: str) -> None:
        if (self._closed or self._login_busy or self._oauth_starting is not None
                or self._oauth_submission_generation is not None
                or self._authenticated):
            return
        self.credentials_page.set_busy(True, "正在转发授权回调...")
        generation = self.oauth_callback.generation
        self._oauth_submission_generation = generation

        def task() -> None:
            try:
                self.oauth_callback.submit(callback_url)
            except Exception as exc:
                if not self._closed:
                    self.signals.oauth_failed.emit(login_error_message(exc), generation)

        threading.Thread(target=task, name="oauth-exchange", daemon=True).start()

    def on_oauth_session(self, config: AgentConfig, generation: int) -> None:
        if (self._closed or self._authenticated or self._checking_history or self._login_busy
                or generation != self.oauth_callback.generation):
            return
        self._oauth_submission_generation = None
        self.credentials_page.oauth_result.clear()
        self.try_login(config, save=True)

    def on_oauth_failed(self, message: str, generation: int) -> None:
        if generation != self._oauth_submission_generation:
            return
        self._oauth_submission_generation = None
        if self._closed or self._authenticated or self._login_busy:
            return
        if generation != self.oauth_callback.generation:
            self.credentials_page.set_busy(False)
            self._sync_oauth_callback()
            return
        self.credentials_page.set_busy(False, message)
        self._sync_oauth_callback()

    def try_login(self, config: AgentConfig, save: bool, *,
                  credentials: tuple[str, str, str] | None = None) -> None:
        if self._closed or self._login_busy:
            return
        self._oauth_starting = None
        self._oauth_submission_generation = None
        self._login_busy = True
        self.oauth_callback.set_busy(True)
        self.credentials_page.set_busy(True, "正在登录...")
        self.server_page.set_loading("正在登录并获取服务器列表...")

        def task() -> None:
            client = None
            try:
                candidate = checked_config(config)
                if credentials is not None:
                    candidate = login_with_password(candidate, *credentials)
                client = KomariClient(candidate)
                if client.get_me().get("logged_in") is not True:
                    raise PanelLoginError("面板尚未确认登录会话，请重新登录。")
                clients = client.validate()
                try:
                    status = normalize_latest_status(client.get_latest_status())
                except Exception:
                    status = {}
                if not self._closed:
                    self.signals.login_success.emit(candidate, {"clients": clients, "status": status, "save": save})
            except Exception as exc:
                if not self._closed:
                    self.signals.login_failed.emit(login_error_message(exc))
            finally:
                if client is not None:
                    client.session.close()

        threading.Thread(target=task, daemon=True).start()

    def on_login_success(self, config: AgentConfig, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        config.mcp_auto_open = self.config.mcp_auto_open
        if payload.get("save"):
            try:
                config.save()
            except OSError:
                self.on_login_failed("面板验证成功，但无法保存配置文件，请检查程序目录的写入权限。")
                return
        self._login_busy = False
        self._checking_history = False
        self._authenticated = True
        self.oauth_callback.stop()
        self.config = config
        self.mcp_runtime.update_config(config)
        self.mcp_page.load_config(config)
        self.set_theme_mode(self.config.app_theme, persist=False)
        self.credentials_page.set_busy(False, "登录成功")
        self.credentials_page.load_config(config)
        self.credentials_page.clear_login_secrets()
        self._sync_oauth_callback()
        self.stack.setCurrentWidget(self.server_page)
        self.on_clients_loaded(payload["clients"], payload["status"])

    def on_login_failed(self, message: str) -> None:
        if self._closed:
            return
        self._login_busy = False
        self._checking_history = False
        self.oauth_callback.set_busy(False)
        self.credentials_page.set_busy(False, f"登录失败: {message}")
        self.server_page.show_error(f"登录失败: {message}")
        self.stack.setCurrentWidget(self.credentials_page)
        self._sync_oauth_callback()

    def refresh_clients(self) -> None:
        self.server_page.set_loading("正在刷新服务器列表...")

        def task() -> None:
            try:
                client = KomariClient(self.config)
                clients = client.get_clients()
                try:
                    status = normalize_latest_status(client.get_latest_status())
                except Exception:
                    status = {}
                self.signals.clients_loaded.emit(clients, status)
            except Exception as exc:
                self.signals.clients_failed.emit(f"刷新失败: {exc}")

        threading.Thread(target=task, daemon=True).start()

    def on_clients_loaded(self, clients: list[dict[str, Any]], status: dict[str, dict[str, Any]]) -> None:
        self.status_by_uuid = status
        self.mcp_runtime.manager.update_catalog(self.config, clients, status)
        self.server_page.active_proxies = self.active_proxy_summaries()
        self.server_page.set_clients(clients, status)

    def save_mcp_config(self, config: AgentConfig) -> None:
        config.save()
        self.config = config
        self.credentials_page.current_config = config

    def open_mcp_page(self) -> None:
        self.save_current_proxy_draft()
        self.status_timer.stop()
        self.mcp_page.load_config(self.config)
        self.mcp_page.refresh()
        self.stack.setCurrentWidget(self.mcp_page)

    def show_credentials(self) -> None:
        self.save_current_proxy_draft()
        self.credentials_page.load_config(self.config)
        self.credentials_page.set_can_go_back(self._authenticated)
        self.stack.setCurrentWidget(self.credentials_page)

    def back_to_servers_from_credentials(self) -> None:
        if self._authenticated:
            self.stack.setCurrentWidget(self.server_page)

    def open_proxy_page(self, client: dict[str, Any]) -> None:
        self.save_current_proxy_draft()
        self.selected_client = client
        uuid = str(client.get("uuid", ""))
        page_config = self.config_for_server(uuid)
        self.proxy_page.set_client(client, page_config)
        self.proxy_page.set_log_level(self.proxy_log_levels.get(uuid, "detail"))
        self.pending_log_lines.pop(uuid, None)
        self.proxy_page.set_log_lines(self.filtered_logs(uuid))
        self.proxy_page.update_metrics(self.status_by_uuid)
        self.update_proxy_page_state()
        self.stack.setCurrentWidget(self.proxy_page)
        self.status_timer.start()
        self.refresh_status()

    def back_to_servers(self) -> None:
        self.save_current_proxy_draft()
        self.status_timer.stop()
        self.stack.setCurrentWidget(self.server_page)
        self.refresh_clients()

    def refresh_status(self) -> None:
        def task() -> None:
            try:
                client = KomariClient(self.config)
                status = normalize_latest_status(client.get_latest_status())
                self.signals.status_loaded.emit(status)
            except Exception:
                pass

        threading.Thread(target=task, daemon=True).start()

    def on_status_loaded(self, status: dict[str, dict[str, Any]]) -> None:
        self.status_by_uuid = status
        self.mcp_runtime.manager.update_catalog(self.config, self.server_page.clients, status)
        if self.stack.currentWidget() is self.proxy_page:
            self.proxy_page.update_metrics(status)

    def start_proxy(self, patch: dict[str, Any]) -> None:
        if not self.selected_client:
            return
        uuid = str(self.selected_client.get("uuid"))
        name = str(self.selected_client.get("name") or uuid)
        port = int(patch.get("ssh_port") or self.config.ssh_port)
        conflict_uuid = self.find_port_conflict(port, uuid)
        if conflict_uuid:
            conflict = self.proxy_servers[conflict_uuid]
            if not self.confirm_port_conflict(port, conflict_uuid, conflict):
                return
            self.stop_proxy(conflict_uuid)
        if uuid in self.proxy_servers:
            self.stop_proxy(uuid)

        proxy_config = replace(self.config)
        for key, value in patch.items():
            setattr(proxy_config, key, value)
        self.persist_ssh_password_default(proxy_config.ssh_password)
        self.draft_configs[uuid] = replace(proxy_config)
        self.proxy_log_store(uuid)
        self.proxy_log_levels.setdefault(uuid, self.proxy_page.current_log_level())
        self.record_proxy_log(uuid, f"准备启动: {name} / {uuid}")
        try:
            server = LocalSSHProxyServer(
                proxy_config,
                uuid,
                name,
                lambda line, item_uuid=uuid: self.signals.ssh_log.emit(item_uuid, line),
                lambda text, running, item_uuid=uuid: self.signals.ssh_state.emit(
                    item_uuid, text, running
                ),
            )
            self.proxy_servers[uuid] = ProxyRuntime(
                server=server,
                client=dict(self.selected_client),
                config=proxy_config,
                running=True,
                state_text="运行中",
            )
            server.start()
            self.update_all_proxy_indicators()
        except Exception as exc:
            self.proxy_servers.pop(uuid, None)
            self.record_proxy_log(uuid, f"启动失败: {exc}")
            self.update_all_proxy_indicators()

    def stop_proxy(self, uuid: str | None = None) -> None:
        uuid = uuid or self.current_uuid()
        if not uuid:
            return
        runtime = self.proxy_servers.pop(uuid, None)
        if runtime:
            runtime.server.stop()
            runtime.running = False
        self.update_all_proxy_indicators()

    def persist_ssh_password_default(self, password: str) -> None:
        if not password or password == self.config.ssh_password:
            return
        self.config.ssh_password = password
        self.config.save()

    def current_uuid(self) -> str | None:
        if not self.selected_client:
            return None
        return str(self.selected_client.get("uuid") or "")

    def config_for_server(self, uuid: str) -> AgentConfig:
        if uuid in self.draft_configs:
            return self.draft_configs[uuid]
        runtime = self.proxy_servers.get(uuid)
        if runtime:
            return runtime.config
        cfg = replace(self.config)
        cfg.public_access = False
        return cfg

    def config_from_patch(self, patch: dict[str, Any]) -> AgentConfig:
        cfg = replace(self.config)
        for key, value in patch.items():
            setattr(cfg, key, value)
        return cfg

    def save_current_proxy_draft(self) -> None:
        uuid = self.current_uuid()
        if not uuid or self.stack.currentWidget() is not self.proxy_page:
            return
        self.draft_configs[uuid] = self.config_from_patch(self.proxy_page.to_config_patch())

    def on_proxy_settings_changed(self) -> None:
        self.save_current_proxy_draft()
        self.update_proxy_page_state()

    def current_draft_differs_from_runtime(self) -> bool:
        uuid = self.current_uuid()
        if not uuid:
            return False
        runtime = self.proxy_servers.get(uuid)
        if not runtime or not runtime.running:
            return False
        draft = self.config_from_patch(self.proxy_page.to_config_patch())
        return proxy_settings_tuple(draft) != proxy_settings_tuple(runtime.config)

    def active_proxy_summaries(self) -> dict[str, dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        for uuid, runtime in self.proxy_servers.items():
            if not runtime.running and not runtime.server.is_running:
                continue
            cfg = runtime.config
            summaries[uuid] = {
                "uuid": uuid,
                "name": runtime.client.get("name") or runtime.server.server_name or uuid,
                "port": int(cfg.ssh_port),
                "username": cfg.ssh_username,
                "password": cfg.ssh_password,
                "bind_host": "0.0.0.0" if cfg.public_access else "127.0.0.1",
            }
        return summaries

    def running_proxy_count(self) -> int:
        return len(self.active_proxy_summaries())

    def find_port_conflict(self, port: int, current_uuid: str) -> str | None:
        for uuid, runtime in self.proxy_servers.items():
            if uuid == current_uuid:
                continue
            if int(runtime.config.ssh_port) == int(port) and (runtime.running or runtime.server.is_running):
                return uuid
        return None

    def confirm_port_conflict(self, port: int, conflict_uuid: str, runtime: ProxyRuntime) -> bool:
        name = runtime.client.get("name") or runtime.server.server_name or conflict_uuid
        dialog = PortConflictDialog(str(name), port, self)
        return dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted

    def update_all_proxy_indicators(self) -> None:
        self.server_page.set_active_proxies(self.active_proxy_summaries())
        self.update_proxy_page_state()

    def update_proxy_page_state(self) -> None:
        selected_uuid = self.current_uuid()
        count = self.running_proxy_count()
        current_running = bool(
            selected_uuid
            and selected_uuid in self.proxy_servers
            and self.proxy_servers[selected_uuid].running
        )
        if count > 1:
            text = f"{count}台SSH代理运行中"
        elif current_running:
            text = "运行中"
        elif count == 1:
            text = "1台SSH代理运行中"
        else:
            text = "未启动"
        self.proxy_page.set_proxy_state(text, current_running, count > 0)
        self.proxy_page.set_restart_visible(
            current_running and self.current_draft_differs_from_runtime()
        )

    def record_proxy_log(self, uuid: str, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        full = f"[{stamp}] {line}"
        self.proxy_log_store(uuid).append(full)
        if self.current_uuid() == uuid and self.log_line_visible(full, self.proxy_log_levels.get(uuid, "detail")):
            self.pending_log_lines.setdefault(uuid, []).append(full)
            if not self.log_flush_timer.isActive():
                self.log_flush_timer.start()

    def on_ssh_log(self, uuid: str, line: str) -> None:
        self.record_proxy_log(uuid, line)

    def flush_visible_logs(self) -> None:
        uuid = self.current_uuid()
        if not uuid:
            self.pending_log_lines.clear()
            self.log_flush_timer.stop()
            return
        lines = self.pending_log_lines.pop(uuid, [])
        if lines:
            self.proxy_page.append_log_batch(lines)
        if not any(self.pending_log_lines.values()):
            self.log_flush_timer.stop()

    def on_ssh_state(self, uuid: str, text: str, running: bool) -> None:
        runtime = self.proxy_servers.get(uuid)
        if runtime:
            runtime.running = running
            runtime.state_text = text
            if not running and text.startswith("启动失败"):
                self.proxy_servers.pop(uuid, None)
        self.update_all_proxy_indicators()

    def filtered_logs(self, uuid: str) -> list[str]:
        level = self.proxy_log_levels.get(uuid, "detail")
        return self.proxy_log_store(uuid).filtered_tail(lambda line: self.log_line_visible(line, level))

    def proxy_log_store(self, uuid: str) -> ProxyLogStore:
        store = self.proxy_logs.get(uuid)
        if store is None:
            store = ProxyLogStore(uuid, self.log_root)
            self.proxy_logs[uuid] = store
        return store

    def export_current_log(self, path: str) -> None:
        uuid = self.current_uuid()
        if not uuid:
            return
        level = self.proxy_log_levels.get(uuid, "detail")
        self.proxy_log_store(uuid).export_to(
            path,
            lambda line: self.log_line_visible(line, level),
        )

    def log_line_visible(self, line: str, level: str) -> bool:
        if level == "off":
            return False
        if level == "detail":
            return True
        noisy_tokens = (
            "IN  SSH -> WS",
            "OUT WS  -> SSH",
            "窗口大小同步",
        )
        return not any(token in line for token in noisy_tokens)

    def clear_current_log(self) -> None:
        uuid = self.current_uuid()
        if not uuid:
            return
        self.proxy_log_store(uuid).clear()
        self.pending_log_lines.pop(uuid, None)
        self.proxy_page.set_log_lines([])

    def set_current_log_level(self, level: str) -> None:
        uuid = self.current_uuid()
        if not uuid:
            return
        self.proxy_log_levels[uuid] = level
        self.pending_log_lines.pop(uuid, None)
        self.proxy_page.set_log_lines(self.filtered_logs(uuid))

    def show_running_proxies(self) -> None:
        summaries = self.active_proxy_summaries()
        if not summaries:
            return
        dialog = RunningProxiesDialog(summaries, self)
        dialog.stop_requested.connect(self.stop_proxy)
        dialog.exec()
        self.update_all_proxy_indicators()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._closed = True
        self.oauth_timer.stop()
        self.oauth_callback.stop()
        self.credentials_page.clear_login_secrets()
        self.status_timer.stop()
        self.log_flush_timer.stop()
        self.mcp_page.shutdown()
        self.mcp_runtime.close()
        for uuid in list(self.proxy_servers):
            self.stop_proxy(uuid)
        for store in self.proxy_logs.values():
            store.close()
        shutil.rmtree(self.log_root, ignore_errors=True)
        event.accept()


def apply_style(app: QtWidgets.QApplication, mode: str = "system") -> None:
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
    theme = _effective_theme(app, mode)
    app.setProperty("effective_theme", theme)
    app.setStyleSheet(_theme_stylesheet(theme))


def _effective_theme(app: QtWidgets.QApplication, mode: str) -> str:
    if mode in {"dark", "light"}:
        return mode
    try:
        if app.styleHints().colorScheme() == QtCore.Qt.ColorScheme.Dark:
            return "dark"
        if app.styleHints().colorScheme() == QtCore.Qt.ColorScheme.Light:
            return "light"
    except Exception:
        pass
    return "dark" if app.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128 else "light"


def theme_cycle_order(system_theme: str) -> list[str]:
    return ["system", "light", "dark"] if system_theme == "dark" else ["system", "dark", "light"]


def _theme_stylesheet(theme: str) -> str:
    dark = theme == "dark"
    c = {
        "bg": "#101010" if dark else "#f5f7f6",
        "title": "#141414" if dark else "#eef3f1",
        "card": "#181818" if dark else "#ffffff",
        "surface": "#151515" if dark else "#f7faf8",
        "field": "#0b0b0b" if dark else "#ffffff",
        "text": "#e8e8e8" if dark else "#14201b",
        "strong": "#ffffff" if dark else "#0e1814",
        "muted": "#a6a6a6" if dark else "#63736b",
        "border": "#2c2c2c" if dark else "#d8e1dd",
        "border2": "#3a3a3a" if dark else "#c9d5cf",
        "button": "#242424" if dark else "#edf3f0",
        "button_hover": "#303030" if dark else "#e1ebe6",
        "disabled": "#191919" if dark else "#eef2f0",
        "disabled_text": "#777777" if dark else "#9aa7a1",
        "accent": "#4fd1a5" if dark else "#18a579",
        "accent_hover": "#6ee7bc" if dark else "#0f8b66",
        "accent_text": "#061310" if dark else "#ffffff",
        "danger": "#6e252a" if dark else "#d94d59",
        "danger_border": "#a33b43" if dark else "#c43f4c",
        "danger_text": "#ffe5e7" if dark else "#ffffff",
        "table_alt": "#161616" if dark else "#f1f5f3",
        "table_header": "#202020" if dark else "#eaf0ed",
        "selected": "#343434" if dark else "#cfeee3",
        "selected_text": "#ffffff" if dark else "#10231d",
        "scroll": "#3a3a3a" if dark else "#c8d3ce",
        "scroll_hover": "#505050" if dark else "#aebdb6",
        "pill_bg": "#222222" if dark else "#dff4ec",
        "pill_text": "#e0e0e0" if dark else "#0d7758",
        "warn_bg": "#7a5700" if dark else "#c88900",
        "warn_hover": "#946a00" if dark else "#b77d00",
        "warn_border": "#a87800" if dark else "#b77d00",
        "warn_text": "#ffffff",
    }
    return f"""
        QWidget {{
            background: transparent;
            color: {c['text']};
            selection-background-color: {c['accent']};
            selection-color: {c['accent_text']};
        }}
        QWidget#Root {{
            background: transparent;
        }}
        QFrame#AppShell {{
            background: {c['bg']};
            border: 1px solid {c['border']};
            border-radius: 10px;
        }}
        QFrame#TitleBar {{
            background: {c['title']};
            border-top-left-radius: 10px;
            border-top-right-radius: 10px;
            border-bottom: 1px solid {c['border']};
        }}
        QLabel#WindowTitle {{
            color: {c['strong']};
            font-size: 13px;
            font-weight: 650;
            background: transparent;
        }}
        QPushButton#WindowButton, QPushButton#CloseButton {{
            min-width: 34px;
            max-width: 34px;
            min-height: 34px;
            max-height: 34px;
            padding: 0;
            border-radius: 0;
            border: 0;
            background: transparent;
        }}
        QStackedWidget {{
            background: {c['bg']};
            border: 0;
        }}
        QDialog {{
            background: {c['bg']};
        }}
        QTabWidget::pane {{
            border: 1px solid {c['border']};
            background: {c['bg']};
        }}
        QTabBar::tab {{
            background: {c['card']};
            color: {c['muted']};
            padding: 9px 16px;
            border: 1px solid {c['border']};
            border-bottom: 2px solid {c['border']};
        }}
        QTabBar::tab:selected {{
            color: {c['text']};
            border-bottom-color: {c['accent']};
        }}
        QScrollArea#TransparentScroll, QWidget#ScrollContent {{
            background: transparent;
            border: 0;
        }}
        QScrollBar:vertical {{
            background: transparent;
            width: 10px;
            margin: 4px 2px 4px 2px;
        }}
        QScrollBar::handle:vertical {{
            background: {c['scroll']};
            border-radius: 4px;
            min-height: 34px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {c['scroll_hover']};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar:horizontal {{
            height: 0;
            background: transparent;
        }}
        QFrame#Card {{
            background: {c['card']};
            border: 1px solid {c['border']};
            border-radius: 8px;
        }}
        QFrame#Surface {{
            background: {c['surface']};
            border: 1px solid {c['border']};
            border-radius: 8px;
        }}
        QFrame#Card QLabel, QFrame#Surface QLabel {{
            background: transparent;
        }}
        QLabel#HeroTitle {{
            font-size: 34px;
            font-weight: 700;
            color: {c['strong']};
            background: transparent;
        }}
        QLabel#PageTitle {{
            font-size: 26px;
            font-weight: 700;
            color: {c['strong']};
            background: transparent;
        }}
        QLabel#SectionTitle {{
            font-size: 16px;
            font-weight: 650;
            color: {c['strong']};
            background: transparent;
        }}
        QLabel#Muted {{
            color: {c['muted']};
            background: transparent;
        }}
        QLabel#StatusPill {{
            background: {c['pill_bg']};
            color: {c['pill_text']};
            border: 1px solid {c['border2']};
            border-radius: 8px;
            padding: 6px 10px;
        }}
        QLabel#StatusPill:disabled {{
            color: {c['disabled_text']};
            background: {c['disabled']};
        }}
        QMenu {{
            background: {c['card']};
            color: {c['text']};
            border: 1px solid {c['border2']};
            border-radius: 8px;
            padding: 6px;
        }}
        QMenu::item {{
            padding: 7px 26px 7px 22px;
            border-radius: 6px;
        }}
        QMenu::item:selected {{
            background: {c['button_hover']};
            color: {c['strong']};
        }}
        QMenu::item:disabled {{
            color: {c['disabled_text']};
        }}
        QMenu::separator {{
            height: 1px;
            background: {c['border']};
            margin: 5px 8px;
        }}
        QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
            background: {c['field']};
            border: 1px solid {c['border2']};
            border-radius: 8px;
            padding: 9px 10px;
            color: {c['text']};
        }}
        QSpinBox#StepperSpinBox {{
            padding: 9px 30px 9px 10px;
        }}
        QSpinBox#NoSpinButtons {{
            padding: 9px 10px;
        }}
        QSpinBox::up-button, QSpinBox::down-button {{
            width: 0;
            border: 0;
        }}
        QSpinBox::up-arrow, QSpinBox::down-arrow {{
            image: none;
            width: 0;
            height: 0;
        }}
        QComboBox {{
            padding-right: 36px;
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 29px;
            border: 0;
            background: transparent;
        }}
        QComboBox::down-arrow {{
            image: none;
            width: 0;
            height: 0;
        }}
        QComboBox QAbstractItemView {{
            background: {c['card']};
            color: {c['text']};
            border: 1px solid {c['border2']};
            border-radius: 8px;
            padding: 4px;
            selection-background-color: {c['selected']};
            selection-color: {c['selected_text']};
            outline: 0;
        }}
        QComboBox QAbstractItemView::item {{
            padding: 8px 12px;
            min-height: 22px;
            border-radius: 5px;
        }}
        QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
            color: {c['disabled_text']};
            background: {c['disabled']};
        }}
        QTextEdit QWidget, QPlainTextEdit QWidget {{
            background: {c['field']};
        }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
            border: 1px solid {c['accent']};
        }}
        QPushButton {{
            border-radius: 8px;
            padding: 9px 15px;
            background: {c['button']};
            border: 1px solid {c['border2']};
            color: {c['text']};
            font-weight: 600;
        }}
        QPushButton:hover {{
            background: {c['button_hover']};
        }}
        QPushButton:disabled {{
            color: {c['disabled_text']};
            background: {c['disabled']};
        }}
        QPushButton#Primary {{
            background: {c['accent']};
            color: {c['accent_text']};
            border: 1px solid {c['accent']};
        }}
        QPushButton#Primary:hover {{
            background: {c['accent_hover']};
        }}
        QPushButton#Danger {{
            background: {c['danger']};
            color: {c['danger_text']};
            border: 1px solid {c['danger_border']};
        }}
        QPushButton#Warning {{
            background: {c['warn_bg']};
            color: {c['warn_text']};
            border: 1px solid {c['warn_border']};
        }}
        QPushButton#Warning:hover {{
            background: {c['warn_hover']};
            border: 1px solid {c['warn_border']};
        }}
        QPushButton#Primary:disabled, QPushButton#Danger:disabled, QPushButton#Warning:disabled {{
            color: {c['disabled_text']};
            background: {c['disabled']};
            border-color: {c['border']};
        }}
        QFrame#Segmented {{
            background: {c['surface']};
            border: 1px solid {c['border2']};
            border-radius: 10px;
        }}
        QLabel#SegmentLabel {{
            color: {c['muted']};
            font-weight: 600;
            background: transparent;
            padding: 0 4px;
        }}
        QPushButton#SegmentButton {{
            border: 0;
            border-radius: 7px;
            padding: 7px 10px;
            background: transparent;
            color: {c['muted']};
            min-width: 42px;
        }}
        QPushButton#SegmentButton:hover {{
            background: {c['button_hover']};
            color: {c['text']};
        }}
        QPushButton#SegmentButton:checked {{
            background: {c['accent']};
            color: {c['accent_text']};
        }}
        QTableWidget {{
            background: {c['field']};
            border: 0;
            gridline-color: {c['border']};
            alternate-background-color: {c['table_alt']};
            outline: 0;
        }}
        QTableWidget QScrollBar:horizontal {{
            background: transparent;
            height: 10px;
            margin: 2px 4px 2px 4px;
        }}
        QTableWidget QScrollBar::handle:horizontal {{
            background: {c['scroll']};
            border-radius: 4px;
            min-width: 34px;
        }}
        QTableWidget QScrollBar::handle:horizontal:hover {{
            background: {c['scroll_hover']};
        }}
        QTableWidget QScrollBar::add-line:horizontal,
        QTableWidget QScrollBar::sub-line:horizontal {{
            width: 0;
        }}
        QHeaderView::section {{
            background: {c['table_header']};
            color: {c['text']};
            border: 0;
            border-bottom: 1px solid {c['border']};
            padding: 8px;
        }}
        QTableWidget::item {{
            padding: 8px;
            border-bottom: 1px solid {c['border']};
        }}
        QTableWidget::item:selected {{
            background: {c['selected']};
            color: {c['selected_text']};
        }}
        QProgressBar {{
            background: {c['field']};
            border: 1px solid {c['border2']};
            border-radius: 7px;
            height: 18px;
            text-align: center;
        }}
        QProgressBar::chunk {{
            background: {c['accent']};
            border-radius: 6px;
        }}
        QCheckBox {{
            spacing: 8px;
            background: transparent;
        }}
        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
            border-radius: 5px;
            border: 1px solid {c['border2']};
            background: {c['field']};
        }}
        QCheckBox::indicator:hover {{
            border: 1px solid {c['accent']};
        }}
        QCheckBox::indicator:checked {{
            background: {c['accent']};
            border: 1px solid {c['accent']};
        }}
    """


def card_frame() -> QtWidgets.QFrame:
    frame = QtWidgets.QFrame()
    frame.setObjectName("Card")
    return frame


def surface_frame() -> QtWidgets.QFrame:
    frame = QtWidgets.QFrame()
    frame.setObjectName("Surface")
    return frame


def line_edit(value: str = "") -> QtWidgets.QLineEdit:
    widget = AppLineEdit()
    widget.setText(value)
    return widget


def text_edit(value: str = "", height: int = 90) -> QtWidgets.QTextEdit:
    widget = AppTextEdit()
    widget.setPlainText(value)
    widget.setFixedHeight(height)
    widget.setAcceptRichText(False)
    return widget


def primary_button(text: str) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text)
    button.setObjectName("Primary")
    return button


def danger_button(text: str) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text)
    button.setObjectName("Danger")
    return button


def warn_button(text: str) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text)
    button.setObjectName("Warning")
    return button


def soft_button(text: str) -> QtWidgets.QPushButton:
    return QtWidgets.QPushButton(text)


def metric_bar() -> QtWidgets.QProgressBar:
    bar = QtWidgets.QProgressBar()
    bar.setRange(0, 100)
    bar.setValue(0)
    return bar


def metric_card(title: str, bar: QtWidgets.QProgressBar) -> QtWidgets.QFrame:
    frame = card_frame()
    layout = QtWidgets.QVBoxLayout(frame)
    layout.addWidget(QtWidgets.QLabel(title))
    layout.addWidget(bar)
    return frame


def mono_font(size: int) -> QtGui.QFont:
    font = QtGui.QFont("Cascadia Mono")
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setPointSize(size)
    return font


def safe_filename(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid or ord(ch) < 32 else ch for ch in value)
    cleaned = cleaned.strip(" .")
    return (cleaned or "server")[:80]


def proxy_settings_tuple(config: AgentConfig) -> tuple[Any, ...]:
    return (
        int(config.ssh_port),
        str(config.ssh_username),
        str(config.ssh_password),
        bool(config.public_access),
        str(config.terminal_2fa_code),
        int(config.terminal_cols),
        int(config.terminal_rows),
        int(config.ssh_sticky_minutes),
    )


def fmt_bytes(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while number >= 1024 and idx < len(units) - 1:
        number /= 1024
        idx += 1
    if idx == 0:
        return f"{number:.0f} {units[idx]}"
    return f"{number:.2f} {units[idx]}"


def float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def clamp_percent(value: float) -> int:
    return max(0, min(100, int(round(value))))


def ip_text(client: dict[str, Any]) -> str:
    return " / ".join(str(v) for v in (client.get("ipv4"), client.get("ipv6")) if v) or "-"


def traffic_text(client: dict[str, Any], status: dict[str, Any]) -> str:
    up = status.get("net_total_out") or status.get("net_total_up") or 0
    down = status.get("net_total_in") or status.get("net_total_down") or 0
    limit = float_or_zero(client.get("traffic_limit"))
    text = f"↑ {fmt_bytes(up)}  ↓ {fmt_bytes(down)}"
    if limit:
        text += f" / {fmt_bytes(limit)}"
    return text
