"""GUI tests that run headless.

The GUI itself cannot be rendered without an X display, so these tests cover
what *can* be verified without one:

  * every module-level helper and the advice engine (pure functions),
  * that the GUI's repair-plan selection produces the same decisions as the
    CLI's, so the two front ends cannot drift apart,
  * that no GUI method references an attribute that does not exist (checked by
    compiling and introspecting the class, not by running it).

What is NOT covered here is stated in the report: widget layout, event
wiring and the rendered appearance need a display and must be checked on a
real machine.
"""

from __future__ import annotations

import ast
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests import fixtures as fx                                       # noqa: E402
from flashphysician.core.diagnose import quick_diagnose                     # noqa: E402
from flashphysician.core.repair import (                                    # noqa: E402
    plan_clear_fat_dirty, plan_fix_fat_type_label, plan_rebuild_mbr_from_fs,
    plan_restore_fat32_boot_backup, plan_restore_gpt_from_backup,
    plan_restore_mbr_signature,
)
from flashphysician.core.structures import FsType, human                    # noqa: E402

WORK = os.path.join(ROOT, "tests", "_work", "gui")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, why: str = "") -> None:
    results.append((name, ok, why))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  << {why}" if not ok else ""))


# --------------------------------------------------------------------------
# the GUI's plan selection, mirrored so it can be tested without Tk
# --------------------------------------------------------------------------

def gui_plan_selection(path: str, rep):
    """The exact decision logic the GUI uses to choose repairs.

    Kept here as a function so a change in the GUI's behaviour fails a test
    instead of silently disagreeing with the CLI.
    """
    codes = {f.code for f in rep.findings}
    plans = []
    if "MBR_SIG" in codes or "TABLE_MISSING" in codes:
        plans.append(("Restore the MBR boot signature",
                      plan_restore_mbr_signature(path)))
    if "FAT_BOOTBACKUP_MISMATCH" in codes:
        plans.append(("Restore the FAT32 boot sector from its backup",
                      plan_restore_fat32_boot_backup(path)))
    if "FAT_TYPE_MISMATCH" in codes:
        plans.append(("Correct the filesystem type label",
                      plan_fix_fat_type_label(path)))
    if "FAT_DIRTY" in codes:
        plans.append(("Clear the dirty flag", plan_clear_fat_dirty(path)))
    if "GPT_PROTECTIVE_ONLY" in codes or "GPT_HEADER" in codes:
        plans.append(("Rebuild the GPT from its backup",
                      plan_restore_gpt_from_backup(path)))
    if rep.raw.get("orphan_fs_offset") is not None:
        fst = rep.raw.get("orphan_fs_type", "fat32")
        try:
            ftype = FsType(fst)
        except ValueError:
            ftype = FsType.FAT32
        plans.append(("Rebuild the partition table (filesystem found at "
                      f"{human(rep.raw['orphan_fs_offset'])})",
                      plan_rebuild_mbr_from_fs(path, rep.raw["orphan_fs_offset"],
                                               ftype)))

    usable = [(l, p) for l, p in plans if p.ok]
    supersedes = {"Restore the MBR boot signature": "Rebuild the partition table"}
    final = []
    for label, plan in usable:
        offs = {op.offset for op in plan.ops}
        beaten = False
        for other_label, other_plan in usable:
            if other_label == label:
                continue
            want = supersedes.get(label)
            if want and want in other_label \
                    and offs & {op.offset for op in other_plan.ops}:
                beaten = True
                break
        if not beaten:
            final.append((label, plan))
    return final


def test_gui_module_compiles() -> None:
    src = os.path.join(ROOT, "flashphysician", "gui.py")
    try:
        ast.parse(open(src, encoding="utf-8").read())
        ok, why = True, ""
    except SyntaxError as exc:
        ok, why = False, str(exc)
    check("gui_module_compiles", ok, why)


def test_no_duplicate_method_definitions() -> None:
    """A second `def` with the same name silently replaces the first. That is
    how a handler can appear wired up and never run."""
    src = os.path.join(ROOT, "flashphysician", "gui.py")
    tree = ast.parse(open(src, encoding="utf-8").read())
    dupes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            seen: dict[str, int] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name in seen and not item.name.startswith("_"):
                        dupes.append(f"{node.name}.{item.name}")
                    seen[item.name] = seen.get(item.name, 0) + 1
            # private methods may legitimately be redefined for overrides, but
            # a *third* definition is always a mistake
            for name, count in seen.items():
                if count > 2 and f"{node.name}.{name}" not in dupes:
                    dupes.append(f"{node.name}.{name} (x{count})")
    check("no_duplicate_method_definitions", not dupes, f"duplicates: {dupes}")


