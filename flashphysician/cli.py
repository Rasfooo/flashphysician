"""Command line interface.

Design rule for every destructive action: **the default is the safe one.**
Without an explicit `--yes`, a repair prints exactly what it would write and
stops. That is the difference between a rescue tool and a data-loss tool.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__
from .backends.base import run as run_cmd
from .core.diagnose import (
    Severity, format_report, quick_diagnose, search_boot_candidates,
)
from .core.imaging import copy_range, discover_drives
from .core.repair import (
    guard_target, plan_clear_fat_dirty, plan_fix_fat_type_label,
    plan_rebuild_mbr_from_fs, plan_restore_fat32_boot_backup,
    plan_restore_gpt_from_backup, plan_restore_mbr_signature,
)
from .core.structures import FsType, human
from .recovery.files import carve_files, recover_files
from .recovery.surface import capacity_test, surface_test

BANNER = r"""
  _   _ ___ ___  ___   ___   ___   ___ _____ ___ ___
 | | | / __| _ )/ _ \ /_\ |   \ / _ \/ __|_   _/ _ \
 | |_| \__ \ _ \ (_) / _ \| |) | (_) \__ \ | || (_) |
  \___/|___/___/\___/_/ \_\___/ \___/|___/ |_| \___/
"""


def _progress_printer(label: str):
    last = [0.0]

    def cb(done, total, stage=""):
        now = time.time()
        if now - last[0] < 0.4 and done < total:
            return
        last[0] = now
        pct = (done / total * 100) if total else 0
        bar = "#" * int(pct / 2.5) + "." * (40 - int(pct / 2.5))
        sys.stderr.write(f"\r  {label:12} [{bar}] {pct:5.1f}%  {stage[:28]:28}")
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")

    return cb


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flashphysician",
        description="Diagnose, repair and recover USB flash drives, SD cards "
                    "and other removable media.",
        epilog="Run `flashphysician <command> --help` for details on any command. "
               "Every write is backed up and printed before it happens unless "
               "--yes is given.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version",
                   version=f"flashphysician {__version__}")
    # accepted both before and after the subcommand, since people type it in
    # both positions and argparse would otherwise reject the one they chose
    p.add_argument("--no-color", action="store_true", help="plain text output")
    p.add_argument("--debug", action="store_true",
                   help="show full tracebacks instead of a short error")
    no_color_parent = argparse.ArgumentParser(add_help=False)
    no_color_parent.add_argument("--no-color", action="store_true",
                                 help="plain text output")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    # list -------------------------------------------------------------
    sp = sub.add_parser("list", help="list removable drives", parents=[no_color_parent])
    sp.add_argument("--all", action="store_true",
                    help="include internal disks (read-only inspection)")
    sp.set_defaults(func=cmd_list)

    # info -------------------------------------------------------------
    sp = sub.add_parser("info", help="show what this system can do")
    sp.set_defaults(func=cmd_info)

    # diagnose ---------------------------------------------------------
    sp = sub.add_parser("diagnose", aliases=["diag", "check"],
                        parents=[no_color_parent],
                        help="read-only health report for a device")
    sp.add_argument("device")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.add_argument("--save", metavar="FILE", help="also write the report to a file")
    sp.set_defaults(func=cmd_diagnose)

    # scan -------------------------------------------------------------
    sp = sub.add_parser("scan", help="surface test: find bad sectors", parents=[no_color_parent])
    sp.add_argument("device")
    sp.add_argument("--size", metavar="MB", type=int, default=64,
                    help="megabytes to test (default 64; 0 = whole device)")
    sp.add_argument("--offset", metavar="MB", type=int, default=0)
    sp.add_argument("--restore", action="store_true",
                    help="save and restore the original contents")
    sp.add_argument("--yes", action="store_true",
                    help="confirm the write test (required; it destroys data "
                         "in the tested range unless --restore is used)")
    sp.set_defaults(func=cmd_scan)

    # capacity ---------------------------------------------------------
    sp = sub.add_parser("capacity", help="detect fake/overreported flash", parents=[no_color_parent])
    sp.add_argument("device")
    sp.add_argument("--sample", metavar="MB", type=int, default=0,
                    help="sample one block every N MB (fast); 0 = full scan")
    sp.add_argument("--yes", action="store_true", help="confirm the write test")
    sp.set_defaults(func=cmd_capacity)

    # repair -----------------------------------------------------------
    sp = sub.add_parser("repair", help="repair partition table / boot sector", parents=[no_color_parent])
    sp.add_argument("device")
    sp.add_argument("--yes", action="store_true",
                    help="actually write (without this, only a plan is shown)")
    sp.add_argument("--force", action="store_true",
                    help="allow writing to a whole block device")
    sp.add_argument("--backup-dir", metavar="DIR",
                    help="where to save the sectors being overwritten")
    sp.set_defaults(func=cmd_repair)

    # fix --------------------------------------------------------------
    sp = sub.add_parser("fix", help="run the OS filesystem checker", parents=[no_color_parent])
    sp.add_argument("device")
    sp.add_argument("--apply", action="store_true",
                    help="repair (default is a read-only check)")
    sp.set_defaults(func=cmd_fix)

    # recover ----------------------------------------------------------
    sp = sub.add_parser("recover", help="recover files from a damaged volume", parents=[no_color_parent])
    sp.add_argument("device")
    sp.add_argument("--to", metavar="DIR", required=True,
                    help="destination directory (never the source device)")
    sp.add_argument("--deleted-only", action="store_true")
    sp.add_argument("--no-deleted", action="store_true")
    sp.add_argument("--carve", action="store_true",
                    help="also carve files by signature (last resort)")
    sp.set_defaults(func=cmd_recover)

    # image ------------------------------------------------------------
    sp = sub.add_parser("image", parents=[no_color_parent],
                        help="copy a device to an image file, "
                             "tolerating bad sectors")
    sp.add_argument("device")
    sp.add_argument("--to", metavar="FILE", required=True)
    sp.add_argument("--size", metavar="MB", type=int, default=0,
                    help="limit the copy (0 = whole device)")
    sp.set_defaults(func=cmd_image)

    # eject ------------------------------------------------------------
    sp = sub.add_parser("eject", help="flush and safely remove a device", parents=[no_color_parent])
    sp.add_argument("device")
    sp.set_defaults(func=cmd_eject)

    return p


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_info(args) -> int:
    from .backends.linux import get_backend
    caps = get_backend().capabilities()
    print(BANNER)
    print(f"  flashphysician {__version__}\n")
    print("  This system:")
    for line in caps.summary().splitlines():
        print("    " + line)
    if caps.notes:
        print("\n  Notes:")
        for n in caps.notes:
            print(f"    - {n}")
    print("\n  Commands: list, diagnose, scan, capacity, repair, fix, "
          "recover, image, eject")
    return 0


def cmd_list(args) -> int:
    drives = [d for d in discover_drives(include_system=args.all)
              if not d.is_loop]
    if not drives:
        print("  No drives detected.")
        if not getattr(args, "all", False):
            print("  (internal disks are hidden by default; try --all)")
        return 1
    flash = [d for d in drives if d.is_flash and not d.is_loop]
    others = [d for d in drives if d not in flash]
    print(f"\n  {'DEVICE':<14} {'SIZE':>9}  {'BUS':<6} {'FS':<7} "
          f"{'LABEL':<14} DESCRIPTION")
    print("  " + "-" * 78)
    for d in flash + others:
        tag = "" if d.is_flash else "  (internal)"
        print(f"  {d.path:<14} {human(d.size_bytes):>9}  {d.bus:<6} "
              f"{d.fstype[:7]:<7} {d.label[:14]:<14} {d.describe()[:28]}{tag}")
    print(f"\n  {len(flash)} removable device(s).")
    return 0


def cmd_diagnose(args) -> int:
    dev = args.device
    if not os.path.exists(dev):
        print(f"  error: {dev} does not exist", file=sys.stderr)
        return 2
    if not os.access(dev, os.R_OK):
        print(f"  error: {dev} is not readable (permissions?)", file=sys.stderr)
        return 2
    print(f"  diagnosing {dev} ...", file=sys.stderr)
    rep = quick_diagnose(dev)
    if args.json:
        import json
        print(json.dumps({
            "device": rep.device, "size": rep.size_bytes,
            "table": rep.table.value,
            "filesystem": rep.fs.fs.value if rep.fs else None,
            "health": rep.health, "grade": rep.grade, "verdict": rep.verdict,
            "findings": [{"severity": f.severity.value, "code": f.code,
                          "title": f.title, "detail": f.detail,
                          "layer": f.layer} for f in rep.findings],
        }, indent=2))
    else:
        print(format_report(rep, color=not args.no_color))
    if args.save:
        Path(args.save).write_text(format_report(rep, color=False))
        print(f"  report saved to {args.save}", file=sys.stderr)
    return 0


def _confirm_write(device: str) -> bool:
    print(f"\n  This writes to {device}. Data in the affected range will be "
          "overwritten.")
    print("  Re-run with --yes to proceed.")
    return False


def cmd_scan(args) -> int:
    dev = args.device
    if not args.yes:
        mb = args.size or "whole"
        print(f"  Surface test of {dev}: writes {mb} MB of test patterns, "
              "then reads them back to verify the medium retains data.")
        if not args.restore:
            print("  Without --restore the tested range is left holding the "
                  "test pattern.")
        return _confirm_write(dev) or 1
    length = (args.size * 1024 * 1024) if args.size else None
    rep = surface_test(dev, start=args.offset * 1024 * 1024, length=length,
                       restore=args.restore,
                       progress=_progress_printer("surface"))
    print(f"\n  {rep.verdict()}")
    print(f"  tested {human(rep.tested_bytes)} in {rep.duration_s:.1f}s")
    for f in rep.faults[:25]:
        print(f"    - {f.describe()}")
    if len(rep.faults) > 25:
        print(f"    ... and {len(rep.faults) - 25} more")
    return 0 if rep.clean else 3


def cmd_capacity(args) -> int:
    dev = args.device
    if not args.yes:
        print(f"  Capacity test of {dev}: writes markers across the device to "
              "prove whether the advertised size is real.")
        print("  This DESTROYS existing data on the device.")
        return _confirm_write(dev) or 1
    stride = args.sample * 1024 * 1024 if args.sample else None
    rep = capacity_test(dev, sample_stride=stride,
                        progress=_progress_printer("capacity"))
    print(f"\n  {rep.verdict()}")
    for n in rep.notes[:6]:
        print(f"    - {n}")
    if rep.is_fake:
        print(f"\n  ! {rep.fake_fraction * 100:.0f}% of the advertised capacity "
              "does not exist. Any data you stored past the real end is gone "
              "or has overwritten older files. This drive cannot be trusted "
              "with data you care about.")
        return 4
    return 0


def cmd_repair(args) -> int:
    dev = args.device
    print(f"  analysing {dev} ...", file=sys.stderr)
    rep = quick_diagnose(dev)
    print(format_report(rep, color=not args.no_color))

    plans = []
    codes = {f.code for f in rep.findings}

    if "MBR_SIG" in codes or "TABLE_MISSING" in codes:
        plans.append(("restore MBR signature", plan_restore_mbr_signature(dev)))
    if "FAT_BOOTBACKUP_MISMATCH" in codes:
        plans.append(("restore FAT32 boot sector from backup",
                      plan_restore_fat32_boot_backup(dev)))
    if "FAT_TYPE_MISMATCH" in codes:
        plans.append(("correct FAT type label",
                      plan_fix_fat_type_label(dev)))
    if "FAT_DIRTY" in codes:
        plans.append(("clear dirty flag", plan_clear_fat_dirty(dev)))
    if "GPT_PROTECTIVE_ONLY" in codes or "GPT_HEADER" in codes:
        plans.append(("restore GPT from backup", plan_restore_gpt_from_backup(dev)))
    if rep.raw.get("orphan_fs_offset") is not None:
        off = rep.raw["orphan_fs_offset"]
        fst = rep.raw.get("orphan_fs_type", "fat32")
        try:
            ftype = FsType(fst)
        except ValueError:
            ftype = FsType.FAT32
        plans.append((f"rebuild partition table pointing at the filesystem "
                      f"at {human(off)}",
                      plan_rebuild_mbr_from_fs(dev, off, ftype)))

    if not plans:
        print("\n  No automatically repairable fault identified.")
        print("  If the drive is not working, try `flashphysician scan` to check the "
              "medium, or `flashphysician fix` for a filesystem check.")
        return 1

    print("\n  " + "=" * 70)
    print("  REPAIR PLAN")
    print("  " + "=" * 70)
    for i, (label, plan) in enumerate(plans, 1):
        print(f"\n  [{i}] {label}")
        if plan.refused:
            print(f"      refused: {plan.refused}")
            continue
        for op in plan.ops:
            print(f"      - {op.describe()}")
        for n in plan.notes:
            print(f"      note: {n}")
        for w in plan.warnings:
            print(f"      WARNING: {w}")

    usable = [(l, p) for l, p in plans if p.ok]
    if not usable:
        print("\n  Nothing can be applied automatically.")
        return 1

    # Several plans can legitimately want to write the same bytes: restoring the
    # MBR signature and rebuilding the partition table both touch sector 0, and
    # the rebuild already produces a valid signature. Size alone cannot rank
    # them (both write 512 bytes), so an explicit precedence is used: a plan
    # that reconstructs a whole structure supersedes one that patches a field.
    SUPERSEDES = {
        "restore MBR signature": {"rebuild partition table"},
        "correct FAT type label": set(),
    }
    if len(usable) > 1:
        drop = set()
        for i, (li, pi) in enumerate(usable):
            i_offs = {op.offset for op in pi.ops}
            for j, (lj, pj) in enumerate(usable):
                if i == j or i in drop or j in drop:
                    continue
                if not i_offs & {op.offset for op in pj.ops}:
                    continue
                superseded = any(want in lj for want in SUPERSEDES.get(li, ()))
                if superseded:
                    drop.add(i)
                    break
        if drop:
            print("\n  note: folded redundant steps into the more complete plan:")
            for i in sorted(drop):
                print(f"    - skipped: {usable[i][0]}  (covered by a wider repair)")
            usable = [u for k, u in enumerate(usable) if k not in drop]

    if not args.yes:
        print(f"\n  {len(usable)} step(s) ready to apply. Nothing has been "
              "written yet.")
        print("  Re-run with --yes to apply. A backup of every sector that "
              "will be overwritten is saved first.")
        return 0

    print("\n  applying ...")
    failed = 0
    for label, plan in usable:
        res = plan.apply(backup_dir=args.backup_dir or "")
        status = "ok" if res["ok"] else "FAILED"
        print(f"    [{status}] {label} "
              f"({res.get('applied', 0)}/{res.get('total', 0)} writes)")
        if res.get("backups"):
            print(f"           backup: {res['backups'][0]}")
        for e in res.get("errors", []):
            print(f"           error: {e}")
            failed += 1

    print("\n  re-checking ...")
    after = quick_diagnose(dev)
    print(f"  health {rep.health} -> {after.health}  ({after.verdict})")
    return 0 if not failed else 5


def cmd_fix(args) -> int:
    from .backends.linux import get_backend
    be = get_backend()
    dev = args.device
    mounts = be.mount_points(dev) if hasattr(be, "mount_points") else []
    if mounts and args.apply:
        print(f"  {dev} is mounted at {', '.join(mounts)} -- unmounting first "
              "(a checker must never run on a mounted volume).")
        be.unmount(dev)
    print(f"  running filesystem check ({'repair' if args.apply else 'read-only'}) "
          f"on {dev} ...")
    res = be.fsck(dev, fix=args.apply)
    print("\n  " + "-" * 60)
    for line in res.combined.splitlines()[:60]:
        print("  " + line)
    print("  " + "-" * 60)
    if res.missing:
        print(f"  not run: {res.stderr}")
        return 2
    if not args.apply and res.returncode in (4, 8):
        print("  Errors were found. Re-run with --apply to repair them.")
        return 3
    return 0 if res.returncode < 4 else 4


def cmd_recover(args) -> int:
    dev, dest = args.device, args.to
    if not os.path.exists(dev):
        print(f"  error: {dev} does not exist", file=sys.stderr)
        return 2

    # Writing recovered files back onto the device we are reading is the one
    # mistake that destroys the data being rescued. A plain string-prefix test
    # is not enough: the real risk is the device's MOUNT POINT, which is where
    # the writes would actually land.
    dest_abs = os.path.abspath(dest)
    src_abs = os.path.abspath(dev)
    if dest_abs == src_abs or dest_abs.startswith(src_abs + os.sep):
        print("  error: --to is inside the device being recovered from. "
              "Choosing a different destination is mandatory.", file=sys.stderr)
        return 2
    try:
        from .backends.linux import get_backend
        be = get_backend()
        for mp in (be.mount_points(dev) if hasattr(be, "mount_points") else []):
            mp_abs = os.path.abspath(mp)
            if mp_abs and (dest_abs == mp_abs or dest_abs.startswith(mp_abs + os.sep)):
                print(f"  error: --to ({dest}) is inside the mounted source "
                      f"device ({mp}). Recovered files would be written to the "
                      "very drive being recovered.", file=sys.stderr)
                return 2
    except Exception:
        pass
    print(f"  recovering from {dev} into {dest} ...", file=sys.stderr)
    rep = recover_files(dev, dest,
                        only_suspicious=args.deleted_only,
                        include_deleted=not args.no_deleted,
                        progress=_progress_printer("recover"))
    print(f"\n  {len(rep.recovered)} file(s) recovered "
          f"({human(rep.total_bytes)}) in {rep.duration_s:.1f}s")
    for n in rep.notes:
        print(f"    - {n}")
    for name, why in rep.failed[:10]:
        print(f"    ! {name}: {why}")

    if args.carve:
        print("\n  carving by signature (this finds files with no directory "
              "entry at all) ...")
        crep = carve_files(dev, os.path.join(dest, "_carved"),
                           progress=_progress_printer("carve"))
        print(f"    {len(crep.carved)} carved file(s) in {dest}/_carved")
        for n in crep.notes:
            print(f"    - {n}")

    if not rep.recovered and not args.carve:
        print("\n  Nothing recovered from directory entries. Try --carve, "
              "which scans for file signatures instead.")
        return 1
    return 0


def cmd_image(args) -> int:
    dev, dest = args.device, args.to
    from .core.imaging import Source, free_space_ok
    with Source(dev) as src:
        size = src.size
    length = args.size * 1024 * 1024 if args.size else size
    if not free_space_ok(dest, length):
        print(f"  error: not enough free space for {human(length)} at {dest}",
              file=sys.stderr)
        return 2
    print(f"  imaging {dev} ({human(length)}) -> {dest}")
    print("  bad sectors are recorded and zero-filled so the rest can continue")
    rep = copy_range(Source(dev), dest, 0, length,
                     progress=_progress_printer("imaging"))
    print(f"\n  wrote {human(rep['bytes_written'])} to {dest}")
    if rep["bad_count"]:
        print(f"  ! {rep['bad_count']} unreadable block(s) found -- "
              "these were zero-filled:")
        for off in rep["bad_offsets"][:10]:
            print(f"      {human(off)}")
        print("\n  The image is usable for recovery; the unreadable regions are "
              "gone. More imaging attempts usually read less, not more.")
    else:
        print("  no read errors -- the image is a complete copy")
    return 0


def cmd_eject(args) -> int:
    from .backends.linux import get_backend
    res = get_backend().eject(args.device)
    print("  " + res.combined[:400])
    return 0 if res.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        print(BANNER)
        parser.print_help()
        return 0
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n  unexpected error: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        if getattr(args, "debug", False) or os.environ.get("FLASHPHYSICIAN_DEBUG"):
            raise
        return 70


if __name__ == "__main__":
    sys.exit(main())