"""Linux backend.

Uses the standard util-linux / dosfstools / exfatprogs / ntfs-3g toolchain.
Every command is assembled as argv (never a shell string), so a device path
containing spaces or shell metacharacters cannot turn into command injection.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .base import (Backend, Capabilities, CommandResult, can_open_rw,
                   is_admin, run, _which_all, _pick_tool)

TOOLS = [
    "fsck.fat", "fsck.vfat", "dosfsck", "fsck.exfat", "exfatfsck",
    "ntfsfix", "e2fsck", "mkfs.fat", "mkfs.exfat", "mkfs.ntfs", "mke2fs",
    "parted", "sfdisk", "fdisk", "blkid", "lsblk", "badblocks", "hdparm",
    "udisksctl", "sync", "wipefs", "smartctl",
]

# fsck exit codes are a bitmask, not success/failure: bit 0 = errors
# corrected, bit 1 = reboot needed, bit 2 = errors left uncorrected.
# Treating non-zero as failure is the classic mistake that makes people
# think a repair did not work when it did.
FSCK_BIT_CORRECTED = 0x1
FSCK_BIT_REBOOT = 0x2
FSCK_BIT_UNCORRECTED = 0x4
FSCK_BIT_OPERATIONAL = 0x8
FSCK_BIT_USAGE = 0x10


def fsck_explain(code: int) -> str:
    if code == 0:
        return "clean -- no errors found"
    bits = []
    if code & FSCK_BIT_CORRECTED:
        bits.append("errors found and corrected")
    if code & FSCK_BIT_REBOOT:
        bits.append("filesystem modified, reboot recommended")
    if code & FSCK_BIT_UNCORRECTED:
        bits.append("errors remain UNCORRECTED")
    if code & FSCK_BIT_OPERATIONAL:
        bits.append("operational error (device unreadable/opened)")
    if code & FSCK_BIT_USAGE:
        bits.append("usage error")
    return "; ".join(bits) or f"unknown exit code {code}"


class LinuxBackend(Backend):
    name = "linux"

    def capabilities(self) -> Capabilities:
        caps = Capabilities(platform="linux", is_admin=is_admin())
        caps.tools = {k: v for k, v in _which_all(TOOLS).items() if v}
        for k in TOOLS:
            caps.tools.setdefault(k, "")

        caps.can_fsck = any(caps.tools.get(t) for t in
                            ("fsck.fat", "fsck.vfat", "dosfsck", "fsck.exfat",
                             "ntfsfix", "e2fsck"))
        caps.can_format = any(caps.tools.get(t) for t in
                              ("mkfs.fat", "mkfs.exfat", "mkfs.ntfs", "mke2fs"))
        caps.can_partition = any(caps.tools.get(t) for t in
                                 ("parted", "sfdisk", "fdisk"))
        caps.can_eject = bool(caps.tools.get("udisksctl")) or os.path.isdir("/sys/block")
        caps.can_write_devices = caps.is_admin

        if not caps.is_admin:
            caps.notes.append(
                "not running as root: repairs that write to the block device will "
                "be refused by the kernel. Re-run with sudo for those actions.")
        if not caps.has("fsck.fat"):
            caps.notes.append("install dosfstools for FAT repair (fsck.fat)")
        if not caps.has("fsck.exfat"):
            caps.notes.append("install exfatprogs for exFAT repair (fsck.exfat)")
        if not caps.has("ntfsfix"):
            caps.notes.append("install ntfs-3g for NTFS repair (ntfsfix)")
        return caps

    # -- filesystem check -------------------------------------------------
    def fsck(self, device: str, fix: bool = False, fstype: str = "") -> CommandResult:
        """Run the right checker for the filesystem, with -n when not fixing.

        `-n` (no-op) is the default because it is the only behaviour that is
        always safe on a drive that still holds the user's data.
        """
        det = self.detect_fs(device)
        fs = fstype or det
        if fs in ("fat12", "fat16", "fat32", "vfat", "fat"):
            exe = _pick_tool("fsck.fat", "dosfsck", "fsck.vfat", "fsck.msdos")
            if not exe:
                return CommandResult(
                    argv=["fsck.fat"], missing=True,
                    stderr="no FAT checker installed -- install dosfstools "
                           "(Debian/Ubuntu: apt install dosfstools; "
                           "Fedora: dnf install dosfstools; "
                           "macOS: brew install dosfstools)")
            argv = [exe, "-v", "-a" if fix else "-n", device]
        elif fs == "exfat":
            exe = _pick_tool("fsck.exfat", "exfatfsck")
            if not exe:
                return CommandResult(
                    argv=["fsck.exfat"], missing=True,
                    stderr="no exFAT checker installed -- install exfatprogs "
                           "(apt install exfatprogs)")
            argv = [exe] + ([] if fix else ["-n"]) + [device]
        elif fs == "ntfs":
            exe = _pick_tool("ntfsfix")
            if not exe:
                return CommandResult(
                    argv=["ntfsfix"], missing=True,
                    stderr="no NTFS repair tool installed -- install ntfs-3g "
                           "(apt install ntfs-3g) or use `usbrevive fix` from a "
                           "Windows machine for chkdsk")
            argv = ([exe, "-b", "-d", device] if fix else [exe, "-n", device])
        elif fs in ("ext2", "ext3", "ext4"):
            exe = _pick_tool("e2fsck", "fsck.ext4", "fsck.ext3", "fsck.ext2")
            if not exe:
                return CommandResult(argv=["e2fsck"], missing=True,
                                     stderr="no ext checker installed (e2fsprogs)")
            argv = [exe, "-f", "-y" if fix else "-n", device]
        else:
            return CommandResult(argv=["fsck"], missing=True,
                                 stderr=f"no checker known for filesystem {fs or 'unknown'}")
        return run(argv)

    def detect_fs(self, device: str) -> str:
        """Ask blkid, then fall back to our own boot-sector parser."""
        if _which_all(["blkid"])["blkid"]:
            res = run(["blkid", "-o", "value", "-s", "TYPE", device], timeout=20)
            if res.ok and res.stdout.strip():
                return res.stdout.strip().splitlines()[0].lower()
        try:
            from ..core.imaging import Source
            from ..core.structures import parse_boot_sector
            with Source(device) as src:
                return parse_boot_sector(src.read_at(0, 512), 0).fs.value
        except Exception:
            return ""

    # -- device control ---------------------------------------------------
    def eject(self, device: str) -> CommandResult:
        """Flush and power down, so removal is safe.

        `sync` first is not optional: the kernel keeps dirty pages for a
        removable device and an eject without a sync is how data gets lost
        even when the tool reports success.
        """
        base = Path(device).name.rstrip("0123456789")
        run(["sync"])
        if _which_all(["udisksctl"])["udisksctl"]:
            res = run(["udisksctl", "unmount", "-b", device], timeout=60)
            if not res.ok:
                run(["udisksctl", "power-off", "-b", f"/dev/{base}"], timeout=60)
                return res
            return run(["udisksctl", "power-off", "-b", f"/dev/{base}"], timeout=60)
        # fallback: unmount every mountpoint we can see
        from ..core.imaging import unmount_all
        done = unmount_all(device)
        from .base import CommandResult as CR
        return CR(argv=["unmount", *done], returncode=0 if done else 1,
                  stdout="unmounted: " + ", ".join(done) if done else "nothing mounted")

    def rescan(self, device: str) -> CommandResult:
        """Ask the kernel to re-read the partition table."""
        base = Path(device).name.rstrip("0123456789")
        if _which_all(["partprobe"])["partprobe"]:
            return run(["partprobe", f"/dev/{base}"], timeout=60)
        if _which_all(["blockdev"])["blockdev"]:
            return run(["blockdev", "--rereadpt", f"/dev/{base}"], timeout=60)
        return CommandResult(argv=["partprobe"], missing=True,
                             stderr="install parted (partprobe) or util-linux (blockdev)")

    def format(self, device: str, fstype: str = "fat32", label: str = "") -> CommandResult:
        """Format via mkfs. The caller is responsible for confirmation; this
        only refuses on obviously wrong input."""
        if device.startswith("/dev/") and not can_open_rw(device):
            return CommandResult(argv=["mkfs"], missing=True,
                                 stderr=f"cannot open {device} read-write (need root)")
        if len(label) > 11 and fstype in ("fat32", "fat16", "fat12"):
            label = label[:11]
        if fstype == "fat32":
            argv = ["mkfs.fat", "-F", "32"]
        elif fstype == "fat16":
            argv = ["mkfs.fat", "-F", "16"]
        elif fstype == "exfat":
            argv = ["mkfs.exfat"]
        elif fstype == "ntfs":
            argv = ["mkfs.ntfs", "-f", "-F"]
        elif fstype in ("ext4", "ext3", "ext2"):
            argv = ["mke2fs", f"-t", fstype, "-F"]
        else:
            return CommandResult(argv=["mkfs"], missing=True,
                                 stderr=f"unsupported filesystem {fstype}")
        if label:
            argv += (["-n", label] if fstype.startswith("fat") or fstype == "exfat"
                     else ["-L", label])
        argv.append(device)
        return run(argv, timeout=3600)

    # -- enumeration ------------------------------------------------------
    def list_devices(self) -> list[dict]:
        out: list[dict] = []
        try:
            from ..core.imaging import discover_drives
            for d in discover_drives(include_system=True):
                out.append({
                    "name": d.name, "path": d.path, "size": d.size_bytes,
                    "bus": d.bus, "removable": d.removable, "readonly": d.readonly,
                    "vendor": d.vendor, "model": d.model, "serial": d.serial,
                    "mounted": d.mounted, "partitions": d.partitions,
                    "fstype": d.fstype, "label": d.label,
                })
        except Exception:
            pass
        return out

    def mount_points(self, device: str) -> list[str]:
        from ..core.imaging import _mounts_for
        return _mounts_for(Path(device).name)

    def unmount(self, device: str) -> CommandResult:
        from ..core.imaging import unmount_all
        done = unmount_all(device)
        return CommandResult(argv=["umount", *done],
                             returncode=0 if done or not self.mount_points(device) else 1,
                             stdout=", ".join(done) or "nothing mounted")

    def smart(self, device: str) -> CommandResult:
        """flash media rarely implements SMART; report that honestly."""
        if not _which_all(["smartctl"])["smartctl"]:
            return CommandResult(argv=["smartctl"], missing=True,
                                 stderr="install smartmontools")
        return run(["smartctl", "-a", device], timeout=120)

    def badblocks_readonly(self, device: str, start: int = 0, count: int | None = None) -> CommandResult:
        argv = ["badblocks", "-sv", "-b", "4096"]
        if count:
            argv += [str(start), str(count)]
        argv.append(device)
        return run(argv, timeout=24 * 3600)


def get_backend() -> Backend:
    if os.name == "nt":
        from .windows import WindowsBackend
        return WindowsBackend()
    return LinuxBackend()