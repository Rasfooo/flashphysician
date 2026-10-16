#!/usr/bin/env python3
"""Draw the USB Doctor logo as SVG -- no design tool, no binary asset.

Keeping the logo as code means it regenerates identically on any machine and
scales to any size (icon, README banner, installer splash) without shipping
five raster files that drift apart. Run this to produce assets/logo.svg and
the PNG sizes used by the README and the PyInstaller icon.

    python3 tools/make_logo.py

Concept: a USB plug whose shell is a medical shield, with an ECG trace across
it -- "we read the drive's pulse". The palette is deliberately clinical rather
than alarming: people arrive here already frightened about their data.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

BG = "#0f1620"
SHELL = "#16202c"
ACCENT = "#4ea1ff"
OK = "#3fb950"
FG = "#e6edf3"
WARN = "#d29922"

FONT = ("Segoe UI, SF Pro Display, Helvetica Neue, Arial, "
        "system-ui, sans-serif")
MONO = "Consolas, SF Mono, Menlo, DejaVu Sans Mono, monospace"


def shield_path(cx: float, cy: float, w: float, h: float) -> str:
    """A shield: flat shoulders, straight sides, tapering to a real point.

    Using a quadratic curve for each half of the point (rather than one wide
    curve across the bottom) is what makes the silhouette read as a shield
    instead of a tombstone.
    """
    x0, x1 = cx - w / 2, cx + w / 2
    y0, y1 = cy - h / 2, cy + h / 2
    r = w * 0.14                      # shoulder radius
    shoulder = y0 + h * 0.30          # where the sides stop being straight
    tip = y1                          # the point

    return (
        f"M {x0 + r:.1f} {y0:.1f} "
        f"L {x1 - r:.1f} {y0:.1f} "
        f"Q {x1:.1f} {y0:.1f} {x1:.1f} {y0 + r:.1f} "
        f"L {x1:.1f} {shoulder:.1f} "
        # right half of the point: curve out to the tip
        f"C {x1:.1f} {shoulder + h * 0.30:.1f} "
        f"{cx + w * 0.20:.1f} {y1 - h * 0.10:.1f} "
        f"{cx:.1f} {tip:.1f} "
        # left half, mirrored
        f"C {cx - w * 0.20:.1f} {y1 - h * 0.10:.1f} "
        f"{x0:.1f} {shoulder + h * 0.30:.1f} "
        f"{x0:.1f} {shoulder:.1f} "
        f"L {x0:.1f} {y0 + r:.1f} "
        f"Q {x0:.1f} {y0:.1f} {x0 + r:.1f} {y0:.1f} Z"
    )


def usb_plug(cx: float, cy: float, scale: float) -> str:
    """A recognisable USB plug: metal shell, four contacts, cable stub.

    Explicit contact pads and the stepped shell are what make this read as a
    USB connector; a plain rounded rectangle with two pills inside does not.
    """
    w, h = 150 * scale, 96 * scale
    x0, y0 = cx - w / 2, cy - h / 2
    sr = 10 * scale

    # cable stub going down out of the plug
    stub = (f'<rect x="{cx - 16 * scale:.1f}" y="{y0 + h:.1f}" '
            f'width="{32 * scale:.1f}" height="{30 * scale:.1f}" '
            f'rx="{7 * scale:.1f}" fill="#0d141c" stroke="{ACCENT}" '
            f'stroke-width="{5 * scale:.1f}"/>')

    # shell
    shell = (f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" '
             f'rx="{sr:.1f}" fill="#0d141c" stroke="{ACCENT}" '
             f'stroke-width="{7 * scale:.1f}"/>')

    # four contact pads along the top inside the shell
    pads = []
    pad_w = w * 0.17
    gap = w * 0.04
    total = pad_w * 4 + gap * 3
    px = cx - total / 2
    for i in range(4):
        pads.append(
            f'<rect x="{px:.1f}" y="{y0 + h * 0.20:.1f}" width="{pad_w:.1f}" '
            f'height="{h * 0.16:.1f}" rx="{3 * scale:.1f}" '
            f'fill="{ACCENT}" fill-opacity="{0.9 - i * 0.13:.2f}"/>')
        px += pad_w + gap

    return stub + shell + "".join(pads)


def ecg_path(cx: float, cy: float, w: float, amp: float) -> str:
    """A heartbeat trace sized to stay inside its container.

    The peak height is a fraction of the available width, so the same code
    works at icon size and banner size without the spike being clipped.
    """
    x0 = cx - w / 2
    step = w / 10.0
    pts = [
        (x0, cy),
        (x0 + step * 2.2, cy),
        (x0 + step * 3.0, cy - amp * 0.35),
        (x0 + step * 3.9, cy + amp * 0.55),
        (x0 + step * 4.9, cy - amp * 1.00),
        (x0 + step * 5.9, cy),
        (x0 + step * 7.6, cy),
        (x0 + w, cy),
    ]
    d = f"M {pts[0][0]:.1f} {pts[0][1]:.1f}"
    for x, y in pts[1:]:
        d += f" L {x:.1f} {y:.1f}"
    return d


def build_svg(size: int = 512) -> str:
    """The mark: a shield containing a USB plug with an ECG trace across it."""
    s = size
    cx = s / 2

    # Shield fills most of the canvas, with breathing room all round so the
    # stroke is never clipped at the icon edge.
    sw, sh = s * 0.62, s * 0.76
    sy = s * 0.50

    # The plug sits in the upper half of the shield, above the point.
    plug_cy = sy - sh * 0.13
    plug = usb_plug(cx, plug_cy, s / 512.0 * 1.15)

    # ECG runs across the shield at the widest part, well inside the sides.
    # Keep the trace inside the shield's tapering sides: at this height the
    # silhouette has already narrowed, so the width is computed from the
    # shoulder geometry rather than taken as a fraction of the full width.
    ecg_y = sy + sh * 0.20
    y0m, y1m = sy - sh / 2, sy + sh / 2
    shoulder = y0m + sh * 0.30
    t = max(0.0, min(1.0, (ecg_y - shoulder) / max(y1m - shoulder, 1e-6)))
    safe_half = (sw / 2) * (1 - t * 0.92) * 0.78
    ecg_w = safe_half * 2
    ecg = ecg_path(cx, ecg_y, ecg_w, sh * 0.10)

    # Status dot inside the shield, in the lower right of the flat area.
    dot_x = cx + sw * 0.30
    dot_y = sy - sh * 0.22

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}"
     width="{s}" height="{s}" role="img"
     aria-label="USB Doctor: diagnose, repair and recover removable storage">
  <title>USB Doctor</title>
  <desc>A shield containing a USB plug with an ECG heartbeat trace, and a
  green status dot.</desc>

  <defs>
    <linearGradient id="shell" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#1e2b3b"/>
      <stop offset="100%" stop-color="{SHELL}"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#7cc0ff"/>
      <stop offset="100%" stop-color="{ACCENT}"/>
    </linearGradient>
    <linearGradient id="pulse" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{ACCENT}"/>
      <stop offset="60%" stop-color="{OK}"/>
      <stop offset="100%" stop-color="{OK}"/>
    </linearGradient>
    <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="{s * 0.010:.1f}" result="b"/>
      <feMerge>
        <feMergeNode in="b"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>

  <rect width="{s}" height="{s}" rx="{s * 0.19:.1f}" fill="{BG}"/>

  <path d="{shield_path(cx, sy, sw, sh)}"
        fill="url(#shell)" stroke="url(#accent)"
        stroke-width="{s * 0.020:.1f}" stroke-linejoin="round"/>

  {plug}

  <path d="{ecg}" fill="none" stroke="url(#pulse)"
        stroke-width="{s * 0.020:.1f}" stroke-linecap="round"
        stroke-linejoin="round" filter="url(#glow)"/>

  <circle cx="{dot_x:.1f}" cy="{dot_y:.1f}" r="{s * 0.030:.1f}"
          fill="{OK}" opacity="0.95"/>
</svg>
'''


def build_wordmark(width: int = 1280, height: int = 400) -> str:
    """Horizontal lockup for the README banner.

    Badge pill widths come from measured character advance rather than fixed
    guesses: hard-coded widths is how label text ends up overflowing its pill.
    """
    badges = [
        ("No install needed", OK, 0.14, 0.45, 16),
        ("Read-only first", ACCENT, 0.14, 0.45, 16),
        ("Backups before writes", WARN, 0.13, 0.40, 16),
    ]
    CHAR_W = 0.56                      # avg advance for 600-weight sans
    PAD_X, GAP, pill_h = 17, 12, 30

    pills = []
    x = 0.0
    for text, color, fo, so, fs in badges:
        w = len(text) * fs * CHAR_W + PAD_X * 2
        pills.append(
            f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="{pill_h}" '
            f'rx="{pill_h / 2}" fill="{color}" fill-opacity="{fo}" '
            f'stroke="{color}" stroke-opacity="{so}"/>'
            f'<text x="{x + w / 2:.1f}" y="20.5" font-family="{FONT}" '
            f'font-size="{fs}" font-weight="600" fill="{color}" '
            f'text-anchor="middle">{_esc(text)}</text>')
        x += w + GAP

    icon_cx, icon_cy = 132, 172
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"
     width="{width}" height="{height}">
  <defs>
    <linearGradient id="bgg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0d1320"/>
      <stop offset="100%" stop-color="#16202c"/>
    </linearGradient>
    <linearGradient id="tg" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="100%" stop-color="#9ccbff"/>
    </linearGradient>
    <linearGradient id="pg" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{ACCENT}"/>
      <stop offset="100%" stop-color="{OK}"/>
    </linearGradient>
  </defs>

  <rect width="{width}" height="{height}" rx="22" fill="url(#bgg)"/>
  <rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="21"
        fill="none" stroke="{ACCENT}" stroke-opacity="0.22" stroke-width="2"/>

  <!-- icon: shield at ~44% of banner height, contents kept well inside it -->
  <path d="{shield_path(icon_cx, icon_cy, 196, 268)}"
        fill="{SHELL}" stroke="url(#accent)" stroke-width="7"
        stroke-linejoin="round"/>
  {usb_plug(icon_cx, icon_cy - 34, 0.335)}
  <path d="{ecg_path(icon_cx, icon_cy + 30, 132, 15)}"
        fill="none" stroke="url(#pg)" stroke-width="8"
        stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="{icon_cx + 50}" cy="{icon_cy - 74}" r="9" fill="{OK}"/>

  <text x="282" y="150" font-family="{FONT}" font-size="78" font-weight="700"
        fill="url(#tg)" letter-spacing="-1.5">USB Doctor</text>
  <text x="286" y="204" font-family="{FONT}" font-size="30" font-weight="400"
        fill="{ACCENT}" letter-spacing="0.4">Diagnose &#183; Repair &#183; Recover</text>
  <text x="286" y="248" font-family="{FONT}" font-size="22" font-weight="400"
        fill="#8b98a5">Rescue tool for USB flash drives, SD cards and memory cards</text>

  <g transform="translate(286, 282)">
    {"".join(pills)}
  </g>
</svg>
'''


def build_terminal_card() -> str:
    """A stylised terminal card for the README, showing real output."""
    lines = [
        ("$ ", "usbdoctor diagnose /dev/sdb", FG),
        ("", "", ""),
        ("", "  USB DOCTOR -- DIAGNOSTIC REPORT", ACCENT),
        ("", "  Device      : /dev/sdb", FG),
        ("", "  Size        : 14.9 GB", FG),
        ("", "  Table       : NONE", WARN),
        ("", "  Filesystem  : RAW", WARN),
        ("", "  Health      : 55/100  grade C", WARN),
        ("", "  Verdict     : Recoverable -- partition table lost, filesystem intact.",
         OK),
        ("", "", ""),
        ("", "  [DEAD] No partition table at all (blank boot sector)", WARN),
        ("", "  [ OK ] MBR boot signature present (55AA)", OK),
        ("", "  [INFO] A FAT32 filesystem was found at offset 1.0 MB", ACCENT),
        ("", "  [WARN] No filesystem at the expected location", WARN),
    ]
    rows = []
    y = 0
    for prompt, text, color in lines:
        if prompt:
            rows.append(
                f'<text x="30" y="{y + 26}" font-family="{MONO}" '
                f'font-size="16" fill="{OK}">{prompt}</text>'
                f'<text x="58" y="{y + 26}" font-family="{MONO}" '
                f'font-size="16" fill="{FG}">{_esc(text)}</text>')
        elif text:
            rows.append(f'<text x="30" y="{y + 26}" font-family="{MONO}" '
                        f'font-size="15" fill="{color}">{_esc(text[:86])}</text>')
        y += 26
    height = y + 40

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 {height}"
     width="900" height="{height}">
  <rect width="900" height="{height}" rx="14" fill="#0b1118"/>
  <rect x="1" y="1" width="898" height="{height - 2}" rx="13" fill="none"
        stroke="#2a3644" stroke-width="2"/>
  <rect width="900" height="34" rx="14" fill="#16202c"/>
  <rect y="20" width="900" height="14" fill="#16202c"/>
  <circle cx="24" cy="17" r="6" fill="#f85149"/>
  <circle cx="46" cy="17" r="6" fill="{WARN}"/>
  <circle cx="68" cy="17" r="6" fill="{OK}"/>
  <text x="450" y="22" font-family="{MONO}" font-size="13"
        fill="#8b98a5" text-anchor="middle">usbdoctor -- diagnose, repair, recover</text>
  <g transform="translate(0, 40)">
    {"".join(rows)}
  </g>
</svg>
'''


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_svg(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}  ({len(content):,} bytes)")


def find_chrome() -> str:
    """Locate a Chromium/Chrome binary for headless rasterisation."""
    from shutil import which
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome", "msedge", "edge"):
        found = which(name)
        if found:
            return found
    for pat in ("/opt/hermes/tools/chromium-*/chrome-linux64/chrome",
                "/opt/hermes/tools/chromium-*/chrome-linux/chrome",
                str(Path.home() / ".cache/puppeteer/*/chrome-linux64/chrome")):
        hits = sorted(Path("/").glob(pat.lstrip("/")))
        if hits:
            return str(hits[-1])
    return ""



def _crop_png_height(path: Path, target_w: int, target_h: int, max_overshoot: int) -> bool:
    """Trim a PNG to exactly `target_h` rows, removing a bottom background band.

    Our PNGs are 8-bit RGBA/RGB with filter bytes per scanline. We decode only
    as far as the target height, then drop every later chunk (IDAT data past
    that point is simply excluded), which is enough to remove a surplus band of
    uniform background without needing a full image library.
    """
    import struct
    import zlib

    raw = path.read_bytes()
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return False

    # --- split chunks -----------------------------------------------------
    chunks = []
    off = 8
    while off + 8 <= len(raw):
        length, ctype = struct.unpack_from(">I4s", raw, off)
        data = raw[off + 8: off + 8 + length]
        chunks.append((ctype, data, raw[off + 8 + length: off + 12 + length]))
        off += 12 + length
        if ctype == b"IEND":
            break

    ihdr = next((c for t, c, _ in chunks if t == b"IHDR"), None)
    if not ihdr or len(ihdr) < 13:
        return False
    w, h = struct.unpack_from(">II", ihdr, 0)
    bit_depth, color_type = ihdr[8], ihdr[9]
    if w != target_w or h <= target_h or target_h <= 0:
        return False
    if bit_depth != 8 or color_type not in (2, 6):
        # only the straightforward 8-bit truecolour formats are handled here
        return False
    bpp = 4 if color_type == 6 else 3
    stride = w * bpp + 1                       # filter byte + pixel bytes

    # --- concatenate IDAT, inflate, decode ---------------------------------
    idat = b"".join(c for t, c, _ in chunks if t == b"IDAT")
    try:
        decompressed = zlib.decompress(idat)
    except Exception:
        return False
    if len(decompressed) < stride * target_h:
        return False

    # undo per-scanline filters so rows become raw pixels
    rows = []
    prev = bytes(w * bpp)
    for y in range(min(target_h, h)):
        start = y * stride
        filt = decompressed[start]
        line = bytearray(decompressed[start + 1: start + stride])
        if filt == 1:
            for i in range(bpp, len(line)):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filt == 2:
            for i in range(len(line)):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:
            for i in range(len(line)):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:
            for i in range(len(line)):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        rows.append(bytes(line))
        prev = bytes(line)

    # --- re-encode ----------------------------------------------------------
    body = b"".join(bytes([0]) + r for r in rows)   # filter 0 per row
    new_ihdr = bytearray(ihdr)
    struct.pack_into(">I", new_ihdr, 4, target_h)

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + ctype + data + \
            struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", bytes(new_ihdr))
    for t, c, _ in chunks:
        if t in (b"IHDR", b"IDAT", b"IEND"):
            continue
        out += chunk(t, c)
    out += chunk(b"IDAT", zlib.compress(body, 9))
    out += chunk(b"IEND", b"")
    path.write_bytes(out)
    return True


def rasterise_via_chrome(svg_path: Path, out_path: Path, w: int, h: int) -> bool:
    """Render an SVG to PNG with headless Chrome, cropped to exactly `h` rows.

    Chrome's --window-size includes browser chrome even headless, so asking
    for the exact height silently cuts the bottom. We therefore ask for more
    and trim the surplus -- without the trim the artefact carries a band of
    stray background below the content.
    """
    chrome = find_chrome()
    if not chrome:
        return False
    import subprocess
    import tempfile
    html = (f'<!doctype html><html><body style="margin:0;background:{BG}">'
            f'<img src="{svg_path.resolve()}" width="{w}" height="{h}">'
            "</body></html>")
    page = ""
    try:
        # Write the page next to the SVG so relative paths resolve.
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                         dir=str(svg_path.parent)) as fh:
            fh.write(html)
            page = fh.name
        overshoot = 140
        res = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars",
             f"--screenshot={out_path}",
             f"--window-size={w},{h + overshoot}",
             f"--default-background-color={BG.lstrip('#')}",
             f"file://{page}"],
            check=False, capture_output=True, timeout=120)
        if out_path.exists() and out_path.stat().st_size > 0:
            if not _crop_png_height(out_path, w, h, overshoot):
                print(f"  note: could not trim {out_path.name} to {h}px; "
                      f"it may have {overshoot}px of extra background")
            try:
                shown = out_path.relative_to(ROOT)
            except ValueError:
                shown = out_path
            print(f"  wrote {shown}  ({w}x{h}, via chrome)")
            return True
        if res.returncode != 0:
            print(f"  chrome exited {res.returncode}: "
                  f"{res.stderr.decode(errors='ignore')[-200:]}")
    except Exception as exc:
        print(f"  chrome rasterise failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            if page:
                os.remove(page)
        except Exception:
            pass
    return False


def try_png(svg_path: Path, out_path: Path, size: int,
            height: int | None = None) -> bool:
    """Convert to PNG, preferring Chrome and falling back to other tools.

    Optional on purpose: the repository ships SVG (which scales and diffs
    cleanly) and PNG is a convenience for GitHub's README rendering.
    """
    h = height or size
    if rasterise_via_chrome(svg_path, out_path, size, h):
        return True
    for tool, argv in (
        ("rsvg-convert", ["rsvg-convert", "-w", str(size), "-h", str(h),
                          str(svg_path), "-o", str(out_path)]),
        ("inkscape", ["inkscape", str(svg_path), "-w", str(size),
                      "-h", str(h), "-o", str(out_path)]),
        ("convert", ["convert", "-background", "none", "-resize",
                     f"{size}x{h}", str(svg_path), str(out_path)]),
    ):
        from shutil import which
        if which(tool):
            try:
                subprocess.run(argv, check=True, capture_output=True, timeout=120)
                print(f"  wrote {out_path.relative_to(ROOT)}  ({size}px, via {tool})")
                return True
            except Exception:
                continue
    return False


def main() -> int:
    print("USB Doctor -- generating logo assets\n")
    write_svg(ASSETS / "logo.svg", build_svg(512))
    write_svg(ASSETS / "logo.svg".replace(".svg", "-small.svg"), build_svg(64))
    write_svg(ASSETS / "wordmark.svg", build_wordmark())
    write_svg(ASSETS / "terminal.svg", build_terminal_card())

    # a plain-text banner for the CLI and for terminals without colour
    banner = r"""
  _   _ ___ ___  ___   ___   ___   ___ _____ ___ ___
 | | | / __| _ )/ _ \ /_\ |   \ / _ \/ __|_   _/ _ \
 | |_| \__ \ _ \ (_) / _ \| |) | (_) \__ \ | || (_) |
  \___/|___/___/\___/_/ \_\___/ \___/|___/ |_| \___/
"""
    (ASSETS / "banner.txt").write_text(banner, encoding="utf-8")
    print("  wrote assets/banner.txt")

    made = [try_png(ASSETS / "logo.svg", ASSETS / "logo.png", 512),
            try_png(ASSETS / "wordmark.svg", ASSETS / "wordmark.png", 1280, 400)]
    if not any(made):
        print("\n  note: no SVG->PNG converter found (rsvg-convert / inkscape /")
        print("        ImageMagick). SVG files are complete and render on GitHub;")
        print("        install one of those to also emit PNG previews.")

    icon = ASSETS / "icon.png"
    if try_png(ASSETS / "logo.svg", icon, 256):
        print("  icon for the Windows .exe: assets/icon.png")
    if not find_chrome():
        print("  note: no Chrome/Chromium found -- SVG output is complete,")
        print("        but PNG previews and the .exe icon were not generated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())