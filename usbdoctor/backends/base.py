"""OS backends: the only place the tool touches the outside world.

The engine decides *what* is wrong and *what* should be written; a backend
executes the operations that a raw byte write cannot express (running a
filesystem check, asking the OS to rescan a device, removing a write lock).

Each backend declares `capabilities()` and every caller must check before
offering an action, because the honest answer on the wrong platform is "not
supported here" -- not a simulated success.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    missing: bool = False
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.missing

    @property
    def combined(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()

    def describe(self) -> str:
        head = " ".join(self.argv)
        if self.missing:
            return f"{head}\n  -> command not available on this system"
        if self.timed_out:
            return f"{head}\n  -> timed out"
        return f"{head}\n  -> exit {self.returncode}\n  {self.combined[:800]}"


def run(argv: list[str], timeout: int = 3600, input_text: str | None = None,
        env: dict | None = None) -> CommandResult:
    """Run a command, never raising. A rescue tool must survive a broken
    helper binary and still report what happened."""
    import time
    t0 = time.time()
    exe = shutil.which(argv[0]) if not os.path.isabs(argv[0]) else argv[0]
    if not exe or not os.path.exists(exe):
        return CommandResult(argv=argv, missing=True,
                             stderr=f"{argv[0]}: not found in PATH")
    try:
        proc = subprocess.run(
            [exe] + argv[1:], capture_output=True, text=True, timeout=timeout,
            input=input_text, env=env,
        )
        return CommandResult(argv=argv, returncode=proc.returncode,
                             stdout=proc.stdout or "", stderr=proc.stderr or "",
                             duration_s=time.time() - t0)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(argv=argv, timed_out=True,
                             stdout=(exc.stdout or b"").decode(errors="ignore")
                             if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                             duration_s=time.time() - t0)
    except OSError as exc:
        return CommandResult(argv=argv, returncode=-1, stderr=str(exc),
                             duration_s=time.time() - t0)


@dataclass
class Capabilities:
    platform: str = "unknown"
    is_admin: bool = False
    can_write_devices: bool = False
    can_format: bool = False
    can_fsck: bool = False
    can_partition: bool = False
    can_eject: bool = False
    tools: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def has(self, tool: str) -> bool:
        return bool(self.tools.get(tool))

    def summary(self) -> str:
        have = [k for k, v in self.tools.items() if v]
        miss = [k for k, v in self.tools.items() if not v]
        return (f"{self.platform} | admin={self.is_admin} "
                f"format={self.can_format} fsck={self.can_fsck} "
                f"eject={self.can_eject}\n  available: {', '.join(have) or 'none'}"
                + (f"\n  missing: {', '.join(miss)}" if miss else ""))


class Backend:
    """Base class. Every method returns a CommandResult or a plain value; a
    method that cannot work on this platform returns missing=True rather than
    pretending."""

    name = "base"

    def capabilities(self) -> Capabilities:
        raise NotImplementedError

    def fsck(self, device: str, fix: bool = False) -> CommandResult:
        raise NotImplementedError

    def eject(self, device: str) -> CommandResult:
        raise NotImplementedError

    def rescan(self, device: str) -> CommandResult:
        raise NotImplementedError

    def format(self, device: str, fstype: str = "fat32", label: str = "") -> CommandResult:
        raise NotImplementedError

    def list_devices(self) -> list[dict]:
        raise NotImplementedError


def _which_all(names: list[str]) -> dict[str, str]:
    return {n: (shutil.which(n) or "") for n in names}


def _pick_tool(*names: str) -> str:
    """First of `names` present in PATH, or ''.

    Needed because the same tool ships under different names across distros
    (fsck.fat / dosfsck / fsck.vfat) and choosing blindly produces the
    'command not found' that makes people think the tool is broken.
    """
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return ""


def is_admin() -> bool:
    """Root on POSIX, elevated on Windows."""
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def can_open_rw(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
        os.close(fd)
        return True
    except OSError:
        return False