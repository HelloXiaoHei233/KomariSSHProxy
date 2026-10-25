from __future__ import annotations

import sys


def main() -> int:
    if "--check-mcp" in sys.argv[1:]:
        import argparse

        parser = argparse.ArgumentParser(description="Check the packaged MCP service without logging in.")
        parser.add_argument("--check-mcp", action="store_true")
        parser.add_argument("--check-mcp-report", help="Write the diagnostic result to a UTF-8 file.")
        return check_mcp_installation(parser.parse_args().check_mcp_report)
    from PyQt6 import QtGui, QtWidgets

    from config import resource_path
    from ui import MainWindow, apply_style

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


def check_mcp_installation(report_path: str | None = None) -> int:
    """Exercise the packaged MCP stack without a panel login or saved credentials."""
    from pathlib import Path
    import tempfile

    try:
        # Keep GUI imports inside the diagnostic boundary: a missing Qt DLL
        # must also produce a report in a windowed executable without stderr.
        from PyQt6 import QtGui, QtWidgets
        import requests

        from config import AgentConfig
        from mcp_server import McpRuntime, TOOLS
        from ui import apply_style

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["komari-installation-check"])
        apply_style(app)
        widget = QtWidgets.QWidget()
        widget.resize(32, 32)
        canvas = QtGui.QPixmap(widget.size())
        widget.render(canvas)
        with tempfile.TemporaryDirectory(prefix="komari-mcp-check-") as directory:
            config = AgentConfig(mcp_port=0)
            runtime = McpRuntime(config, Path(directory))
            try:
                runtime.start(config)
                with requests.Session() as client:
                    client.trust_env = False
                    response = client.post(runtime.http.url, json={}, timeout=5)
                    if response.status_code != 401:
                        raise RuntimeError("MCP authentication check failed")
                    response = client.post(runtime.http.url, timeout=5,
                        headers={"Authorization": f"Bearer {config.mcp_password}",
                                 "Accept": "application/json, text/event-stream",
                                 "MCP-Protocol-Version": "2025-11-25"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                    response.raise_for_status()
                    if {tool["name"] for tool in response.json()["result"]["tools"]} != set(TOOLS):
                        raise RuntimeError("MCP tools check failed")
            finally:
                runtime.close()
        message = "MCP installation check passed"
        exit_code = 0
    except Exception as exc:
        # This check uses temporary, synthetic credentials only. Preserve the
        # exception chain so missing bundled dependencies are visible in CI.
        errors = []
        while exc is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
            exc = exc.__cause__ or exc.__context__
        message = "MCP installation check failed\n" + "\n".join(errors)
        exit_code = 1
    if report_path:
        Path(report_path).write_text(message + "\n", encoding="utf-8")
    stream = sys.stderr if exit_code else sys.stdout
    if stream is not None:
        print(message, file=stream)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
