"""CLI contract tests.

Two things are being verified, and both matter more than the happy path:

1. **Exit codes.** A rescue tool gets scripted and driven from GUIs; an exit
   code of 0 on failure means an automation happily continues after a repair
   that never happened.
2. **Refusals.** Every destructive command must do nothing, and say so, without
   an explicit confirmation flag. The tests assert the file bytes are unchanged
   after a refused run, not merely that a message was printed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests import fixtures as fx                                       # noqa: E402

WORK = os.path.join(ROOT, "tests", "_work", "cli")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, why: str = "") -> None:
    results.append((name, ok, why))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  << {why}" if not ok else ""))


def cli(*argv: str, timeout: int = 300) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    proc = subprocess.run([sys.executable, "-m", "flashphysician", *argv],
                          capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def setup() -> None:
    os.makedirs(WORK, exist_ok=True)


def test_help_and_version() -> None:
    rc, out, _ = cli("--help")
    rc2, out2, _ = cli("--version")
    check("help_and_version", rc == 0 and rc2 == 0 and "flashphysician" in out2,
          f"rc={rc}/{rc2}")


def test_info_lists_capabilities() -> None:
    rc, out, _ = cli("info")
    ok = rc == 0 and "This system:" in out and "Commands:" in out
    check("info_lists_capabilities", ok, f"rc={rc}")


def test_diagnose_exit_codes() -> None:
    p = os.path.join(WORK, "d1.img")
    fx.make_fat32(p, 1024)
    rc_ok, out_ok, _ = cli("diagnose", p, "--no-color")
    rc_missing, _, err = cli("diagnose", os.path.join(WORK, "nope.img"))
    check("diagnose_exit_codes",
          rc_ok == 0 and rc_missing == 2 and "does not exist" in err,
          f"ok={rc_ok} missing={rc_missing}")


def test_diagnose_json_is_valid() -> None:
    p = os.path.join(WORK, "d2.img")
    fx.make_fat32(p, 1024, dirty=True)
    rc, out, _ = cli("diagnose", p, "--json")
    try:
        data = json.loads(out)
        ok = (rc == 0 and data["health"] < 100
              and any(f["code"] == "FAT_DIRTY" for f in data["findings"]))
        why = f"health={data.get('health')} findings={len(data.get('findings', []))}"
    except Exception as exc:
        ok, why = False, f"invalid JSON: {exc}"
    check("diagnose_json_is_valid", ok, why)


def test_scan_requires_confirmation_and_writes_nothing() -> None:
    p = os.path.join(WORK, "s1.img")
    fx.blank_image(p, 8)
    before = open(p, "rb").read()
    rc, out, _ = cli("scan", p, "--size", "2", "--no-color")
    after = open(p, "rb").read()
    check("scan_requires_confirmation_and_writes_nothing",
          rc != 0 and before == after and "--yes" in out,
          f"rc={rc} unchanged={before == after}")


def test_capacity_requires_confirmation() -> None:
    p = os.path.join(WORK, "c1.img")
    fx.blank_image(p, 8)
    before = open(p, "rb").read()
    rc, _, _ = cli("capacity", p)
    after = open(p, "rb").read()
    check("capacity_requires_confirmation",
          rc != 0 and before == after, f"rc={rc} unchanged={before == after}")


def test_repair_without_yes_writes_nothing() -> None:
    p = os.path.join(WORK, "r1.img")
    fx.make_fat32(p, 1024, corrupt_sig=True)
    before = open(p, "rb").read()
    rc, out, _ = cli("repair", p, "--no-color")
    after = open(p, "rb").read()
    check("repair_without_yes_writes_nothing",
          rc == 0 and before == after and "Re-run with --yes" in out,
          f"rc={rc} unchanged={before == after}")


def test_repair_with_yes_actually_fixes() -> None:
    p = os.path.join(WORK, "r2.img")
    fx.make_fat32(p, 1024, corrupt_sig=True)
    rc, _, _ = cli("repair", p, "--yes", "--no-color")
    rc_d, out_d, _ = cli("diagnose", p, "--json")
    data = json.loads(out_d)
    healthy = data["filesystem"] == "fat32"
    check("repair_with_yes_actually_fixes",
          rc == 0 and healthy,
          f"rc={rc} fs={data.get('filesystem')} health={data.get('health')}")


def test_repair_creates_backup() -> None:
    p = os.path.join(WORK, "r3.img")
    bdir = os.path.join(WORK, "bk3")
    fx.make_fat32(p, 1024, corrupt_sig=True)
    original = open(p, "rb").read(512)
    cli("repair", p, "--yes", "--backup-dir", bdir, "--no-color")
    files = os.listdir(bdir) if os.path.isdir(bdir) else []
    ok = False
    why = f"no backup files in {bdir}"
    for f in files:
        if open(os.path.join(bdir, f), "rb").read(512) == original:
            ok, why = True, f
            break
    check("repair_creates_backup", ok, why)


def test_recover_refuses_destination_inside_source() -> None:
    p = os.path.join(WORK, "rec1.img")
    fx.make_fat32_with_files(p, 1024)
    inside = os.path.join(p, "out")
    rc, _, err = cli("recover", p, "--to", inside)
    check("recover_refuses_destination_inside_source",
          rc == 2 and "inside the device" in err, f"rc={rc} err={err[:80]}")


def test_recover_extracts_files() -> None:
    p = os.path.join(WORK, "rec2.img")
    dest = os.path.join(WORK, "rec2_out")
    fx.make_fat32_with_files(p, 1024, deleted=True, hidden=True)
    rc, out, _ = cli("recover", p, "--to", dest)
    got = sorted(os.listdir(dest)) if os.path.isdir(dest) else []
    payload = b"This is the live file: FlashPhysician recovery test.\n" * 40
    live_ok = any(open(os.path.join(dest, f), "rb").read() == payload for f in got)
    check("recover_extracts_files",
          rc == 0 and len(got) == 4 and live_ok,
          f"rc={rc} files={got}")


def test_recover_carve_when_no_filesystem() -> None:
    p = os.path.join(WORK, "rec3.img")
    dest = os.path.join(WORK, "rec3_out")
    fx.make_carve_target(p)
    rc, out, _ = cli("recover", p, "--to", dest, "--carve")
    carved_dir = os.path.join(dest, "_carved")
    got = sorted(os.listdir(carved_dir)) if os.path.isdir(carved_dir) else []
    check("recover_carve_when_no_filesystem", len(got) == 3,
          f"rc={rc} carved={got}")


def test_image_copies_and_reports() -> None:
    p = os.path.join(WORK, "im1.img")
    out_img = os.path.join(WORK, "im1_copy.img")
    fx.make_fat32_with_files(p, 1024)
    rc, out, _ = cli("image", p, "--to", out_img, "--size", "32")
    size = os.path.getsize(out_img) if os.path.exists(out_img) else 0
    check("image_copies_and_reports",
          rc == 0 and size == 32 * 1024 * 1024,
          f"rc={rc} size={size}")


def test_list_runs() -> None:
    rc, out, _ = cli("list")
    ok = rc in (0, 1) and ("removable device" in out or "No drives" in out)
    check("list_runs", ok, f"rc={rc}")


def test_unknown_flag_fails_cleanly() -> None:
    rc, out, err = cli("diagnose", "--bogus-flag")
    check("unknown_flag_fails_cleanly", rc != 0, f"rc={rc}")


def main() -> int:
    setup()
    print("FlashPhysician -- CLI contract test suite\n")
    for fn in (test_help_and_version, test_info_lists_capabilities,
               test_diagnose_exit_codes, test_diagnose_json_is_valid,
               test_scan_requires_confirmation_and_writes_nothing,
               test_capacity_requires_confirmation,
               test_repair_without_yes_writes_nothing,
               test_repair_with_yes_actually_fixes,
               test_repair_creates_backup,
               test_recover_refuses_destination_inside_source,
               test_recover_extracts_files,
               test_recover_carve_when_no_filesystem,
               test_image_copies_and_reports, test_list_runs,
               test_unknown_flag_fails_cleanly):
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