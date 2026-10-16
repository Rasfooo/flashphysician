#!/usr/bin/env python3
"""PyInstaller entry point for the console executable.

The package's cli.py uses relative imports (`from .core import ...`), which
works when invoked as `python -m flashphysician` but fails when PyInstaller
runs the file as a top-level script. This wrapper imports the package properly
and delegates, so the packaged exe behaves identically to the module entry.
"""

import sys

from flashphysician.cli import main

if __name__ == "__main__":
    sys.exit(main())
