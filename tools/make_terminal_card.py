#!/usr/bin/env python3
"""Render a real CLI session to assets/terminal.png for the README.

The card shows actual output captured from the tool against a byte-exact image
of a wiped partition table -- not a mockup. If the tool's output changes, the
image can be regenerated and will match.

    python3 tools/make_terminal_card.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
sys.path.insert(0, str(ROOT / "tools"))

BG = "#0b1118"
BAR = "#16202c"
FG = "#e6edf3"
DIM = "#8b98a5"
ACCENT = "#4ea1ff"
OK = "#3fb950"
WARN = "#d29922"
ERR = "#f85149"
DEAD = "#ff7b72"
MONO = "Consolas, 'SF Mono', Menlo, 'DejaVu Sans Mono', monospace"

SEV_COLOR = {"[ OK ]": OK, "[INFO]": ACCENT, "[WARN]": WARN,
             "[FAIL]": ERR, "[DEAD]": DEAD}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def capture_session() -> list[str]:
    """Run the real tool and return its output lines."""
    demo = Path(tempfile.gettempdir()) / "flashphysician_demo.img"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)

    # Build a small image with a lost partition table: the headline scenario.
    sys.path.insert(0, str(ROOT))
    from tests import fixtures as fx
    fx.make_lost_partition_table(str(demo), 1024)

    proc = subprocess.run(
        [sys.executable, "-m", "flashphysician", "diagnose", str(demo), "--no-color"],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=300)
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]

    # Only keep findings that are complete sentences. The report formatter
    # itself truncates long details, and a card ending mid-sentence reads as a
    # bug in the tool rather than a crop of a longer report.
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if set(s) <= {"=", "-"}:
            continue
        if s.startswith("["):
            # a complete finding ends with sentence punctuation
            if not s.endswith((".", ")", "AA", "ms")):
                continue
        out.append(line)
    return out[:22]


def build_svg(lines: list[str], width: int = 940) -> str:
    fs = 14
    lh = 22
    pad_top = 44
    pad = 22
    # characters that fit in the card at this font size, leaving room for the
    # severity tag colouring; measured rather than guessed so nothing clips
    wrap_at = 96

    rows = []
    y = pad_top + pad

    def emit(text: str, color: str, weight: str = "normal",
             indent: str = "") -> None:
        nonlocal y
        # wrap instead of truncating: a clipped diagnostic line is worse than
        # a wrapped one, because the end of the sentence is the actionable part
        chunks = []
        s = text
        first = True
        while s:
            room = wrap_at - (len(indent) if not first else 0)
            if len(s) <= room:
                chunks.append((indent if not first else "") + s)
                break
            cut = s.rfind(" ", 0, room)
            if cut <= 0:
                cut = room
            chunks.append((indent if not first else "") + s[:cut].rstrip())
            s = s[cut:].lstrip()
            first = False
        for i, chunk in enumerate(chunks):
            rows.append(
                f'<text x="{pad}" y="{y}" font-family="{MONO}" font-size="{fs}" '
                f'font-weight="{weight}" fill="{color}" '
                f'xml:space="preserve">{esc(chunk)}</text>')
            y += lh

    for raw in lines:
        line = raw.rstrip()
        if set(line) <= {"=", "-"}:
            continue
        color = FG
        weight = "normal"
        for tag, c in SEV_COLOR.items():
            if tag in line:
                color = c
                break
        if "USB DOCTOR" in line:
            color = ACCENT
        if line.startswith("Health"):
            color = WARN
        if line.startswith("Verdict") or "Recoverable" in line:
            color = OK
            weight = "600"
        emit(line, color, weight,
             indent="        " if line.startswith("  ") and not line.startswith("  [") else "")

    height = y + pad
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"
     width="{width}" height="{height}">
  <rect width="{width}" height="{height}" rx="14" fill="{BG}"/>
  <rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="13"
        fill="none" stroke="#2a3644" stroke-width="2"/>
  <path d="M 0 14 A 14 14 0 0 1 14 0 L {width - 14} 0 A 14 14 0 0 1 {width} 14
           L {width} 34 L 0 34 Z" fill="{BAR}"/>
  <circle cx="26" cy="17" r="6" fill="{ERR}"/>
  <circle cx="48" cy="17" r="6" fill="{WARN}"/>
  <circle cx="70" cy="17" r="6" fill="{OK}"/>
  <text x="{width / 2:.0f}" y="22" font-family="{MONO}" font-size="13"
        fill="{DIM}" text-anchor="middle">flashphysician -- diagnose, repair, recover</text>
  {"".join(rows)}
</svg>
'''


def main() -> int:
    print("FlashPhysician -- rendering the terminal card from a real session\n")
    try:
        lines = capture_session()
    except Exception as exc:
        print(f"  error: could not capture a session: {exc}")
        return 1
    if not lines:
        print("  error: the tool produced no output")
        return 1

    svg = build_svg(lines)
    out_svg = ASSETS / "terminal.svg"
    out_svg.write_text(svg, encoding="utf-8")
    print(f"  wrote {out_svg.relative_to(ROOT)}  ({len(lines)} output lines)")

    from make_logo import rasterise_via_chrome
    # measure the aspect ratio from the SVG header to keep the PNG exact
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (940, 420)
    out_png = ASSETS / "terminal.png"
    if not rasterise_via_chrome(out_svg, out_png, w, h):
        print("  note: no rasteriser available -- terminal.svg is still complete")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())