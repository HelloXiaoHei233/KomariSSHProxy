from __future__ import annotations

import sys

from PyQt6 import QtGui, QtWidgets

from config import resource_path
from ui import MainWindow, apply_style


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    icon_path = resource_path("assets/app-icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))
    apply_style(app)
    window = MainWindow()
    if icon_path.exists():
        window.setWindowIcon(QtGui.QIcon(str(icon_path)))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
