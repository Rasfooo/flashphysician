"""Surface integrity testing: the part that decides hardware vs software.

Two questions a diagnostic must answer before promising any repair:

1. **Can this medium actually hold data?**  A drive with dead cells will
   "repair" fine and corrupt the data again next week. We write a known
   pattern, read it back, and verify -- sector by sector.
2. **Is the advertised capacity real?**  Fraudulent flash reports a larger
   size than it can address; writes past the real end silently wrap around
   and destroy earlier data. This is the single most common "my files keep
   disappearing" cause on cheap drives, and the standard test is F3's
   approach: write a counter across the whole device and read it back.

Both functions here take a *range*, so the same code drives a full-device
test and a quick sampled spot check, and both work on a disk image or a
real device.
"""

from __future__ import annotations

import hashlib
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

from ..core.imaging import Source
from ..core.structures import human


@dataclass
class SectorFault:
    offset: int
    kind: str          # write-failed / read-failed / verify-mismatch / zeroed
    expected: str = ""
    got: str = ""

    def describe(self) -> str:
        return (f"{self.kind} at {human(self.offset)} ({self.offset:#x})"
                + (f": expected {self.expected}, got {self.got}" if self.got else ""))


@dataclass
class SurfaceReport:
    device: str
    tested_bytes: int = 0
    written_bytes: int = 0
    faults: list[SectorFault] = field(default_factory=list)
    sectors_tested: int = 0
    read_errors: int = 0
    write_errors: int = 0
    verify_errors: int = 0
    duration_s: float = 0.0
    worst_read_mbps: float = 0.0
    avg_write_mbps: float = 0.0
    avg_read_mbps: float = 0.0
    aborted: str = ""
    pattern: str = ""
    mode: str = "write-read-verify"

    @property
    def clean(self) -> bool:
        return not self.faults and not self.aborted

    @property
    def write_ok(self) -> bool:
        return self.write_errors == 0 and not self.aborted

    def verdict(self) -> str:
        if self.aborted:
            return f"Test aborted: {self.aborted}"
        if not self.faults:
            return (f"PASS -- {human(self.tested_bytes)} verified with zero faults "
                    f"({self.avg_write_mbps:.1f} MB/s write, "
                    f"{self.avg_read_mbps:.1f} MB/s read)")
        if self.verify_errors and not self.write_errors and not self.read_errors:
            return (f"FAIL -- {self.verify_errors} sector(s) did not retain the data "
                    "written. Classic failing flash: cells no longer hold a charge.")
        return (f"FAIL -- {len(self.faults)} faulty sector(s): "
                f"{self.write_errors} write, {self.read_errors} read, "
                f"{self.verify_errors} verify")


def make_pattern(offset: int, size: int, seed: bytes = b"USBD") -> bytes:
    """Deterministic pattern for a given offset.

    Deterministic is the point: the verify pass recomputes it from the offset
    alone, so we can verify a sector without keeping the whole test buffer in
    memory, and re-verify after a power cycle.
    """
    out = bytearray(size)
    block = hashlib.sha256(seed + struct.pack("<Q", offset)).digest()
    for i in range(0, size, 32):
        out[i:i + 32] = block
        block = hashlib.sha256(block).digest()
    return bytes(out)


