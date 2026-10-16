"""On-disk structure definitions and low-level parsers.

Pure-Python readers for the metadata that actually matters when a flash
drive goes bad: the exact bytes on the medium, not what the OS thinks.
Everything here works on a *source* (a device path, a disk image, or any
seekable file), so the whole diagnostic engine is testable without root
and without touching real hardware.

Scope deliberately covers the layers that break in the field:
  * MBR  (partition table + boot signature + CHS sanity)
  * GPT  (header, CRC32, backup header, entry array CRC)
  * FAT12/16/32 (BPB, FATs, FSInfo, root dir)
  * exFAT (boot sector, checksum, allocation bitmap)
  * NTFS  (boot sector, MFT record signature)
  * Generic signature/entropy scan for unknown layouts
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import BinaryIO, Iterator


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

SECTOR = 512
SECTOR_CANDIDATES = (512, 4096)


class FsType(str, Enum):
    UNKNOWN = "unknown"
    RAW = "raw"
    FAT12 = "fat12"
    FAT16 = "fat16"
    FAT32 = "fat32"
    EXFAT = "exfat"
    NTFS = "ntfs"
    EXT2 = "ext2"
    EXT3 = "ext3"
    EXT4 = "ext4"
    HFS = "hfs"
    APFS = "apfs"
    ISO9660 = "iso9660"


class TableType(str, Enum):
    NONE = "none"
    MBR = "mbr"
    GPT = "gpt"
    HYBRID = "hybrid"
    CORRUPT = "corrupt"


@dataclass
class Partition:
    index: int
    table: TableType
    offset_bytes: int
    size_bytes: int
    type_code: str = ""
    type_name: str = ""
    bootable: bool = False
    fs: FsType = FsType.UNKNOWN
    label: str = ""
    guid: str = ""
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # CHS values as stored (MBR only) -- used for sanity checks
    chs_start: tuple[int, int, int] | None = None
    chs_end: tuple[int, int, int] | None = None

    @property
    def end_bytes(self) -> int:
        return self.offset_bytes + self.size_bytes


@dataclass
class FsInfo:
    """What we learned about a filesystem's own metadata."""

    fs: FsType = FsType.UNKNOWN
    label: str = ""
    serial: str = ""
    bytes_per_sector: int = 0
    sectors_per_cluster: int = 0
    cluster_size: int = 0
    total_sectors: int = 0
    total_bytes: int = 0
    free_clusters: int | None = None
    root_entries: int = 0
    fat_count: int = 0
    fat_size_sectors: int = 0
    reserved_sectors: int = 0
    oem: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # FAT specific
    fat_used_ratio: float | None = None
    clean_shutdown: bool | None = None
    dirty_bit: bool | None = None
    # exFAT / NTFS specific
    checksum_ok: bool | None = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


def i16(b: bytes, o: int) -> int:
    return struct.unpack_from("<h", b, o)[0]


def i32(b: bytes, o: int) -> int:
    return struct.unpack_from("<i", b, o)[0]


def cstr(raw: bytes) -> str:
    """Decode a fixed-width field: prefer cp437 (OEM code page), fall back."""
    for enc in ("cp437", "ascii", "utf-8"):
        try:
            return raw.decode(enc, errors="ignore").strip("\x00 ").strip()
        except Exception:
            continue
    return ""


def human(n: int | float) -> str:
    if n is None:
        return "n/a"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} EB"


def chs_to_str(c: tuple[int, int, int] | None) -> str:
    if not c:
        return "-"
    return f"{c[0]}/{c[1]}/{c[2]}"


def decode_chs(b0: int, b1: int, b2: int) -> tuple[int, int, int]:
    head = b0
    sector = b1 & 0x3F
    cyl = ((b1 & 0xC0) << 2) | b2
    return (cyl, head, sector)


def lba_from_chs(cyl: int, head: int, sector: int, heads: int, spt: int) -> int:
    if sector <= 0 or heads <= 0 or spt <= 0:
        return -1
    return (cyl * heads + head) * spt + (sector - 1)


# --------------------------------------------------------------------------
# CRC / checksums used by GPT and exFAT
# --------------------------------------------------------------------------

