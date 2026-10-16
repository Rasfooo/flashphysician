"""Surface + capacity tests.

These prove the two claims that decide whether a repair is even worth doing:
the medium retains data, and the advertised size is real. Both tests use the
same wraparound simulation a fake drive produces, built here in software so
the detector is proven without owning fraudulent hardware.
"""

from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests import fixtures as fx                                     # noqa: E402
from flashphysician.recovery.surface import (                             # noqa: E402
    capacity_test, make_pattern, surface_test,
)

IMG = os.path.join(ROOT, "tests", "_images")
WORK = os.path.join(ROOT, "tests", "_work")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, why: str = "") -> None:
    results.append((name, ok, why))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  << {why}" if not ok else ""))


class FakeFlash:
    """A file handle that silently aliases every access past `real` back to the
    start -- byte-for-byte the behaviour of fraudulent flash.

    Installing this on Source lets the capacity detector be tested against
    the exact failure it exists to catch, without owning fake hardware.
    """

    def __init__(self, fh, real: int):
        self.fh, self.real = fh, real
        self._virt = 0

    def seek(self, off, whence=0):
        if whence == 0 and off >= 0:
            self._virt = off
            return self.fh.seek(min(off, self.real))
        self._virt = None
        return self.fh.seek(off, whence)

    def read(self, n):
        """Reads above the real capacity return whatever lives at the aliased
        address -- which is what a controller with a lying size table does."""
        if self._virt is not None and self._virt >= self.real and self.real:
            self.fh.seek(self._virt % self.real)
        return self.fh.read(n)

    def write(self, b):
        """Writes above the real capacity are acknowledged and DISCARDED.

        That acknowledgement is the whole trick behind fake flash: the host
        is told the write succeeded, so nothing looks wrong until the data is
        read back later.
        """
        if self._virt is not None and self._virt >= self.real:
            return len(b)                      # pretend it worked, drop it
        return self.fh.write(b)

    def tell(self):
        return self._virt if self._virt is not None else self.fh.tell()

    def close(self):
        return self.fh.close()

    def tell(self):
        return self.fh.tell()


def wrap(path: str, real_mb: int, fake_mb: int) -> None:
    """Create the image file and arrange for Source to be wrapped.

    The caller must install `install_fake_flash(path, real_bytes)` before
    invoking the test; this function only prepares the file.
    """
    fx.blank_image(path, fake_mb)


def install_fake_flash(real_bytes: int):
    """Patch Source.open so every handle aliases past `real_bytes`."""
    from flashphysician.core import imaging as imgmod
    real_open = open

    def patched_open(self):
        if self._fh is None:
            fh = real_open(self.path, "r+b", buffering=0)
            self._fh = FakeFlash(fh, real_bytes)
        return self

    imgmod.Source.open = patched_open
    return imgmod


def test_healthy_surface_passes() -> None:
    p = os.path.join(WORK, "s01.img")
    fx.blank_image(p, 8)
    rep = surface_test(p, 0, 4 * 1024 * 1024)
    check("healthy_surface_passes", rep.clean,
          f"faults={len(rep.faults)} verdict={rep.verdict()[:70]}")


def test_dead_sector_detected() -> None:
    """A sector that silently reads back as zeros must be reported."""
    p = os.path.join(WORK, "s02.img")
    fx.blank_image(p, 8)
    # emulate a stuck-at-zero cell: patch Source.read_at via a wrapper file
    real_open = open

    class Sticky:
        def __init__(self, fh, bad_off):
            self.fh, self.bad = fh, bad_off

        def seek(self, *a):
            return self.fh.seek(*a)

        def read(self, n):
            """A stuck-at-zero cell returns the same number of bytes, all zero
            -- that is what makes it dangerous: nothing looks short, the data
            is simply wrong."""
            pos = self.fh.tell()
            data = self.fh.read(n)
            if pos <= self.bad < pos + len(data):
                i = self.bad - pos
                span = min(4096, len(data) - i)
                data = data[:i] + b"\x00" * span + data[i + span:]
            return data

        def write(self, b):
            return self.fh.write(b)

        def close(self):
            return self.fh.close()

    import flashphysician.recovery.surface as surf
    from flashphysician.core import imaging as imgmod
    bad_off = 1 * 1024 * 1024
    orig_init = imgmod.Source.__init__

    def patched_init(self, path, mode="rb"):
        orig_init(self, path, mode)

    orig_open = imgmod.Source.open

    def patched_open(self):
        if self._fh is None:
            fh = real_open(self.path, "r+b", buffering=0)
            self._fh = Sticky(fh, bad_off)
        return self

    imgmod.Source.open = patched_open
    try:
        rep = surface_test(p, 0, 2 * 1024 * 1024)
    finally:
        imgmod.Source.open = orig_open
    check("dead_sector_detected", not rep.clean and rep.verify_errors > 0,
          f"verify_errors={rep.verify_errors} verdict={rep.verdict()[:70]}")


