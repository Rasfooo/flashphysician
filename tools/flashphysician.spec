# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: build the portable Windows .exe.

    pyinstaller tools/flashphysician.spec --noconfirm

Produces in dist/:

    FlashPhysician-portable.exe   -- one-file windowed GUI (the download)
    FlashPhysician/               -- one-dir windowed GUI (faster start)
    flashphysician-cli.exe        -- one-file console build for scripting

Hard-won notes on AV false positives -- this is why the file looks fussy:

1. **UPX is off, everywhere.** `upx=True` compresses the sections of a PE file,
   and compressed sections are the textbook signature of a packed malware stub.
   PyInstaller's docs recommend it for size, but for a tool that ships to
   strangers on a USB stick the size saving is not worth being quarantined on
   download: an unpacked ~8 MB exe never trips the packer heuristic.
2. **Every exe carries a version resource** (`tools/version_info.txt`). An
   unsigned binary with no metadata has no publisher, no product and no
   copyright -- exactly what a generic dropper looks like, and the reason
   SmartScreen says "unrecognised app" for an otherwise innocent binary.
3. **No shell strings in the payload.** The Windows backend no longer embeds
   `cmd /c ... > file` or `-ExecutionPolicy Bypass`; see
   flashphysician/backends/windows.py. Those two strings alone are enough to
   put a PyInstaller one-file exe on some vendors' block lists.
4. **Deterministic builds.** Building from this spec on a stock
   windows-latest GitHub runner (.github/workflows/build.yml) gives the
   published artefact a traceable provenance and a clean build hash, which is
   what an AV vendor wants before whitelisting a remaining false positive.

Cross-building note: PyInstaller cannot produce a real Windows binary from
Linux; it needs to run on Windows (or under Wine). The GitHub Actions workflow
exists precisely so the released .exe is a genuine Windows build rather than a
cross-compiled guess.
"""

import os
from pathlib import Path

# -- paths ------------------------------------------------------------------
SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent if SPEC_DIR.name == "tools" else SPEC_DIR

block_cipher = None

ASSETS = ROOT / "assets"
VERSION_FILE = ROOT / "tools" / "version_info.txt"

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

ICON = (str(ASSETS / "icon.ico") if (ASSETS / "icon.ico").exists()
        else (str(ASSETS / "icon.png") if (ASSETS / "icon.png").exists() else None))

VERSION = str(VERSION_FILE) if VERSION_FILE.exists() else None

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

# -- one-dir GUI build (fastest startup, directory delivery) ----------------
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="FlashPhysician",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # see module docstring, point 1
    console=False,               # windowed: no console flashing up
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION,             # see module docstring, point 2
)

coll_gui = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FlashPhysician",
)

# -- one-file GUI build (the portable download) -----------------------------
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
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON,
    version=VERSION,
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
    upx=False,
    console=True,                # console app: usable from cmd.exe / PowerShell
    icon=ICON,
    version=VERSION,
)

coll_cli = COLLECT(
    exe_cli,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="flashphysician-cli",
)

# -- one-file console build (what the release actually ships) --------------
exe_cli_onefile = EXE(
    pyz_cli,
    a_cli.scripts,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    [],
    name="flashphysician-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    icon=ICON,
    version=VERSION,
)
