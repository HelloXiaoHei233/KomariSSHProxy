from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtSvg, QtWidgets


from config import APP_DISPLAY_NAME


CONTEXT_MENU_LABELS = {
    "Undo": "撤销",
    "Redo": "重做",
    "Cut": "剪切",
    "Copy": "复制",
    "Paste": "粘贴",
    "Delete": "删除",
    "Select All": "全选",
}


def show_localized_context_menu(widget: QtWidgets.QWidget, event: QtGui.QContextMenuEvent) -> None:
    menu_factory = getattr(widget, "createStandardContextMenu", None)
    menu = menu_factory() if callable(menu_factory) else None
    if menu is None:
        line_edit_getter = getattr(widget, "lineEdit", None)
        line_edit = line_edit_getter() if callable(line_edit_getter) else None
        menu = line_edit.createStandardContextMenu() if line_edit is not None else QtWidgets.QMenu(widget)
    localize_context_menu(menu)
    menu.exec(event.globalPos())


def localize_context_menu(menu: QtWidgets.QMenu) -> None:
    for action in menu.actions():
        raw = action.text().replace("&", "").split("\t", 1)[0].strip()
        if raw in CONTEXT_MENU_LABELS:
            action.setText(CONTEXT_MENU_LABELS[raw])


class AppLineEdit(QtWidgets.QLineEdit):
    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        show_localized_context_menu(self, event)


class AppTextEdit(QtWidgets.QTextEdit):
    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        show_localized_context_menu(self, event)


class AppPlainTextEdit(QtWidgets.QPlainTextEdit):
    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        show_localized_context_menu(self, event)


class AppNumberSpinBox(QtWidgets.QSpinBox):
    def __init__(self):
        super().__init__()
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.DefaultContextMenu)
        editor = self.lineEdit()
        editor.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        editor.customContextMenuRequested.connect(
            lambda pos, source=editor: self.show_editor_context_menu(source, pos)
        )

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        show_localized_context_menu(self, event)

    def show_editor_context_menu(self, source: QtWidgets.QLineEdit, pos: QtCore.QPoint) -> None:
        menu = source.createStandardContextMenu()
        localize_context_menu(menu)
        menu.exec(source.mapToGlobal(pos))


class AppCheckBox(QtWidgets.QCheckBox):
    def __init__(self, text: str):
        super().__init__(text)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QtCore.QSize:
        metrics = self.fontMetrics()
        return QtCore.QSize(metrics.horizontalAdvance(self.text()) + 34, 30)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        app = QtWidgets.QApplication.instance()
        dark = bool(app and app.property("effective_theme") == "dark")
        field = QtGui.QColor("#0b0b0b" if dark else "#ffffff")
        border = QtGui.QColor("#3a3a3a" if dark else "#c9d5cf")
        accent = QtGui.QColor("#4fd1a5" if dark else "#18a579")
        text = QtGui.QColor("#e8e8e8" if dark else "#14201b")
        if not self.isEnabled():
            accent = QtGui.QColor("#505050" if dark else "#bec9c4")
            text = QtGui.QColor("#777777" if dark else "#95a19b")

        box_size = 18
        box_y = (self.height() - box_size) // 2
        box = QtCore.QRectF(0, box_y, box_size, box_size)
        painter.setPen(QtGui.QPen(accent if self.isChecked() else border, 1.2))
        painter.setBrush(accent if self.isChecked() else field)
        painter.drawRoundedRect(box, 5, 5)

        if self.isChecked():
            pen = QtGui.QPen(QtGui.QColor("#ffffff"), 2.0)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QtGui.QPainterPath()
            path.moveTo(4.5, box_y + 9.5)
            path.lineTo(8.0, box_y + 13.0)
            path.lineTo(14.0, box_y + 5.5)
            painter.drawPath(path)

        painter.setPen(text)
        text_rect = QtCore.QRect(28, 0, self.width() - 28, self.height())
        painter.drawText(text_rect, QtCore.Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.hasFocus() and self.isEnabled():
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.setPen(QtGui.QPen(accent, 1, QtCore.Qt.PenStyle.DotLine))
            painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 5, 5)