def gpt_crc32(data: bytes) -> int:
    """GPT uses the standard CRC-32 (IEEE 802.3), little-endian, over a
    zeroed-out field."""
    return zlib.crc32(data) & 0xFFFFFFFF


def exfat_checksum(boot_region: bytes) -> int:
    """exFAT volume boot region checksum: rotate-right-xor over 11 sectors,
    skipping the VolumeFlags (offset 106) and PercentInUse (107) bytes."""
    chk = 0
    for i, byte in enumerate(boot_region):
        if i in (106, 107) or i in (112, 113):
            continue
        chk = ((chk << 31) | (chk >> 1)) & 0xFFFFFFFF
        chk = (chk + byte) & 0xFFFFFFFF
    return chk


# --------------------------------------------------------------------------
# MBR
# --------------------------------------------------------------------------

MBR_TYPES: dict[int, str] = {
    0x00: "Empty", 0x01: "FAT12", 0x04: "FAT16 <32M", 0x05: "Extended",
    0x06: "FAT16", 0x07: "NTFS/exFAT", 0x0B: "FAT32 (CHS)", 0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)", 0x0F: "Extended (LBA)", 0x11: "Hidden FAT12",
    0x12: "Compaq diag", 0x14: "Hidden FAT16 <32M", 0x16: "Hidden FAT16",
    0x17: "Hidden NTFS", 0x1B: "Hidden FAT32", 0x1C: "Hidden FAT32 (LBA)",
    0x1E: "Hidden FAT16 (LBA)", 0x27: "WinRE (NTFS)", 0x42: "Win LDM",
    0x82: "Linux swap", 0x83: "Linux", 0x85: "Linux extended", 0x86: "NTFS volume set",
    0x87: "NTFS volume set", 0x8E: "Linux LVM", 0xA5: "FreeBSD", 0xA8: "macOS UFS",
    0xAB: "macOS boot", 0xAF: "macOS HFS+", 0xEE: "GPT protective", 0xEF: "EFI System",
}

GPT_TYPE_NAMES: dict[str, str] = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "a2a0d0eb-e5b9-3344-87c0-68b6b72699c7": "Linux data (old)",
}


@dataclass
class MbrResult:
    valid_signature: bool
    partitions: list[Partition]
    disk_signature: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    boot_code_nonzero: bool = False
    protective: bool = False


