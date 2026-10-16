#!/usr/bin/env python3
"""PyInstaller entry point for the windowed GUI executable.

Problems handled here:

1. The GUI's gui.py uses relative imports (`from .core import ...`), which
   fail when PyInstaller runs a file as a top-level script. Importing the
   package first makes them resolve.
2. A Windows embeddable Python ships without tkinter, so a packaged GUI can
   fail with ModuleNotFoundError on the very first line. Rather than crash
   silently (a windowed exe shows no console), fall back to the CLI with a
   message the user can actually read.
3. A headless environment (SSH, CI, container) has no X11 display. Tk raises
   TclError: couldn't connect to display. Surface that as a clear message
   instead of a traceback.
"""

import sys


def main() -> int:
    try:
        from flashphysician.gui import main as gui_main
    except ImportError as exc:
        msg = (
            "FlashPhysician GUI could not start:\n"
            f"  {exc}\n\n"
            "This usually means tkinter is not available in the bundled "
            "Python. The command-line tool works regardless:\n"
            "    flashphysician --help"
        )
        print(msg, file=sys.stderr)
        _show_popup(msg)
        return 70

    try:
        return gui_main()
    except Exception as exc:
        name = type(exc).__name__
        if "display" in str(exc).lower() or name == "TclError":
            msg = (
                "FlashPhysician GUI could not open a window:\n"
                f"  {name}: {exc}\n\n"
                "This usually means there is no graphical display available "
                "(headless session, SSH, or no X server). "
                "The command-line tool works regardless:\n"
                "    flashphysician --help"
            )
        else:
            msg = (
                "FlashPhysician GUI crashed:\n"
                f"  {name}: {exc}\n\n"
                "Run 'flashphysician --help' in a terminal for the CLI, or "
                "report this error."
            )
        print(msg, file=sys.stderr)
        _show_popup(msg)
        return 70


def _show_popup(msg: str) -> None:
    """Best-effort message box so a windowed exe's stderr is not lost."""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "FlashPhysician", 0x10)
        else:
            import shutil, subprocess
            if shutil.which("zenity"):
                subprocess.run(
                    ["zenity", "--error", "--title", "FlashPhysician",
                     "--width", "520", "--text", msg],
                    timeout=20, check=False)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
