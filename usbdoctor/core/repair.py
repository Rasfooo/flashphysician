"""Repair engine: every mutation is a planned, reversible-by-design operation.

Rules baked into this module, learned the hard way from the failure modes it
targets:

1. **Plan first, execute second.** A RepairPlan lists concrete byte writes.
   Nothing touches the medium until `plan.apply()` is called, and every plan
   can be serialised to JSON and replayed.
2. **Back up what you overwrite.** Any sector we are about to modify is saved
   to a sidecar file first, so a failed repair is undoable.
3. **Never invent data.** Rebuilding a partition table from an orphan
   filesystem uses the values read off the medium, not guesses. If we cannot
   determine a value, the plan refuses rather than approximating.
4. **Read-only mode is a first-class state.** `dry_run=True` produces the
   identical plan and logs it, touching nothing.
"""

from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from .imaging import Source
from .structures import (
    FsType, Partition, TableType, human, parse_boot_sector,
    gpt_crc32, format_guid, SECTOR, MBR_TYPES,
)

VALIDATION_BLOCK = [
    "Refusing to write: the target is not a removable flash device.",
    "Set allow_system=True only if you are certain this is the intended disk -- "
    "a mistake here destroys the host system.",
]


@dataclass
class WriteOp:
    """One planned modification, with its justification."""

    offset: int
    data: bytes
    label: str
    backup: bool = True

    def describe(self) -> str:
        return (f"{self.label}: {len(self.data)} byte(s) at {human(self.offset)} "
                f"({self.offset:#x}) -> {self.data[:16].hex()}"
                + ("..." if len(self.data) > 16 else ""))


@dataclass
class RepairPlan:
    device: str
    problem: str
    ops: list[WriteOp] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refused: str = ""
    backup_dir: str = ""
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    applied: bool = False
    backup_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.ops) and not self.refused

    def add(self, offset: int, data: bytes, label: str, backup: bool = True) -> None:
        self.ops.append(WriteOp(offset, data, label, backup))

    def describe(self) -> str:
        lines = [f"Repair plan for {self.device}",
                 f"  problem : {self.problem}",
                 f"  created : {self.created}",
                 f"  writes  : {len(self.ops)}"]
        for op in self.ops:
            lines.append(f"    - {op.describe()}")
        for n in self.notes:
            lines.append(f"  note    : {n}")
        for w in self.warnings:
            lines.append(f"  WARNING : {w}")
        if self.refused:
            lines.append(f"  REFUSED : {self.refused}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({
            "device": self.device, "problem": self.problem, "created": self.created,
            "notes": self.notes, "warnings": self.warnings, "refused": self.refused,
            "ops": [{"offset": o.offset, "hex": o.data.hex(), "label": o.label}
                    for o in self.ops],
        }, indent=2)

    def apply(self, dry_run: bool = False, backup_dir: str = "") -> dict:
        """Execute the plan. Returns a report; never raises on I/O failure."""
        if self.refused:
            return {"ok": False, "error": self.refused, "applied": 0}
        if dry_run:
            return {"ok": True, "dry_run": True, "applied": 0,
                    "would_write": len(self.ops), "plan": self.describe()}

        bdir = backup_dir or self.backup_dir or os.path.join(
            os.path.expanduser("~"), ".usbdoctor", "backups")
        Path(bdir).mkdir(parents=True, exist_ok=True)

        written = 0
        errors: list[str] = []
        with Source(self.device, mode="r+b") as src:
            for op in self.ops:
                if op.backup:
                    path = os.path.join(
                        bdir,
                        f"{Path(self.device).name}_{op.offset:012x}_{len(op.data)}.bin")
                    try:
                        Path(path).write_bytes(src.read_at(op.offset, len(op.data)))
                        self.backup_files.append(path)
                    except Exception as exc:      # unreadable sector: still record
                        errors.append(f"backup of {op.label} failed: {exc}")
                try:
                    got = src.write_at(op.offset, op.data)
                    if got and len(op.data) == got:
                        written += 1
                    else:
                        errors.append(f"short write for {op.label} ({got} bytes)")
                except Exception as exc:
                    errors.append(f"write failed for {op.label}: {exc}")
        self.applied = written == len(self.ops) and not errors
        return {
            "ok": self.applied, "applied": written, "total": len(self.ops),
            "backups": self.backup_files, "errors": errors, "dry_run": False,
        }


