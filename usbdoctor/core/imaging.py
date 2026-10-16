"""Source abstraction: a physical device, a disk image, or any seekable file.

The whole diagnostic engine talks to this layer, which is why the tool can
be developed, demoed and regression-tested on disk images without root and
without risking a real drive.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


@dataclass
class DriveInfo:
    """A storage device as the OS reports it (or as discovered by probing)."""

    name: str
    path: str
    size_bytes: int = 0
    removable: bool = False
    bus: str = "unknown"          # usb / mmc / nvme / sata / file
    vendor: str = ""
    model: str = ""
    serial: str = ""
    tran: str = ""
    readonly: bool = False
    mounted: list[str] = field(default_factory=list)
    partitions: list[str] = field(default_factory=list)
    label: str = ""
    fstype: str = ""
    mountpoint: str = ""
    is_loop: bool = False
    sysfs_path: str = ""

    @property
    def is_flash(self) -> bool:
        """Heuristic: what the field problems are actually about."""
        if self.bus in ("usb", "mmc"):
            return True
        if self.size_bytes and self.size_bytes <= 512 * 1024**3 and self.removable:
            return True
        return False

    def describe(self) -> str:
        bits = [self.name, f"({_human(self.size_bytes)})"]
        if self.vendor or self.model:
            bits.append(f"{self.vendor} {self.model}".strip())
        if self.bus != "unknown":
            bits.append(f"[{self.bus}]")
        if self.readonly:
            bits.append("READ-ONLY")
        return " ".join(bits)


def _human(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


class Source:
    """Read/write access to bytes plus identity metadata."""

    def __init__(self, path: str, mode: str = "rb"):
        self.path = path
        self.mode = mode
        self._fh: BinaryIO | None = None
        self._size: int | None = None
        self.info = DriveInfo(name=Path(path).name, path=path)

    # -- lifecycle -------------------------------------------------------
    def open(self) -> "Source":
        if self._fh is None:
            flags = os.O_RDONLY
            if "w" in self.mode or "+" in self.mode:
                flags = os.O_RDWR
            fd = os.open(self.path, flags)
            self._fh = os.fdopen(fd, self.mode, buffering=0)
        return self

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def __enter__(self) -> "Source":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- size ------------------------------------------------------------
    @property
    def size(self) -> int:
        if self._size is not None:
            return self._size
        try:
            if self._fh is not None:
                self._size = self._fh.seek(0, os.SEEK_END)
                self._fh.seek(0)
            else:
                self._size = os.path.getsize(self.path)
        except Exception:
            self._size = 0
        return self._size

    # -- IO --------------------------------------------------------------
    def read_at(self, offset: int, size: int) -> bytes:
        fh = self._fh if self._fh is not None else self.open()._fh
        assert fh is not None
        try:
            fh.seek(offset)
            return fh.read(size)
        except OSError:
            return b""

    def write_at(self, offset: int, data: bytes) -> int:
        fh = self._fh if self._fh is not None else self.open()._fh
        assert fh is not None
        try:
            fh.seek(offset)
            return fh.write(data)
        except OSError:
            return 0

    def read_sector(self, lba: int, size: int = 512) -> bytes:
        return self.read_at(lba * size, size)

    def read_exact(self, offset: int, size: int) -> bytes:
        """Loop until `size` bytes are read or the medium hard-fails.
        Returns what was readable -- important for dying drives."""
        out = bytearray()
        pos = offset
        remaining = size
        while remaining > 0:
            chunk = self.read_at(pos, min(remaining, 1024 * 1024))
            if not chunk:
                break
            out += chunk
            pos += len(chunk)
            remaining -= len(chunk)
        return bytes(out)

    @property
    def writable(self) -> bool:
        try:
            fd = os.open(self.path, os.O_RDWR)
            os.close(fd)
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------
# device discovery
# --------------------------------------------------------------------------

def _read_text(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="ignore").strip()
    except Exception:
        return default


def discover_drives(include_loop: bool = True, include_system: bool = False) -> list[DriveInfo]:
    """Enumerate block devices via sysfs.

    sysfs rather than `lsblk` so we work on minimal systems, and because we
    want the raw attributes (bus, removable, serial) that tell a cheap flash
    drive apart from an internal disk.
    """
    out: list[DriveInfo] = []
    block = Path("/sys/block")
    if not block.is_dir():
        return out

    for dev in sorted(block.iterdir()):
        name = dev.name
        try:
            if name.startswith(("ram", "zram", "dm-", "md", "sr", "fd")):
                continue
            is_loop = name.startswith("loop")
            if is_loop and not include_loop:
                continue

            size = int(_read_text(dev / "size", "0") or 0) * 512
            removable = _read_text(dev / "removable", "0") == "1"
            ro = _read_text(dev / "ro", "0") == "1"

            if size == 0 and not is_loop:
                continue

            # device class path, e.g. .../usb1/1-1/1-1:1.0/host6/target.../block/sdb
            real = dev.resolve()
            bus, vendor, model, serial, tran = _usb_or_mmc_identity(real, dev)
            if is_loop:
                bus = "file"

            if bus in ("usb", "mmc") and not is_loop:
                pass
            elif not include_system and not is_loop:
                continue

            mounts = _mounts_for(name)
            info = DriveInfo(
                name=name,
                path=f"/dev/{name}",
                size_bytes=size,
                removable=removable,
                bus=bus,
                vendor=vendor,
                model=model,
                serial=serial,
                tran=tran,
                readonly=ro,
                mounted=mounts,
                is_loop=is_loop,
                sysfs_path=str(dev),
            )
            info.partitions = _partitions_for(dev)
            _fill_fs_hint(info)
            out.append(info)
        except PermissionError:
            continue
        except Exception:
            continue
    return out


def _usb_or_mmc_identity(real: Path, dev: Path) -> tuple[str, str, str, str, str]:
    bus, vendor, model, serial, tran = "unknown", "", "", "", ""
    parts = real.parts
    for i, seg in enumerate(parts):
        if seg.startswith("usb"):
            bus = "usb"
            # the USB interface dir holds uevent with PRODUCT / SERIAL
            for j in range(i, min(i + 6, len(parts))):
                ue = Path(*parts[: j + 1]) / "uevent"
                if ue.is_file():
                    txt = _read_text(str(ue))
                    m = re.search(r"PRODUCT=([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", txt)
                    if m:
                        tran = f"usb:{m.group(1)}:{m.group(2)}"
                    if "SERIAL=" in txt:
                        serial = txt.split("SERIAL=")[1].splitlines()[0].strip()
                if (Path(*parts[: j + 1]) / "manufacturer").is_file():
                    vendor = _read_text(str(Path(*parts[: j + 1]) / "manufacturer"))
                    model = _read_text(str(Path(*parts[: j + 1]) / "product"))
                    s = _read_text(str(Path(*parts[: j + 1]) / "serial"))
                    if s:
                        serial = s
            break
        if seg.startswith("mmc"):
            bus = "mmc"
            break
        if seg.startswith("nvme"):
            bus = "nvme"
            break
        if seg.startswith(("ata", "host")) and bus == "unknown":
            bus = "sata"

    if not model:
        model = _read_text(dev / "device" / "model")
    if not vendor:
        vendor = _read_text(dev / "device" / "vendor")
    if not serial:
        serial = _read_text(dev / "device" / "serial")
    return bus, vendor.strip(), model.strip(), serial.strip(), tran


def _partitions_for(dev: Path) -> list[str]:
    out = []
    try:
        for child in sorted(dev.iterdir()):
            if child.name.startswith(dev.name) and child.name != dev.name:
                out.append(f"/dev/{child.name}")
    except Exception:
        pass
    return out


def _mounts_for(devname: str) -> list[str]:
    out: list[str] = []
    try:
        for line in Path("/proc/mounts").read_text(errors="ignore").splitlines():
            f = line.split()
            if len(f) >= 2 and f[0].startswith("/dev/"):
                base = f[0].rsplit("/", 1)[-1]
                if base == devname or base.startswith(devname):
                    out.append(f[1])
    except Exception:
        pass
    return out


def _fill_fs_hint(info: DriveInfo) -> None:
    """Best-effort filesystem hint from /proc/mounts or the first 512 bytes."""
    try:
        with Source(info.path) as src:
            if src.size >= 512:
                from .structures import parse_boot_sector

                fs = parse_boot_sector(src.read_at(info.partitions and 0 or 0, 512))
                info.fstype = fs.fs.value
                info.label = fs.label
                return
    except Exception:
        pass


def unmount_all(devname: str) -> list[str]:
    """Unmount every mountpoint belonging to a device (needed before write
    operations). Returns the list of attempted mountpoints."""
    done = []
    for mp in _mounts_for(Path(devname).name):
        try:
            import subprocess

            subprocess.run(["umount", mp], check=False, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            done.append(mp)
        except Exception:
            pass
    return done


def make_test_image(path: str, size_mb: int = 64) -> str:
    """Sparse file standing in for a drive -- used by the test suite."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    return str(p)


