"""Build disk images of every broken state, so the engine can be proven.

We cannot rely on `mkfs` (blocked by policy and absent on minimal hosts), so
the images are written byte-for-byte in Python. That is a feature: each
fixture encodes exactly the corruption we want to detect, and the test suite
asserts the engine names it correctly.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LFN = 0x0F

SECTOR = 512

# ----------------------------------------------------------------------------
# low-level builders
# ----------------------------------------------------------------------------


def blank_image(path: str, size_mb: int = 32) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    return str(p)


def write_at(path: str, offset: int, data: bytes) -> None:
    with open(path, "r+b") as fh:
        fh.seek(offset)
        fh.write(data)


def read_at(path: str, offset: int, size: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(size)


def _crc32(data: bytes) -> int:
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


# ----------------------------------------------------------------------------
# MBR
# ----------------------------------------------------------------------------

def mbr_entry(status: int, type_code: int, lba_start: int, lba_count: int,
              chs_start: bytes = b"\xFE\xFF\xFF", chs_end: bytes = b"\xFE\xFF\xFF") -> bytes:
    """16-byte partition entry: status, CHS start, type, CHS end, LBA start, LBA count."""
    return (struct.pack("<B", status) + bytes(chs_start[:3]).ljust(3, b"\xFF") +
            struct.pack("<B", type_code) + bytes(chs_end[:3]).ljust(3, b"\xFF") +
            struct.pack("<II", lba_start, lba_count))


def build_mbr(entries: list[bytes], disk_signature: int = 0x12345678,
              boot_code: bytes = b"", valid_signature: bool = True) -> bytes:
    """Assemble a 512-byte MBR.

    Byte-exact layout (this is the part everyone gets wrong):
      boot code  0..439        (440 bytes)
      disk sig   440..443      (4 bytes)
      reserved   444..445      (2 bytes)
      entries    446..505      (4 entries x 16 bytes)
      signature  510..511      (55 AA)
    """
    code = boot_code[:440].ljust(440, b"\x00")
    body = code + struct.pack("<I", disk_signature) + b"\x00\x00"
    for i in range(4):
        body += entries[i] if i < len(entries) else b"\x00" * 16
    assert len(body) == 510, f"MBR body is {len(body)} bytes, expected 510"
    body += b"\x55\xAA" if valid_signature else b"\x00\x00"
    assert len(body) == 512
    return body


# ----------------------------------------------------------------------------
# FAT
# ----------------------------------------------------------------------------

def fat_size_for(size_bytes: int, reserved: int = 32, fats: int = 2,
                 spc: int = 8, bps: int = 512, target: str = "auto") -> tuple[int, int]:
    """Pick (fat_size_sectors, nclusters) for a spec-correct volume.

    Closed-form solution. For a volume of T sectors with R reserved sectors,
    F FAT copies and C clusters of S sectors each, the FAT must hold
    ceil(C * e / bps) sectors where e is bytes per entry. Solving the two
    simultaneous constraints (table big enough, region sum fits) gives:

        C = (T - R - F * fat_sectors) / S
        fat_sectors >= C * e / bps

    which we iterate twice -- it converges immediately because fat_sectors is
    tiny relative to T. We search bytes-per-cluster upward from the requested
    minimum until the resulting cluster count lands inside the target band,
    which is what real formatters do (and why a 32 MB drive can never be
    a legitimate FAT32).

    Returns (fat_size_sectors, nclusters).
    """
    total_sectors = size_bytes // bps
    epc_bits = {"fat12": 12, "fat16": 16, "fat32": 32}[target if target != "auto" else "fat32"]
    if target == "fat32":
        lo, hi = 65526, 1 << 28
    elif target == "fat16":
        lo, hi = 4086, 65525
    else:
        lo, hi = 2, 4085

    best: tuple[int, int] | None = None
    candidate = max(spc, 1)
    while candidate <= 1024:
        # cluster count as a function of cluster size, ignoring the table first
        clusters = (total_sectors - reserved) // candidate
        if clusters > 0:
            fat_sectors = (clusters * epc_bits + 8 * bps - 1) // (8 * bps)
            for _ in range(4):                       # converge
                clusters = (total_sectors - reserved - fats * fat_sectors) // candidate
                need = (max(clusters, 1) * epc_bits + 8 * bps - 1) // (8 * bps)
                if need == fat_sectors:
                    break
                fat_sectors = need
            clusters = (total_sectors - reserved - fats * fat_sectors) // candidate
            if lo <= clusters <= hi and fat_sectors > 0:
                return fat_sectors, clusters
            if best is None or abs(clusters - lo) < abs(best[1] - lo):
                best = (fat_sectors, clusters)
        candidate *= 2

    return best if best else (16, max((total_sectors - reserved) // max(spc, 1), 1))


def build_fat_boot(vol_label: str = "TESTVOL", fs: str = "fat32",
                   total_sectors: int = 65536, bps: int = 512, spc: int = 8,
                   fats: int = 2, reserved: int = 32, root_entries: int = 512,
                   fatsz: int = 506, serial: int = 0x1A2B3C4D,
                   oem: str = "USBDOCTR", media: int = 0xF8) -> bytes:
    b = bytearray(SECTOR)
    b[0:3] = b"\xEB\x58\x90"
    b[3:11] = oem.encode()[:8].ljust(8, b" ")
    struct.pack_into("<H", b, 11, bps)
    b[13] = spc
    struct.pack_into("<H", b, 14, reserved)
    b[16] = fats
    struct.pack_into("<H", b, 17, 0 if fs == "fat32" else root_entries)
    struct.pack_into("<H", b, 19, 0 if fs == "fat32" else total_sectors)
    b[21] = media
    struct.pack_into("<H", b, 22, 0 if fs == "fat32" else fatsz)
    struct.pack_into("<H", b, 24, 63)      # sectors per track
    struct.pack_into("<H", b, 26, 255)     # heads
    struct.pack_into("<I", b, 28, 0)       # hidden
    struct.pack_into("<I", b, 32, total_sectors if fs == "fat32" else 0)

    if fs == "fat32":
        struct.pack_into("<I", b, 36, fatsz)
        struct.pack_into("<H", b, 40, 0)   # ext flags
        struct.pack_into("<H", b, 42, 0)   # fs version
        struct.pack_into("<I", b, 44, 2)   # root cluster
        struct.pack_into("<H", b, 48, 1)   # FSInfo sector (inside reserved area)
        struct.pack_into("<H", b, 50, 6)   # backup boot sector
        b[64] = 0x80
        b[66] = 0x29
        struct.pack_into("<I", b, 67, serial)
        b[71:82] = vol_label.encode()[:11].ljust(11, b" ")
        b[82:90] = b"FAT32   "
        b[510:512] = b"\x55\xAA"
    else:
        b[36] = 0x80
        b[38] = 0x29
        struct.pack_into("<I", b, 39, serial)
        b[43:54] = vol_label.encode()[:11].ljust(11, b" ")
        tag = b"FAT16   " if fs == "fat16" else b"FAT12   "
        b[54:62] = tag
        b[510:512] = b"\x55\xAA"
    return bytes(b)


def build_fsinfo(free_clusters: int = 999, next_free: int = 3) -> bytes:
    b = bytearray(SECTOR)
    struct.pack_into("<I", b, 0, 0x41615252)
    struct.pack_into("<I", b, 484, 0x61417272)
    struct.pack_into("<I", b, 488, free_clusters)
    struct.pack_into("<I", b, 492, next_free)
    b[510:512] = b"\x55\xAA"
    return bytes(b)


def build_fat_table(fs: str, nclusters: int, entries_used: int = 2,
                    bad_clusters: list[int] | None = None,
                    out_of_range: list[int] | None = None,
                    dirty: bool = False) -> bytes:
    bad_clusters = bad_clusters or []
    out_of_range = out_of_range or []
    sz = ({"fat12": 3, "fat16": 2, "fat32": 4}[fs]) * nclusters
    if fs == "fat12":
        sz = int(nclusters * 1.5)
    b = bytearray(max(sz, 512))

    def put(i: int, v: int) -> None:
        if fs == "fat32":
            struct.pack_into("<I", b, i * 4, v & 0x0FFFFFFF)
        elif fs == "fat16":
            struct.pack_into("<H", b, i * 2, v & 0xFFFF)
        else:
            o = (i * 3) // 2
            if i % 2:
                struct.pack_into("<H", b, o, (v & 0x0FFF) << 4)
            else:
                struct.pack_into("<H", b, o, v & 0x0FFF)

    eoc = {"fat12": 0xFFF, "fat16": 0xFFFF, "fat32": 0x0FFFFFFF}[fs]
    badv = {"fat12": 0xFF7, "fat16": 0xFFF7, "fat32": 0x0FFFFFF7}[fs]
    put(0, eoc if fs != "fat12" else 0xFF8)
    if fs == "fat32":
        # FAT[1]: bit 27 set = cleanly shut down, bit 26 = hard error seen.
        # A clean volume has 0x0FFFFFFF here with the dirty flag *clear*.
        v = 0x0FFFFFFF
        if dirty:
            v &= ~0x08000000                 # clear ClnShutBit -> dirty
        struct.pack_into("<I", b, 4, v)
    for c in bad_clusters:
        if 2 <= c < nclusters:
            put(c, badv)
    for c in out_of_range:
        if 2 <= c < nclusters:
            put(c, nclusters + 5000)
    return bytes(b)


def make_fat32(path: str, size_mb: int = 1024, dirty: bool = False,
               bad_clusters: list[int] | None = None,
               out_of_range: list[int] | None = None,
               break_backup: bool = False, zero_fsecond_fat: bool = False,
               corrupt_sig: bool = False, label: str = "TESTVOL",
               spc: int = 8) -> str:
    """Build a spec-correct FAT32 image.

    Note the default size is 256 MB: below ~64 MB a *correct* formatter
    cannot make FAT32 at all (the cluster count lands in the FAT16 band), so
    small fixtures would be lying about their own type -- and the engine
    would rightly flag them.
    """
    blank_image(path, size_mb)
    total_sectors = (size_mb * 1024 * 1024) // SECTOR
    reserved, fats = 32, 2
    bps = 512
    fatsz, nclusters = fat_size_for(size_mb * 1024 * 1024, reserved, fats, spc, bps, "fat32")
    boot = build_fat_boot(label, "fat32", total_sectors, bps, spc, fats, reserved, fatsz=fatsz)
    if corrupt_sig:
        boot = boot[:510] + b"\x00\x00"
    write_at(path, 0, boot)
    write_at(path, 6 * bps, boot if not break_backup else boot[:-1] + b"\x77")
    # FSInfo lives inside the reserved region (conventionally sector 1), not
    # after it -- writing it past `reserved` collides with the FAT.
    write_at(path, 1 * bps, build_fsinfo())
    fat1 = build_fat_table("fat32", nclusters, dirty=dirty,
                           bad_clusters=bad_clusters, out_of_range=out_of_range)
    write_at(path, reserved * bps, fat1)
    fat2 = build_fat_table("fat32", nclusters, dirty=dirty,
                           bad_clusters=bad_clusters, out_of_range=out_of_range)
    if zero_fsecond_fat:
        fat2 = b"\x00" * len(fat2)
    write_at(path, (reserved + fatsz) * bps, fat2)
    return str(path)


def make_fat16(path: str, size_mb: int = 16, dirty: bool = False) -> str:
    """FAT16 image with a correct cluster count so the type byte is right."""
    blank_image(path, size_mb)
    total_sectors = (size_mb * 1024 * 1024) // SECTOR
    bps, spc, fats, reserved = 512, 4, 2, 4
    root_entries = 512
    root_dir_sectors = ((root_entries * 32) + bps - 1) // bps
    # choose fatsz so that cluster count lands in the FAT16 band (4085..65524)
    data = total_sectors - reserved - root_dir_sectors
    clusters = data // spc
    fatsz = (clusters * 2 + bps - 1) // bps
    while (total_sectors - reserved - fats * fatsz - root_dir_sectors) // spc >= 65525:
        fatsz += 1
    boot = build_fat_boot("TESTFAT16", "fat16", total_sectors, bps, spc, fats,
                          reserved, root_entries, fatsz)
    write_at(path, 0, boot)
    nclusters = (total_sectors - reserved - fats * fatsz - root_dir_sectors) // spc
    fat = build_fat_table("fat16", nclusters, dirty=False)
    write_at(path, reserved * bps, fat)
    write_at(path, (reserved + fatsz) * bps, fat)
    return str(path)


# ----------------------------------------------------------------------------
# exFAT
# ----------------------------------------------------------------------------

def build_exfat_boot(vol_label: str = "TESTEXFAT", total_sectors: int = 131072,
                     serial: int = 0xDEADBEEF, dirty: bool = False) -> bytes:
    b = bytearray(SECTOR)
    b[0:3] = b"\xEB\x76\x90"
    b[3:11] = b"EXFAT   "
    struct.pack_into("<Q", b, 64, 0)                      # PartitionOffset
    struct.pack_into("<Q", b, 72, total_sectors)          # VolumeLength
    struct.pack_into("<I", b, 80, 24)                     # FatOffset
    struct.pack_into("<I", b, 84, 128)                    # FatLength
    struct.pack_into("<I", b, 88, 24 + 128 * 2)           # ClusterHeapOffset
    struct.pack_into("<I", b, 92, 8192)                   # ClusterCount
    struct.pack_into("<I", b, 96, 4)                      # FirstClusterOfRootDirectory
    struct.pack_into("<I", b, 100, serial)                # VolumeSerialNumber
    struct.pack_into("<H", b, 104, 0x0100)                # FileSystemRevision
    flags = 0x0002 if dirty else 0x0000
    struct.pack_into("<H", b, 106, flags)                 # VolumeFlags
    b[108] = 9    # 512 bytes/sector
    b[109] = 3    # 8 sectors/cluster
    b[110] = 1    # NumberOfFats
    b[111] = 0x80
    b[112] = 5    # PercentInUse
    b[510:512] = b"\x55\xAA"
    return bytes(b)


def build_exfat_region(boot: bytes, dirty: bool = False) -> bytes:
    """12-sector boot region: sector 0 + 8 extended + OEM + reserved."""
    region = bytearray(12 * SECTOR)
    region[0:SECTOR] = boot
    # ExtendedBootSignature at offset 512*1 = 0xAA550000
    struct.pack_into("<I", region, 512, 0xAA550000)
    chk = 0
    for i in range(11 * SECTOR):
        if i in (106, 107) or i in (112, 113):
            continue
        chk = ((chk << 31) | (chk >> 1)) & 0xFFFFFFFF
        chk = (chk + region[i]) & 0xFFFFFFFF
    struct.pack_into("<I", region, 11 * SECTOR, chk)
    return bytes(region)


def make_exfat(path: str, size_mb: int = 64, dirty: bool = False,
               corrupt_checksum: bool = False) -> str:
    blank_image(path, size_mb)
    total_sectors = (size_mb * 1024 * 1024) // SECTOR
    boot = build_exfat_boot("TESTEXFAT", total_sectors, dirty=dirty)
    region = bytearray(build_exfat_region(boot, dirty))
    if corrupt_checksum:
        region[100] ^= 0xFF
    write_at(path, 0, bytes(region))
    write_at(path, (total_sectors - 12) * SECTOR, build_exfat_region(boot, dirty))
    return str(path)


# ----------------------------------------------------------------------------
# NTFS
# ----------------------------------------------------------------------------

def build_ntfs_boot(total_sectors: int = 262144, mft_lcn: int = 1024,
                    serial: int = 0x1122334455667788, bps: int = 512,
                    spc: int = 8, valid_sig: bool = True) -> bytes:
    b = bytearray(SECTOR)
    b[0:3] = b"\xEB\x52\x90"
    b[3:11] = b"NTFS    "
    struct.pack_into("<H", b, 11, bps)
    b[13] = spc
    struct.pack_into("<Q", b, 40, total_sectors)
    struct.pack_into("<Q", b, 48, mft_lcn)
    struct.pack_into("<Q", b, 56, mft_lcn * 2)
    struct.pack_into("<i", b, 64, -10)
    struct.pack_into("<i", b, 68, 1)
    struct.pack_into("<Q", b, 72, serial)
    b[510:512] = b"\x55\xAA" if valid_sig else b"\x00\x00"
    return bytes(b)


def make_ntfs(path: str, size_mb: int = 64, good_mft: bool = True,
              valid_sig: bool = True) -> str:
    blank_image(path, size_mb)
    total_sectors = (size_mb * 1024 * 1024) // SECTOR
    boot = build_ntfs_boot(total_sectors, valid_sig=valid_sig)
    write_at(path, 0, boot)
    if good_mft:
        # cluster size = 512 * 8 = 4096; $MFT lives at LCN 1024
        rec = bytearray(1024)
        rec[0:4] = b"FILE"
        rec[510:512] = b"\x55\xAA"
        write_at(path, 1024 * 4096, bytes(rec))
    return str(path)


# ----------------------------------------------------------------------------
# GPT
# ----------------------------------------------------------------------------

def guid_bytes(s: str) -> bytes:
    parts = s.replace("-", "")
    d1 = int(parts[0:8], 16)
    d2 = int(parts[8:12], 16)
    d3 = int(parts[12:16], 16)
    d4 = bytes.fromhex(parts[16:20])
    d5 = bytes.fromhex(parts[20:32])
    return struct.pack("<IHH", d1, d2, d3) + d4 + d5


EFI_SYSTEM = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MS_BASIC = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"


def build_gpt(path: str, size_mb: int = 256, nparts: int = 2,
              break_primary_crc: bool = False,
              break_entries_crc: bool = False,
              no_backup: bool = False,
              with_filesystem: bool = True) -> str:
    blank_image(path, size_mb)
    total_sectors = (size_mb * 1024 * 1024) // SECTOR
    last_lba = total_sectors - 1

    entry_size = 128
    entry_count = 128
    entries_bytes = 128 * 128          # 16 KiB = 32 sectors
    entries_lba = 2
    first_usable = entries_lba + (entries_bytes // SECTOR)
    last_usable = last_lba - 33

    # partition entries
    tbl = bytearray(entries_bytes)
    span = (last_usable - first_usable) // max(nparts, 1)
    for i in range(nparts):
        first = first_usable + 1 + i * span
        last = first + span - 2
        o = i * entry_size
        tbl[o:o + 16] = guid_bytes(EFI_SYSTEM if i == 0 else MS_BASIC)
        tbl[o + 16:o + 32] = guid_bytes(f"11111111-2222-3333-4444-{i:012d}")
        struct.pack_into("<Q", tbl, o + 32, first)
        struct.pack_into("<Q", tbl, o + 40, last)
        name = (f"Part{i + 1}").encode("utf-16-le")
        tbl[o + 56:o + 56 + len(name)] = name

    if break_entries_crc:
        tbl[300] ^= 0xFF
    entries_crc = _crc32(bytes(tbl))
    if break_entries_crc:
        entries_crc = (_crc32(bytes(tbl))) ^ 0xDEADBEEF

    def build_header(current_lba: int, backup_lba: int, ent_lba: int, ent_crc: int,
                     break_crc: bool = False) -> bytes:
        h = bytearray(SECTOR)
        h[0:8] = b"EFI PART"
        struct.pack_into("<I", h, 8, 0x00010000)
        struct.pack_into("<I", h, 12, 92)
        struct.pack_into("<I", h, 16, 0)   # crc placeholder, must be zeroed
        struct.pack_into("<I", h, 20, 0)
        struct.pack_into("<Q", h, 24, current_lba)
        struct.pack_into("<Q", h, 32, backup_lba)
        struct.pack_into("<Q", h, 40, first_usable)
        struct.pack_into("<Q", h, 48, last_usable)
        h[56:72] = guid_bytes("aabbccdd-1122-3344-5566-778899aabbcc")
        struct.pack_into("<Q", h, 72, ent_lba)
        struct.pack_into("<I", h, 80, entry_count)
        struct.pack_into("<I", h, 84, entry_size)
        struct.pack_into("<I", h, 88, ent_crc)
        crc = _crc32(bytes(h[:92]))
        if break_crc:
            crc ^= 0x00112233
        struct.pack_into("<I", h, 16, crc)
        return bytes(h)

    prim = build_header(1, last_lba, entries_lba, entries_crc, break_primary_crc)
    write_at(path, 512, prim)
    write_at(path, entries_lba * 512, bytes(tbl))

    if not no_backup:
        bkp_entries_lba = last_lba - 32
        bkp = build_header(last_lba, 1, bkp_entries_lba, entries_crc)
        write_at(path, last_lba * 512, bkp)
        write_at(path, bkp_entries_lba * 512, bytes(tbl))

    # Put a real, spec-correct FAT32 volume inside partition 1 so the fixture
    # resembles an actual formatted drive rather than a bare container.
    if nparts >= 1 and with_filesystem:
        p1_first = first_usable + 1
        p1_last = p1_first + span - 2
        p1_bytes = (p1_last - p1_first + 1) * SECTOR
        part_img = str(Path(path)) + ".part"
        # A genuine FAT32 volume needs >65525 clusters, which for a small
        # partition means SMALL clusters, not large ones -- so walk spc
        # downward from the largest legal value and take the first fit.
        ps_mb = max(p1_bytes // (1024 * 1024), 16)
        pspc, best_nc = 1, 0
        for cand in (64, 32, 16, 8, 4, 2, 1):
            _, nc = fat_size_for(ps_mb * 1024 * 1024, 32, 2, cand, SECTOR, "fat32")
            if nc >= 65526:
                pspc, best_nc = cand, nc
                break
        make_fat32(part_img, ps_mb, label="GPTPART1", spc=pspc)
        with open(part_img, "rb") as fh:
            data = fh.read(p1_bytes)
        write_at(path, p1_first * SECTOR, data)
        try:
            os.remove(part_img)
        except OSError:
            pass

    # protective MBR: type 0xEE entry *plus* a valid 55AA signature (both are
    # required; a missing signature is what makes a GPT disk read as RAW)
    e = mbr_entry(0x00, 0xEE, 1, min(total_sectors - 1, 0xFFFFFFFF))
    write_at(path, 0, build_mbr([e], boot_code=b"", valid_signature=True))
    return str(path)


# ----------------------------------------------------------------------------
# corruption scenarios
# ----------------------------------------------------------------------------

def make_raw_wiped(path: str, size_mb: int = 32) -> str:
    """What a power-loss during format, or a bad controller, leaves behind."""
    blank_image(path, size_mb)
    return str(path)


def make_lost_partition_table(path: str, size_mb: int = 1024) -> str:
    """Filesystem intact, but nothing points at it (table wiped)."""
    p = make_fat32(path, size_mb)
    write_at(path, 0, b"\x00" * 512)
    return p


def make_zero_size_fake(path: str) -> str:
    """A device that enumerates but answers nothing."""
    with open(path, "wb"):
        pass
    return str(path)


def make_write_protected_fat(path: str, size_mb: int = 1024) -> str:
    """FAT32 whose first FAT was zeroed (controller went read-only mid-write)."""
    p = make_fat32(path, size_mb)
    write_at(path, 32 * 512, b"\x00" * 4096)
    return p


def make_multiple_tables(path: str, size_mb: int = 1024) -> str:
    """Two FAT32 partitions: normal dual-partition flash drive layout."""
    p = make_fat32(path, size_mb)
    total = (size_mb * 1024 * 1024) // SECTOR
    half = total // 2
    e1 = mbr_entry(0x80, 0x0C, 2048, half - 2048)
    e2 = mbr_entry(0x00, 0x0C, half, total - half - 2048)
    write_at(path, 0, build_mbr([e1, e2]))
    write_at(path, 2048 * SECTOR,
             build_fat_boot("PART1", "fat32", half - 2048, fatsz=200))
    return p


def make_oversized_partition(path: str, size_mb: int = 16) -> str:
    """Partition claims far more space than the device has (fake capacity)."""
    total = (size_mb * 1024 * 1024) // SECTOR
    e = mbr_entry(0x00, 0x0C, 2048, total * 24)   # claims 24x the real size
    p = blank_image(path, size_mb)
    write_at(path, 0, build_mbr([e]))
    return str(p)


def make_dirty_fat32(path: str, size_mb: int = 1024) -> str:
    return make_fat32(path, size_mb, dirty=True)


def make_bad_clusters(path: str, size_mb: int = 1024) -> str:
    return make_fat32(path, size_mb, bad_clusters=[10, 11, 12, 40, 41])


def make_fat_copies_mismatch(path: str, size_mb: int = 1024) -> str:
    return make_fat32(path, size_mb, zero_fsecond_fat=True)


def make_fat_type_mismatch(path: str, size_mb: int = 1024) -> str:
    """FAT32-labelled boot sector on a geometry that is really FAT16.

    FAT12/16/32 are *derived* from the cluster count -- there is no type
    field in the boot sector, only the "FAT32   " label and the FAT32-only
    fields (FSInfo pointer, root cluster, backup boot sector). A formatter
    that writes those FAT32 fields onto a FAT16-sized volume produces a boot
    sector that says FAT32 while the FAT can only be 16-bit: Windows then
    shows RAW. That is the mismatch this fixture encodes.
    """
    blank_image(path, size_mb)
    bps, spc = 512, 64
    reserved, fats = 32, 2
    fatsz = 512
    # Declare a volume whose data region holds ~16k clusters (FAT16 band) but
    # still carry the FAT32 label string and FAT32-only fields.
    total = (spc * 16000 + reserved + fats * fatsz) // 1
    boot = bytearray(build_fat_boot("MISMATCH", "fat32", total, bps, spc, fats,
                                    reserved, fatsz=fatsz))
    write_at(path, 0, bytes(boot))
    nclusters = (total - reserved - fats * fatsz) // spc
    write_at(path, reserved * bps, build_fat_table("fat32", max(nclusters, 16)))
    write_at(path, (reserved + fatsz) * bps, build_fat_table("fat32", max(nclusters, 16)))
    return str(path)


# ----------------------------------------------------------------------------
# FAT32 with real directory entries and file data
# ----------------------------------------------------------------------------

def _dirent(short: str, ext: str, attr: int, cluster: int, size: int,
            deleted: bool = False, ctime: int = 0x57A9) -> bytes:
    """One 32-byte FAT directory entry."""
    b = bytearray(32)
    name = short.upper().encode("cp437")[:8].ljust(8, b" ")
    ex = ext.upper().encode("cp437")[:3].ljust(3, b" ")
    b[0:8] = name
    b[8:11] = ex
    if deleted:
        b[0] = 0xE5
    b[11] = attr
    b[12] = 0
    struct.pack_into("<H", b, 14, (ctime & 0xFFFF))
    struct.pack_into("<H", b, 16, 0x57A9)
    struct.pack_into("<H", b, 20, (cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", b, 22, 0x57A9)
    struct.pack_into("<H", b, 24, 0x7000)
    struct.pack_into("<H", b, 26, cluster & 0xFFFF)
    struct.pack_into("<I", b, 28, size)
    return bytes(b)


def _lfn_entry(seq: int, chunk: str, last: bool, short_checksum: int) -> bytes:
    b = bytearray(32)
    b[0] = (seq | (0x40 if last else 0)) & 0xFF
    b[11] = 0x0F
    b[12] = 0
    b[13] = short_checksum
    enc = chunk.encode("utf-16-le")
    enc = enc.ljust(26, b"\x00")
    b[1:11] = enc[0:10]
    b[14:26] = enc[10:22]
    b[28:32] = enc[22:26]
    return bytes(b)


def _lfn_checksum(short11: bytes) -> int:
    s = 0
    for c in short11:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def make_fat32_with_files(path: str, size_mb: int = 1024,
                          deleted: bool = False,
                          hidden: bool = False,
                          orphan_lfn: bool = False) -> str:
    """A FAT32 volume with real files: one live, one optionally deleted, one
    optionally hidden, plus a subdirectory.

    This is the fixture family that makes recovery testable -- the point is
    that the *data region* is intact and only the directory metadata is in
    the state we want to detect.
    """
    make_fat32(path, size_mb, label="FILDISK")
    boot = read_at(path, 0, 512)
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    fatsz = struct.unpack_from("<I", boot, 36)[0]
    csize = bps * spc
    data_start_sector = reserved + fats * fatsz

    def clus_off(cluster: int) -> int:
        return (data_start_sector + (cluster - 2) * spc) * bps

    ROOT = 2
    SUBDIR = 3
    payload_live = b"This is the live file: USB Doctor recovery test.\n" * 40
    payload_deleted = b"DELETED FILE CONTENT -- should come back intact.\n" * 40
    payload_hidden = b"Hidden file content, attribute byte says HIDDEN.\n" * 40
    payload_sub = b"Nested file inside a subdirectory.\n" * 40

    # root directory entries
    root = bytearray(csize)
    off = 0
    root[off:off + 32] = _dirent("LIVE", "TXT", ATTR_ARCHIVE, 4, len(payload_live))
    off += 32
    root[off:off + 32] = _dirent("GONE", "TXT", ATTR_ARCHIVE, 5, len(payload_deleted),
                                 deleted=deleted)
    off += 32
    root[off:off + 32] = _dirent("SECRET", "TXT",
                                 ATTR_ARCHIVE | (ATTR_HIDDEN if hidden else 0),
                                 6, len(payload_hidden))
    off += 32
    root[off:off + 32] = _dirent("SUBDIR", "", ATTR_DIRECTORY, SUBDIR, 0)
    write_at(path, clus_off(ROOT), bytes(root))

    # subdirectory with one file
    sub = bytearray(csize)
    sub[0:32] = _dirent("NESTED", "TXT", ATTR_ARCHIVE, 7, len(payload_sub))
    write_at(path, clus_off(SUBDIR), bytes(sub))

    # a long-name entry for the live file so LFN reassembly is exercised
    if orphan_lfn:
        lfn = _lfn_entry(1, "A Very Long File Name.txt", True, 0x42)
        cur = read_at(path, clus_off(ROOT), 32)
        write_at(path, clus_off(ROOT), lfn + cur)   # shifts entries forward

    write_at(path, clus_off(4), payload_live)
    write_at(path, clus_off(5), payload_deleted)
    write_at(path, clus_off(6), payload_hidden)
    write_at(path, clus_off(7), payload_sub)

    # FAT chains: each file occupies exactly one cluster
    eoc = 0x0FFFFFFF
    fat_entries = {
        0: 0x0FFFFFF8, 1: 0x0FFFFFFF, 2: eoc, 3: eoc,
        4: eoc, 5: eoc, 6: eoc, 7: eoc,
    }
    fat = bytearray(512)
    for i, v in fat_entries.items():
        struct.pack_into("<I", fat, i * 4, v)
    write_at(path, reserved * bps, bytes(fat))
    write_at(path, (reserved + fatsz) * bps, bytes(fat))
    return str(path)


def make_fat32_with_multicluster_file(path: str, size_mb: int = 1024,
                                      nclusters: int = 6) -> str:
    """A file spanning several clusters in a deliberately NON-contiguous
    chain, so the recovery code is proven to follow the FAT rather than
    assuming contiguity."""
    make_fat32(path, size_mb, label="CHAINFS")
    boot = read_at(path, 0, 512)
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    fatsz = struct.unpack_from("<I", boot, 36)[0]
    csize = bps * spc
    data_start_sector = reserved + fats * fatsz

    def clus_off(c: int) -> int:
        return (data_start_sector + (c - 2) * spc) * bps

    chain = [4, 9, 5, 12, 6, 20][:nclusters]
    payload = bytearray()
    for i, c in enumerate(chain):
        block = (f"cluster {i} (real cluster {c}) ".encode() * 8).ljust(csize, b".")
        write_at(path, clus_off(c), block[:csize])
        payload += block[:csize]
    size = len(payload) - 100                    # last cluster not fully used

    root = bytearray(csize)
    root[0:32] = _dirent("CHAIN", "BIN", ATTR_ARCHIVE, chain[0], size)
    write_at(path, clus_off(2), bytes(root))

    fat = bytearray(512)
    struct.pack_into("<I", fat, 0, 0x0FFFFFF8)
    struct.pack_into("<I", fat, 4, 0x0FFFFFFF)
    struct.pack_into("<I", fat, 8, 0x0FFFFFFF)      # root dir
    for i, c in enumerate(chain):
        nxt = chain[i + 1] if i + 1 < len(chain) else 0x0FFFFFFF
        if c * 4 + 4 <= len(fat):
            struct.pack_into("<I", fat, c * 4, nxt)
    write_at(path, reserved * bps, bytes(fat))
    write_at(path, (reserved + fatsz) * bps, bytes(fat))
    return str(path)


def make_carve_target(path: str) -> str:
    """A blank medium carrying recognisable file bodies but no filesystem at
    all -- the state after a format, where only carving can help."""
    blank_image(path, 16)
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 200 + b"IEND\xAE\x42\x60\x82")
    jpg = (b"\xFF\xD8\xFF\xE0" + b"\x00" * 300 + b"\xFF\xD9")
    pdf = (b"%PDF-1.4\n" + b"0" * 400 + b"\n%%EOF")
    write_at(path, 1024 * 1024, png)
    write_at(path, 3 * 1024 * 1024, jpg)
    write_at(path, 6 * 1024 * 1024, pdf)
    return str(path)
