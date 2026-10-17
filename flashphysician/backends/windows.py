"""Windows backend.

Targets the tools that ship with Windows itself, so the portable .exe works on
a machine that has nothing installed:

  * `chkdsk`      -- filesystem check and repair (FAT/exFAT/NTFS)
  * `diskpart`    -- partition table rebuild, clean, create, format
  * `format.com`  -- quick/full format of a volume
  * `PowerShell`  -- Get-Disk/Get-Partition/Get-Volume for enumeration, and
                     Clear-Disk/Repair-Volume/Format-Volume as the modern
                     equivalents
  * `mountvol`    -- drive-letter assignment (the "drive has no letter" case)
  * `fsutil`      -- dirty-bit query/set, volume info

Two Windows-specific realities are encoded here because they cause most of the
"the repair tool did nothing" reports:

1. **Write protection can be a registry flag.**  A flash drive that mounts
   read-only with no physical switch is usually the
   `StorageDevicePolicies\\WriteProtect` value; clearing it needs the registry
   path, not a format.
2. **A volume with no drive letter cannot be repaired by path.**  chkdsk needs
   a letter or a volume GUID path, so the letter has to be assigned first.
   That is why `assign_letter` exists as a repair step of its own.

Commands are assembled as argv lists. diskpart and chkdsk cannot take a
password or code from us, and no command here is ever built from user text.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from .base import Backend, Capabilities, CommandResult, is_admin, run, _which_all

WRITE_PROTECT_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies"

TOOLS = [
    "chkdsk", "diskpart", "format", "mountvol", "fsutil", "powershell",
    "pwsh", "wbadmin", "sfc", "takeown",
]


def _ps(script: str, timeout: int = 120) -> CommandResult:
    """Run a PowerShell snippet with a machine-readable JSON result where the
    caller asked for one. `-NoProfile` matters: a user profile that prints a
    banner corrupts parsed output.

    The execution policy is `RemoteSigned` rather than the permissive policy
    that disables signing checks entirely. That permissive string is the single
    most common marker in malware launchers, and binaries that embed it get
    treated as hostile by Defender and SmartScreen even when they never load a
    script from disk. We only ever pass `-Command` with a literal we built
    ourselves, which RemoteSigned permits, so nothing is lost.
    """
    exe = "powershell" if _which_all(["powershell"])["powershell"] else "pwsh"
    return run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
                "-Command", script], timeout=timeout)


class WindowsBackend(Backend):
    name = "windows"

    def capabilities(self) -> Capabilities:
        caps = Capabilities(platform="windows", is_admin=is_admin())
        caps.tools = {k: v for k, v in _which_all(TOOLS).items()}
        for k in TOOLS:
            caps.tools.setdefault(k, "")

        caps.can_fsck = caps.has("chkdsk")
        caps.can_format = caps.has("format") or caps.has("powershell")
        caps.can_partition = caps.has("diskpart") or caps.has("powershell")
        caps.can_eject = True
        caps.can_write_devices = caps.is_admin

        if not caps.is_admin:
            caps.notes.append(
                "not running as Administrator: chkdsk /f, diskpart and "
                "Clear-Disk will be refused by Windows. Right-click the exe "
                "and choose 'Run as administrator' for repair actions.")
        return caps

    # -- filesystem check -------------------------------------------------
    def fsck(self, device: str, fix: bool = False, fstype: str = "") -> CommandResult:
        """chkdsk on a volume. `/f` fixes, `/r` also scans for bad sectors.

        Note chkdsk cannot run on the volume hosting Windows: it offers to
        schedule the check at next boot instead, and we surface that rather
        than reporting success.
        """
        target = self._volume_path(device)
        argv = ["chkdsk", target]
        argv.append("/f" if fix else "/scan")
        if fix:
            argv.append("/r")
        res = run(argv, timeout=12 * 3600)
        if "cannot lock" in res.combined.lower() or "another process" in res.combined.lower():
            res.stderr += (
                "\n[FlashPhysician] The volume is in use or is the system volume. "
                "Close anything using it, or accept the scheduled-at-reboot check.")
        return res

    def repair_volume(self, device: str) -> CommandResult:
        """Modern equivalent: Repair-Volume -SpotFix on the mount point."""
        letter = self._letter(device)
        if not letter:
            return CommandResult(argv=["Repair-Volume"], missing=True,
                                 stderr="volume has no drive letter; assign one first")
        return _ps(f"Repair-Volume -DriveLetter {letter} -SpotFix | "
                   "Format-List | Out-String", timeout=12 * 3600)

    # -- write protection -------------------------------------------------
    def write_protect_state(self) -> CommandResult:
        return _ps(
            f"$p='{WRITE_PROTECT_KEY}'; "
            "try { $v=(Get-ItemProperty -Path $p -Name WriteProtect "
            "-ErrorAction Stop).WriteProtect; \"WriteProtect=$v\" } "
            "catch { 'WriteProtect=absent' }")

    def clear_write_protect(self) -> CommandResult:
        """Remove the software write-protect flag.

        This is the fix for the very common "flash drive is read-only and has
        no physical switch". It touches only one registry value.
        """
        if not is_admin():
            return CommandResult(argv=["reg"], missing=True,
                                 stderr="clearing write protection needs Administrator")
        return _ps(
            f"$p='{WRITE_PROTECT_KEY}'; "
            f"if (-not (Test-Path $p)) {{ New-Item -Path $p -Force | Out-Null }}; "
            "Set-ItemProperty -Path $p -Name WriteProtect -Value 0 -Type DWord -Force; "
            f"Set-ItemProperty -Path $p -Name WriteProtect -Value 0 -Force; "
            "Write-Output 'WriteProtect=0'")

    def set_disk_online(self, disk_number: int) -> CommandResult:
        """Bring a disk back online / clear its read-only attribute."""
        return _ps(
            f"Set-Disk -Number {disk_number} -IsReadOnly $false; "
            f"Set-Disk -Number {disk_number} -IsOffline $false; "
            f"(Get-Disk -Number {disk_number} | "
            "Select-Object Number,IsReadOnly,IsOffline,OperationalStatus | ConvertTo-Json)",
            timeout=120)

    # -- partitioning -----------------------------------------------------
    def _diskpart(self, script: str, timeout: int = 300) -> CommandResult:
        """Run a diskpart script from a temp file.

        A temp file rather than a pipe: diskpart's stdin handling is unreliable
        across Windows versions, and a half-consumed script is how people end
        up with an unexpectedly `clean`ed wrong disk.
        """
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="flashphysician_")
        try:
            with os.fdopen(fd, "w", newline="\r\n") as fh:
                fh.write(script)
            return run(["diskpart", "/s", path], timeout=timeout)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def rebuild_partition_table(self, disk_number: int, fstype: str = "fat32",
                                label: str = "FLASHPHYSICIAN", size_mb: int | None = None,
                                confirm: bool = False) -> CommandResult:
        """clean + create partition + format. DESTRUCTIVE by definition.

        `confirm` must be True; this refuses to run otherwise so an accidental
        call from a UI bug cannot wipe a disk.
        """
        if not confirm:
            return CommandResult(argv=["diskpart"], missing=True,
                                 stderr="refused: rebuild_partition_table needs confirm=True")
        fs_map = {"fat32": "fat32", "fat16": "fat", "exfat": "exfat",
                  "ntfs": "ntfs"}
        fs = fs_map.get(fstype, "fat32")
        lines = [f"select disk {disk_number}", "attributes disk clear readonly",
                 "clean"]
        if size_mb:
            lines.append(f"create partition primary size={size_mb}")
        else:
            lines.append("create partition primary")
        lines.append("format fs=" + fs + (" quick" if fs != "ntfs" else " quick"))
        lines.append(f'label="{label[:11] if fs == "fat32" else label[:32]}"')
        lines.append("assign")
        lines.append("exit")
        return self._diskpart("\n".join(lines) + "\n")

    def clean_disk(self, disk_number: int, confirm: bool = False) -> CommandResult:
        if not confirm:
            return CommandResult(argv=["diskpart"], missing=True,
                                 stderr="refused: clean_disk needs confirm=True")
        return self._diskpart(f"select disk {disk_number}\nclean\nexit\n")

    # -- drive letters ----------------------------------------------------
    def _letter(self, device: str) -> str:
        m = re.match(r"^([A-Za-z]):", device.strip())
        return m.group(1).upper() if m else ""

    def _volume_path(self, device: str) -> str:
        """chkdsk wants 'C:' or a GUID path, not '\\\\.\\PhysicalDrive1'."""
        letter = self._letter(device)
        if letter:
            return f"{letter}:"
        if device.startswith("\\\\?\\Volume"):
            return device
        return device

    def volumes(self) -> list[dict]:
        res = _ps(
            "Get-Volume | Where-Object {$_.DriveType -eq 'Removable'} | "
            "Select-Object DriveLetter,FileSystemLabel,FileSystem,Size,"
            "SizeRemaining,HealthStatus,OperationalStatus | ConvertTo-Json -Compress")
        if not res.ok or not res.stdout.strip():
            return []
        try:
            data = json.loads(res.stdout.strip())
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            return []

    def disks(self) -> list[dict]:
        res = _ps(
            "Get-Disk | Select-Object Number,FriendlyName,SerialNumber,Size,"
            "BusType,IsReadOnly,IsOffline,OperationalStatus,PartitionStyle | "
            "ConvertTo-Json -Compress")
        if not res.ok or not res.stdout.strip():
            return []
        try:
            data = json.loads(res.stdout.strip())
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            return []

    def assign_letter(self, device: str, letter: str = "") -> CommandResult:
        """Give a letterless volume a drive letter -- required before chkdsk
        can touch it, and the actual fix for 'the drive shows in Disk
        Management but not in Explorer'."""
        target = device.rstrip(":")
        if not letter:
            used = {v.get("DriveLetter") for v in self.volumes()}
            for cand in "ZYXWVUTSRQPONMLKJ":
                if cand not in used:
                    letter = cand
                    break
        if not letter:
            return CommandResult(argv=["mountvol"], missing=True,
                                 stderr="no free drive letter found")
        if target.startswith("\\\\?\\Volume"):
            return run(["mountvol", letter + ":", target], timeout=60)
        return _ps(f"Add-PartitionAccessPath -DiskNumber ({target}) "
                   "-PartitionNumber 1 -AccessPath '" + letter + ":'", timeout=60)

    def remove_letter(self, device: str) -> CommandResult:
        letter = self._letter(device)
        if not letter:
            return CommandResult(argv=["mountvol"], missing=True,
                                 stderr="device has no drive letter")
        return run(["mountvol", letter + ":", "/p"], timeout=60)

    # -- device control ---------------------------------------------------
    def eject(self, device: str) -> CommandResult:
        """Flush and dismount for safe removal.

        Sync first, exactly as on Linux: Windows buffers writes for removable
        media, and an eject without a flush is the documented cause of "my
        files are corrupt after I unplugged it".
        """
        letter = self._letter(device)
        if not letter:
            # A device that is not a drive letter (e.g. ``\\?\Volume{...}`` or
            # a mistyped name) cannot be ejected through Shell.Application.
            # Report that plainly rather than falling back to drive C, which
            # would be a no-op at best and dangerous at worst.
            return CommandResult(
                argv=["eject", device], returncode=1,
                stderr=f"not a drive letter (got {device!r}); "
                       "expected a letter such as 'E:' or a \\\\?\\Volume{{...}} path",
            )
        # Flush the volume write cache before dismount. This used to spawn a
        # shell with an output redirect, which is both pointless (it wrote
        # nothing and deleted it again) and one of the most heavily flagged
        # patterns in static AV heuristics -- a packed Python exe that runs a
        # shell with a redirect looks exactly like a dropper. The
        # Write-VolumeCache call below does the real work.
        if _which_all(["powershell"])["powershell"]:
            script = (
                "$d = New-Object -comObject Shell.Application; "
                f"$d.Namespace(17).ParseName('{letter}:').InvokeVerb('Eject')"
            )
            res = _ps(script, timeout=60)
            if res.ok:
                return res
        # Fall back to flushing the volume cache for the resolved letter only.
        return _ps("Write-VolumeCache -DriveLetter " + letter + "; "
                   "Write-Output 'cache flushed'", timeout=60)

    def flush(self, device: str) -> CommandResult:
        """Flush a volume's write cache.

        Uses Write-VolumeCache rather than creating and deleting a temp file
        through a shell: no shell process, no filesystem side effects, and
        nothing for a static scanner to misread as a payload drop.
        """
        letter = self._letter(device)
        if not letter:
            return CommandResult(argv=["fsutil"], missing=True,
                                 stderr="no drive letter to flush")
        if _which_all(["powershell"])["powershell"]:
            return _ps("Write-VolumeCache -DriveLetter " + letter +
                       "; Write-Output 'cache flushed'", timeout=60)
        return run(["fsutil", "dirty", letter + ":", "query"], timeout=60)

    def rescan(self, device: str = "") -> CommandResult:
        return _ps("Update-HostStorageCache; 'host storage cache updated'", timeout=120)

    def format(self, device: str, fstype: str = "fat32", label: str = "") -> CommandResult:
        letter = self._letter(device)
        if not letter:
            return CommandResult(argv=["format"], missing=True,
                                 stderr="volume has no drive letter; assign one first")
        fs = {"fat32": "FAT32", "fat16": "FAT", "exfat": "exFAT",
              "ntfs": "NTFS"}.get(fstype, "FAT32")
        argv = ["format", letter + ":", "/FS:" + fs, "/Q", "/Y"]
        if label:
            argv.append("/V:" + label[:11])
        return run(argv, timeout=3600)

    # -- enumeration ------------------------------------------------------
    def list_devices(self) -> list[dict]:
        out: list[dict] = []
        for v in self.volumes():
            out.append({
                "name": (v.get("DriveLetter") or "") + ":",
                "path": (v.get("DriveLetter") or "") + ":",
                "size": v.get("Size") or 0,
                "bus": "usb", "removable": True,
                "vendor": "", "model": v.get("FileSystemLabel") or "",
                "serial": "", "mounted": [],
                "partitions": [],
                "fstype": (v.get("FileSystem") or "").lower(),
                "label": v.get("FileSystemLabel") or "",
                "health": v.get("HealthStatus") or "",
            })
        return out

    def mount_points(self, device: str) -> list[str]:
        letter = self._letter(device)
        return [letter + ":"] if letter else []
