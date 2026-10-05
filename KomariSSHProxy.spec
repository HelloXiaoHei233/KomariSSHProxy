# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import copy_metadata


if sys.platform == "win32":
    # DLL discovery searches PATH. Unrelated applications can ship incompatible
    # copies of Windows ICU/CRT DLLs with the same names that Qt imports.
    windows = Path(os.environ["SystemRoot"])
    dll_paths = [
        Path(sys.executable).parent,
        Path(sys.base_prefix),
        Path(sys.base_prefix) / "DLLs",
        windows / "System32",
        windows,
    ]
    os.environ["PATH"] = os.pathsep.join(str(path) for path in dll_paths if path.is_dir())


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/app-icon.ico", "assets")] + copy_metadata("mcp") + copy_metadata("mcp-types"),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Komari SSH Proxy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["assets/app-icon.ico"],
)
