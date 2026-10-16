"""The diagnostic engine: turn raw bytes into a verdict a human can act on.

Design rule: **never trust the OS, always read the medium.** A drive that
Windows reports as "RAW, 0 bytes" often still has a perfectly readable boot
sector and FAT -- the failure is in the partition table, and that is
recoverable. Conversely a drive that mounts fine can be lying about its
capacity. Both cases are decided by bytes, not by `lsblk` output.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .imaging import DriveInfo, Source
from .structures import (
    FsInfo,
    FsType,
    MbrResult,
    Partition,
    TableType,
    human,
    parse_boot_sector,
    parse_fsinfo,
    parse_gpt_entries,
    parse_gpt_header,
    fat_flags,
    read_fat_entries,
    fat_eoc,
    fat_bad,
    fat_reserved_lo,
    exfat_checksum,
)


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"


SEV_WEIGHT = {
    Severity.OK: 0,
    Severity.INFO: 1,
    Severity.WARN: 6,
    Severity.ERROR: 18,
    Severity.FATAL: 40,
}


@dataclass
class Finding:
    severity: Severity
    title: str
    detail: str = ""
    layer: str = "disk"          # disk / table / fs / data
    code: str = ""
    evidence: dict = field(default_factory=dict)

    def line(self) -> str:
        icon = {
            Severity.OK: "[ OK ]",
            Severity.INFO: "[INFO]",
            Severity.WARN: "[WARN]",
            Severity.ERROR: "[FAIL]",
            Severity.FATAL: "[DEAD]",
        }[self.severity]
        return f"{icon} {self.title}" + (f" -- {self.detail}" if self.detail else "")


@dataclass
class Report:
    device: str
    size_bytes: int = 0
    table: TableType = TableType.NONE
    partitions: list[Partition] = field(default_factory=list)
    fs: FsInfo | None = None
    findings: list[Finding] = field(default_factory=list)
    health: int = 100
    grade: str = "?"
    verdict: str = ""
    duration_s: float = 0.0
    stamp: str = ""
    raw: dict = field(default_factory=dict)

    def add(self, sev: Severity, title: str, detail: str = "", **kw) -> Finding:
        f = Finding(severity=sev, title=title, detail=detail, **kw)
        self.findings.append(f)
        return f

    def count(self, sev: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == sev)

    @property
    def worst(self) -> Severity:
        order = [Severity.FATAL, Severity.ERROR, Severity.WARN, Severity.INFO, Severity.OK]
        for s in order:
            if self.count(s):
                return s
        return Severity.OK

    def summary(self) -> str:
        return (
            f"{self.device} | {human(self.size_bytes)} | table={self.table.value} | "
            f"fs={(self.fs.fs.value if self.fs else '?')} | health={self.health}/100 ({self.grade})"
        )


def _grade(health: int) -> str:
    if health >= 90:
        return "A"
    if health >= 75:
        return "B"
    if health >= 55:
        return "C"
    if health >= 35:
        return "D"
    if health >= 15:
        return "E"
    return "F"


# --------------------------------------------------------------------------
# partition table
# --------------------------------------------------------------------------

def inspect_partition_table(src: Source, rep: Report) -> None:
    head = src.read_at(0, 512)
    from .structures import parse_mbr

    total = src.size
    mbr = parse_mbr(head, total)

    gpt_hdr_raw = src.read_at(512, 512)
    gpt_hdr, gpt_hdr_errs = parse_gpt_header(gpt_hdr_raw, "primary GPT header")

    has_gpt = gpt_hdr is not None
    has_mbr = mbr.valid_signature and any(p.type_code != "0xEE" for p in mbr.partitions)

    if has_gpt and mbr.valid_signature:
        rep.table = TableType.HYBRID if has_mbr else TableType.GPT
    elif has_gpt:
        rep.table = TableType.GPT
    elif mbr.valid_signature:
        rep.table = TableType.MBR
    elif any(p for p in mbr.partitions):
        rep.table = TableType.CORRUPT
    else:
        rep.table = TableType.NONE

    # ---- MBR signature / boot code ----
    if not mbr.valid_signature:
        if rep.table == TableType.NONE:
            rep.add(
                Severity.FATAL,
                "No partition table at all (blank boot sector)",
                "Sector 0 holds no 55AA signature: the drive is unformatted, "
                "wiped, or the partition table was destroyed.",
                layer="table",
                code="TABLE_MISSING",
            )
        elif rep.table == TableType.GPT:
            rep.add(
                Severity.ERROR,
                "Protective MBR is missing its 55AA signature",
                "The GPT is intact, but a host that only reads a legacy MBR will "
                "see the disk as unformatted. Windows disk management and BIOS "
                "boot often do exactly that.",
                layer="table",
                code="MBR_SIG",
            )
        else:
            rep.add(
                Severity.ERROR,
                "Missing MBR boot signature (55AA)",
                "Partition entries exist but the signature is gone -- the boot "
                "sector was partially overwritten. Rebuildable.",
                layer="table",
                code="MBR_SIG",
            )
    else:
        rep.add(Severity.OK, "MBR boot signature present (55AA)", layer="table", code="MBR_SIG_OK")

    for e in mbr.errors:
        rep.add(Severity.ERROR, e, layer="table", code="MBR_ENTRY")
    for w in mbr.warnings:
        rep.add(Severity.WARN, w, layer="table", code="MBR_WARN")

    if mbr.boot_code_nonzero and not has_gpt:
        rep.add(
            Severity.INFO,
            "Non-zero boot code in the MBR",
            "Boot code present: the drive was set up as bootable at some point.",
            layer="table",
            code="MBR_BOOTCODE",
        )

    rep.partitions = list(mbr.partitions)

    # ---- GPT ----
    if has_gpt and gpt_hdr:
        rep.raw["gpt_primary"] = gpt_hdr
        # A GPT disk always carries a protective MBR (type 0xEE). That is not
        # a hybrid layout -- hybrid means real, non-EE partitions coexist.
        real_mbr_parts = [p for p in mbr.partitions if p.type_code != "0xEE"]
        if real_mbr_parts and mbr.valid_signature:
            rep.table = TableType.HYBRID
            rep.add(
                Severity.WARN,
                "Hybrid MBR + GPT layout detected",
                f"{len(real_mbr_parts)} legacy MBR partition(s) alongside the GPT. "
                "Windows may see a different layout than Linux/macOS.",
                layer="table", code="HYBRID",
            )
        else:
            rep.table = TableType.GPT
        if gpt_hdr["crc_ok"]:
            rep.add(Severity.OK, "GPT primary header CRC32 valid",
                    f"disk GUID {gpt_hdr['disk_guid']}", layer="table", code="GPT_CRC_OK")
        for e in gpt_hdr_errs:
            rep.add(Severity.ERROR, e, layer="table", code="GPT_HEADER")

        entries_raw = src.read_at(gpt_hdr["entries_lba"] * 512,
                                  min(gpt_hdr["entry_count"] * gpt_hdr["entry_size"], 1024 * 1024))
        gparts, perrs = parse_gpt_entries(entries_raw, gpt_hdr)
        for e in perrs:
            rep.add(Severity.ERROR, e, layer="table", code="GPT_ENTRIES")
        if gparts:
            rep.partitions = gparts
            if all(gpt_hdr["crc_ok"] for _ in [0]) and not perrs:
                rep.add(Severity.OK, f"GPT table lists {len(gparts)} partition(s)",
                        layer="table", code="GPT_OK")

        # backup header + entries
        last_lba = (src.size // 512) - 1
        bkp_raw = src.read_at(gpt_hdr["backup_lba"] * 512, 512)
        bkp_hdr, bkp_errs = parse_gpt_header(bkp_raw, "backup GPT header")
        rep.raw["gpt_backup"] = bkp_hdr
        if bkp_hdr is None:
            rep.add(Severity.ERROR, "Backup GPT header unreadable or invalid",
                    f"expected at LBA {gpt_hdr['backup_lba']} (device ends at LBA {last_lba})",
                    layer="table", code="GPT_BACKUP_MISSING")
        else:
            if bkp_hdr["crc_ok"]:
                rep.add(Severity.OK, "GPT backup header CRC32 valid",
                        layer="table", code="GPT_BACKUP_OK")
            else:
                rep.add(Severity.WARN, "GPT backup header CRC32 mismatch",
                        "Primary is authoritative; backup can be regenerated.",
                        layer="table", code="GPT_BACKUP_CRC")
            if bkp_hdr["current_lba"] != last_lba:
                rep.add(
                    Severity.WARN,
                    "GPT backup header is not at the last sector",
                    f"says LBA {bkp_hdr['current_lba']}, device last LBA is {last_lba} -- "
                    "the drive was imaged or its size changed.",
                    layer="table", code="GPT_BACKUP_LBA",
                )
            if bkp_hdr["last_usable"] > (src.size // 512):
                rep.add(Severity.WARN, "GPT claims usable space beyond the device",
                        layer="table", code="GPT_OVERSIZE")

        if rep.table == TableType.CORRUPT and gparts:
            rep.add(Severity.INFO, "GPT entries recovered despite signature damage",
                    "Reconstructable from the partition entry array.",
                    layer="table", code="GPT_SALVAGE")

    # ---- protective-MBR with no GPT ----
    if mbr.protective and not has_gpt:
        rep.add(
            Severity.ERROR,
            "Protective MBR (type 0xEE) but the GPT header is missing",
            "Signature of a drive wiped with `diskpart clean` or a clobbered "
            "GPT. The backup GPT at the end of the disk may still hold the table.",
            layer="table", code="GPT_PROTECTIVE_ONLY",
        )
        # try the backup at last LBA anyway
        last = (src.size // 512) - 1
        bkp_raw = src.read_at(last * 512, 512)
        bkp_hdr, _ = parse_gpt_header(bkp_raw, "backup GPT header")
        if bkp_hdr and bkp_hdr["crc_ok"]:
            rep.add(
                Severity.INFO,
                "Backup GPT found at the end of the drive",
                f"Recoverable: partition entries at LBA {bkp_hdr['entries_lba']}, "
                "usable to rebuild the primary table.",
                layer="table", code="GPT_BACKUP_RECOVERABLE",
            )


# --------------------------------------------------------------------------
# filesystem
# --------------------------------------------------------------------------

def inspect_filesystem(src: Source, rep: Report, offset: int = 0) -> None:
    """Identify and audit the filesystem at `offset`."""
    boot = src.read_at(offset, 512)
    info = parse_boot_sector(boot, offset)
    rep.fs = info

    if info.fs == FsType.RAW:
        rep.add(Severity.FATAL, "Filesystem is RAW (blank boot region)",
                info.errors[0] if info.errors else "", layer="fs", code="FS_RAW")
        return
    if info.fs == FsType.UNKNOWN:
        for e in info.errors:
            rep.add(Severity.ERROR, e, layer="fs", code="FS_UNKNOWN")
        return

    rep.add(Severity.OK, f"Filesystem recognised: {info.fs.value.upper()}",
            (f"label {info.label!r} " if info.label else "") +
            (f"serial {info.serial}" if info.serial else ""),
            layer="fs", code="FS_OK")

    for e in info.errors:
        rep.add(Severity.ERROR, f"{info.fs.value.upper()}: {e}", layer="fs", code="FS_META")
    for w in info.warnings:
        rep.add(Severity.WARN, f"{info.fs.value.upper()}: {w}", layer="fs", code="FS_META_WARN")

    # Surface a label/geometry disagreement under a stable code, since this is
    # the single most common reason a physically fine drive shows as RAW.
    boot_geom = src.read_at(offset, 512)
    if boot_geom[82:90] in (b"FAT32   ", b"FAT16   ", b"FAT12   "):
        declared = boot_geom[82:90].decode().strip().lower()
        if declared != info.fs.value and any("labelled" in e for e in info.errors):
            rep.add(
                Severity.ERROR,
                f"Filesystem type/geometry mismatch: boot sector is labelled "
                f"{declared.upper()} but the layout is {info.fs.value.upper()}",
                "The FAT size and cluster count describe a different FAT type "
                "than the label claims. Windows mounts such a volume as RAW "
                "even though the data region is intact -- rewriting the boot "
                "sector to match the real layout usually restores full access.",
                layer="fs", code="FAT_TYPE_MISMATCH",
                evidence={"declared": declared, "actual": info.fs.value},
            )

    if info.total_bytes and src.size:
        # A filesystem whose size wildly disagrees with its container is a
        # hallmark of a partition table that was rebuilt with wrong values.
        ratio = info.total_bytes / src.size
        if ratio > 1.02:
            rep.add(
                Severity.ERROR,
                "Filesystem is larger than its container",
                f"FS claims {human(info.total_bytes)}, container holds {human(src.size)} "
                f"({ratio * 100:.0f}%) -- boot sector belongs to a different partition table.",
                layer="fs", code="FS_OVERSIZE",
            )
        elif ratio < 0.5 and offset == 0:
            rep.add(Severity.INFO, "Filesystem is much smaller than the device",
                    f"FS {human(info.total_bytes)} vs device {human(src.size)} -- "
                    "the rest is unallocated (normal after repartitioning).",
                    layer="fs", code="FS_UNDERSIZE")

    if info.fs in (FsType.FAT32, FsType.FAT16, FsType.FAT12):
        _audit_fat(src, rep, info, offset)
    elif info.fs == FsType.EXFAT:
        _audit_exfat(src, rep, info, offset)
    elif info.fs == FsType.NTFS:
        _audit_ntfs(src, rep, info, offset)


def _audit_fat(src: Source, rep: Report, info: FsInfo, offset: int) -> None:
    bps = info.bytes_per_sector

    # --- boot sector backup (FAT32 number at 50) ---
    if info.fs == FsType.FAT32:
        boot = src.read_at(offset, 512)
        bkp_no = boot[50] | (boot[51] << 8)
        if bkp_no:
            bkp = src.read_at(offset + bkp_no * bps, 512)
            if bkp == boot:
                rep.add(Severity.OK, "FAT32 boot sector backup matches the primary",
                        f"backup at sector {bkp_no}", layer="fs", code="FAT_BOOTBACKUP_OK")
            else:
                rep.add(
                    Severity.WARN,
                    "FAT32 boot sector backup differs from the primary",
                    f"backup at sector {bkp_no} -- if the primary is ever lost this "
                    "copy is not usable.",
                    layer="fs", code="FAT_BOOTBACKUP_MISMATCH",
                )

        # --- FSInfo ---
        fsinfo_no = boot[48] | (boot[49] << 8)
        if fsinfo_no:
            fi = src.read_at(offset + fsinfo_no * bps, 512)
            parse_fsinfo(fi, info)
            if info.free_clusters is not None:
                rep.add(Severity.OK, "FAT32 FSInfo is readable",
                        f"{info.free_clusters:,} free clusters "
                        f"({human(info.free_clusters * info.cluster_size)})",
                        layer="fs", code="FSINFO_OK")

        # --- clean shutdown / dirty bit ---
        fat_start = offset + info.reserved_sectors * bps
        fat_first = src.read_at(fat_start, 16)
        fat_flags(fat_first, info)
        if info.dirty_bit:
            rep.add(
                Severity.WARN,
                "Volume is flagged DIRTY (not unmounted cleanly)",
                "Cause: unsafe removal or power loss mid-write. `fsck`/`chkdsk` "
                "normally clears this on the next mount.",
                layer="fs", code="FAT_DIRTY",
            )
        else:
            rep.add(Severity.OK, "Volume is marked clean (properly unmounted)",
                    layer="fs", code="FAT_CLEAN")

    # --- FAT copies agree? ---
    if info.fat_count >= 2 and info.fat_size_sectors and bps:
        f1 = src.read_at(offset + info.reserved_sectors * bps, min(info.fat_size_sectors * bps, 4 * 1024 * 1024))
        f2 = src.read_at(offset + (info.reserved_sectors + info.fat_size_sectors) * bps,
                         min(info.fat_size_sectors * bps, 4 * 1024 * 1024))
        if f1 and f2:
            if f1 == f2:
                rep.add(Severity.OK, f"FAT copies identical ({info.fat_count}x)",
                        f"{human(len(f1))} compared", layer="fs", code="FAT_COPIES_OK")
            else:
                diff = sum(1 for a, b in zip(f1, f2) if a != b)
                rep.add(
                    Severity.ERROR,
                    "FAT copies disagree",
                    f"{diff:,} byte(s) differ across {human(len(f1))} -- the second FAT "
                    "is stale or one copy is damaged (classic interrupted format).",
                    layer="fs", code="FAT_COPIES_DIFF",
                )

    # --- cluster chain sanity ---
    entries = read_fat_entries(src, info, offset, limit=8192)
    if entries:
        eoc = fat_eoc(info)
        bad = fat_bad(info)
        res_lo = fat_reserved_lo(info)
        mask = {"fat32": 0x0FFFFFFF, "fat16": 0xFFFF, "fat12": 0xFFF}.get(info.fs.value, 0xFFFFFFFF)

        used = 0
        dead = 0
        out_of_range = 0
        max_cluster = (info.total_sectors * bps) // info.cluster_size if info.cluster_size else 0
        for idx, e in enumerate(entries[2:], start=2):
            v = e & mask
            if v == 0:
                continue
            used += 1
            if v == bad:
                dead += 1
            elif res_lo <= v < eoc:
                # reserved-but-not-EOC values are legal markers, not errors
                continue
            elif v >= 2 and (max_cluster == 0 or v <= max_cluster) and v < res_lo:
                pass
            elif v != eoc and v != mask:
                out_of_range += 1
        total_clusters = max(max_cluster - 2, 1)
        # FAT[1] holds status bits, not a cluster pointer -- and reserved
        # entries at the top of the table are normal, so bound the sample we
        # use for the "how full is this volume" estimate.
        scan_window = max(len(entries) - 2, 1)
        info.fat_used_ratio = min(used / scan_window, 1.0)

        if dead:
            rep.add(
                Severity.WARN,
                f"{dead} cluster(s) marked BAD in the FAT",
                "The controller already remapped these. Repeated growth means the "
                "flash is wearing out -- back up now.",
                layer="data", code="FAT_BADCLUSTER", evidence={"bad_clusters": dead},
            )
        else:
            rep.add(Severity.OK, "No clusters flagged bad in the FAT",
                    f"{len(entries):,} entries scanned", layer="data", code="FAT_NO_BAD")

        if out_of_range:
            rep.add(
                Severity.ERROR,
                f"{out_of_range} FAT entry/entries point outside the data region",
                "Corrupt allocation table -- directory entries will appear as "
                "garbage or files will be unreadable.",
                layer="fs", code="FAT_RANGE", evidence={"out_of_range": out_of_range},
            )

    # --- type byte vs implied type, in BOTH directions ---
    # A formatter that writes the wrong type byte is one of the most common
    # field failures: Windows then refuses to mount and shows the volume as
    # RAW even though every sector is readable. Check the count against what
    # the boot sector claims, and report whichever way it disagrees.
    if info.fat_count == 2 and info.total_sectors > 0 and bps and info.sectors_per_cluster:
        root_dir_sectors = ((info.root_entries * 32) + bps - 1) // bps
        data_sectors = (info.total_sectors - info.reserved_sectors
                        - info.fat_count * info.fat_size_sectors - root_dir_sectors)
        data_clusters = data_sectors // info.sectors_per_cluster
        if data_clusters > 0:
            implied = ("fat12" if data_clusters < 4085
                       else ("fat16" if data_clusters < 65525 else "fat32"))
            eff = info.fs.value
            # FAT12/16/32 are separated by cluster count only; if the count
            # says otherwise, the type byte is wrong.
            wrong_by_band = (eff != implied)
            # a FAT32 label on a FAT16-sized volume is 'wrong' only if the
            # FAT table is also too small to be a real FAT32 FAT
            fat_bits_needed = 32 if eff == "fat32" else (16 if eff == "fat16" else 12)
            fat_sectors_needed = int((data_clusters * fat_bits_needed / 8 + bps - 1) // bps)
            fat_too_small = info.fat_size_sectors < fat_sectors_needed

            if wrong_by_band or fat_too_small:
                rep.add(
                    Severity.ERROR,
                    f"Filesystem type/geometry mismatch: boot sector says "
                    f"{eff.upper()}, cluster count implies {implied.upper()}",
                    f"{data_clusters:,} data clusters, FAT is {info.fat_size_sectors} "
                    f"sector(s) but {eff.upper()} needs {fat_sectors_needed}. "
                    "This is why Windows reports the volume as RAW -- the "
                    "boot sector must be rewritten to match the real layout "
                    "(or vice versa); the data itself is usually intact.",
                    layer="fs",
                    code="FAT_TYPE_MISMATCH",
                    evidence={"declared": eff, "implied": implied,
                              "clusters": data_clusters,
                              "fat_size": info.fat_size_sectors,
                              "fat_needed": fat_sectors_needed},
                )


def _audit_exfat(src: Source, rep: Report, info: FsInfo, offset: int) -> None:
    region = src.read_at(offset, 12 * 512)
    if len(region) < 11 * 512:
        return
    stored = u32_at(region, 5632)
    calc = exfat_checksum(region[: 11 * 512])
    if stored == calc:
        rep.add(Severity.OK, "exFAT boot region checksum valid",
                f"volume serial {info.serial}", layer="fs", code="EXFAT_CKSUM_OK")
    else:
        rep.add(
            Severity.ERROR,
            "exFAT boot region checksum mismatch",
            f"stored {stored:08X}, computed {calc:08X} -- Windows will report the "
            "volume as RAW until the boot region is rebuilt.",
            layer="fs", code="EXFAT_CKSUM",
        )

    # backup boot region lives at the end of the volume
    if info.total_sectors >= 12:
        bkp_off = offset + (info.total_sectors - 12) * info.bytes_per_sector
        bkp = src.read_at(bkp_off, 12 * 512)
        if len(bkp) >= 11 * 512:
            if bkp[3:11] == b"EXFAT   ":
                bstored = u32_at(bkp, 5632)
                bcalc = exfat_checksum(bkp[: 11 * 512])
                if bstored == bcalc:
                    rep.add(Severity.OK, "exFAT backup boot region valid",
                            "Recoverable if the primary boot region is lost.",
                            layer="fs", code="EXFAT_BKP_OK")
                else:
                    rep.add(Severity.WARN, "exFAT backup boot region checksum mismatch",
                            layer="fs", code="EXFAT_BKP_CKSUM")
            else:
                rep.add(Severity.WARN, "exFAT backup boot region is unreadable or blank",
                        layer="fs", code="EXFAT_BKP_MISSING")

    # FAT region readability
    boot512 = src.read_at(offset, 512)
    fat_offset_sectors = u32_at(boot512, 80)
    if info.fat_size_sectors and info.bytes_per_sector and fat_offset_sectors:
        fat_start = offset + fat_offset_sectors * info.bytes_per_sector
        f1 = src.read_at(fat_start, min(info.fat_size_sectors * info.bytes_per_sector, 4 * 1024 * 1024))
        if not f1:
            rep.add(Severity.ERROR, "exFAT FAT region is unreadable",
                    f"FatOffset sector {fat_offset_sectors} -> {human(fat_start)}",
                    layer="fs", code="EXFAT_FAT_UNREADABLE")
        else:
            probe = f1[:65536]
            if probe and all(b == 0 for b in probe):
                rep.add(Severity.WARN, "exFAT FAT region is entirely zero",
                        "Sign of a wiped or never-finalised format.",
                        layer="fs", code="EXFAT_FAT_ZERO")
            else:
                rep.add(Severity.OK, "exFAT FAT region is readable",
                        f"FatOffset sector {fat_offset_sectors}, {human(len(f1))} read",
                        layer="fs", code="EXFAT_FAT_OK")


def _audit_ntfs(src: Source, rep: Report, info: FsInfo, offset: int) -> None:
    boot = src.read_at(offset, 512)
    if boot[510:512] != b"\x55\xAA":
        rep.add(Severity.ERROR, "NTFS: missing boot signature (55AA)",
                layer="fs", code="NTFS_SIG")
    else:
        rep.add(Severity.OK, "NTFS: boot signature present", layer="fs", code="NTFS_SIG")

    # $MFT record signature at mft_lcn * cluster_size
    if info.cluster_size:
        mft_lcn = int.from_bytes(boot[48:56], "little")
        if mft_lcn:
            off = offset + mft_lcn * info.cluster_size
            rec = src.read_at(off, 512)
            if rec[:4] == b"FILE":
                rep.add(Severity.OK, "$MFT record 0 has a valid FILE signature",
                        f"at {human(off)}", layer="fs", code="NTFS_MFT_OK")
            elif not rec:
                rep.add(Severity.ERROR, "$MFT is unreadable",
                        f"expected at offset {human(off)}", layer="fs", code="NTFS_MFT_DEAD")
            else:
                rep.add(
                    Severity.ERROR,
                    "$MFT record 0 has an invalid signature",
                    f"found {rec[:4]!r} instead of 'FILE' -- NTFS metadata is damaged, "
                    "recovery must run read-only with `ntfsfix -n`/`chkdsk` first.",
                    layer="fs", code="NTFS_MFT_BAD",
                )
    if info.cluster_size == 0:
        rep.add(Severity.ERROR, "NTFS: cluster size is zero -- boot sector corrupt",
                layer="fs", code="NTFS_CLUSTER")


def u32_at(buf: bytes, off: int) -> int:
    if off + 4 > len(buf):
        return 0
    return int.from_bytes(buf[off : off + 4], "little")


# --------------------------------------------------------------------------
# full pass
# --------------------------------------------------------------------------

def quick_diagnose(path: str, progress: Callable[[str], None] | None = None) -> Report:
    """Read-only diagnosis: partition table, filesystem, metadata integrity.

    Touches only metadata, so it is safe on a dying drive and takes
    milliseconds -- the "first look" the tool always does before anything
    destructive is offered.
    """
    t0 = time.time()
    rep = Report(device=path)
    rep.stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    def note(msg: str) -> None:
        if progress:
            progress(msg)

    with Source(path) as src:
        rep.size_bytes = src.size
        if src.size == 0:
            rep.add(Severity.FATAL, "Device reports zero size",
                    "No addressable bytes: the medium is not responding. This is a "
                    "physical/controller fault, not a filesystem one.",
                    layer="disk", code="DEAD_ZERO")
            rep.health, rep.grade = 0, "F"
            rep.verdict = "Unreadable"
            rep.duration_s = time.time() - t0
            return rep

        rep.add(Severity.INFO, f"Device size {human(src.size)}",
                f"{src.size:,} bytes", layer="disk", code="SIZE")

        if not src.writable:
            rep.add(Severity.INFO, "Device is read-only to this process",
                    "Repair actions will need elevated privileges.",
                    layer="disk", code="RO")

        note("reading partition table")
        inspect_partition_table(src, rep)
        rep.raw["mbr_partitions"] = [p.index for p in rep.partitions]

        target_offset = 0
        if rep.partitions:
            p0 = rep.partitions[0]
            target_offset = p0.offset_bytes
            rep.add(Severity.OK if p0.size_bytes else Severity.WARN,
                    f"{len(rep.partitions)} partition(s) found",
                    "; ".join(f"#{p.index} {p.type_name} @ {human(p.offset_bytes)}"
                              f" ({human(p.size_bytes)})" for p in rep.partitions[:6]),
                    layer="table", code="PARTS")
        else:
            note("no partitions -- scanning for a filesystem at LBA 0")
            rep.add(Severity.WARN, "No usable partition entries found",
                    "Falling back to a filesystem scan at the start of the device.",
                    layer="table", code="NO_PARTS")

        note("reading filesystem metadata")
        inspect_filesystem(src, rep, target_offset)

        # If the first partition has no filesystem, peek at other candidates
        if rep.fs and rep.fs.fs in (FsType.RAW, FsType.UNKNOWN) and len(rep.partitions) > 1:
            for p in rep.partitions[1:]:
                probe = parse_boot_sector(src.read_at(p.offset_bytes, 512), p.offset_bytes)
                if probe.fs not in (FsType.RAW, FsType.UNKNOWN):
                    p.fs = probe.fs
                    rep.add(Severity.INFO,
                            f"Partition #{p.index} holds a {probe.fs.value.upper()} filesystem",
                            f"at {human(p.offset_bytes)} -- usable if partition #1 is dead.",
                            layer="table", code="ALT_PART_FS")
                    break

        # plain-text signature sweep for a filesystem that is not at a boundary
        if rep.fs and rep.fs.fs in (FsType.RAW, FsType.UNKNOWN):
            found = search_boot_candidates(src, limit_scan=256 * 1024 * 1024)
            if found:
                off, fst = found[0]
                # The RAW finding was raised before we knew a filesystem exists
                # elsewhere; demote it, otherwise the report contradicts itself.
                for f in rep.findings:
                    if f.code == "FS_RAW":
                        f.severity = Severity.WARN
                        f.title = "No filesystem at the expected location"
                        f.detail = ("The table is gone, so there is nothing at "
                                    "offset 0 -- but a filesystem WAS found "
                                    f"further in (see below).")
                rep.add(
                    Severity.INFO,
                    f"A {fst.upper()} filesystem was found at offset {human(off)}",
                    "Not aligned to any partition entry, so the partition table lost "
                    "its pointer. This is recoverable by rebuilding the table at "
                    "that offset.",
                    layer="table",
                    code="FS_ORPHAN",
                    evidence={"offset": off, "fs": fst},
                )
                rep.raw["orphan_fs_offset"] = off
                rep.raw["orphan_fs_type"] = fst

    _score(rep)
    rep.duration_s = time.time() - t0
    return rep


BOOT_SIGNATURES = (
    ("NTFS    ", 3, "ntfs"),
    ("EXFAT   ", 3, "exfat"),
    (b"\x53\xEF", 1080, "ext"),
)


def search_boot_candidates(src: Source, limit_scan: int = 512 * 1024 * 1024,
                           step: int = 512) -> list[tuple[int, str]]:
    """Scan for filesystem boot sectors that are not where the table says.

    Reads sector-aligned chunks and checks for the cheap signatures first,
    then validates with the full parser so we do not report false positives.
    """
    out: list[tuple[int, str]] = []
    limit = min(src.size, limit_scan)
    chunk = 1024 * 1024
    pos = 0
    while pos < limit:
        buf = src.read_at(pos, min(chunk, limit - pos))
        if len(buf) < 512:
            break
        for i in range(0, len(buf) - 512, step):
            sec = buf[i : i + 512]
            if len(sec) < 512:
                break
            hit = None
            if sec[3:11] == b"NTFS    ":
                hit = "ntfs"
            elif sec[3:11] == b"EXFAT   ":
                hit = "exfat"
            elif sec[510:512] == b"\x55\xAA" and sec[11:13] in (b"\x00\x02", b"\x00\x04"):
                hit = "fat"
            elif len(sec) >= 1082 and sec[1080:1082] == b"\x53\xEF":
                hit = "ext"
            if hit:
                off = pos + i
                info = parse_boot_sector(src.read_at(off, 512), off)
                if info.fs not in (FsType.RAW, FsType.UNKNOWN):
                    out.append((off, info.fs.value))
                    if len(out) >= 8:
                        return out
        pos += len(buf) - 512 if len(buf) == chunk else len(buf)
    return out


def _score(rep: Report) -> None:
    penalty = 0
    for f in rep.findings:
        penalty += SEV_WEIGHT[f.severity]

    # A lost partition table with a healthy filesystem behind it is the single
    # most *recoverable* state there is -- the data is fully intact. Scoring it
    # as fatal made the tool tell people to give up on drives it could fix, so
    # the presence of a found (orphan) filesystem caps the penalty instead.
    orphan = rep.raw.get("orphan_fs_offset") is not None
    if rep.count(Severity.FATAL):
        if orphan:
            # table is gone but the data is there: this is a repair job
            penalty = min(penalty, 45)
            for f in rep.findings:
                if f.code == "TABLE_MISSING":
                    f.detail = (f.detail + "  A filesystem WAS found elsewhere on "
                                "the device, so the data is intact -- only the "
                                "table needs rebuilding.").strip()
        else:
            penalty = max(penalty, 70)

    rep.health = max(0, min(100, 100 - penalty))
    rep.grade = _grade(rep.health)

    if orphan:
        rep.verdict = ("Recoverable -- partition table lost, filesystem intact. "
                       "Run `usbdoctor repair` to rebuild the table.")
        rep.health = max(rep.health, 45)
        rep.grade = _grade(rep.health)
        return

    if rep.health >= 90:
        rep.verdict = "Healthy -- no action needed"
    elif rep.health >= 70:
        rep.verdict = "Usable with warnings -- back up and repair"
    elif rep.health >= 45:
        rep.verdict = "Degraded -- repair required, back up first"
    elif rep.health >= 20:
        rep.verdict = "Badly damaged -- read-only recovery only"
    else:
        rep.verdict = "Unrecoverable by software -- hardware/controller fault"


def format_report(rep: Report, color: bool = True) -> str:
    """Plain-text report -- the same text the GUI shows and the log saves."""
    C = {
        "ok": "\033[32m", "info": "\033[36m", "warn": "\033[33m",
        "error": "\033[31m", "fatal": "\033[41;97m",
    } if color else {}
    R = "\033[0m" if color else ""
    import shutil as _sh

    w = min(_sh.get_terminal_size((100, 24)).columns, 100)
    out = []
    out.append("=" * w)
    out.append("  USB DOCTOR -- DIAGNOSTIC REPORT")
    out.append("=" * w)
    out.append(f"  Device      : {rep.device}")
    out.append(f"  Size        : {human(rep.size_bytes)}")
    out.append(f"  Table       : {rep.table.value.upper()}")
    out.append(f"  Filesystem  : {(rep.fs.fs.value.upper() if rep.fs else 'unknown')}"
               + (f"  label {rep.fs.label!r}" if rep.fs and rep.fs.label else ""))
    out.append(f"  Health      : {rep.health}/100  grade {rep.grade}")
    out.append(f"  Verdict     : {rep.verdict}")
    out.append(f"  Scanned     : {rep.stamp}  in {rep.duration_s * 1000:.0f} ms")
    out.append("-" * w)
    for f in rep.findings:
        col = C.get(f.severity.value, "")
        out.append(f"  {col}{f.line()}{R}")
    out.append("-" * w)
    counts = ", ".join(f"{s.value}={rep.count(s)}"
                       for s in (Severity.FATAL, Severity.ERROR, Severity.WARN, Severity.INFO))
    out.append(f"  {counts}")
    out.append("=" * w)
    return "\n".join(out)