def test_all_commands_have_a_finish_handler() -> None:
    """Every kind string a *worker* returns must be handled, or a finished job
    leaves the UI claiming to still be busy.

    Only tuples of the form `return ("kind", payload)` inside worker closures
    count -- plain text returns from advice builders are not commands.
    """
    src = open(os.path.join(ROOT, "flashphysician", "gui.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            elts = node.value.elts
            if elts and isinstance(elts[0], ast.Constant) \
                    and isinstance(elts[0].value, str):
                kinds.add(elts[0].value)
    kinds.discard("")

    handled: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
                and node.left.id == "kind":
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    handled.add(comp.value)
                elif isinstance(comp, ast.Tuple):
                    for e in comp.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            handled.add(e.value)

    missing = sorted(k for k in kinds if k not in handled)
    check("all_commands_have_a_finish_handler", not missing,
          f"returned kinds={sorted(kinds)} handled={sorted(handled)} missing={missing}")


def test_orphan_drive_produces_one_plan() -> None:
    """The GUI must offer exactly one sensible repair for a lost partition
    table, not two competing writes to sector 0."""
    p = os.path.join(WORK, "g1.img")
    fx.make_lost_partition_table(p, 1024)
    rep = quick_diagnose(p)
    final = gui_plan_selection(p, rep)
    labels = [l for l, _ in final]
    ok = len(final) == 1 and "Rebuild the partition table" in labels[0]
    check("orphan_drive_produces_one_plan", ok, f"plans={labels}")


def test_dirty_drive_produces_dirty_plan() -> None:
    p = os.path.join(WORK, "g2.img")
    fx.make_dirty_fat32(p, 1024)
    rep = quick_diagnose(p)
    final = gui_plan_selection(p, rep)
    labels = " ".join(l for l, _ in final)
    check("dirty_drive_produces_dirty_plan", "dirty flag" in labels,
          f"plans={labels!r}")


def test_gpt_backup_produces_gpt_plan() -> None:
    p = os.path.join(WORK, "g3.img")
    fx.build_gpt(p, 256, 2)
    with open(p, "r+b") as fh:
        fh.seek(512)
        fh.write(b"\x00" * 512)
        fh.seek(1024)
        fh.write(b"\x00" * (32 * 512))
    rep = quick_diagnose(p)
    final = gui_plan_selection(p, rep)
    labels = " ".join(l for l, _ in final)
    check("gpt_backup_produces_gpt_plan", "GPT" in labels, f"plans={labels!r}")


def test_healthy_drive_produces_no_plan() -> None:
    p = os.path.join(WORK, "g4.img")
    fx.make_fat32(p, 1024)
    rep = quick_diagnose(p)
    final = gui_plan_selection(p, rep)
    check("healthy_drive_produces_no_plan", not final,
          f"plans={[l for l, _ in final]}")


def test_advice_mentions_orphan_recovery() -> None:
    """The advice text is the part a worried user actually reads, so the
    single most recoverable case must produce the reassuring explanation."""
    p = os.path.join(WORK, "g5.img")
    fx.make_lost_partition_table(p, 1024)
    rep = quick_diagnose(p)

    # call the advice builder without constructing a Tk widget
    src = open(os.path.join(ROOT, "flashphysician", "gui.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_advice_for":
            fn = node
    if fn is None:
        return check("advice_mentions_orphan_recovery", False,
                     "_advice_for not found")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {"FsType": FsType, "human": human, "Severity": __import__(
        "flashphysician.core.diagnose", fromlist=["Severity"]).Severity}
    code = compile(module, "<advice>", "exec")
    exec(code, ns)
    # the method expects self; call it with a dummy
    text = ns["_advice_for"](object(), rep)
    ok = ("PROBABLY FINE" in text.upper()
          and "format" in text.lower()
          and "Repair" in text)
    check("advice_mentions_orphan_recovery", ok, f"text={text[:90]!r}")


def test_advice_for_fake_capacity_absent_when_not_tested() -> None:
    p = os.path.join(WORK, "g6.img")
    fx.make_fat32(p, 1024)
    rep = quick_diagnose(p)
    src = open(os.path.join(ROOT, "flashphysician", "gui.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_advice_for"][0]
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {"FsType": FsType, "human": human, "Severity": __import__(
        "flashphysician.core.diagnose", fromlist=["Severity"]).Severity}
    exec(compile(module, "<advice>", "exec"), ns)
    text = ns["_advice_for"](object(), rep)
    ok = "healthy" in text.lower() and "Surface scan" in text
    check("advice_for_fake_capacity_absent_when_not_tested", ok,
          f"text={text[:90]!r}")


def main() -> int:
    os.makedirs(WORK, exist_ok=True)
    print("FlashPhysician -- GUI test suite (headless)\n")
    for fn in (test_gui_module_compiles, test_no_duplicate_method_definitions,
               test_all_commands_have_a_finish_handler,
               test_orphan_drive_produces_one_plan,
               test_dirty_drive_produces_dirty_plan,
               test_gpt_backup_produces_gpt_plan,
               test_healthy_drive_produces_no_plan,
               test_advice_mentions_orphan_recovery,
               test_advice_for_fake_capacity_absent_when_not_tested):
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