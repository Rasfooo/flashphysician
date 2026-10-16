"""Recovery tests: prove the recovered bytes are the original bytes.

The assertion that matters is byte equality against the payload the fixture
wrote -- a recovery tool that returns *a* file of roughly the right size but
the wrong content is worse than useless, because the user only finds out
after wiping the source.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests import fixtures as fx                                       # noqa: E402
from usbdoctor.core.imaging import Source                              # noqa: E402
from usbdoctor.core.structures import (                                # noqa: E402
    parse_boot_sector, read_fat_entries,
)
from usbdoctor.recovery.files import (                                 # noqa: E402
    ATTR_ARCHIVE, ATTR_HIDDEN, carve_files, extract_fat_file,
    recover_files, walk_fat_directory,
)

IMG = os.path.join(ROOT, "tests", "_images")
WORK = os.path.join(ROOT, "tests", "_work")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, why: str = "") -> None:
    results.append((name, ok, why))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  << {why}" if not ok else ""))


def load(path):
    with Source(path) as src:
        info = parse_boot_sector(src.read_at(0, 512), 0)
        fat = read_fat_entries(src, info, 0, limit=262144)
        entries = walk_fat_directory(src, info, 0, fat)
    return info, fat, entries


def test_live_file_recovered_exactly() -> None:
    p = os.path.join(WORK, "f01.img")
    fx.make_fat32_with_files(p, 1024)
    _, fat, entries = load(p)
    live = [e for e in entries if e.name.upper().startswith("LIVE")]
    if not live:
        return check("live_file_recovered_exactly", False, "LIVE.TXT not enumerated")
    with Source(p) as src:
        info = parse_boot_sector(src.read_at(0, 512), 0)
        data = extract_fat_file(src, info, 0, live[0], fat)
    want = b"This is the live file: USB Doctor recovery test.\n" * 40
    check("live_file_recovered_exactly", data == want,
          f"len {len(data)} vs {len(want)} equal={data == want}")


def test_hidden_file_detected_and_readable() -> None:
    p = os.path.join(WORK, "f02.img")
    fx.make_fat32_with_files(p, 1024, hidden=True)
    _, fat, entries = load(p)
    secret = [e for e in entries if e.name.upper().startswith("SECRET")]
    if not secret:
        return check("hidden_file_detected_and_readable", False, "SECRET.TXT missing")
    e = secret[0]
    ok_flag = "hidden" in e.flags
    with Source(p) as src:
        info = parse_boot_sector(src.read_at(0, 512), 0)
        data = extract_fat_file(src, info, 0, e, fat)
    want = b"Hidden file content, attribute byte says HIDDEN.\n" * 40
    check("hidden_file_detected_and_readable", ok_flag and data == want,
          f"flags={e.flags} bytes_match={data == want}")


def test_deleted_entry_recovered_via_contiguous_fallback() -> None:
    """A deleted file whose clusters were freed: the FAT no longer chains it,
    but the data is still there and normally contiguous.

    Note the first character of a deleted file's name is gone by definition --
    FAT overwrites it with 0xE5 -- so the entry is found by its deleted flag,
    not by name.
    """
    p = os.path.join(WORK, "f03.img")
    fx.make_fat32_with_files(p, 1024, deleted=True)
    _, fat, entries = load(p)
    gone = [e for e in entries if e.deleted]
    if not gone:
        return check("deleted_entry_recovered_via_contiguous_fallback", False,
                     f"no deleted entry enumerated ({[e.name for e in entries]})")
    e = gone[0]
    # clear the FAT chain for its cluster, exactly as a delete would
    with open(p, "r+b") as fh:
        fh.seek(32 * 512 + 5 * 4)
        fh.write(b"\x00\x00\x00\x00")
    with Source(p) as src:
        info = parse_boot_sector(src.read_at(0, 512), 0)
        fat2 = read_fat_entries(src, info, 0, limit=262144)
        data = extract_fat_file(src, info, 0, e, fat2)
    want = b"DELETED FILE CONTENT -- should come back intact.\n" * 40
    check("deleted_entry_recovered_via_contiguous_fallback",
          e.deleted and data == want,
          f"deleted={e.deleted} name={e.name!r} bytes_match={data == want} "
          f"len={len(data)}")


def test_multicluster_chain_followed() -> None:
    """A non-contiguous file must be reassembled by following the FAT, not by
    assuming the clusters are sequential -- which is where naive tools fail."""
    p = os.path.join(WORK, "f04.img")
    fx.make_fat32_with_multicluster_file(p, 1024, 6)
    _, fat, entries = load(p)
    chain = [e for e in entries if e.name.upper().startswith("CHAIN")]
    if not chain:
        return check("multicluster_chain_followed", False, "CHAIN.BIN not enumerated")
    e = chain[0]
    with Source(p) as src:
        info = parse_boot_sector(src.read_at(0, 512), 0)
        data = extract_fat_file(src, info, 0, e, fat)
    # rebuild the expected payload the same way the fixture wrote it
    expect = bytearray()
    csize = info.cluster_size
    for i, c in enumerate([4, 9, 5, 12, 6, 20]):
        block = (f"cluster {i} (real cluster {c}) ".encode() * 8).ljust(csize, b".")
        expect += block[:csize]
    expect = bytes(expect[: e.size])
    check("multicluster_chain_followed", data == expect and e.size > csize * 3,
          f"size={e.size} cluster_size={csize} bytes_match={data == expect}")


def test_subdirectory_traversed() -> None:
    p = os.path.join(WORK, "f05.img")
    fx.make_fat32_with_files(p, 1024)
    _, fat, entries = load(p)
    nested = [e for e in entries if e.name.upper().startswith("NESTED")]
    check("subdirectory_traversed", bool(nested),
          f"entries={[e.name for e in entries]}")


def test_full_recovery_writes_files() -> None:
    p = os.path.join(WORK, "f06.img")
    dest = os.path.join(WORK, "out_f06")
    fx.make_fat32_with_files(p, 1024, deleted=True, hidden=True)
    rep = recover_files(p, dest)
    names = sorted(os.path.basename(n) for n, _ in rep.recovered)
    want_payloads = {
        b"This is the live file: USB Doctor recovery test.\n" * 40,
        b"DELETED FILE CONTENT -- should come back intact.\n" * 40,
        b"Hidden file content, attribute byte says HIDDEN.\n" * 40,
        b"Nested file inside a subdirectory.\n" * 40,
    }
    got = set()
    for n, _ in rep.recovered:
        got.add(open(n, "rb").read())
    check("full_recovery_writes_files",
          got == want_payloads and len(rep.recovered) == 4,
          f"recovered={len(rep.recovered)} names={names} "
          f"payloads_match={got == want_payloads}")


def test_recovery_is_readonly_on_source() -> None:
    p = os.path.join(WORK, "f07.img")
    dest = os.path.join(WORK, "out_f07")
    fx.make_fat32_with_files(p, 1024)
    before = open(p, "rb").read()
    recover_files(p, dest)
    after = open(p, "rb").read()
    check("recovery_is_readonly_on_source", before == after,
          f"source changed={before != after}")


def test_carve_png_jpg_pdf() -> None:
    p = os.path.join(WORK, "f08.img")
    dest = os.path.join(WORK, "out_f08")
    fx.make_carve_target(p)
    rep = carve_files(p, dest, types=["png", "jpg", "pdf"])
    exts = sorted(os.path.splitext(n)[1] for n, _ in rep.carved)
    check("carve_png_jpg_pdf", exts == [".jpg", ".pdf", ".png"],
          f"carved={exts} notes={rep.notes[:1]}")


def test_carve_reconstructs_valid_body() -> None:
    p = os.path.join(WORK, "f09.img")
    dest = os.path.join(WORK, "out_f09")
    fx.make_carve_target(p)
    rep = carve_files(p, dest, types=["png"])
    if not rep.carved:
        return check("carve_reconstructs_valid_body", False, "nothing carved")
    data = open(rep.carved[0][0], "rb").read()
    ok = data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xAE\x42\x60\x82")
    check("carve_reconstructs_valid_body", ok,
          f"len={len(data)} head={data[:8]!r} tail={data[-8:]!r}")


def test_no_fat_reports_honestly() -> None:
    """Carving target has no FAT at all: recover_files must say so instead of
    silently returning nothing."""
    p = os.path.join(WORK, "f10.img")
    dest = os.path.join(WORK, "out_f10")
    fx.make_carve_target(p)
    rep = recover_files(p, dest)
    ok = (not rep.recovered) and any("carving" in n for n in rep.notes)
    check("no_fat_reports_honestly", ok, f"notes={rep.notes}")


def main() -> int:
    os.makedirs(IMG, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    print("USB Doctor -- file recovery test suite\n")
    for fn in (test_live_file_recovered_exactly, test_hidden_file_detected_and_readable,
               test_deleted_entry_recovered_via_contiguous_fallback,
               test_multicluster_chain_followed, test_subdirectory_traversed,
               test_full_recovery_writes_files, test_recovery_is_readonly_on_source,
               test_carve_png_jpg_pdf, test_carve_reconstructs_valid_body,
               test_no_fat_reports_honestly):
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
