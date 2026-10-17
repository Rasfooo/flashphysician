# Changelog

All notable changes to FlashPhysician are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.1] - 2026-10-17

### Fixed

**Windows builds were reported as malware.** The engine was never the problem --
the packaging was. Users saw "Windows Defender says this file is a virus" and
some files were quarantined outright, for a tool that only reads and repairs
USB drives.

- **UPX is disabled for every build.** `upx=True` compressed the PE sections,
  and a compressed PE with an appended high-entropy archive is exactly the
  shape of a packed malware stub. The size saving was not worth it.
- **Version resource added** (`tools/version_info.txt`). An unsigned exe with
  no publisher, product or copyright has no reputation to build on, which is
  why SmartScreen called it "unrecognised app" and some scanners scored it as
  generic. Every Windows binary is now stamped with CompanyName, ProductName,
  FileDescription and LegalCopyright.
- **`-ExecutionPolicy Bypass` is gone** from the Windows backend; it uses
  `RemoteSigned`. The permissive policy string is the single most common
  marker in malware launchers, and its presence in an unsigned PyInstaller
  binary is enough to land on a block list.
- **The `cmd /c echo. > file` volume flush is gone**, replaced by
  `Write-VolumeCache`. A packaged Python exe that spawns `cmd.exe` with an
  output redirect looks like a dropper to a static scanner, and the old code
  achieved nothing -- it wrote a file and deleted it again.

### Added

- **Packaging guard suite** (`tests/test_packaging.py`, 145 checks) asserting
  that no AV-trigger pattern can silently return: UPX off, version resource
  wired into every EXE, no policy-bypass or shell-redirect literals, README
  accurate about the unsigned warning, workflow enforcing the same rules.
- **`windows-latest` build workflow** (`.github/workflows/build.yml`) that
  tests, builds, stamps, hashes and uploads the artefacts, and fails the build
  if a guard regresses. Releases now carry `SHA256SUMS.txt`.
- README section explaining the SmartScreen warning, how to verify a download
  by hash, and where to report a vendor false positive.

### Notes

The binaries remain **unsigned**. SmartScreen will still show "Windows
protected your PC" on first run for a newly published publisher; that is a
reputation warning, not a detection. Removing it entirely requires a
code-signing certificate.

## [1.0.0] - 2026-10-16

First release.

### Added

**Diagnosis (read-only)**
- Partition table inspection: MBR (signature, CHS sanity, overlap,
  out-of-bounds entries, hybrid layouts) and GPT (primary + backup header
  CRC32, partition-entry-array CRC32, protective-MBR handling).
- Filesystem detection and audit for FAT12/16/32, exFAT, NTFS, ext2/3/4,
  HFS, APFS and ISO9660 from the raw boot-sector bytes.
- Orphaned-filesystem discovery: when the partition table is gone but the
  filesystem survives, the report says *recoverable* rather than
  *unrecoverable* and records the offset for the rebuild.
- Health score (0-100), letter grade and plain-language verdict, with the
  score weighted so a missing table with intact data is capped (an earlier
  build reported the most salvageable case as a total loss).
- Machine-readable output via `diagnose --json`.

**Repair (planned before it writes)**
- Every repair is a `RepairPlan`: concrete byte writes with offsets,
  printed before anything happens, applied only with `--yes`.
- Sectors are backed up to `~/.flashphysician/backups/` before being written.
- MBR signature restore, MBR rebuild from an orphaned filesystem, GPT rebuild
  from its backup, FAT32 boot-sector restore from its own backup, FAT
  type-label correction, dirty-flag clear.
- Conflicting plans for the same bytes are folded into one (a rebuild
  supersedes a signature patch).
- `guard_target()` refuses whole block devices without `--force`.

**Filesystem repair (OS-level)**
- Linux: `fsck.fat` / `dosfsck` / `fsck.exfat` / `ntfsfix` / `e2fsck`,
  selected by whichever exists, with an actionable install hint when none is
  present. Read-only by default; `--apply` to repair.
- Windows: `chkdsk` / `Repair-Volume`, `diskpart` clean + create + format
  (`confirm=True` required), `Clear-Disk`, `mountvol` drive-letter assign,
  software write-protect flag clearing.

**Recovery**
- FAT directory walking including deleted entries (first char `0xE5`) and
  hidden files, with LFN reassembly and subdirectory traversal.
- Multi-cluster files reassembled by following the FAT chain; contiguous
  fallback for deleted files.
- Signature carving for 13 file types when no filesystem remains.
- Recovered files always written outside the source device; a destination
  inside it is refused.

**Medium tests**
- `scan`: write-read-verify over a range, reporting exact failing offsets.
- `capacity`: fake-flash detection by writing offset-encoding markers and
  locating where reads stop agreeing — reports the *real* capacity, not just
  "something is wrong". Fake flash is destructive to test, so `--yes` is
  required.

**CLI**
- `list`, `info`, `diagnose`, `scan`, `capacity`, `repair`, `fix`, `recover`,
  `image`, `eject`.
- Meaningful exit codes (0 success, 1 nothing to do, 2 bad usage, 3 errors
  found, 4 fake capacity, 5 repair step failed, 70 internal error) so the
  tool is safe to script against.

**GUI**
- Tkinter desktop app (zero dependencies) with drive picker, health card,
  report / advice / detail tabs, and guarded destructive actions.

**Packaging and assets**
- `pyproject.toml` with `flashphysician` and `flashphysician-gui` entry points,
  `dependencies = []`.
- `tools/flashphysician.spec` for PyInstaller: windowed, one-file portable and
  console builds.
- Logo, wordmark, terminal card, PNG and multi-resolution ICO generators
  driven from SVG source with headless Chrome.

**Tests**
- 74 tests across 6 suites (engine, repair, surface, recovery, CLI, GUI),
  all runnable with plain `python3` and no test framework.
- Fixtures written byte-for-byte in Python so each corruption is encoded
  exactly, including states that are awkward to produce on real hardware.

[1.0.0]: https://github.com/Rasfooo/flashphysician/releases/tag/v1.0.0