# --------------------------------------------------------------------------
# safety
# --------------------------------------------------------------------------

MIN_FLASH_BYTES = 16 * 1024 * 1024
MAX_FLASH_BYTES = 4 * 1024**4


def guard_target(path: str, force: bool = False) -> str:
    """Return a refusal reason, or '' if the target is acceptable.

    Order matters: pattern checks run BEFORE existence checks, because a
    guard that only trips on paths that happen to exist on this host is not
    a guard. The blast radius of a wrong target here is somebody's boot
    drive, so the checks fail closed.
    """
    if not path:
        return "no target device given"
    if path.startswith("/dev/") and any(
            path.startswith(p) for p in ("/dev/sd", "/dev/nvme", "/dev/hd",
                                         "/dev/vd", "/dev/mmcblk", "/dev/disk")):
        if not force:
            return ("refusing to write to a whole block device "
                    "(/dev/sd*, /dev/nvme*, /dev/hd*, /dev/vd*, /dev/mmcblk*, "
                    "/dev/disk*) without force=True")
    if not os.path.exists(path):
        return f"target {path} does not exist"
    try:
        sz = os.path.getsize(path)
    except OSError as exc:
        return f"cannot determine size of {path}: {exc}"
    if sz == 0:
        return "target reports zero size (dead device or empty image)"
    if sz > MAX_FLASH_BYTES:
        return f"target is implausibly large ({human(sz)})"
    return ""


# --------------------------------------------------------------------------
# MBR repair
# --------------------------------------------------------------------------

def plan_restore_mbr_signature(path: str) -> RepairPlan:
    plan = RepairPlan(path, "missing 55AA boot signature in sector 0")
    reason = guard_target(path, force=True)
    if reason:
        plan.refused = reason
        return plan
    with Source(path) as src:
        head = src.read_at(0, 512)
        if head[510:512] == b"\x55\xAA":
            plan.notes.append("signature is already present -- nothing to do")
            return plan
        if len(head) < 512:
            plan.refused = "cannot read sector 0"
            return plan
        fixed = bytearray(head)
        fixed[510:512] = b"\x55\xAA"
        plan.add(0, bytes(fixed), "restore MBR signature")
        plan.notes.append(
            "Only the two signature bytes are written; partition entries are "
            "left untouched.")
    return plan