def parse_mbr(buf: bytes, total_size: int | None = None) -> MbrResult:
    """Parse a 512-byte MBR sector."""
    errs: list[str] = []
    warns: list[str] = []
    parts: list[Partition] = []

    if len(buf) < 512:
        return MbrResult(False, [], errors=["MBR sector is truncated (device too small or unreadable)"])

    sig = buf[510:512]
    valid = sig == b"\x55\xAA"
    disk_sig = u32(buf, 440)
    boot_nonzero = any(b not in (0x00, 0xFF) for b in buf[0:440])
    if not valid:
        errs.append(f"Invalid MBR boot signature {sig.hex().upper() or 'blank'} (expected 55AA)")

    overshoot = False
    overlap: list[tuple[int, int]] = []
    protective_entry = False

    for i in range(4):
        o = 446 + i * 16
        status = buf[o]
        tcode = buf[o + 4]
        lba_start = u32(buf, o + 8)
        lba_count = u32(buf, o + 12)
        chs_s = decode_chs(buf[o + 1], buf[o + 2], buf[o + 3])
        chs_e = decode_chs(buf[o + 5], buf[o + 6], buf[o + 7])

        if tcode == 0 and lba_count == 0:
            continue

        if tcode == 0xEE:
            protective_entry = True
            # A protective MBR entry is *allowed* to be odd: status 0x00 is
            # normal, the CHS fields are conventionally 0xFFFFFF/0x02... and
            # the declared length covers the whole disk. Never flag it.
            parts.append(
                Partition(
                    index=i + 1,
                    table=TableType.MBR,
                    offset_bytes=lba_start * SECTOR,
                    size_bytes=lba_count * SECTOR,
                    type_code="0xEE",
                    type_name=MBR_TYPES.get(tcode, "GPT protective"),
                    bootable=False,
                    chs_start=chs_s,
                    chs_end=chs_e,
                )
            )
            continue

        if status not in (0x00, 0x80):
            warns.append(f"Partition {i + 1}: illegal status byte 0x{status:02X}")

        notes: list[str] = []
        flags: list[str] = []

        if lba_count == 0:
            errs.append(f"Partition {i + 1} ({MBR_TYPES.get(tcode, '?')}): zero length")
            notes.append("declared length is 0 -- entry is corrupt")
        if lba_start == 0 and tcode not in (0x00,):
            notes.append("starts at LBA 0 -- overlaps the MBR itself")
            flags.append("overlap")

        size = lba_count * SECTOR
        end = lba_start * SECTOR + size

        if total_size and end > total_size:
            overshoot = True
            notes.append(
                f"extends {human(end - total_size)} past the end of the device "
                f"(claimed {human(end)}, device {human(total_size)})"
            )
            flags.append("out-of-bounds")

        # CHS sanity: a wrong CHS pair is a classic sign of a botched
        # format utility or a partially written partition table.
        if chs_e[2] == 0 and chs_s[2] == 0 and lba_count:
            warns.append(f"Partition {i + 1}: CHS fields are zero/inconsistent")
            flags.append("chs-bad")

        if lba_count:
            overlap.append((lba_start * SECTOR, end))

        parts.append(
            Partition(
                index=i + 1,
                table=TableType.MBR,
                offset_bytes=lba_start * SECTOR,
                size_bytes=size,
                type_code=f"0x{tcode:02X}",
                type_name=MBR_TYPES.get(tcode, f"Unknown (0x{tcode:02X})"),
                bootable=status == 0x80,
                notes=notes,
                flags=flags,
                chs_start=chs_s,
                chs_end=chs_e,
            )
        )

    # overlap detection between primary entries
    overlap.sort()
    for a, b in zip(overlap, overlap[1:]):
        if b[0] < a[1]:
            warns.append(
                f"Partitions overlap: {human(a[0])}-{human(a[1])} collides with "
                f"{human(b[0])}-{human(b[1])}"
            )

    protective = any(p.type_code == "0xEE" for p in parts)
    if overshoot:
        errs.append("At least one partition claims space beyond the physical device")

    return MbrResult(valid, parts, disk_sig, errs, warns, boot_nonzero, protective)


# --------------------------------------------------------------------------
# GPT
# --------------------------------------------------------------------------

GPT_SIG = b"EFI PART"


@dataclass
class GptResult:
    primary_ok: bool
    backup_ok: bool
    header: dict | None = None
    partitions: list[Partition] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_gpt_header(buf: bytes, label: str) -> tuple[dict | None, list[str]]:
    """Parse one GPT header sector (LBA 1 or last LBA)."""
    errs: list[str] = []
    if len(buf) < 92:
        return None, [f"{label}: header sector unreadable"]
    if buf[0:8] != GPT_SIG:
        return None, [f"{label}: bad signature {buf[0:8]!r} (expected 'EFI PART')"]

    hsize = u32(buf, 12)
    if hsize < 92 or hsize > 512:
        errs.append(f"{label}: implausible header size {hsize} (expected 92..512)")

    stored_crc = u32(buf, 16)
    zeroed = bytearray(buf[: max(hsize, 92)])
    zeroed[16:20] = b"\x00\x00\x00\x00"
    calc_crc = gpt_crc32(bytes(zeroed))
    crc_ok = stored_crc == calc_crc
    if not crc_ok:
        errs.append(
            f"{label}: header CRC32 mismatch (stored {stored_crc:08X}, computed {calc_crc:08X})"
        )

    hdr = {
        "revision": f"{u16(buf, 8)}.{u16(buf, 10)}",
        "header_size": hsize,
        "crc_ok": crc_ok,
        "current_lba": u64(buf, 24),
        "backup_lba": u64(buf, 32),
        "first_usable": u64(buf, 40),
        "last_usable": u64(buf, 48),
        "disk_guid": format_guid(buf[56:72]),
        "entries_lba": u64(buf, 72),
        "entry_count": u32(buf, 80),
        "entry_size": u32(buf, 84),
        "entries_crc": u32(buf, 88),
        "label": label,
    }
    return hdr, errs