class AppSpinBox(AppNumberSpinBox):
    def __init__(self):
        super().__init__()
        self.setObjectName("StepperSpinBox")
        self.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setMouseTracking(True)
        self._pressed_step = 0
        self._repeat_timer = QtCore.QTimer(self)
        self._repeat_timer.timeout.connect(self._repeat_step)

    def _stop_repeat(self) -> None:
        self._repeat_timer.stop()
        self._pressed_step = 0

    def _repeat_step(self) -> None:
        if not self._pressed_step or not self.isEnabled() or self.isReadOnly() or not self.isVisible():
            self._stop_repeat()
            return
        self.stepBy(self._pressed_step)
        self.selectAll()
        self._repeat_timer.setInterval(75)

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        self._stop_repeat()
        super().focusOutEvent(event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        app = QtWidgets.QApplication.instance()
        dark = bool(app and app.property("effective_theme") == "dark")
        button = QtGui.QColor("#202020" if dark else "#eef4f1")
        button_hover = QtGui.QColor("#2b2b2b" if dark else "#e0ebe5")
        border = QtGui.QColor("#3a3a3a" if dark else "#c9d5cf")
        arrow = QtGui.QColor("#d8d8d8" if dark else "#35433d")
        if not self.isEnabled():
            button = QtGui.QColor("#181818" if dark else "#f0f3f1")
            arrow = QtGui.QColor("#666666" if dark else "#a2ada7")
        hover_step = self._step_at(self.mapFromGlobal(QtGui.QCursor.pos())) if self.isEnabled() else 0

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        painter.setClipPath(clip)
        rail = self._rail_rect()
        top = QtCore.QRectF(rail.left(), rail.top(), rail.width(), rail.height() / 2)
        bottom = QtCore.QRectF(
            rail.left(),
            rail.top() + rail.height() / 2,
            rail.width(),
            rail.height() / 2,
        )

        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(button_hover if hover_step == 1 else button)
        painter.drawRoundedRect(top.adjusted(0, 0, 0, 4), 7, 7)
        painter.setBrush(button_hover if hover_step == -1 else button)
        painter.drawRoundedRect(bottom.adjusted(0, -4, 0, 0), 7, 7)

        painter.setPen(QtGui.QPen(border, 1))
        middle_y = rail.top() + rail.height() / 2
        painter.drawLine(QtCore.QPointF(rail.left(), middle_y), QtCore.QPointF(rail.right(), middle_y))
        painter.drawLine(
            QtCore.QPointF(rail.left(), rail.top() + 1),
            QtCore.QPointF(rail.left(), rail.bottom() - 1),
        )

        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(arrow)
        center_x = rail.center().x()
        up_y = top.center().y()
        down_y = bottom.center().y()

        up = QtGui.QPolygonF(
            [
                QtCore.QPointF(center_x - 5, up_y + 3),
                QtCore.QPointF(center_x + 5, up_y + 3),
                QtCore.QPointF(center_x, up_y - 3),
            ]
        )
        down = QtGui.QPolygonF(
            [
                QtCore.QPointF(center_x - 5, down_y - 3),
                QtCore.QPointF(center_x + 5, down_y - 3),
                QtCore.QPointF(center_x, down_y + 3),
            ]
        )
        painter.drawPolygon(up)
        painter.drawPolygon(down)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        step = self._step_at(event.position().toPoint())
        if event.button() == QtCore.Qt.MouseButton.LeftButton and step and not self.isReadOnly():
            self._pressed_step = step
            self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
            self.stepBy(step)
            self.selectAll()
            self._repeat_timer.start(400)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        self._stop_repeat()
        self.update()
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._pressed_step and self._step_at(event.position().toPoint()) != self._pressed_step:
            self._stop_repeat()
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._stop_repeat()
        self.update()
        super().leaveEvent(event)

    def _rail_rect(self) -> QtCore.QRectF:
        return QtCore.QRectF(self.width() - 25, 1, 24, self.height() - 2)

    def _step_at(self, pos: QtCore.QPoint) -> int:
        rail = self._rail_rect()
        if not rail.contains(QtCore.QPointF(pos)):
            return 0
        return 1 if pos.y() < rail.center().y() else -1


class AppComboBox(QtWidgets.QComboBox):
    """The same frame, button rail and arrow colors as AppSpinBox."""

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setView(QtWidgets.QListView())
        self.view().setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        app = QtWidgets.QApplication.instance()
        dark = bool(app and app.property("effective_theme") == "dark")
        background = QtGui.QColor("#202020" if dark else "#eef4f1")
        border = QtGui.QColor("#3a3a3a" if dark else "#c9d5cf")
        arrow = QtGui.QColor("#d8d8d8" if dark else "#35433d")
        if not self.isEnabled():
            background = QtGui.QColor("#181818" if dark else "#f0f3f1")
            arrow = QtGui.QColor("#666666" if dark else "#a2ada7")
        elif self.underMouse() or self.view().isVisible():
            background = QtGui.QColor("#2b2b2b" if dark else "#e0ebe5")
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        painter.setClipPath(clip)
        rail = QtCore.QRectF(self.width() - 30, 1, 29, self.height() - 2)
        painter.fillRect(rail, background)
        painter.setPen(QtGui.QPen(border, 1))
        painter.drawLine(QtCore.QPointF(rail.left(), rail.top()), QtCore.QPointF(rail.left(), rail.bottom()))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(arrow)
        center = rail.center()
        painter.drawPolygon(QtGui.QPolygonF([
            QtCore.QPointF(center.x() - 5, center.y() - 2),
            QtCore.QPointF(center.x() + 5, center.y() - 2),
            QtCore.QPointF(center.x(), center.y() + 4),
        ]))

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self.update()
        super().leaveEvent(event)


SVG_ICONS = {
    "system": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect x="4" y="5" width="16" height="11" rx="1.8" fill="none" '
        'stroke="{color}" stroke-width="1.8"/>'
        '<path d="M9 20h6M12 16v4" fill="none" stroke="{color}" '
        'stroke-width="1.8" stroke-linecap="round"/></svg>'
    ),
    "dark": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M20 14.4A7.6 7.6 0 0 1 9.6 4a8.1 8.1 0 1 0 10.4 10.4z" '
        'fill="none" stroke="{color}" stroke-width="1.8" stroke-linejoin="round"/></svg>'
    ),
    "light": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<circle cx="12" cy="12" r="3.5" fill="none" stroke="{color}" stroke-width="1.8"/>'
        '<path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6'
        'M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6" '
        'fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round"/></svg>'
    ),
    "minimize": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M7 12h10" fill="none" stroke="{color}" stroke-width="1.8" '
        'stroke-linecap="round"/></svg>'
    ),
    "maximize": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect x="7" y="7" width="10" height="10" rx="1" fill="none" '
        'stroke="{color}" stroke-width="1.8"/></svg>'
    ),
    "restore": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M9 7h8v8" fill="none" stroke="{color}" stroke-width="1.8" '
        'stroke-linejoin="round"/>'
        '<rect x="7" y="9" width="8" height="8" rx="1" fill="none" '
        'stroke="{color}" stroke-width="1.8"/></svg>'
    ),
    "close": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M7.5 7.5l9 9M16.5 7.5l-9 9" fill="none" stroke="{color}" '
        'stroke-width="1.8" stroke-linecap="round"/></svg>'
    ),
}