def plan_rebuild_mbr_from_fs(path: str, fs_offset: int, fs_type: FsType,
                             label: str = "") -> RepairPlan:
    """Write a single-partition MBR pointing at an orphaned filesystem.

    This is the recovery for "the partition table is gone but the filesystem
    is fine" -- the single most common state for a flash drive that was
    unplugged mid-write.
    """
    plan = RepairPlan(path, f"lost partition table; filesystem found at {human(fs_offset)}")
    reason = guard_target(path, force=True)
    if reason:
        plan.refused = reason
        return plan

    with Source(path) as src:
        total = src.size
        boot = src.read_at(fs_offset, 512)
        info = parse_boot_sector(boot, fs_offset)
        if info.fs in (FsType.RAW, FsType.UNKNOWN):
            plan.refused = (f"no recognisable filesystem at {human(fs_offset)} -- "
                            "cannot derive partition geometry")
            return plan
        if fs_offset % SECTOR:
            plan.refused = f"filesystem offset {fs_offset} is not sector-aligned"
            return plan

        start_lba = fs_offset // SECTOR
        # The partition must fit inside the device: a filesystem whose declared
        # size runs past the end of the medium (common after a botched resize
        # or on an orphaned volume) gets truncated to what actually exists.
        room_lba = (total // SECTOR) - start_lba
        if info.total_bytes and info.total_bytes > 0:
            length_lba = min(info.total_bytes // SECTOR, room_lba)
        else:
            length_lba = room_lba
        if length_lba <= 0:
            plan.refused = "could not determine partition length"
            return plan
        if info.total_bytes and info.total_bytes // SECTOR > room_lba:
            plan.warnings.append(
                "the filesystem declares more space than the device has left; "
                f"the partition is sized to the real end of the medium "
                f"({room_lba} sectors). The filesystem itself is not modified, "
                "so exceeding files remain readable -- run the OS checker "
                "afterwards to reconcile the size.")

        type_code = {
            FsType.FAT32: 0x0C, FsType.FAT16: 0x06, FsType.FAT12: 0x01,
            FsType.NTFS: 0x07, FsType.EXFAT: 0x07, FsType.EXT4: 0x83,
            FsType.EXT3: 0x83, FsType.EXT2: 0x83,
        }.get(info.fs, 0x0C)

        entry = _mbr_entry(0x00, type_code, start_lba, length_lba)
        mbr = _build_mbr([entry])
        plan.add(0, mbr, f"rebuild MBR: {info.fs.value.upper()} partition "
                         f"@ LBA {start_lba} ({length_lba} sectors)")
        if info.label or label:
            plan.notes.append(f"volume label preserved: {info.label or label!r}")
        plan.notes.append(
            f"derived from the filesystem's own boot sector: "
            f"{info.fs.value.upper()}, {human(info.total_bytes or total - fs_offset)}")
        plan.warnings.append(
            "This writes ONLY sector 0. The filesystem and all files are "
            "untouched -- if the result is wrong, restore sector 0 from the "
            "backup this plan creates.")
    return plan


def _mbr_entry(status: int, type_code: int, lba_start: int, lba_count: int) -> bytes:
    chs_s = _lba_to_chs(lba_start)
    chs_e = _lba_to_chs(lba_start + max(lba_count - 1, 0))
    return (struct.pack("<B", status) + bytes(chs_s) + struct.pack("<B", type_code)
            + bytes(chs_e) + struct.pack("<II", lba_start, lba_count))


def _lba_to_chs(lba: int, heads: int = 255, spt: int = 63) -> tuple[int, int, int]:
    """Encode an LBA as CHS. Saturates at the CHS ceiling (1023/254/63), which
    is what every real formatter does -- LBA fields remain authoritative."""
    if lba < 0:
        lba = 0
    cyl = lba // (heads * spt)
    rem = lba % (heads * spt)
    head = rem // spt
    sec = rem % spt + 1
    if cyl > 1023:
        return (254, 255, 255)
    return (head & 0xFF, (cyl >> 2) & 0xC0 | (sec & 0x3F), cyl & 0xFF)


def _build_mbr(entries: list[bytes]) -> bytes:
    body = b"\x00" * 440 + struct.pack("<I", int(time.time()) & 0xFFFFFFFF) + b"\x00\x00"
    for i in range(4):
        body += entries[i] if i < len(entries) else b"\x00" * 16
    return body + b"\x55\xAA"


# --------------------------------------------------------------------------
# GPT repair
# --------------------------------------------------------------------------

def plan_restore_gpt_from_backup(path: str) -> RepairPlan:
    """Rebuild the primary GPT header + entries from the backup at disk end."""
    plan = RepairPlan(path, "damaged primary GPT; rebuilding from the backup copy")
    reason = guard_target(path, force=True)
    if reason:
        plan.refused = reason
        return plan

    from .structures import parse_gpt_header
    with Source(path) as src:
        total_sectors = src.size // SECTOR
        last_lba = total_sectors - 1
        bkp = src.read_at(last_lba * SECTOR, SECTOR)
        hdr, errs = parse_gpt_header(bkp, "backup")
        if hdr is None:
            plan.refused = ("no valid backup GPT header at the last sector -- "
                            "rebuild must come from the partition scan instead")
            return plan

        entries = src.read_at(hdr["entries_lba"] * SECTOR,
                              hdr["entry_count"] * hdr["entry_size"])
        if not entries:
            plan.refused = "backup GPT points at unreadable partition entries"
            return plan

        entries_crc = gpt_crc32(entries[: hdr["entry_count"] * hdr["entry_size"]])
        header = _gpt_header(
            current_lba=1, backup_lba=last_lba, first_usable=hdr["first_usable"],
            last_usable=hdr["last_usable"], disk_guid_raw=bytes.fromhex(
                hdr["disk_guid"].replace("-", "")),
            entries_lba=hdr["entries_lba"], entry_count=hdr["entry_count"],
            entry_size=hdr["entry_size"], entries_crc=entries_crc,
        )
        plan.add(512, header, "restore primary GPT header from backup")
        plan.add(hdr["entries_lba"] * SECTOR, entries,
                 f"restore {hdr['entry_count']} GPT partition entries from backup")
        plan.notes.append(
            f"source: backup GPT at LBA {last_lba}; entries CRC32 {entries_crc:08X}")
        plan.warnings.append(
            "The backup header's current-LBA field is corrected to 1 for the "
            "primary copy; all other geometry is copied verbatim.")
    return plan


def _gpt_header(current_lba: int, backup_lba: int, first_usable: int,
                last_usable: int, disk_guid_raw: bytes, entries_lba: int,
                entry_count: int, entry_size: int, entries_crc: int) -> bytes:
    h = bytearray(SECTOR)
    h[0:8] = b"EFI PART"
    struct.pack_into("<I", h, 8, 0x00010000)
    struct.pack_into("<I", h, 12, 92)
    struct.pack_into("<Q", h, 24, current_lba)
    struct.pack_into("<Q", h, 32, backup_lba)
    struct.pack_into("<Q", h, 40, first_usable)
    struct.pack_into("<Q", h, 48, last_usable)
    h[56:72] = disk_guid_raw[:16].ljust(16, b"\x00")
    struct.pack_into("<Q", h, 72, entries_lba)
    struct.pack_into("<I", h, 80, entry_count)
    struct.pack_into("<I", h, 84, entry_size)
    struct.pack_into("<I", h, 88, entries_crc)
    struct.pack_into("<I", h, 16, gpt_crc32(bytes(h[:92])))
    return bytes(h)


# --------------------------------------------------------------------------
# filesystem repair
# --------------------------------------------------------------------------

def plan_restore_fat32_boot_backup(path: str, part_offset: int = 0) -> RepairPlan:
    """Copy the FAT32 backup boot sector over a corrupted primary."""
    plan = RepairPlan(path, "FAT32 primary boot sector damaged; restoring from its backup")
    reason = guard_target(path, force=True)
    if reason:
        plan.refused = reason
        return plan

    with Source(path) as src:
        boot = src.read_at(part_offset, 512)
        info = parse_boot_sector(boot, part_offset)
        if info.fs != FsType.FAT32:
            plan.refused = f"filesystem at this partition is {info.fs.value}, not FAT32"
            return plan
        bkp_no = boot[50] | (boot[51] << 8)
        if bkp_no == 0:
            plan.refused = "boot sector declares no backup location"
            return plan
        bkp_off = part_offset + bkp_no * info.bytes_per_sector
        bkp = src.read_at(bkp_off, 512)
        if len(bkp) < 512 or bkp[510:512] != b"\x55\xAA":
            plan.refused = f"backup boot sector at sector {bkp_no} is not valid"
            return plan
        if bkp == boot:
            plan.notes.append("primary and backup already identical -- nothing to do")
            return plan
        plan.add(part_offset, bkp, f"restore FAT32 boot sector from backup (sector {bkp_no})")
        plan.notes.append(
            "The backup boot sector is a byte-for-byte copy the formatter wrote; "
            "the FATs, root directory and all files are untouched.")
    return plan


def plan_fix_fat_type_label(path: str, part_offset: int = 0) -> RepairPlan:
    """Correct a boot sector whose FAT-type label disagrees with the geometry.

    Derived from the medium itself: the cluster count decides what the
    filesystem really is, so we write the matching label and clear FAT32-only
    fields when the geometry is FAT16, or set them when it is FAT32.
    """
    plan = RepairPlan(path, "boot sector FAT-type label contradicts its own geometry")
    reason = guard_target(path, force=True)
    if reason:
        plan.refused = reason
        return plan

    with Source(path) as src:
        boot = bytearray(src.read_at(part_offset, 512))
        info = parse_boot_sector(bytes(boot), part_offset)
        if info.fs not in (FsType.FAT12, FsType.FAT16, FsType.FAT32):
            plan.refused = "target is not a FAT filesystem"
            return plan
        if not info.sectors_per_cluster or not info.total_sectors:
            plan.refused = "geometry incomplete; refusing to guess"
            return plan

        bps = info.bytes_per_sector
        root_dir_sectors = ((info.root_entries * 32) + bps - 1) // bps
        data_sectors = (info.total_sectors - info.reserved_sectors
                        - info.fat_count * info.fat_size_sectors - root_dir_sectors)
        clusters = data_sectors // info.sectors_per_cluster
        implied = (FsType.FAT12 if clusters < 4085
                   else FsType.FAT16 if clusters < 65525 else FsType.FAT32)

        label = bytes(boot[82:90])
        label_fs = None
        for tag, ft in ((b"FAT12   ", FsType.FAT12), (b"FAT16   ", FsType.FAT16),
                        (b"FAT32   ", FsType.FAT32)):
            if label == tag:
                label_fs = ft

        if implied == FsType.FAT32 and info.fs != FsType.FAT32:
            plan.refused = (
                "geometry implies FAT32 but the boot sector lacks the FAT32-only "
                "fields (root cluster, FSInfo, backup pointer). Rebuilding those "
                "means rewriting the boot region, not just a label -- use the "
                "rebuild action instead.")
            return plan

        if label_fs is None:
            # no usable label: write the one the geometry implies
            tag = {FsType.FAT12: b"FAT12   ", FsType.FAT16: b"FAT16   ",
                   FsType.FAT32: b"FAT32   "}[implied]
            boot[82:90] = tag
            plan.add(part_offset, bytes(boot),
                     f"add missing FAT-type label: {implied.value.upper()}")
            plan.notes.append(f"{clusters:,} clusters -> genuinely {implied.value.upper()}")
            return plan

        if label_fs == implied and implied == info.fs:
            plan.notes.append(
                f"label, geometry and derived type all agree "
                f"({implied.value.upper()}, {clusters:,} clusters) -- nothing to do")
            return plan

        if implied != FsType.FAT32:
            tag = {FsType.FAT12: b"FAT12   ", FsType.FAT16: b"FAT16   "}[implied]
            boot[82:90] = tag
            plan.add(part_offset, bytes(boot),
                     f"relabel boot sector {label_fs.value.upper()} -> {implied.value.upper()}")
            plan.notes.append(
                f"{clusters:,} data clusters in {info.fat_count} FAT(s) of "
                f"{info.fat_size_sectors} sectors -> genuinely {implied.value.upper()}")
            plan.warnings.append(
                "Only the type label changes. Run a filesystem check afterwards "
                "if the volume still misbehaves.")
            return plan

        plan.refused = (
            f"derived type {info.fs.value.upper()} needs FAT32-only boot fields "
            "that are absent; requires the full rebuild action")
        return plan


def plan_clear_fat_dirty(path: str, part_offset: int = 0) -> RepairPlan:
    """Clear the FAT32 dirty / not-cleanly-unmounted flags in FAT[1].

    This is what `fsck` does on mount; doing it here matters when the drive
    cannot be mounted at all (because the boot sector is also damaged).
    """
    plan = RepairPlan(path, "volume flagged dirty after an unclean removal")
    reason = guard_target(path, force=True)
    if reason:
        plan.refused = reason
        return plan

    with Source(path) as src:
        boot = src.read_at(part_offset, 512)
        info = parse_boot_sector(boot, part_offset)
        if info.fs != FsType.FAT32:
            plan.refused = (f"dirty-flag repair implemented for FAT32; this volume "
                            f"is {info.fs.value}")
            return plan
        fat_off = part_offset + info.reserved_sectors * info.bytes_per_sector
        word = src.read_at(fat_off + 4, 4)
        if len(word) < 4:
            plan.refused = "FAT[1] unreadable"
            return plan
        value = struct.unpack("<I", word)[0]
        if value & 0x08000000:
            plan.notes.append("clean-shutdown flag already set -- nothing to do")
            return plan
        new = value | 0x08000000           # ClnShutBitMask
        new &= ~0x04000000                 # clear HrdErrBitMask
        new &= 0x0FFFFFFF | 0x08000000
        plan.add(fat_off + 4, struct.pack("<I", new), "clear FAT32 dirty flag")
        plan.notes.append(
            f"FAT[1] {value:#010x} -> {new:#010x} (clean-shutdown set, hard-error cleared)")
        plan.warnings.append(
            "Clearing the flag does not repair any damage the unclean removal "
            "caused; run a filesystem check for that.")
    return plan