def format_guid(raw: bytes) -> str:
    if len(raw) < 16:
        return ""
    d1 = struct.unpack_from("<I", raw, 0)[0]
    d2 = struct.unpack_from("<H", raw, 4)[0]
    d3 = struct.unpack_from("<H", raw, 6)[0]
    d4 = raw[8:10].hex()
    d5 = raw[10:16].hex()
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{d4}-{d5}"


def parse_gpt_entries(buf: bytes, hdr: dict) -> tuple[list[Partition], list[str]]:
    errs: list[str] = []
    parts: list[Partition] = []

    stored = hdr["entries_crc"]
    calc = gpt_crc32(buf[: hdr["entry_count"] * hdr["entry_size"]])
    if stored != calc:
        errs.append(
            f"Partition entry array CRC32 mismatch "
            f"(stored {stored:08X}, computed {calc:08X}) -- table is likely corrupt"
        )

    count = min(hdr["entry_count"], 128)
    esz = hdr["entry_size"]
    if esz < 128 or esz > 4096:
        return [], errs + [f"implausible partition entry size {esz}"]

    for i in range(count):
        o = i * esz
        if o + 128 > len(buf):
            break
        etype = format_guid(buf[o : o + 16])
        if etype == "00000000-0000-0000-0000-000000000000":
            continue
        first = u64(buf, o + 32)
        last = u64(buf, o + 40)
        name_raw = buf[o + 56 : o + 128]
        try:
            name = name_raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
        except Exception:
            name = ""
        attrs = u64(buf, o + 48)

        notes: list[str] = []
        if last < first:
            notes.append("last LBA is before first LBA -- entry is corrupt")
        if first < hdr["first_usable"] and first != 0:
            notes.append(f"starts below first usable LBA ({hdr['first_usable']})")

        part = Partition(
            index=i + 1,
            table=TableType.GPT,
            offset_bytes=first * SECTOR,
            size_bytes=(last - first + 1) * SECTOR if last >= first else 0,
            type_code=etype,
            type_name=GPT_TYPE_NAMES.get(etype, "Unknown GPT type"),
            label=name,
            guid=format_guid(buf[o + 16 : o + 32]),
            notes=notes,
            flags=[] if attrs == 0 else [f"attrs=0x{attrs:X}"],
        )
        parts.append(part)

    return parts, errs


# --------------------------------------------------------------------------
# FAT
# --------------------------------------------------------------------------

@dataclass
class FatResult:
    info: FsInfo
    fat_entries: list[int] = field(default_factory=list)
    used_clusters: set[int] = field(default_factory=set)


def parse_boot_sector(buf: bytes, part_offset: int = 0) -> FsInfo:
    """Identify and describe the filesystem whose boot sector is `buf`."""
    info = FsInfo()

    if len(buf) < 512:
        info.fs = FsType.UNKNOWN
        info.errors.append("boot sector unreadable (short read)")
        return info

    # --- exFAT ---------------------------------------------------------
    if buf[3:11] == b"EXFAT   ":
        return _parse_exfat(buf, info)

    # --- NTFS ----------------------------------------------------------
    if buf[3:11] == b"NTFS    ":
        return _parse_ntfs(buf, info)

    # --- FAT12/16/32 ---------------------------------------------------
    if _looks_like_fat(buf):
        return _parse_fat(buf, part_offset, info)

    # --- ext2/3/4 ------------------------------------------------------
    if len(buf) >= 1082 and buf[1080:1082] == b"\x53\xEF":
        return _parse_ext(buf, info)

    # --- HFS+ / APFS / ISO --------------------------------------------
    if buf[1024:1026] == b"H+" or buf[1024:1026] == b"HX":
        info.fs = FsType.HFS
        info.label = cstr(buf[1024 + 2 : 1024 + 30])
        return info
    if buf[32:36] == b"NXSB":
        info.fs = FsType.APFS
        return info
    if buf[0x8001:0x8006] == b"CD001" or buf[0x01:0x06] == b"CD001":
        info.fs = FsType.ISO9660
        return info

    # --- nothing recognisable ------------------------------------------
    nonblank = sum(1 for b in buf[:512] if b not in (0x00, 0xFF))
    if nonblank < 8:
        info.fs = FsType.RAW
        info.errors.append(
            "No filesystem found -- region is blank (0x00/0xFF). "
            "Classic signature of a wiped, RAW, or never-formatted drive."
        )
    else:
        info.fs = FsType.UNKNOWN
        info.errors.append(
            "Unrecognised boot sector: no FAT/NTFS/exFAT/ext/HFS/APFS/ISO signature"
        )
    return info