class SvgIconButton(QtWidgets.QPushButton):
    def __init__(self, icon_name: str, *, close: bool = False):
        super().__init__()
        self.icon_name = icon_name
        self.is_close_button = close
        self.setObjectName("CloseButton" if close else "WindowButton")
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(34, 34)

    def set_icon_name(self, icon_name: str) -> None:
        self.icon_name = icon_name
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        dark = self._is_dark()
        hovered = self.underMouse()
        down = self.isDown()

        if self.is_close_button and hovered:
            bg = QtGui.QColor("#c93f4a")
            icon_color = "#ffffff"
        else:
            bg = QtGui.QColor("#2e2e2e" if dark else "#e6eeea")
            if down:
                bg = QtGui.QColor("#3a3a3a" if dark else "#d8e5df")
            elif not hovered:
                bg.setAlpha(0)
            icon_color = "#dddddd" if dark else "#36443e"

        if bg.alpha() > 0:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 8, 8)

        svg = SVG_ICONS[self.icon_name].format(color=icon_color).encode("utf-8")
        renderer = QtSvg.QSvgRenderer(svg)
        icon_rect = QtCore.QRectF(
            (rect.width() - 16) / 2,
            (rect.height() - 16) / 2,
            16,
            16,
        )
        renderer.render(painter, icon_rect)

    @staticmethod
    def _is_dark() -> bool:
        app = QtWidgets.QApplication.instance()
        return bool(app and app.property("effective_theme") == "dark")