def test_capacity_honest_drive() -> None:
    p = os.path.join(WORK, "c01.img")
    fx.blank_image(p, 16)
    rep = capacity_test(p)
    check("capacity_honest_drive",
          rep.verified and not rep.is_fake
          and rep.genuine_bytes == rep.advertised_bytes,
          f"fake={rep.is_fake} genuine={rep.genuine_bytes} adv={rep.advertised_bytes}")


def test_capacity_fake_drive_detected() -> None:
    """A drive advertising 32 MB but able to address only 8 MB."""
    p = os.path.join(WORK, "c02.img")
    wrap(p, real_mb=8, fake_mb=32)
    mod = install_fake_flash(8 * 1024 * 1024)
    try:
        rep = capacity_test(p)
    finally:
        restore_source()
    rep.advertised_bytes = 32 * 1024 * 1024
    check("capacity_fake_drive_detected",
          rep.is_fake and 7 * 1024 * 1024 <= rep.genuine_bytes <= 9 * 1024 * 1024,
          f"fake={rep.is_fake} genuine={rep.genuine_bytes / 1048576:.1f}MB "
          f"adv={rep.advertised_bytes / 1048576:.1f}MB "
          f"wrap={rep.wrap_offset} notes={rep.notes[:2]}")


def test_capacity_sampled_catches_wrap() -> None:
    """The quick (sampled) mode must still catch a wrapping drive."""
    p = os.path.join(WORK, "c03.img")
    wrap(p, real_mb=8, fake_mb=64)
    install_fake_flash(8 * 1024 * 1024)
    try:
        rep = capacity_test(p, sample_stride=1024 * 1024)
    finally:
        restore_source()
    rep.advertised_bytes = 64 * 1024 * 1024
    check("capacity_sampled_catches_wrap",
          rep.first_bad_offset is not None and rep.first_bad_offset <= 12 * 1024 * 1024,
          f"first_bad={rep.first_bad_offset} sampled={rep.sampled} "
          f"notes={rep.notes[:1]}")


def test_pattern_is_deterministic() -> None:
    a = make_pattern(4096, 4096)
    b = make_pattern(4096, 4096)
    c = make_pattern(8192, 4096)
    check("pattern_is_deterministic", a == b and a != c,
          f"same={a == b} different_at_offset={a != c}")


def test_write_failure_aborts_cleanly() -> None:
    """A read-only target must produce an honest refusal, not a fake pass."""
    p = os.path.join(WORK, "s03.img")
    fx.blank_image(p, 4)
    os.chmod(p, 0o444)
    try:
        try:
            rep = surface_test(p, 0, 1024 * 1024)
            ok = bool(rep.aborted) and not rep.clean
            detail = rep.aborted[:60]
        except PermissionError as exc:
            # an unopenable target must be reported, not crash the tool
            ok = True
            detail = f"PermissionError surfaced cleanly: {exc}"
        check("write_failure_aborts_cleanly", ok, detail)
    finally:
        os.chmod(p, 0o644)


_ORIG_OPEN = None


def restore_source() -> None:
    from flashphysician.core import imaging as imgmod
    if _ORIG_OPEN is not None:
        imgmod.Source.open = _ORIG_OPEN


def _capture_original():
    global _ORIG_OPEN
    from flashphysician.core import imaging as imgmod
    _ORIG_OPEN = imgmod.Source.open


def main() -> int:
    os.makedirs(IMG, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    print("FlashPhysician -- surface + capacity test suite\n")
    _capture_original()
    for fn in (test_pattern_is_deterministic, test_healthy_surface_passes,
               test_dead_sector_detected, test_write_failure_aborts_cleanly,
               test_capacity_honest_drive, test_capacity_fake_drive_detected,
               test_capacity_sampled_catches_wrap):
        try:
            fn()
        except Exception:
            import traceback
            check(fn.__name__, False, "EXCEPTION: " + traceback.format_exc())
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "-" * 64)
    print(f"  {passed}/{len(results)} passed")
    for name, ok, why in results:
        if not ok:
            print(f"    {name}: {why}")
    print("-" * 64)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