def _looks_like_fat(buf: bytes) -> bool:
    if len(buf) < 90:
        return False
    if buf[510:512] != b"\x55\xAA":
        return False
    bps = u16(buf, 11)
    if bps not in SECTOR_CANDIDATES:
        return False
    spc = buf[13]
    if spc == 0 or spc & (spc - 1):
        return False
    if buf[16] not in (1, 2):
        return False
    media = buf[21]
    if media < 0xF0:
        return False
    fatsz16 = u16(buf, 22)
    fatsz32 = u32(buf, 36)
    if fatsz16 == 0 and fatsz32 == 0:
        return False
    return True


def _parse_fat(buf: bytes, part_offset: int, info: FsInfo) -> FsInfo:
    try:
        bps = u16(buf, 11)
        spc = buf[13]
        reserved = u16(buf, 14)
        nfats = buf[16]
        root_entries = u16(buf, 17)
        total16 = u16(buf, 19)
        media = buf[21]
        fatsz16 = u16(buf, 22)
        spt = u16(buf, 24)
        heads = u16(buf, 26)
        hidden = u32(buf, 28)
        total32 = u32(buf, 32)
        fatsz32 = u32(buf, 36)
        root_cluster = u32(buf, 44)
        fsinfo_sector = u16(buf, 48)
        backup_boot = u16(buf, 50)
    except struct.error:
        info.fs = FsType.UNKNOWN
        info.errors.append("FAT boot sector is truncated")
        return info

    fatsz = fatsz16 or fatsz32
    total = total16 or total32
    root_dir_sectors = ((root_entries * 32) + (bps - 1)) // bps
    data_sectors = total - (reserved + nfats * fatsz + root_dir_sectors)
    count_clusters = data_sectors // spc if spc else 0

    if count_clusters < 4085:
        info.fs = FsType.FAT12
    elif count_clusters < 65525:
        info.fs = FsType.FAT16
    else:
        info.fs = FsType.FAT32

    if not fatsz16 and not fatsz32:
        info.errors.append("Both FAT size fields are zero -- boot sector is corrupt")
    if total == 0:
        info.errors.append("Total sector count is zero -- boot sector is corrupt")
    if spc == 0:
        info.errors.append("Sectors-per-cluster is zero -- boot sector is corrupt")
    if media < 0xF0:
        info.errors.append(f"Invalid media descriptor 0x{media:02X} (expected >= 0xF0)")

    # FAT32-only sanity
    if info.fs == FsType.FAT32:
        if buf[66] != 0x28 and buf[66] != 0x29:
            info.warnings.append("FAT32: missing extended boot signature (0x28/0x29)")
        if backup_boot == 0:
            info.warnings.append("FAT32: backup boot sector pointer is 0")
        if root_cluster < 2:
            info.errors.append(f"FAT32: invalid root cluster {root_cluster} (must be >= 2)")
        # root_entries is *always* 0 on FAT32 -- it lives in a cluster chain.
        # Only complain if a formatter wrongly left a count in there.
        if root_entries:
            info.warnings.append(
                f"FAT32: root entry count is {root_entries} (should be 0 on FAT32)"
            )
    else:
        if root_entries == 0:
            info.errors.append(f"{info.fs.value.upper()}: root entry count is zero")

    # Volume label + serial
    if buf[38:43] == b"FAT12" or buf[54:59] == b"FAT16" or buf[82:87] == b"FAT32":
        pass
    if info.fs == FsType.FAT32:
        info.label = cstr(buf[71:82])
        info.serial = f"{u32(buf, 67):08X}"
    else:
        info.label = cstr(buf[43:54])
        info.serial = f"{u32(buf, 39):08X}"

    info.bytes_per_sector = bps
    info.sectors_per_cluster = spc
    info.cluster_size = bps * spc
    info.total_sectors = total
    info.total_bytes = total * bps
    info.reserved_sectors = reserved
    info.fat_count = nfats
    info.fat_size_sectors = fatsz
    info.root_entries = root_entries
    info.oem = cstr(buf[3:11])

    # The FAT32 *label string* is written at offset 82. If it is present but
    # the derived type came out FAT12/FAT16, the formatter wrote FAT32-only
    # fields onto a non-FAT32 geometry -- Windows reports such a volume as
    # RAW. This is a real, distinct fault from a plain type mismatch.
    label_str = buf[82:90]
    if label_str == b"FAT32   " and info.fs != FsType.FAT32:
        info.errors.append(
            f"Boot sector is labelled FAT32 but the geometry is {info.fs.value.upper()} "
            f"({count_clusters:,} clusters) -- Windows will show this volume as RAW"
        )
    elif label_str == b"FAT16   " and info.fs == FsType.FAT32:
        info.errors.append(
            "Boot sector is labelled FAT16 but the geometry is FAT32 "
            f"({count_clusters:,} clusters)"
        )

    if info.fs == FsType.FAT32 and fsinfo_sector:
        # FSInfo is a free-space *hint*; parse_fsinfo() fills free_clusters
        # when the caller reads that sector. No warning needed here.
        pass
    return info


