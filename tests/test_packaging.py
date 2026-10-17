#!/usr/bin/env python3
"""Guard suite: packaging hygiene that keeps Windows from flagging the build.

This suite exists because of a real problem. The released Windows builds came
back from users as "Windows Defender says this is a virus". Nothing in the
Python code was malicious -- the diagnosis and repair engines are clean -- but
the *packaged* artefacts tripped generic heuristics:

  * UPX compression (a packed PE is the textbook malware stub shape),
  * no version resource (no publisher, no product, so no reputation),
  * embedded shell strings: `-ExecutionPolicy Bypass`, `cmd /c ... > file`,
  * a spare payload appended to the PE overlay (PyInstaller's one-file data,
    which is indistinguishable from a dropper stub to a naive scanner).

Those are all cheap to get right and expensive to get wrong, so they are
asserted here instead of being rediscovered after every release.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def code_only(path: Path) -> str:
    """Strip line comments only, keeping docstrings.

    Docstrings matter here: they are string literals and they DO end up in the
    compiled bytecode and therefore inside the packaged exe. A guard that reads
    only the executable statements would pass while the shipped binary still
    carried the flagged phrase in a docstring."""
    src = read(path)
    src = re.sub(r"#.*", "", src)                        # line comments
    return src


# -- 1. the PyInstaller spec -------------------------------------------------
def test_spec() -> None:
    spec = ROOT / "tools" / "flashphysician.spec"
    check("spec exists", spec.exists())

    text = read(spec)
    # Check the actual keyword argument, not prose about it: the spec's
    # docstring legitimately explains why UPX is off, and that explanation
    # mentions the string `upx=True`.
    check("UPX disabled (no packer heuristic)",
          not re.search(r"^\s*upx\s*=\s*True", text, re.MULTILINE),
          "upx=True is the #1 AV packer trigger")

    check("version resource wired into every EXE",
          len(re.findall(r"^\s*version\s*=\s*VERSION", text, re.MULTILINE)) >= 3,
          f"found {len(re.findall(r'^\\s*version\\s*=\\s*VERSION', text, re.MULTILINE))} of 3+")

    check("one-file and one-dir GUI builds both present",
          "FlashPhysician-portable" in text and "COLLECT(" in text)

    check("console build present for scripting",
          "flashphysician-cli" in text)


# -- 2. version resource content --------------------------------------------
def test_version_info() -> None:
    vi = ROOT / "tools" / "version_info.txt"
    check("version_info.txt exists", vi.exists())
    if not vi.exists():
        return

    text = read(vi)
    for field in ("CompanyName", "ProductName", "FileDescription",
                  "FileVersion", "LegalCopyright"):
        check(f"version resource declares {field}", field in text)

    # An unsigned exe with no publisher is what SmartScreen calls "unrecognised".
    check("version resource is non-empty for CompanyName",
          bool(re.search(r"'CompanyName',\s*'[^']+'", text)))


# -- 3. no shell-exec / policy-bypass literals in shipped code ---------------
def test_no_flagged_literals() -> None:
    targets = sorted((ROOT / "flashphysician").rglob("*.py"))
    targets += [ROOT / "flashphysician_cli_entry.py",
                ROOT / "flashphysician_gui_entry.py"]

    bad_patterns = {
        "ExecutionPolicy Bypass": r"ExecutionPolicy\D{0,4}Bypass",
        "cmd /c with output redirect": r"cmd\"?,?\s*\"/c\",\s*\S*echo",
        "shell=True on subprocess": r"shell\s*=\s*True",
        "os.system(": r"os\.system\s*\(",
        "raw base64 decode of a blob": r"b64decode\s*\(",
        "eval/exec of a string": r"\b(eval|exec)\s*\(\s*[a-zA-Z_]",
        "PowerShell download cradle": r"(DownloadString|Invoke-WebRequest|IEX)\s*\(",
    }

    for path in targets:
        if not path.exists():
            continue
        body = code_only(path)
        rel = path.relative_to(ROOT)
        for label, pat in bad_patterns.items():
            found = re.search(pat, body)
            check(f"no {label} in {rel}", found is None,
                  found.group(0)[:60] if found else "")


# -- 4. Windows backend uses native mechanisms, not shell redirection --------
def test_windows_backend() -> None:
    backend = ROOT / "flashphysician" / "backends" / "windows.py"
    check("windows backend exists", backend.exists())
    if not backend.exists():
        return

    body = code_only(backend)
    check("flush uses Write-VolumeCache, not a temp file",
          "Write-VolumeCache" in body)
    check("no temp-file flush marker",
          "__flashphysician_flush" not in body)
    check("eject uses the Shell.Application verb",
          "InvokeVerb" in body)
    check("RemoteSigned policy used",
          "RemoteSigned" in body)


# -- 5. README tells the user the truth about the warning -------------------
def test_readme() -> None:
    readme = ROOT / "README.md"
    check("README exists", readme.exists())
    if not readme.exists():
        return

    text = read(readme).lower()
    check("README explains the SmartScreen warning",
          "smartscreen" in text)
    check("README tells the user how to proceed",
          "run anyway" in text or "more info" in text)
    # Claims must match reality: the tool really is unsigned and dependency-free.
    check("README states the build is unsigned",
          "unsigned" in text)


# -- 6. build workflow exists and enforces the same rules -------------------
def test_workflow() -> None:
    wf = ROOT / ".github" / "workflows" / "build.yml"
    check("build workflow exists", wf.exists())
    if not wf.exists():
        return

    text = read(wf)
    check("workflow builds on a real Windows runner",
          "windows-latest" in text)
    check("workflow refuses a regressed UPX setting",
          "upx" in text.lower() and "throw" in text)
    check("workflow publishes SHA-256 sums",
          "SHA256SUMS" in text or "Get-FileHash" in text)
    check("workflow verifies the version resource survived",
          "VersionInfo" in text)


def main() -> int:
    print("=" * 72)
    print("FlashPhysician -- packaging guard suite")
    print("=" * 72)
    for fn in (test_spec, test_version_info, test_no_flagged_literals,
               test_windows_backend, test_readme, test_workflow):
        print(f"\n{fn.__doc__ and fn.__doc__.strip().splitlines()[0] or fn.__name__}")
        fn()
    print()
    print("=" * 72)
    total = PASS + FAIL
    print(f"  {PASS}/{total} guard checks passed")
    print("  packaging is AV-hygienic" if FAIL == 0
          else f"  {FAIL} CHECK(S) FAILED -- do not ship this build")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())