class TitleBar(QtWidgets.QFrame):
    theme_requested = QtCore.pyqtSignal()

    def __init__(self, parent: QtWidgets.QWidget | None = None, *, show_theme: bool = True):
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(46)
        self._drag_pos: QtCore.QPoint | None = None

        self.title = QtWidgets.QLabel(APP_DISPLAY_NAME)
        self.title.setObjectName("WindowTitle")
        self.title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self.theme_btn = SvgIconButton("system")
        self.theme_btn.setVisible(show_theme)
        self.min_btn = SvgIconButton("minimize")
        self.max_btn = SvgIconButton("maximize")
        self.close_btn = SvgIconButton("close", close=True)

        self.theme_btn.clicked.connect(self.theme_requested.emit)
        self.min_btn.clicked.connect(lambda: self.window().showMinimized())
        self.max_btn.clicked.connect(self.toggle_maximized)
        self.close_btn.clicked.connect(lambda: self.window().close())

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 8, 0)
        layout.setSpacing(8)
        layout.addWidget(self.title, 1)
        layout.addWidget(self.theme_btn)
        layout.addWidget(self.min_btn)
        layout.addWidget(self.max_btn)
        layout.addWidget(self.close_btn)

    def set_theme_mode(self, mode: str) -> None:
        labels = {"system": "跟随系统", "dark": "暗色模式", "light": "亮色模式"}
        icons = {"system": "system", "dark": "dark", "light": "light"}
        self.theme_btn.set_icon_name(icons.get(mode, "system"))
        self.theme_btn.setToolTip(labels.get(mode, "跟随系统"))

    def toggle_maximized(self) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
            self.max_btn.set_icon_name("maximize")
        else:
            window.showMaximized()
            self.max_btn.set_icon_name("restore")

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton and not self.window().isMaximized():
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        self._drag_pos = None
        event.accept()