def parse_fsinfo(buf: bytes, info: FsInfo) -> None:
    """Parse a FAT32 FSInfo sector into `info` (free cluster count)."""
    if len(buf) < 512:
        return
    if u32(buf, 0) != 0x41615252:
        info.warnings.append("FSInfo: bad lead signature (0x41615252 missing)")
        return
    if u32(buf, 484) != 0x61417272:
        info.warnings.append("FSInfo: bad trail signature (0x61417272 missing)")
        return
    free = u32(buf, 488)
    nxt = u32(buf, 492)
    if free != 0xFFFFFFFF:
        info.free_clusters = free
    if nxt != 0xFFFFFFFF and nxt < 2:
        info.warnings.append(f"FSInfo: free-cluster hint {nxt} is out of range")


def fat_flags(buf: bytes, info: FsInfo) -> None:
    """Read FAT32 FAT[1] status bits.

    FAT[1] holds two flags in its high bits (Microsoft FAT spec):
      bit 27 (0x08000000) ClnShutBitMask -- 1 means cleanly shut down
      bit 26 (0x04000000) HrdErrBitMask  -- 1 means a hard error was seen
    Reading these inverted is the classic bug that reports healthy drives
    as dirty, so the sense of each bit is set explicitly here.
    """
    if info.fs != FsType.FAT32 or len(buf) < 8:
        return
    fat1 = u32(buf, 4)
    info.clean_shutdown = bool(fat1 & 0x08000000)
    info.dirty_bit = not info.clean_shutdown
    if info.dirty_bit:
        info.warnings.append(
            "Volume dirty bit is SET -- the drive was not unmounted cleanly "
            "(unsafe removal or power loss)."
        )
    if fat1 & 0x04000000:
        info.warnings.append(
            "FAT32 hard-error flag is set -- the volume hit an I/O error while mounted"
        )


