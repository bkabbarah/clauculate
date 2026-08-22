# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec.

accounts.json is deliberately NOT bundled: it is user config and must stay
editable next to the exe.
"""

block_cipher = None

a = Analysis(
    ["run.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pystray picks its backend at import time; PyInstaller cannot see it.
        "pystray._win32",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the binary lean: none of these are used.
        "numpy", "matplotlib", "pandas", "scipy", "PyQt5", "PySide2",
        "IPython", "pytest", "setuptools",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Clauculate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # tray app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