class WindowResizeHandle(QtWidgets.QWidget):
    """Delegate edge drags to the OS, with a fallback for other Qt platforms."""

    def __init__(self, window: QtWidgets.QWidget, edges: QtCore.Qt.Edge,
                 cursor: QtCore.Qt.CursorShape):
        super().__init__(window)
        self.edges = edges
        self.setObjectName("WindowResizeHandle")
        self.setCursor(cursor)
        self._drag_origin: QtCore.QPoint | None = None
        self._drag_geometry = QtCore.QRect()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        # Windows layered windows pass mouse events through fully transparent pixels.
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 1))

    def _start_system_resize(self) -> bool:
        handle = self.window().windowHandle()
        return bool(handle and handle.startSystemResize(self.edges))

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        window = self.window()
        if (event.button() != QtCore.Qt.MouseButton.LeftButton
                or window.isMaximized() or window.isFullScreen()):
            return
        self._drag_origin = None
        if not self._start_system_resize():
            self._drag_origin = event.globalPosition().toPoint()
            self._drag_geometry = window.geometry()
        event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._drag_origin is None or not event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            return
        window = self.window()
        delta = event.globalPosition().toPoint() - self._drag_origin
        rect = QtCore.QRect(self._drag_geometry)
        minimum = window.minimumSize().expandedTo(window.minimumSizeHint())
        edge = QtCore.Qt.Edge
        if self.edges & edge.LeftEdge:
            width = max(minimum.width(), min(window.maximumWidth(), rect.width() - delta.x()))
            rect.setLeft(rect.right() - width + 1)
        elif self.edges & edge.RightEdge:
            rect.setWidth(max(minimum.width(), min(window.maximumWidth(), rect.width() + delta.x())))
        if self.edges & edge.TopEdge:
            height = max(minimum.height(), min(window.maximumHeight(), rect.height() - delta.y()))
            rect.setTop(rect.bottom() - height + 1)
        elif self.edges & edge.BottomEdge:
            rect.setHeight(max(minimum.height(), min(window.maximumHeight(), rect.height() + delta.y())))
        window.setGeometry(rect)
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        self._drag_origin = None
        event.accept()


class FramelessWindow(QtWidgets.QMainWindow):
    """Shared border and corner resize areas for the application windows."""

    def __init__(self):
        super().__init__()
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        edge, cursor = QtCore.Qt.Edge, QtCore.Qt.CursorShape
        self.resize_handles = {
            edges: WindowResizeHandle(self, edges, shape)
            for edges, shape in (
                (edge.LeftEdge, cursor.SizeHorCursor),
                (edge.RightEdge, cursor.SizeHorCursor),
                (edge.TopEdge, cursor.SizeVerCursor),
                (edge.BottomEdge, cursor.SizeVerCursor),
                (edge.TopEdge | edge.LeftEdge, cursor.SizeFDiagCursor),
                (edge.TopEdge | edge.RightEdge, cursor.SizeBDiagCursor),
                (edge.BottomEdge | edge.LeftEdge, cursor.SizeBDiagCursor),
                (edge.BottomEdge | edge.RightEdge, cursor.SizeFDiagCursor),
            )
        }

    def _position_resize_handles(self) -> None:
        if not hasattr(self, "resize_handles"):
            return
        edge = QtCore.Qt.Edge
        border, corner = 10, 18
        width, height = self.width(), self.height()
        rectangles = {
            edge.LeftEdge: (0, corner, border, height - 2 * corner),
            edge.RightEdge: (width - border, corner, border, height - 2 * corner),
            edge.TopEdge: (corner, 0, width - 2 * corner, border),
            edge.BottomEdge: (corner, height - border, width - 2 * corner, border),
            edge.TopEdge | edge.LeftEdge: (0, 0, corner, corner),
            edge.TopEdge | edge.RightEdge: (width - corner, 0, corner, corner),
            edge.BottomEdge | edge.LeftEdge: (0, height - corner, corner, corner),
            edge.BottomEdge | edge.RightEdge: (width - corner, height - corner, corner, corner),
        }
        enabled = not self.isMaximized() and not self.isFullScreen()
        for edges, handle in self.resize_handles.items():
            handle.setGeometry(*rectangles[edges])
            handle.setVisible(enabled)
            handle.raise_()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_resize_handles()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self._position_resize_handles()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.WindowStateChange:
            self._position_resize_handles()
            if hasattr(self, "title_bar"):
                self.title_bar.max_btn.set_icon_name("restore" if self.isMaximized() else "maximize")