def read_fat_entries(src, info: FsInfo, part_offset: int, limit: int = 4096) -> list[int]:
    """Read the first `limit` entries of FAT#1. Detects a broken/missing FAT.

    `src` may be a raw file object or a Source wrapper -- both expose
    read_at(); we never call seek() directly so either works.
    """
    entries: list[int] = []
    if not info.fat_size_sectors or not info.bytes_per_sector:
        return entries
    start = part_offset + info.reserved_sectors * info.bytes_per_sector
    want = min(info.fat_size_sectors * info.bytes_per_sector, limit * 4)
    try:
        if hasattr(src, "read_at"):
            raw = src.read_at(start, want)
        else:
            raw = _pread(src, start, want)
    except Exception:
        return entries
    if not raw:
        return entries
    if info.fs == FsType.FAT32:
        for i in range(min(len(raw) // 4, limit)):
            entries.append(u32(raw, i * 4))
    elif info.fs == FsType.FAT16:
        for i in range(min(len(raw) // 2, limit)):
            entries.append(u16(raw, i * 2))
    else:
        for i in range(min(len(raw) * 2 // 3, limit)):
            o = (i * 3) // 2
            if o + 2 > len(raw):
                break
            v = u16(raw, o)
            entries.append((v >> 4) if i % 2 else (v & 0x0FFF))
    return entries


def fat_eoc(info: FsInfo) -> int:
    return {"fat12": 0xFF8, "fat16": 0xFFF8, "fat32": 0x0FFFFFF8}.get(info.fs.value, 0xFFFFFFF8)


def fat_bad(info: FsInfo) -> int:
    return {"fat12": 0xFF7, "fat16": 0xFFF7, "fat32": 0x0FFFFFF7}.get(info.fs.value, 0xFFFFFFF7)


def fat_reserved_lo(info: FsInfo) -> int:
    return {"fat12": 0xFF0, "fat16": 0xFFF0, "fat32": 0x0FFFFFF0}.get(info.fs.value, 0x0FFFFFF0)


# --------------------------------------------------------------------------
# exFAT
# --------------------------------------------------------------------------

def _parse_exfat(buf: bytes, info: FsInfo) -> FsInfo:
    info.fs = FsType.EXFAT
    try:
        part_offset = u64(buf, 64)          # PartitionOffset (sectors)
        vol_len = u64(buf, 72)              # VolumeLength (sectors)
        fat_off = u32(buf, 80)
        fat_len = u32(buf, 84)
        cluster_heap = u32(buf, 88)
        cluster_count = u32(buf, 92)
        root_cluster = u32(buf, 96)
        vol_serial = u32(buf, 100)
        revision = u16(buf, 104)
        flags = u16(buf, 106)
        bps_shift = buf[108]
        spc_shift = buf[109]
        nfats = buf[110]
        drive_sel = buf[111]
    except struct.error:
        info.errors.append("exFAT boot sector is truncated")
        return info

    bps = 1 << bps_shift
    spc = 1 << spc_shift

    info.bytes_per_sector = bps
    info.sectors_per_cluster = spc
    info.cluster_size = bps * spc
    info.total_sectors = vol_len
    info.total_bytes = vol_len * bps
    info.fat_count = nfats
    info.fat_size_sectors = fat_len
    info.serial = f"{vol_serial:08X}"
    info.oem = "exFAT"
    info.clean_shutdown = not bool(flags & 0x0002)
    info.dirty_bit = bool(flags & 0x0002)

    if bps_shift < 9 or bps_shift > 12:
        info.errors.append(f"exFAT: illegal bytes-per-sector shift {bps_shift} (expect 9..12)")
    if spc_shift > 25:
        info.errors.append(f"exFAT: illegal sectors-per-cluster shift {spc_shift}")
    if nfats not in (1, 2):
        info.errors.append(f"exFAT: NumberOfFats is {nfats} (must be 1 or 2)")
    if fat_off < 24:
        info.errors.append(f"exFAT: FatOffset {fat_off} overlaps the boot region")
    if root_cluster < 2:
        info.errors.append(f"exFAT: invalid root directory cluster {root_cluster}")
    if bps_shift >= 9 and spc_shift <= 25:
        heap_lba = cluster_heap
        if heap_lba and fat_off + fat_len * nfats > heap_lba:
            info.warnings.append("exFAT: FAT region overlaps the cluster heap")
    if revision < 0x0100:
        info.warnings.append(f"exFAT: unusual revision 0x{revision:04X}")

    if info.dirty_bit:
        info.warnings.append(
            "exFAT volume dirty flag is SET -- the volume was not unmounted cleanly"
        )

    # PercentInUse at offset 112
    pct = buf[112]
    if pct not in (0xFF,) and cluster_count:
        info.free_clusters = int(cluster_count * (100 - pct) / 100)
    return info


def exfat_flag(buf: bytes, info: FsInfo) -> None:
    """Read VolumeFlags from sector 0 of a 12-sector boot region."""
    if len(buf) < 108:
        return
    flags = u16(buf, 106)
    info.dirty_bit = bool(flags & 0x0002)
    info.clean_shutdown = not info.dirty_bit


# --------------------------------------------------------------------------
# NTFS
# --------------------------------------------------------------------------

def _parse_ntfs(buf: bytes, info: FsInfo) -> FsInfo:
    info.fs = FsType.NTFS
    try:
        bps = u16(buf, 11)
        spc = buf[13]
        total = u64(buf, 40)
        mft_lcn = u64(buf, 48)
        mft_mirror_lcn = u64(buf, 56)
        clusters_per_mft = i32(buf, 64)
        clusters_per_index = i32(buf, 68)
        serial = u64(buf, 72)
    except struct.error:
        info.errors.append("NTFS boot sector is truncated")
        return info

    spc = spc if spc else 1
    info.bytes_per_sector = bps
    info.sectors_per_cluster = spc
    info.cluster_size = bps * spc
    info.total_sectors = total
    info.total_bytes = total * bps
    info.serial = f"{serial:016X}"
    info.oem = "NTFS"

    if bps not in SECTOR_CANDIDATES:
        info.errors.append(f"NTFS: illegal bytes-per-sector {bps}")
    if mft_lcn == 0:
        info.errors.append("NTFS: $MFT logical cluster number is 0 -- boot sector is corrupt")
    if total == 0:
        info.errors.append("NTFS: total sector count is 0 -- boot sector is corrupt")
    if clusters_per_mft == 0:
        info.warnings.append("NTFS: clusters-per-MFT-record is 0 (corrupt or unusual)")
    if mft_mirror_lcn == 0:
        info.warnings.append("NTFS: $MFTMirr location is 0 -- boot sector may be damaged")

    decoder = (buf[68:72] if False else buf[3:11])
    return info


def ntfs_boot_checksum_ok(buf: bytes) -> bool | None:
    """NTFS keeps an end-of-sector signature (0xAA55) at 510 and, for
    sectors >512, at the end of each sector."""
    if len(buf) < 512:
        return None
    return buf[510:512] == b"\x55\xAA"


# --------------------------------------------------------------------------
# ext2/3/4 (read-only, diagnostic only)
# --------------------------------------------------------------------------

def _parse_ext(buf: bytes, info: FsInfo) -> FsInfo:
    info.fs = FsType.UNKNOWN
    try:
        inodes = u32(buf, 1024)
        blocks = u32(buf, 1028)
        log_block = u32(buf, 1024 + 24)
        log_frag = u32(buf, 1024 + 28)
        blocks_per_group = u32(buf, 1024 + 32)
        inodes_per_group = u32(buf, 1024 + 40)
        mtime = u32(buf, 1024 + 48)
        wtime = u32(buf, 1024 + 52)
        state = u16(buf, 1024 + 58)
        incompat = u32(buf, 1024 + 96)
    except struct.error:
        return info

    if incompat & 0x40:
        info.fs = FsType.EXT4
    elif incompat & 0x4:
        info.fs = FsType.EXT3
    else:
        info.fs = FsType.EXT2

    info.bytes_per_sector = 1 << (log_block + 10)
    info.cluster_size = info.bytes_per_sector << log_frag
    info.inode_count = inodes
    info.total_bytes = 0
    info.oem = "ext"

    if state & 0x1:
        info.errors.append("ext: filesystem was not cleanly unmounted")
    if wtime and mtime and wtime > mtime:
        info.warnings.append("ext: mount time is later than last write time (clock or corruption)")
    if state & 0x2:
        info.errors.append("ext: filesystem recorded an error condition")
    if blocks == 0:
        info.errors.append("ext: block count is zero -- superblock is corrupt")

    if mtime:
        import datetime

        try:
            info.label = datetime.datetime.utcfromtimestamp(mtime).strftime("last-mounted %Y-%m-%d")
        except Exception:
            pass
    return info


# --------------------------------------------------------------------------
# read helper
# --------------------------------------------------------------------------

def _pread(src: BinaryIO, offset: int, size: int) -> bytes:
    try:
        src.seek(offset)
        return src.read(size)
    except Exception:
        return b""


def read_at(src: BinaryIO, offset: int, size: int) -> bytes:
    return _pread(src, offset, size)