#!/usr/bin/env python3
"""Run every test suite and print one summary.

Each suite is a standalone script, so this only has to invoke them and tally.
It is deliberately dependency-free: a rescue tool should be verifiable on a
machine where nothing can be installed.

    python3 tests/run_all.py           # summary
    python3 tests/run_all.py -v        # stream each suite's output
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITES = [
    ("test_engine", "structure parsing (MBR, GPT, FAT, exFAT, NTFS)"),
    ("test_repair", "repair plans, safety guards, round-trip verification"),
    ("test_surface", "surface integrity and fake-capacity detection"),
    ("test_recovery", "file recovery and signature carving"),
    ("test_cli", "CLI contract, exit codes, refusal behaviour"),
    ("test_gui", "GUI logic (headless)"),
    ("test_windows_discovery", "Windows drive detection (RAW, Fixed, letterless)"),
    ("test_packaging", "packaging hygiene (no AV triggers, version info)"),
]

TALLY = re.compile(r"^\s+(\d+)/(\d+) (?:tests |guard checks |checks )?passed\s*$", re.MULTILINE)


def main() -> int:
    verbose = "-v" in sys.argv
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)

    print("FlashPhysician -- full test suite")
    print("=" * 72)
    total_pass = total_all = 0
    failed_suites: list[str] = []

    for name, what in SUITES:
        script = ROOT / "tests" / f"{name}.py"
        if not script.exists():
            print(f"  [SKIP] {name:16} (missing)")
            failed_suites.append(name)
            continue
        proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                              env=env, capture_output=True, text=True,
                              timeout=1800)
        out = proc.stdout + proc.stderr
        if verbose:
            print(out)
        m = TALLY.search(out)
        if m:
            got, want = int(m.group(1)), int(m.group(2))
        else:
            got, want = 0, 0
        total_pass += got
        total_all += want
        ok = got == want and want > 0
        if not ok:
            failed_suites.append(name)
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name:16} {got:>3}/{want:<3}  {what}")
        if not ok and not verbose:
            for line in out.splitlines():
                if line.strip().startswith("[FAIL]") or ":" in line and "  " in line[:2]:
                    print(f"          {line.strip()[:100]}")

    print("=" * 72)
    print(f"  {total_pass}/{total_all} tests passed across {len(SUITES)} suites")
    if failed_suites:
        print(f"  failing suites: {', '.join(failed_suites)}")
    else:
        print("  all suites green")
    print("=" * 72)
    return 0 if not failed_suites and total_all else 1


if __name__ == "__main__":
    sys.exit(main())