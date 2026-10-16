# FlashPhysician — Awesome List Submissions (updated)

## Priority targets (ranked by fit)

### 1. ⭐ ricco020/awesome-data-recovery — **BEST FIT**

- **Repo**: https://github.com/ricco020/awesome-data-recovery
- **Section**: `## Free & open-source` (alongside ddrescue, Foremost, Scalpel, TSK, Autopsy, Safecopy)
- **Format**: `- [Name](url) — short description.`
- **Contributing rules**: actively maintained + freely available; no SEO-spam
- **Branch**: `add-flashphysician`

**Entry**:

```markdown
- [FlashPhysician](https://github.com/Rasfooo/flashphysician) — Diagnose, repair and recover USB flash drives and SD cards: health-scored byte-level report, partition-table rebuild, fake-capacity detection (F3 method), signature carving. Portable Windows .exe, zero-dep Linux.
```

**PR title**: `Add FlashPhysician (USB/SD rescue tool)`

**PR body**:

```markdown
## What

Adding [FlashPhysician](https://github.com/Rasfooo/flashphysician) to the
**Free & open-source** section.

## Why it fits

- **Actively maintained**: v1.0.0 released 2026-10-16, 74 tests, MIT license
- **Open source**: full source at github.com/Rasfooo/flashphysician
- **Non-commercial**: no paid tier, no upsell
- **Not SEO-spam**: real tool with byte-level diagnostics, real test suite,
  real Windows/Linux portable builds

## Entry

```markdown
- [FlashPhysician](https://github.com/Rasfooo/flashphysician) — Diagnose, repair and recover USB flash drives and SD cards: health-scored byte-level report, partition-table rebuild, fake-capacity detection (F3 method), signature carving. Portable Windows .exe, zero-dep Linux.
```

## Relevant links

- Repo: https://github.com/Rasfooo/flashphysician
- Releases: https://github.com/Rasfooo/flashphysician/releases/tag/v1.0.0
- Tests: 74 passing (engine 23, repair 10, surface 7, recovery 10, CLI 15, GUI 9)
```

---

### 2. cugu/awesome-forensics — Carving section

- **Repo**: https://github.com/cugu/awesome-forensics
- **Section**: `### Carving` (after photorec)
- **Branch**: `add-flashphysician`

**Entry**:

```markdown
- [FlashPhysician](https://github.com/Rasfooo/flashphysician) - Diagnose, repair and recover USB flash drives, SD cards and memory cards; rebuilds lost partition tables, detects fake-capacity flash, carves files by signature
```

**PR title**: `Add FlashPhysician (USB/SD rescue tool with carving)`

**PR body**:

```markdown
## What

Adding [FlashPhysician](https://github.com/Rasfooo/flashphysician) to the
**Carving** section, alongside PhotoRec.

## Why it fits

- **Free / open source**: MIT license
- **Carving**: JPEG, PNG, GIF, PDF, ZIP, RTF, MP3, WAV, MKV, OGG, SQLite, OLE/Office
- **Beyond carving**: byte-level MBR/GPT/FAT/exFAT/NTFS diagnosis with health
  score, partition-table repair with per-sector backups, fake-capacity detection
  (F3 method), surface scanning, dying-drive imaging
- **Portable**: Windows .exe (1.6 MB, no install), Linux from source, zero deps
- **Tested**: 74 tests, byte-exact fixtures for each failure mode

## Entry to add (in `### Carving`, after photorec)

```markdown
- [FlashPhysician](https://github.com/Rasfooo/flashphysician) - Diagnose, repair and recover USB flash drives, SD cards and memory cards; rebuilds lost partition tables, detects fake-capacity flash, carves files by signature
```

## Links

- Repo: https://github.com/Rasfooo/flashphysician
- Releases: https://github.com/Rasfooo/flashphysician/releases/tag/v1.0.0
```

---

### 3. bst04/tools-for-data-recovery (secondary)

- **Repo**: https://github.com/bst04/tools-for-data-recovery
- **Entry**:

```markdown
- [FlashPhysician](https://github.com/Rasfooo/flashphysician) - Open-source rescue tool for USB/SD: diagnose (health score), repair partition table, recover deleted + carve by signature, detect fake flash
```

---

## Social posts

### 4. r/datarecovery (Reddit)

**Title**: `Open-source rescue tool for USB flash / SD card — diagnose, repair, recover (FlashPhysician)`

**Body**:

```
Hey — I built a rescue tool for USB flash drives / SD cards that keeps failing
in similar ways.

Repo: https://github.com/Rasfooo/flashphysician

**What it does:**

- `diagnose` — read-only report: health score (0-100), verdict, byte-level
  findings (MBR/GPT/FAT/exFAT/NTFS). Safe on a dying drive.
- `repair` — rebuilds a lost partition table from the filesystem it finds
  elsewhere on the medium. Shows the exact byte writes first, `--yes` to
  apply, backs up every overwritten sector.
- `capacity` — F3-method fake flash detection (reports real capacity, not
  just "failed")
- `recover` — deleted entries + hidden files + signature carving
- `image` — clone a dying drive to an image, tolerating bad sectors
- `scan` — write-read-verify surface test

**Windows**: portable .exe (1.6 MB, no install, no Python needed)
**Linux**: `python3 -m flashphysician diagnose /dev/sdb` (zero dependencies)

74 tests, MIT license. Feedback welcome — especially on real-world failures
I haven't reproduced yet.

Not affiliated with any commercial tool.
```

---

### 5. Hacker News — Show HN

**Title**: `Show HN: FlashPhysician – diagnose and repair corrupt USB/SD drives`
**URL**: https://github.com/Rasfooo/flashphysician

**First comment (post immediately)**:

```
I built this because every time a flash drive fails, the diagnosis ("RAW",
"0 bytes", "files vanished", "write-protected") has a different cause and a
different fix, and the tools that tell them apart are scattered.

FlashPhysician reads the actual bytes (MBR, GPT, FAT tables, exFAT boot
region, $MFT) and gives you a health score + verdict + byte-level findings.
Then:

- `repair` shows the exact byte writes before touching anything (--yes to
  apply, every overwritten sector backed up)
- `capacity` runs the F3 method to detect fake-capacity flash
- `recover` does deleted + hidden + signature carving
- `image` clones a failing drive while tolerating bad sectors

Design invariants I held to:
- diagnosis NEVER writes (safe on a dying drive)
- every write is plannable and shown first
- recovery never writes to the source device
- whole block devices refused without --force

Windows portable .exe, Linux runs from source with zero deps. 74 tests, MIT.

Would love feedback on real-world failures I haven't reproduced yet.
```

---

### 6. Dev.to article (optional, after HN/Reddit traction)

**Title**: `FlashPhysician: an open-source rescue tool for USB flash drives`

Outline:

1. The problem: 4 symptoms, 4 causes, 4 fixes — how do you know which?
2. Reading bytes, not guessing: MBR/GPT/FAT/exFAT/$MFT analysis
3. Health score + verdict: from byte offsets to a 0-100 score
4. Safe by default: plan → show → backup → apply
5. Fake flash: F3 method, detecting controllers that ACK writes past capacity
6. Recovery: 4 lines of defence (live entries → deleted → hidden → carving)
7. Dying drives: image first, work on the image
8. Testing: byte-exact fixtures for each failure mode (74 tests, no framework)
9. Get it: Windows portable .exe / Linux from source
10. What's missing: contributions welcome

---

## Submission checklist

- [ ] awesome-data-recovery PR (ricco020) — **priority 1**
- [ ] awesome-forensics PR (cugu) — **priority 2**
- [ ] bst04/tools-for-data-recovery — lower priority
- [ ] r/datarecovery post
- [ ] HN Show HN submission + first comment
- [ ] Dev.to article (optional, if HN/Reddit traction)

## Notes

- **Do NOT spam** — space submissions out (1 per day)
- **HN**: best window is Tue–Thu, 9am–12pm US Eastern
- **Reddit**: r/datarecovery is strict about self-promotion — frame as
  "I built this, feedback welcome" not "download my tool"
- **Awesome lists**: read CONTRIBUTING.md first, most require specific format
- **Disclose affiliation**: always mention "I built this" or "I'm the author"