def copy_range(src: Source, dst_path: str, start: int, length: int,
               chunk: int = 4 * 1024 * 1024, progress=None) -> dict:
    """Image a byte range, tolerating bad sectors.

    Returns a report including which offsets failed -- for a failing drive
    this is the deliverable, not an error.
    """
    bad_offsets: list[int] = []
    written = 0
    pos = start
    end = start + length
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dst_path, "wb") as out:
        while pos < end:
            n = min(chunk, end - pos)
            data = src.read_at(pos, n)
            if len(data) != n:
                # narrow down which sectors failed
                for off in range(pos, pos + n, 4096):
                    probe = src.read_at(off, 4096)
                    if len(probe) < 4096:
                        bad_offsets.append(off)
                        probe = probe + b"\x00" * (4096 - len(probe))
                    out.write(probe)
                    written += 4096
            else:
                out.write(data)
                written += len(data)
            pos += n
            if progress:
                try:
                    progress(min(written, length), length)
                except Exception:
                    pass
    return {
        "path": dst_path,
        "bytes_written": written,
        "bad_offsets": bad_offsets,
        "bad_count": len(bad_offsets),
    }


def free_space_ok(path: str, needed: int) -> bool:
    try:
        return shutil.disk_usage(Path(path).parent if Path(path).parent.exists() else ".").free > needed
    except Exception:
        return False