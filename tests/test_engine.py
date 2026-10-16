"""End-to-end proof that the engine names each failure correctly.

Every fixture is a byte-exact image of a real-world failure mode. The test
asserts both the health verdict *and* the specific diagnostic code, so a
regression in detection cannot pass silently.

Run:  python3 tests/test_engine.py            (quiet summary)
      python3 tests/test_engine.py -v         (full reports)
"""

from __future__ import annotations

import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests import fixtures as fx                                     # noqa: E402
from flashphysician.core.diagnose import (                                # noqa: E402
    Severity, format_report, quick_diagnose,
)
from flashphysician.core.structures import TableType, FsType              # noqa: E402

IMG = os.path.join(ROOT, "tests", "_images")

VERBOSE = "-v" in sys.argv

results: list[tuple[str, bool, str]] = []


def case(name: str, builder, expects_fs=None, expects_table=None,
         expects_health_lt=None, expects_health_gte=None,
         expects_codes=(), forbids_codes=(), expects_severity=None) -> None:
    path = os.path.join(IMG, f"{name}.img")
    ok = True
    why: list[str] = []
    try:
        builder(path)
        rep = quick_diagnose(path)

        if expects_fs is not None and rep.fs and rep.fs.fs != expects_fs:
            ok = False
            why.append(f"fs: got {rep.fs.fs.value}, want {expects_fs.value}")
        if expects_table is not None and rep.table != expects_table:
            ok = False
            why.append(f"table: got {rep.table.value}, want {expects_table.value}")
        if expects_health_lt is not None and not rep.health < expects_health_lt:
            ok = False
            why.append(f"health {rep.health} not < {expects_health_lt}")
        if expects_health_gte is not None and not rep.health >= expects_health_gte:
            ok = False
            why.append(f"health {rep.health} not >= {expects_health_gte}")

        codes = {f.code for f in rep.findings}
        for c in expects_codes:
            if c not in codes:
                ok = False
                why.append(f"missing diagnostic code {c}")
        for c in forbids_codes:
            if c in codes:
                ok = False
                why.append(f"unexpected diagnostic code {c}")
        if expects_severity is not None and rep.worst != expects_severity:
            ok = False
            why.append(f"worst severity {rep.worst.value}, want {expects_severity.value}")

        if VERBOSE:
            print(format_report(rep))
    except Exception:
        ok = False
        why.append("EXCEPTION:\n" + traceback.format_exc())

    results.append((name, ok, "; ".join(why)))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  << {why}" if not ok else ""))