def surface_test(path: str, start: int = 0, length: int | None = None,
                 chunk: int = 1024 * 1024, verify: bool = True,
                 restore: bool = False,
                 progress: Callable[[int, int, str], None] | None = None,
                 stop_flag: Callable[[], bool] | None = None,
                 seed: bytes = b"USBD") -> SurfaceReport:
    """Write-read-verify a byte range.

    `restore=True` backs up the original bytes first and puts them back
    afterwards, which is what you want for a non-destructive health check on
    a drive that still holds data. Without it, the range is left with the
    test pattern (fine for a blank or already-lost drive).
    """
    t0 = time.time()
    rep = SurfaceReport(device=path, pattern=seed.decode(errors="ignore"),
                        mode="write-read-verify" if verify else "write-only")
    with Source(path, mode="r+b") as src:
        total = src.size
        if length is None:
            length = max(total - start, 0)
        length = min(length, max(total - start, 0))
        if length <= 0:
            rep.aborted = "nothing to test (zero-length range)"
            return rep
        if not src.writable:
            rep.aborted = ("device is not writable by this process -- a "
                           "write test needs write access")
            return rep

        rep.tested_bytes = length
        originals: bytes | None = None
        if restore:
            originals = src.read_exact(start, length)

        written = 0
        w_t0 = time.time()
        pos = start
        while pos < start + length:
            n = min(chunk, start + length - pos)
            data = make_pattern(pos - start, n, seed)
            if stop_flag and stop_flag():
                rep.aborted = "cancelled by user"
                break
            got = src.write_at(pos, data)
            if not got or got != n:
                rep.write_errors += 1
                rep.faults.append(SectorFault(pos, "write-failed",
                                              expected=f"{n} bytes",
                                              got=f"{got} bytes"))
                # narrow to the failing sector so the report is actionable
                for off in range(pos, pos + n, 4096):
                    if not src.write_at(off, b"\x00" * min(4096, pos + n - off)):
                        rep.faults.append(SectorFault(off, "write-failed"))
                if rep.write_errors > 64:
                    rep.aborted = "too many write failures -- medium is dying"
                    break
            else:
                written += got
            pos += n
            if progress:
                progress(min(pos - start, length), length, "writing")
        rep.written_bytes = written
        w_dt = max(time.time() - w_t0, 1e-6)
        rep.avg_write_mbps = (written / 1024 / 1024) / w_dt

        if verify and not rep.aborted:
            r_t0 = time.time()
            pos = start
            read_total = 0
            while pos < start + length:
                n = min(chunk, start + length - pos)
                if stop_flag and stop_flag():
                    rep.aborted = "cancelled by user"
                    break
                data = src.read_at(pos, n)
                if len(data) != n:
                    rep.read_errors += 1
                    rep.faults.append(SectorFault(pos, "read-failed",
                                                  expected=f"{n} bytes",
                                                  got=f"{len(data)} bytes"))
                    pos += n
                    continue
                read_total += len(data)
                expect = make_pattern(pos - start, n, seed)
                if data != expect:
                    # locate the exact mismatching sectors
                    for off in range(0, n, 4096):
                        a = data[off:off + 4096]
                        b = expect[off:off + 4096]
                        if a != b:
                            rep.verify_errors += 1
                            zeroed = all(x == 0 for x in a)
                            rep.faults.append(SectorFault(
                                pos + off,
                                "zeroed" if zeroed else "verify-mismatch",
                                expected=b[:8].hex(), got=a[:8].hex()))
                            if rep.verify_errors > 256:
                                rep.aborted = ("hundreds of sectors did not retain "
                                               "data -- stop reading, this drive "
                                               "must be replaced")
                                break
                    if rep.aborted:
                        break
                pos += n
                if progress:
                    progress(min(pos - start, length), length, "verifying")
            r_dt = max(time.time() - r_t0, 1e-6)
            rep.avg_read_mbps = (read_total / 1024 / 1024) / r_dt

        if restore and originals is not None:
            src.write_at(start, originals)

    rep.sectors_tested = rep.tested_bytes // 512
    rep.duration_s = time.time() - t0
    return rep


@dataclass
class CapacityReport:
    device: str
    advertised_bytes: int = 0
    genuine_bytes: int = 0
    verified: bool = False
    wrap_offset: int | None = None
    first_bad_offset: int | None = None
    sampled: bool = False
    samples: int = 0
    duration_s: float = 0.0
    aborted: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def is_fake(self) -> bool:
        if self.advertised_bytes and self.genuine_bytes:
            return self.genuine_bytes < self.advertised_bytes * 0.95
        return False

    @property
    def fake_fraction(self) -> float:
        """How much of the advertised size is fiction (0.0 = honest drive)."""
        if not self.advertised_bytes:
            return 0.0
        missing = max(self.advertised_bytes - self.genuine_bytes, 0)
        return missing / self.advertised_bytes

    def verdict(self) -> str:
        if self.aborted:
            return f"Capacity test aborted: {self.aborted}"
        if not self.verified:
            return "Capacity not verified (sampling only)"
        if self.is_fake:
            return (f"FAKE CAPACITY -- only {human(self.genuine_bytes)} of the "
                    f"advertised {human(self.advertised_bytes)} is real "
                    f"({self.fake_fraction * 100:.0f}% is not addressable). "
                    "Data written past the real end overwrites the start of "
                    "the drive: this is why files vanish.")
        return (f"Capacity verified genuine: {human(self.genuine_bytes)} "
                "addressable, matching the advertised size")


