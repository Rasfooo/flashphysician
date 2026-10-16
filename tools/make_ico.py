#!/usr/bin/env python3
"""Generate assets/icon.ico for the Windows build -- pure stdlib, no Pillow.

Windows wants a multi-resolution .ico (16/32/48/64/128/256) so Explorer,
the taskbar and the Alt-Tab switcher each get a crisp bitmap. An ICO file is
just a directory of PNG images, so once we can render one PNG at each size we
can assemble the container ourselves.

Rendering uses the same headless-Chrome path as make_logo.py, so the icon is
always regenerated from the SVG source rather than being a stale binary blob
checked into the repository by hand.

    python3 tools/make_ico.py
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = [16, 24, 32, 48, 64, 128, 256]

sys.path.insert(0, str(ROOT / "tools"))


def render_png(svg: Path, size: int) -> bytes | None:
    """Rasterise the SVG at one size and return the PNG bytes."""
    try:
        from make_logo import rasterise_via_chrome
    except ImportError:
        return None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / f"icon_{size}.png"
        if rasterise_via_chrome(svg, out, size, size):
            return out.read_bytes()
    return None


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Width and height from the PNG IHDR chunk."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    w, h = struct.unpack_from(">II", data, 16)
    return w, h


def build_ico(pngs: list[tuple[int, bytes]], out_path: Path) -> None:
    """Assemble an ICO container around the given PNGs.

    ICO layout: a 6-byte header, then one 16-byte directory entry per image,
    then the image payloads. PNG payloads are stored verbatim (allowed since
    Vista) which keeps the file small for the large sizes.
    """
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    payload = b""
    offset = 6 + count * 16
    for size, data in pngs:
        w, h = png_dimensions(data)
        # 0 means 256 in the ICO directory format -- it is a single byte
        entries += struct.pack("<BBBBHHII",
                               w % 256, h % 256, 0, 0, 1, 32,
                               len(data), offset)
        payload += data
        offset += len(data)
    out_path.write_bytes(header + entries + payload)
    print(f"  wrote {out_path.relative_to(ROOT)}  "
          f"({count} sizes: {', '.join(str(s) for s, _ in pngs)}, "
          f"{out_path.stat().st_size:,} bytes)")


def main() -> int:
    svg = ASSETS / "logo.svg"
    if not svg.exists():
        print("  error: assets/logo.svg missing -- run tools/make_logo.py first")
        return 1

    pngs: list[tuple[int, bytes]] = []
    for size in SIZES:
        data = render_png(svg, size)
        if data:
            pngs.append((size, data))
        else:
            print(f"  skipped {size}px (no rasteriser available)")
    if not pngs:
        print("\n  error: could not rasterise any size. A PNG-carrying .ico is")
        print("         required for the Windows build, so install Chrome/Chromium")
        print("         (or rsvg-convert) and re-run.")
        return 1

    build_ico(pngs, ASSETS / "icon.ico")
    if len(pngs) < len(SIZES):
        print("  note: partial icon -- Explorer will scale the sizes present")
    return 0


if __name__ == "__main__":
    sys.exit(main())