def main() -> int:
    os.makedirs(IMG, exist_ok=True)
    print("FlashPhysician -- engine test suite")
    print(f"images in {IMG}\n")

    print("partition tables")
    case("t01_healthy_gpt", lambda p: fx.build_gpt(p, 256, 2),
         expects_table=TableType.GPT, expects_health_gte=75,
         expects_codes={"GPT_CRC_OK", "GPT_BACKUP_OK", "PARTS"})

    case("t02_gpt_primary_crc_bad", lambda p: fx.build_gpt(p, 64, 2, break_primary_crc=True),
         expects_codes={"GPT_HEADER"}, expects_health_lt=85)

    case("t03_gpt_entries_crc_bad", lambda p: fx.build_gpt(p, 64, 2, break_entries_crc=True),
         expects_codes={"GPT_ENTRIES"}, expects_health_lt=85)

    case("t04_gpt_no_backup", lambda p: fx.build_gpt(p, 64, 2, no_backup=True),
         expects_codes={"GPT_BACKUP_MISSING"}, expects_health_lt=90)

    case("t05_blank_device", lambda p: fx.make_raw_wiped(p, 32),
         expects_table=TableType.NONE, expects_fs=FsType.RAW,
         expects_codes={"TABLE_MISSING", "FS_RAW"}, expects_health_lt=20)

    case("t06_lost_partition_table", lambda p: fx.make_lost_partition_table(p, 1024),
         expects_table=TableType.NONE,
         expects_codes={"TABLE_MISSING", "FS_ORPHAN"},
         expects_health_gte=40)

    case("t07_oversized_partition", lambda p: fx.make_oversized_partition(p, 16),
         expects_codes={"MBR_ENTRY"}, expects_health_lt=90)

    print("\nFAT family")
    case("f01_healthy_fat32", lambda p: fx.make_fat32(p, 1024),
         expects_fs=FsType.FAT32, expects_table=TableType.MBR,
         expects_codes={"FAT_COPIES_OK", "FAT_CLEAN", "FAT_BOOTBACKUP_OK", "FSINFO_OK"},
         expects_health_gte=85)

    case("f02_fat32_dirty", lambda p: fx.make_dirty_fat32(p, 1024),
         expects_fs=FsType.FAT32, expects_codes={"FAT_DIRTY"}, expects_health_lt=95)

    case("f03_bad_clusters", lambda p: fx.make_bad_clusters(p, 1024),
         expects_fs=FsType.FAT32, expects_codes={"FAT_BADCLUSTER"}, expects_health_lt=95)

    case("f04_fat_copies_mismatch", lambda p: fx.make_fat_copies_mismatch(p, 1024),
         expects_fs=FsType.FAT32, expects_codes={"FAT_COPIES_DIFF"}, expects_health_lt=90)

    case("f05_boot_backup_mismatch", lambda p: fx.make_fat32(p, 1024, break_backup=True),
         expects_codes={"FAT_BOOTBACKUP_MISMATCH"}, expects_health_lt=95)

    case("f06_fat_type_mismatch", lambda p: fx.make_fat_type_mismatch(p, 1024),
         expects_codes={"FAT_TYPE_MISMATCH"}, expects_health_lt=85)

    case("f07_healthy_fat16", lambda p: fx.make_fat16(p, 16),
         expects_fs=FsType.FAT16, expects_health_gte=80)

    case("f08_fat32_corrupt_signature", lambda p: fx.make_fat32(p, 1024, corrupt_sig=True),
         expects_table=TableType.NONE, expects_codes={"TABLE_MISSING"}, expects_health_lt=40)

    case("f09_out_of_range_fat", lambda p: fx.make_fat32(p, 1024, out_of_range=[5, 6, 7]),
         expects_codes={"FAT_RANGE"}, expects_health_lt=90)

    print("\nexFAT and NTFS")
    case("e01_healthy_exfat", lambda p: fx.make_exfat(p, 64),
         expects_fs=FsType.EXFAT, expects_codes={"EXFAT_CKSUM_OK"}, expects_health_gte=80)

    case("e02_exfat_bad_checksum", lambda p: fx.make_exfat(p, 64, corrupt_checksum=True),
         expects_fs=FsType.EXFAT, expects_codes={"EXFAT_CKSUM"}, expects_health_lt=85)

    case("e03_exfat_dirty", lambda p: fx.make_exfat(p, 64, dirty=True),
         expects_fs=FsType.EXFAT, expects_codes={"EXFAT_CKSUM_OK"}, expects_health_lt=95)

    case("n01_healthy_ntfs", lambda p: fx.make_ntfs(p, 64),
         expects_fs=FsType.NTFS, expects_codes={"NTFS_MFT_OK", "NTFS_SIG"},
         expects_health_gte=80)

    case("n02_ntfs_bad_mft", lambda p: fx.make_ntfs(p, 64, good_mft=False),
         expects_fs=FsType.NTFS, expects_codes={"NTFS_MFT_BAD"}, expects_health_lt=90)

    case("n03_ntfs_bad_signature", lambda p: fx.make_ntfs(p, 64, valid_sig=False),
         expects_fs=FsType.NTFS, expects_codes={"NTFS_SIG"}, expects_health_lt=95)

    print("\ndegenerate devices")
    case("d01_zero_size", lambda p: fx.make_zero_size_fake(p),
         expects_codes={"DEAD_ZERO"}, expects_health_lt=5)

    print("\n" + "-" * 64)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"  {passed}/{total} passed")
    if passed != total:
        print("\n  failures:")
        for name, ok, why in results:
            if not ok:
                print(f"    {name}: {why}")
    print("-" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())