def capacity_test(path: str, sample_stride: int | None = None,
                  chunk: int = 1024 * 1024,
                  progress: Callable[[int, int, str], None] | None = None,
                  stop_flag: Callable[[], bool] | None = None) -> CapacityReport:
    """Detect fraudulent flash by writing a position-dependent marker and
    reading it back.

    Method (same principle as F3): write a block whose content encodes its own
    offset. On a drive that wraps, the read-back at offset N returns the
    content belonging to offset N minus the real capacity -- so the marker
    itself reveals where the real end is, instead of merely telling us
    "something is wrong".

    `sample_stride` > 0 tests only one block per stride (a fast check that can
    still catch wrapping); None scans the whole device (authoritative).
    """
    t0 = time.time()
    rep = CapacityReport(device=path)
    with Source(path, mode="r+b") as src:
        total = src.size
        rep.advertised_bytes = total
        if total == 0:
            rep.aborted = "device reports zero size"
            return rep
        if not src.writable:
            rep.aborted = "device is not writable -- capacity test needs writes"
            return rep

        stride = sample_stride or 0
        rep.sampled = stride > 0
        offsets: list[int] = []
        if stride:
            off = 0
            while off < total:
                offsets.append(off)
                off += stride
        else:
            offsets = list(range(0, total, chunk))
        rep.samples = len(offsets)

        # write pass
        for i, off in enumerate(offsets):
            if stop_flag and stop_flag():
                rep.aborted = "cancelled by user"
                return rep
            n = min(chunk, total - off)
            src.write_at(off, _cap_marker(off, n))
            if progress:
                progress(i + 1, len(offsets), "writing capacity markers")

        # Read back from the START, then locate the boundary by structure.
        #
        # On a wrapping medium every write above the real capacity lands
        # somewhere below it, so after the write pass the medium looks like a
        # jigsaw: a mixture of markers belonging to various offsets. The real
        # capacity is the largest offset whose own marker is still intact,
        # because markers above it necessarily overwrote each other. Scanning
        # every offset and taking that maximum recovers the true boundary
        # regardless of the order writes happened to land in.
        intact: list[int] = []
        broken: list[int] = []
        for i, off in enumerate(offsets):
            if stop_flag and stop_flag():
                rep.aborted = "cancelled by user"
                return rep
            n = min(chunk, total - off)
            got = src.read_at(off, n)
            if len(got) != n:
                broken.append(off)
            elif got == _cap_marker(off, n):
                intact.append(off)
            else:
                broken.append(off)
                if not rep.notes:
                    origin = _cap_origin(got)
                    rep.notes.append(
                        f"content read at {human(off)} belongs to "
                        + (human(origin) if origin is not None else "no known offset")
                        + " -- writes are aliasing, the classic fraudulent-flash "
                        "signature")
            if progress:
                progress(i + 1, len(offsets), "reading capacity markers")

        if intact:
            # The highest offset holding its own data bounds the real capacity.
            best = max(intact)
            genuine_end = min(best + min(chunk, total - best), total)
        else:
            genuine_end = 0

        if broken:
            rep.first_bad_offset = min(broken)
            rep.wrap_offset = rep.first_bad_offset

        rep.genuine_bytes = genuine_end
        rep.verified = True
        if not broken and genuine_end >= total:
            rep.notes.append("every marker read back correctly from its own offset")
    rep.duration_s = time.time() - t0
    return rep


def _cap_marker(offset: int, size: int) -> bytes:
    """A block that encodes its own offset, so a wrapped read is self-identifying."""
    head = struct.pack("<QQ", 0x555342444F43544F, offset)      # "USBDOCTO", offset
    tail = struct.pack("<Q", 0x454E444F46444154)               # "ENDOFDAT"
    body = bytearray(size)
    body[0:len(head)] = head
    if size >= 16:
        body[-8:] = tail
    # fill the middle with a cheap repeating hash so partial writes show up
    fill = hashlib.blake2b(struct.pack("<Q", offset), digest_size=16).digest()
    for i in range(16, size - 8, 16):
        body[i:i + 16] = fill
    return bytes(body)


def _cap_origin(block: bytes) -> int | None:
    """Extract the offset a marker block claims to belong to."""
    if len(block) < 16:
        return None
    magic, off = struct.unpack_from("<QQ", block, 0)
    if magic != 0x555342444F43544F:
        return None
    return off
