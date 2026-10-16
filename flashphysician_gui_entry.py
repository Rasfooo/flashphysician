#!/usr/bin/env python3
"""PyInstaller entry point for the windowed GUI executable.

Two problems are solved here:

1. The GUI's gui.py uses relative imports (`from .core import ...`), which
   fail when PyInstaller runs a file as a top-level script. Importing the
   package first makes them resolve.
2. A Windows embeddable Python ships without tkinter, so a packaged GUI can
   fail with ModuleNotFoundError on the very first line. Rather than crash
   silently (a windowed exe shows no console), fall back to the CLI with a
   message the user can actually read.
"""

import sys


def main() -> int:
    try:
        from flashphysician.gui import main as gui_main
    except ImportError as exc:
        # Windowed exe: print goes nowhere the user can see it, so also
        # surface the error via a console subprocess if possible.
        msg = (
            "FlashPhysician GUI could not start:\n"
            f"  {exc}\n\n"
            "This usually means tkinter is not available in the bundled "
            "Python. The command-line tool works regardless:"
            "\n    FlashPhysician-cli.exe --help"
        )
        print(msg, file=sys.stderr)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "FlashPhysician", 0x10)
        except Exception:
            pass
        return 70
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
