# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: build the portable Windows .exe.

    pyinstaller tools/flashphysician.spec --noconfirm

Produces two artefacts in dist/:

    FlashPhysician.exe      -- windowed GUI, double-click to run
    flashphysician-cli.exe  -- console build for scripting and headless use

Cross-building note: PyInstaller cannot produce a real Windows binary from
Linux; it needs to run on Windows (or under Wine). That is why this file ships
with the source and is exercised by the GitHub Actions workflow in
.github/workflows/build-exe.yml, which builds on windows-latest so the
released .exe is a genuine Windows build rather than a cross-compiled guess.
"""

import os
from pathlib import Path

# -- paths ------------------------------------------------------------------
SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent if SPEC_DIR.name == "tools" else SPEC_DIR

block_cipher = None

ASSETS = ROOT / "assets"
datas = []
for pattern in ("*.svg", "*.png", "*.txt"):
    for f in ASSETS.glob(pattern):
        datas.append((str(f), "assets"))

hiddenimports = [
    "flashphysician.core.structures",
    "flashphysician.core.imaging",
    "flashphysician.core.diagnose",
    "flashphysician.core.repair",
    "flashphysician.recovery.files",
    "flashphysician.recovery.surface",
    "flashphysician.backends.base",
    "flashphysician.backends.linux",
    "flashphysician.backends.windows",
    # Tkinter pieces PyInstaller occasionally misses
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
]

# Windows-only modules: excluding them keeps the binary small, but they must
# not be excluded when actually running on Windows.
excludes = [
    "pytest", "setuptools", "pip", "wheel",
    "numpy", "matplotlib", "PIL", "scipy", "pandas",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook", "test", "unittest",
]

# -- shared analysis --------------------------------------------------------
common = dict(
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

a_gui = Analysis([str(ROOT / "flashphysician_gui_entry.py")], **common)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data, cipher=block_cipher)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="FlashPhysician",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,               # windowed: no console flashing up
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ASSETS / "icon.ico") if (ASSETS / "icon.ico").exists()
    else (str(ASSETS / "icon.png") if (ASSETS / "icon.png").exists() else None),
)

coll_gui = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="FlashPhysician",
)

# -- one-file GUI build -----------------------------------------------------
exe_onefile = EXE(
    pyz_gui,
    a_gui.scripts,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    [],
    name="FlashPhysician-portable",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ASSETS / "icon.ico") if (ASSETS / "icon.ico").exists()
    else (str(ASSETS / "icon.png") if (ASSETS / "icon.png").exists() else None),
)

# -- console build for scripting -------------------------------------------
a_cli = Analysis([str(ROOT / "flashphysician_cli_entry.py")], **common)
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="flashphysician-cli",
    debug=False,
    strip=False,
    upx=True,
    console=True,                # console app: usable from cmd.exe / PowerShell
    icon=str(ASSETS / "icon.ico") if (ASSETS / "icon.ico").exists()
    else (str(ASSETS / "icon.png") if (ASSETS / "icon.png").exists() else None),
)

coll_cli = COLLECT(
    exe_cli,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="flashphysician-cli",
)
