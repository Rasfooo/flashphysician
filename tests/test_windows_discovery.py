#!/usr/bin/env python3
"""Drive discovery on Windows: the bug that made `list` useless.

Reported symptom, in the user's words: "the flash drive is not detected --
some show the format prompt when plugged in, others do not, and the tool
detects neither."

Root cause was two independent defects, both in the Windows path:

1. `cmd_list` and `gui.refresh_drives` both called `discover_drives()` from
   core/imaging.py. That function reads `/sys/block`, which does not exist off
   Linux, so it returned an empty list every time. On Windows `list` printed
   "No drives detected" regardless of what was plugged in.
2. The Windows backend's `volumes()` filtered
   `Get-Volume | Where DriveType -eq 'Removable'`. A flash drive with no
   partition table or a RAW filesystem has *no volume at all*, so it could
   never appear -- it is the drive Windows offers to format. Drives that
   report themselves as `Fixed` (most USB-SATA/IDE bridges, card readers, and
   plenty of cheap sticks) were also discarded.

These tests pin the behaviour that fixes it, using a fake PowerShell so they
run anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def _fake_ps_factory(disks, volumes, partitions):
    """Stand in for _ps() by answering the three queries the backend makes.

    Matching on the leading cmdlet keeps this honest: the backend must ask
    Get-Disk for disks and Get-Volume for volumes, and a test that ignored
    which cmdlet was called would not catch the original bug (which asked
    Get-Volume and called the answer a disk list)."""
    def fake_ps(script: str, timeout: int = 120):
        class R:
            ok = True
            returncode = 0
            stderr = ""
            stdout = ""
        r = R()
        s = script.strip()
        if s.startswith("Get-Disk"):
            r.stdout = json.dumps(disks)
        elif s.startswith("Get-Volume"):
            r.stdout = json.dumps(volumes)
        elif s.startswith("Get-Partition"):
            r.stdout = json.dumps(partitions)
        else:
            r.ok = False
            r.stdout = ""
        return r
    return fake_ps


def _load_backend(monkeypatch):
    import importlib
    from flashphysician.backends import windows as w
    importlib.reload(w)
    return w


def run_case(label, disks, volumes, partitions, expect_disk_numbers):
    from flashphysician.backends import windows as w
    orig = w._ps
    w._ps = _fake_ps_factory(disks, volumes, partitions)
    try:
        rows = w.WindowsBackend().discover()
    finally:
        w._ps = orig
    got = sorted(r["disk_number"] for r in rows)
    check(label, got == sorted(expect_disk_numbers),
          f"disks={got} expected={sorted(expect_disk_numbers)}")
    return {r["disk_number"]: r for r in rows}


def main() -> int:
    print("=" * 72)
    print("FlashPhysician -- Windows drive discovery")
    print("=" * 72)
    print("\nGet-Disk is the source of truth, not Get-Volume")

    # -- the reported case: a drive Windows wants to format -------------------
    # No partition table (PartitionStyle RAW), no partition, no volume, no
    # letter. This is the drive the user plugs in and gets "you need to format
    # the disk before you can use it". The tool must still list it.
    rows = run_case(
        "RAW/unformatted USB drive is detected",
        disks=[{"Number": 1, "FriendlyName": "USB Flash Disk",
                "Size": 8_000_000_000, "BusType": "USB",
                "PartitionStyle": "RAW", "IsReadOnly": False,
                "IsOffline": False, "HealthStatus": "Healthy"}],
        volumes=[],
        partitions=[],
        expect_disk_numbers=[1],
    )
    if 1 in rows:
        r = rows[1]
        check("RAW drive reports no volume", r["volume"] is None)
        check("RAW drive reports no letter", r["letters"] == [])
        check("RAW drive keeps partition style visible",
              (r["partition_style"] or "").upper() == "RAW")

    # -- a drive that reports itself as Fixed, not Removable -----------------
    # The old filter (`DriveType -eq 'Removable'`) discarded these even though
    # they are flash drives and work fine.
    run_case(
        "USB drive reporting DriveType Fixed is detected",
        disks=[{"Number": 2, "FriendlyName": "USB DISK 3.0",
                "Size": 16_000_000_000, "BusType": "USB",
                "PartitionStyle": "MBR", "IsReadOnly": False,
                "IsOffline": False, "HealthStatus": "Healthy"}],
        volumes=[{"DriveLetter": "E", "FileSystemLabel": "SANDBOX",
                  "FileSystem": "FAT32", "DriveType": "Fixed",
                  "Size": 16_000_000_000, "HealthStatus": "Healthy"}],
        partitions=[{"DiskNumber": 2, "PartitionNumber": 1, "DriveLetter": "E",
                     "Size": 16_000_000_000, "Type": "IFS"}],
        expect_disk_numbers=[2],
    )

    # -- volume with no drive letter ----------------------------------------
    # Letterless volumes were dropped because identity came from DriveLetter.
    rows = run_case(
        "letterless volume is still detected",
        disks=[{"Number": 3, "FriendlyName": "Generic STORAGE",
                "Size": 32_000_000_000, "BusType": "USB",
                "PartitionStyle": "MBR", "IsReadOnly": False,
                "IsOffline": False, "HealthStatus": "Healthy"}],
        volumes=[{"DriveLetter": None, "FileSystemLabel": "",
                  "FileSystem": "", "DriveType": "Fixed",
                  "Size": 32_000_000_000, "HealthStatus": "Healthy"}],
        partitions=[{"DiskNumber": 3, "PartitionNumber": 1, "DriveLetter": None,
                     "Size": 32_000_000_000, "Type": "IFS"}],
        expect_disk_numbers=[3],
    )

    # -- several drives at once: the camera-card case ------------------------
    run_case(
        "multiple removable drives all appear",
        disks=[{"Number": n, "FriendlyName": f"Card {n}", "Size": 4_000_000_000,
                "BusType": "USB", "PartitionStyle": "MBR", "IsReadOnly": False,
                "IsOffline": False, "HealthStatus": "Healthy"}
               for n in (0, 1, 2)],
        volumes=[{"DriveLetter": "F", "FileSystem": "exFAT", "DriveType": "Removable",
                  "Size": 4_000_000_000, "HealthStatus": "Healthy",
                  "FileSystemLabel": "DCIM"}],
        partitions=[{"DiskNumber": 2, "PartitionNumber": 1, "DriveLetter": "F",
                     "Size": 4_000_000_000, "Type": "IFS"}],
        expect_disk_numbers=[0, 1, 2],
    )

    print("\nvolumes() no longer over-filters")
    from flashphysician.backends import windows as w
    orig = w._ps
    try:
        w._ps = _fake_ps_factory([], [
            {"DriveLetter": "E", "DriveType": "Fixed", "Size": 1000,
             "FileSystem": "FAT32"},
            {"DriveLetter": "F", "DriveType": "Removable", "Size": 2000,
             "FileSystem": "exFAT"},
            {"DriveLetter": None, "DriveType": "Fixed", "Size": 3000,
             "FileSystem": ""},
            {"DriveLetter": "D", "DriveType": "CDROM", "Size": 700,
             "FileSystem": "CDFS"},
        ], [])
        vols = w.WindowsBackend().volumes()
    finally:
        w._ps = orig
    check("Fixed drives are kept", sum(1 for v in vols
                                       if v.get("DriveType") == "Fixed") == 2)
    check("Removable drives are kept",
          any(v.get("DriveType") == "Removable" for v in vols))
    check("letterless volume is kept",
          any(v.get("DriveLetter") is None for v in vols))
    check("optical media is dropped",
          not any(v.get("DriveType") == "CDROM" for v in vols))

    print("\nlist_devices() exposes a usable path for every row")
    orig = w._ps
    try:
        w._ps = _fake_ps_factory(
            [{"Number": 1, "FriendlyName": "RAW Stick", "Size": 8_000_000_000,
              "BusType": "USB", "PartitionStyle": "RAW", "IsReadOnly": False,
              "IsOffline": False, "HealthStatus": "Healthy"}],
            [], [])
        devs = w.WindowsBackend().list_devices()
    finally:
        w._ps = orig
    check("a RAW drive still yields a device entry", len(devs) == 1)
    if devs:
        p = devs[0]["path"]
        check("path is openable without a drive letter",
              p.startswith("\\\\.\\PhysicalDrive"), p)
        check("size is reported for the RAW drive",
              devs[0]["size"] == 8_000_000_000)

    print("\nthe CLI does not use the Linux-only enumeration on Windows")
    cli_src = (ROOT / "flashphysician" / "cli.py").read_text()
    check("cmd_list dispatches on sys.platform",
          "sys.platform" in cli_src and "_list_windows" in cli_src,
          "without this, /sys/block is consulted and always returns nothing")
    check("cmd_list has a Windows path",
          "def _list_windows" in cli_src)
    gui_src = (ROOT / "flashphysician" / "gui.py").read_text()
    check("gui refresh_drives dispatches on sys.platform",
          "sys.platform" in gui_src and "_refresh_drives_windows" in gui_src)
    check("gui imports sys",
          "\nimport sys" in gui_src)

    print()
    print("=" * 72)
    total = PASS + FAIL
    print(f"  {PASS}/{total} checks passed")
    print("  discovery is correct on Windows" if FAIL == 0
          else f"  {FAIL} CHECK(S) FAILED")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())