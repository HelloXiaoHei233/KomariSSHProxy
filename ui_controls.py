from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets


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
