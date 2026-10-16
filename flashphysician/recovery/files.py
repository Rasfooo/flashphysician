"""File-level recovery: what users actually want back.

Three distinct cases, in order of how often they occur in the field:

1. **Hidden / vanished files (FAT).**  Almost always one of: the directory
   entry's attribute byte got set to hidden/system, the entry was marked
   deleted (first byte 0xE5) but the clusters are still intact, or the long
   file name got orphaned from its 8.3 short name. All three are readable
   from the directory structure without touching the data region.
2. **Files in orphaned clusters.**  After a FAT corruption the allocation
   table no longer points at the data, but the directory entries still do
   (or the data is still contiguous). We walk the directory tree first, then
   fall back to a linear cluster sweep.
3. **Carving.**  No directory metadata left at all: scan for known file
   signatures and reconstruct. Last resort, and we say so.

Everything here is READ-ONLY on the source. Recovered files are written to a
destination directory, never back onto the damaged drive -- writing to a
drive you are recovering from is how people destroy their own data.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.imaging import Source
from ..core.structures import (
    FsInfo, FsType, human, parse_boot_sector, read_fat_entries, fat_eoc,
)

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LFN = 0x0F


@dataclass
class FatEntry:
    """A directory entry (short or reconstructed)."""

    name: str
    short_name: str = ""
    attr: int = 0
    offset: int = 0                    # where the entry lives
    dir_path: str = ""
    first_cluster: int = 0
    size: int = 0
    deleted: bool = False
    hidden: bool = False
    lfn_orphaned: bool = False
    mtime: str = ""
    recoverable: bool = False
    reason: str = ""

    @property
    def is_dir(self) -> bool:
        return bool(self.attr & ATTR_DIRECTORY)

    @property
    def flags(self) -> list[str]:
        out = []
        if self.deleted:
            out.append("deleted")
        if self.attr & ATTR_HIDDEN:
            out.append("hidden")
        if self.attr & ATTR_SYSTEM:
            out.append("system")
        if self.attr & ATTR_READ_ONLY:
            out.append("read-only")
        if self.lfn_orphaned:
            out.append("lfn-orphaned")
        return out

    def describe(self) -> str:
        return (f"{self.name!r} ({human(self.size)}) cluster {self.first_cluster} "
                f"[{', '.join(self.flags) or 'normal'}]")


@dataclass
class RecoveryReport:
    device: str
    entries: list[FatEntry] = field(default_factory=list)
    recovered: list[tuple[str, int]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    carved: list[tuple[str, int]] = field(default_factory=list)
    dest: str = ""
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)
    read_errors: int = 0

    @property
    def suspicious(self) -> list[FatEntry]:
        return [e for e in self.entries if e.flags and not e.is_dir]

    @property
    def total_bytes(self) -> int:
        return sum(n for _, n in self.recovered)


# --------------------------------------------------------------------------
# directory parsing
# --------------------------------------------------------------------------

def _decode_lfn(entries: list[bytes]) -> str:
    """Assemble a long filename from its 0x0F component entries."""
    parts: list[str] = []
    for raw in entries:
        chunk = raw[1:11] + raw[14:26] + raw[28:32]
        try:
            txt = chunk.decode("utf-16-le", errors="ignore")
        except Exception:
            continue
        txt = txt.split("\x00")[0]
        parts.append(txt)
    return "".join(parts)


def _parse_dirent(raw: bytes, index: int, dir_path: str, base_off: int) -> FatEntry | None:
    if len(raw) < 32:
        return None
    first = raw[0]
    if first == 0x00:
        return None                     # end of directory
    attr = raw[11]
    if attr == ATTR_LFN:
        return None                     # long-name component, handled by caller
    if attr & ATTR_VOLUME and not (attr & ATTR_DIRECTORY):
        return None                     # volume label, not a file

    deleted = first == 0xE5
    name_bytes = raw[0:8]
    if deleted:
        name_bytes = b"_" + name_bytes[1:]
    short = name_bytes.decode("cp437", errors="ignore").strip()
    ext = raw[8:11].decode("cp437", errors="ignore").strip()
    short_name = f"{short}.{ext}" if ext else short

    hi = struct.unpack_from("<H", raw, 20)[0]
    lo = struct.unpack_from("<H", raw, 26)[0]
    first_cluster = (hi << 16) | lo
    size = struct.unpack_from("<I", raw, 28)[0]

    dt = struct.unpack_from("<H", raw, 22)[0]
    try:
        y = 1980 + ((dt >> 9) & 0x7F)
        mo = (dt >> 5) & 0x0F
        d = dt & 0x1F
        tm = struct.unpack_from("<H", raw, 24)[0]
        hh, mm = (tm >> 11) & 0x1F, (tm >> 5) & 0x3F
        mtime = f"{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mm:02d}"
    except Exception:
        mtime = ""

    return FatEntry(
        name=short_name,
        short_name=short_name,
        attr=attr,
        offset=base_off + index * 32,
        dir_path=dir_path,
        first_cluster=first_cluster,
        size=size,
        deleted=deleted,
        hidden=bool(attr & ATTR_HIDDEN),
        mtime=mtime,
    )


def _cluster_count(info: FsInfo) -> int:
    bps = info.bytes_per_sector or 512
    spc = info.sectors_per_cluster or 1
    root_sectors = ((info.root_entries * 32) + bps - 1) // bps
    data = (info.total_sectors - info.reserved_sectors
            - info.fat_count * info.fat_size_sectors - root_sectors)
    return max(data // spc, 0)


def _cluster_to_offset(info: FsInfo, cluster: int, part_offset: int) -> int:
    """Byte offset of a cluster. Layout: reserved, FATs, (root dir), data."""
    bps = info.bytes_per_sector or 512
    spc = info.sectors_per_cluster or 1
    root_sectors = ((info.root_entries * 32) + bps - 1) // bps
    if info.fs == FsType.FAT32:
        data_start = info.reserved_sectors + info.fat_count * info.fat_size_sectors
    else:
        data_start = (info.reserved_sectors + info.fat_count * info.fat_size_sectors
                      + root_sectors)
    return part_offset + (data_start + (cluster - 2) * spc) * bps


def walk_fat_directory(src: Source, info: FsInfo, part_offset: int,
                       fat: list[int] | None = None,
                       max_entries: int = 20000,
                       progress=None) -> list[FatEntry]:
    """Enumerate directory entries across the whole FAT tree.

    Both the live directory tree AND deleted entries are collected, because a
    deleted entry is the single most common "my files disappeared" case.
    """
    out: list[FatEntry] = []
    bps = info.bytes_per_sector or 512
    csize = info.cluster_size or 4096
    eoc = fat_eoc(info)
    max_cluster = max(_cluster_count(info), 3)

    def parse_region(offset: int, size: int, dir_path: str) -> list[int]:
        """Parse 32-byte entries in a region; return subdirectory clusters."""
        subs: list[int] = []
        raw = src.read_at(offset, size)
        if not raw:
            return subs
        lfn_parts: list[bytes] = []
        n = len(raw) // 32
        for i in range(n):
            blk = raw[i * 32:(i + 1) * 32]
            if len(blk) < 32:
                break
            if blk[0] == 0x00:
                break
            if blk[11] == ATTR_LFN:
                lfn_parts.append(blk)
                continue
            entry = _parse_dirent(blk, i, dir_path, offset)
            if entry is None:
                lfn_parts = []
                continue
            if lfn_parts:
                lfn = _decode_lfn(list(reversed(lfn_parts)))
                if lfn:
                    entry.name = lfn
                lfn_parts = []
            out.append(entry)
            if entry.is_dir and not entry.deleted and 2 <= entry.first_cluster < max_cluster:
                subs.append(entry.first_cluster)
            if len(out) >= max_entries:
                break
        return subs

    if info.fs == FsType.FAT32:
        queue: list[tuple[int, str]] = [(2, "/")]
    else:
        root_start = part_offset + (info.reserved_sectors
                                    + info.fat_count * info.fat_size_sectors) * bps
        root_size = ((info.root_entries * 32) + bps - 1) // bps * bps
        first_subs = parse_region(root_start, root_size, "/")
        queue = [(c, "/") for c in first_subs]

    seen: set[int] = set()
    while queue and len(out) < max_entries:
        cluster, path = queue.pop(0)
        if cluster in seen or cluster < 2 or cluster >= max_cluster:
            continue
        seen.add(cluster)

        # collect the chain for this directory
        chain = [cluster]
        if fat:
            cur = cluster
            for _ in range(4096):
                if cur >= len(fat):
                    break
                nxt = fat[cur] & 0x0FFFFFFF
                if nxt < 2 or nxt >= eoc or nxt >= max_cluster:
                    break
                if nxt in seen:
                    out.append(FatEntry(name="<cross-linked directory>",
                                        dir_path=path, attr=ATTR_DIRECTORY,
                                        reason="directory chain loops back"))
                    break
                seen.add(nxt)
                chain.append(nxt)
                cur = nxt

        for c in chain:
            off = _cluster_to_offset(info, c, part_offset)
            subs = parse_region(off, csize, path)
            for sub in subs:
                if sub not in seen:
                    queue.append((sub, path.rstrip("/") + "/" + str(sub)))
            if progress:
                progress(len(out), max_entries, f"scanning {path}")

    return out


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def extract_fat_file(src: Source, info: FsInfo, part_offset: int, entry: FatEntry,
                     fat: list[int] | None = None, follow_chain: bool = True) -> bytes:
    """Read a file's data, preferring the FAT chain and falling back to
    assuming contiguous clusters (which is what a deleted file usually is)."""
    if entry.size <= 0:
        return b""
    csize = info.cluster_size or 4096
    max_cluster = max(_cluster_count(info), 3)
    eoc = fat_eoc(info)

    chunks = bytearray()
    if follow_chain and fat and 2 <= entry.first_cluster < max_cluster:
        cur = entry.first_cluster
        guard = 0
        while len(chunks) < entry.size and guard < 1_000_000:
            guard += 1
            if cur < 2 or cur >= max_cluster:
                break
            off = _cluster_to_offset(info, cur, part_offset)
            block = src.read_at(off, csize)
            if len(block) < csize:
                block = block + b"\x00" * (csize - len(block))
            chunks += block
            nxt = fat[cur] & 0x0FFFFFFF if cur < len(fat) else eoc
            if nxt < 2 or nxt >= eoc or nxt >= max_cluster:
                break
            cur = nxt
    else:
        # contiguous fallback: deleted files almost always still are
        need = entry.size
        cur = entry.first_cluster
        guard = 0
        while need > 0 and guard < 1_000_000:
            guard += 1
            if cur < 2 or cur >= max_cluster:
                break
            off = _cluster_to_offset(info, cur, part_offset)
            block = src.read_at(off, min(csize, need))
            if not block:
                break
            chunks += block
            need -= len(block)
            cur += 1

    return bytes(chunks[: entry.size])


def recover_files(path: str, dest: str,
                  only_suspicious: bool = False,
                  include_deleted: bool = True,
                  progress=None,
                  stop_flag=None) -> RecoveryReport:
    """Recover FAT files from a damaged volume. READ-ONLY on source."""
    t0 = time.time()
    rep = RecoveryReport(device=path, dest=dest)
    Path(dest).mkdir(parents=True, exist_ok=True)

    with Source(path) as src:
        boot = src.read_at(0, 512)
        info = parse_boot_sector(boot, 0)
        part_offset = 0

        if info.fs not in (FsType.FAT12, FsType.FAT16, FsType.FAT32):
            from ..core.structures import parse_mbr
            mbr = parse_mbr(boot, src.size)
            for p in mbr.partitions:
                probe = parse_boot_sector(src.read_at(p.offset_bytes, 512), p.offset_bytes)
                if probe.fs in (FsType.FAT12, FsType.FAT16, FsType.FAT32):
                    info, part_offset = probe, p.offset_bytes
                    rep.notes.append(
                        f"filesystem found in partition at {human(p.offset_bytes)}")
                    break

        if info.fs not in (FsType.FAT12, FsType.FAT16, FsType.FAT32):
            rep.notes.append(
                f"no FAT filesystem found (detected: {info.fs.value}) -- "
                "directory-tree recovery is not possible; use signature carving")
            rep.duration_s = time.time() - t0
            return rep

        fat = read_fat_entries(src, info, part_offset, limit=262144)
        if not fat:
            rep.notes.append("allocation table unreadable -- using contiguous-cluster "
                             "fallback for every file")

        entries = walk_fat_directory(src, info, part_offset, fat, progress=progress)
        rep.entries = entries
        rep.notes.append(f"{len(entries)} directory entries found, "
                         f"{len(rep.suspicious)} with anomalies")

        used_names: set[str] = set()
        for entry in entries:
            if stop_flag and stop_flag():
                rep.notes.append("cancelled by user")
                break
            if entry.is_dir or entry.size == 0:
                continue
            if entry.deleted and not include_deleted:
                continue
            if only_suspicious and not entry.flags:
                continue

            data = extract_fat_file(src, info, part_offset, entry, fat)
            if not data:
                rep.failed.append((entry.name, "no data readable at cluster "
                                              f"{entry.first_cluster}"))
                continue

            safe = _safe_name(entry.name, entry.deleted, used_names)
            out_path = os.path.join(dest, safe)
            try:
                Path(out_path).write_bytes(data)
                rep.recovered.append((out_path, len(data)))
                entry.recoverable = True
            except OSError as exc:
                rep.failed.append((entry.name, str(exc)))
            if progress:
                progress(len(rep.recovered), len(entries), "recovering")

    rep.duration_s = time.time() - t0
    return rep


def _safe_name(name: str, deleted: bool, used: set[str]) -> str:
    bad = '<>:"/\\|?*\x00'
    clean = "".join(c if (c.isprintable() and c not in bad) else "_" for c in name)
    clean = clean.strip() or "unnamed"
    if deleted:
        base, ext = os.path.splitext(clean)
        clean = f"RECOVERED_{base}{ext or ''}"
    n, candidate = 1, clean
    while candidate.lower() in used:
        base, ext = os.path.splitext(clean)
        n += 1
        candidate = f"{base}_{n}{ext}"
    used.add(candidate.lower())
    return candidate


# --------------------------------------------------------------------------
# signature carving (last resort)
# --------------------------------------------------------------------------

CARVE_SIGNATURES: list[tuple[bytes, str, int, bytes | None]] = [
    (b"\xFF\xD8\xFF", "jpg", 2 * 1024 * 1024, b"\xFF\xD9"),
    (b"\x89PNG\r\n\x1a\n", "png", 2 * 1024 * 1024, b"IEND\xAE\x42\x60\x82"),
    (b"GIF87a", "gif", 1024 * 1024, b"\x00\x3B"),
    (b"GIF89a", "gif", 1024 * 1024, b"\x00\x3B"),
    (b"%PDF-", "pdf", 8 * 1024 * 1024, b"%%EOF"),
    (b"PK\x03\x04", "zip", 16 * 1024 * 1024, None),
    (b"{\\rtf", "rtf", 1024 * 1024, b"}"),
    (b"ID3", "mp3", 8 * 1024 * 1024, None),
    (b"RIFF", "wav", 16 * 1024 * 1024, None),
    (b"\x1A\x45\xDF\xA3", "mkv", 32 * 1024 * 1024, None),
    (b"OggS", "ogg", 8 * 1024 * 1024, None),
    (b"SQLite format 3\x00", "sqlite", 8 * 1024 * 1024, None),
    (b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", "doc", 8 * 1024 * 1024, None),
]


def carve_files(path: str, dest: str, start: int = 0, length: int | None = None,
                chunk: int = 4 * 1024 * 1024, max_files: int = 500,
                types: list[str] | None = None,
                progress=None, stop_flag=None) -> RecoveryReport:
    """Scan for known file signatures and extract them.

    Honest about its limits: carving recovers *content*, not names or dates,
    and a file whose fragments are not contiguous comes back truncated. The
    report says so rather than silently producing broken files.
    """
    t0 = time.time()
    rep = RecoveryReport(device=path, dest=dest)
    Path(dest).mkdir(parents=True, exist_ok=True)
    wanted = [s for s in CARVE_SIGNATURES if not types or s[1] in types]

    with Source(path) as src:
        total = src.size
        scan_end = min(total, start + (length if length is not None else total))
        pos = start
        found = 0
        overlap = 64 * 1024

        while pos < scan_end and found < max_files:
            if stop_flag and stop_flag():
                rep.notes.append("cancelled by user")
                break
            buf = src.read_at(pos, min(chunk, scan_end - pos))
            if not buf:
                break
            for sig, ext, maxsize, endmark in wanted:
                idx = buf.find(sig)
                while idx != -1 and found < max_files:
                    abs_off = pos + idx
                    data = _extract_carved(src, abs_off, maxsize, endmark, scan_end)
                    if data and len(data) > 64:
                        name = f"carved_{abs_off:012x}.{ext}"
                        out = os.path.join(dest, name)
                        try:
                            Path(out).write_bytes(data)
                            rep.carved.append((out, len(data)))
                            found += 1
                        except OSError as exc:
                            rep.failed.append((name, str(exc)))
                    idx = buf.find(sig, idx + 1)
                if progress:
                    progress(found, max_files, f"carving {ext}")
            if len(buf) <= overlap:
                break
            pos += len(buf) - overlap

    rep.notes.append(
        f"{len(rep.carved)} file(s) carved by signature. Carving recovers "
        "content without names, dates or folder structure, and truncates any "
        "file whose fragments are not contiguous on the medium.")
    rep.duration_s = time.time() - t0
    return rep


def _extract_carved(src: Source, offset: int, maxsize: int,
                    endmark: bytes | None, scan_end: int) -> bytes:
    step = 512 * 1024
    collected = bytearray()
    pos = offset
    limit = min(maxsize, scan_end - offset)
    while len(collected) < limit:
        block = src.read_at(pos, min(step, limit - len(collected)))
        if not block:
            break
        collected += block
        if endmark is not None:
            i = collected.find(endmark)
            if i != -1:
                return bytes(collected[: i + len(endmark)])
        pos += len(block)
    return bytes(collected)