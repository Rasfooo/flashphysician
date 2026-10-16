"""Desktop GUI.

Tkinter on purpose: it ships with CPython on Windows, so the portable .exe has
zero external dependencies. A flash-drive rescue tool that needs a runtime
installed is a tool most people cannot run at the moment they need it.

The window is organised the way the work actually happens -- pick a drive,
look, then act -- and every destructive button stays disabled until a
diagnosis has run, because asking someone to confirm a repair for a problem
the tool has not yet identified is how mistakes happen.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .backends.linux import get_backend
from .core.diagnose import Severity, format_report, quick_diagnose
from .core.imaging import discover_drives
from .core.repair import (
    plan_clear_fat_dirty, plan_fix_fat_type_label, plan_rebuild_mbr_from_fs,
    plan_restore_fat32_boot_backup, plan_restore_gpt_from_backup,
    plan_restore_mbr_signature,
)
from .core.structures import FsType, human
from .recovery.files import carve_files, recover_files
from .recovery.surface import capacity_test, surface_test

# A calm clinical palette: this is a tool for people who are already worried
# about their data, so nothing here should look like a warning sign until
# there is an actual warning.
BG = "#0f1620"
BG2 = "#16202c"
FG = "#e6edf3"
DIM = "#8b98a5"
ACCENT = "#4ea1ff"
OK = "#3fb950"
WARN = "#d29922"
ERR = "#f85149"
DEAD = "#ff7b72"

SEV_COLOR = {
    "ok": OK, "info": ACCENT, "warn": WARN, "error": ERR, "fatal": DEAD,
}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"FlashPhysician {__version__} -- Diagnose, Repair, Recover")
        self.geometry("1080x740")
        self.minsize(900, 620)
        self.configure(bg=BG)

        self.device = tk.StringVar()
        self.status = tk.StringVar(value="Select a drive and press Analyse.")
        self.report = None
        self.drives: list = []
        self.q: queue.Queue = queue.Queue()
        self.busy = False

        self._style()
        self._build()
        self.refresh_drives()
        self.after(120, self._drain)

    # -- chrome ----------------------------------------------------------
    def _style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG,
                     fieldbackground=BG2, troughcolor=BG2)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=BG2)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Dim.TLabel", background=BG, foreground=DIM)
        st.configure("Head.TLabel", background=BG, foreground=ACCENT,
                     font=("Segoe UI", 15, "bold"))
        st.configure("Sub.TLabel", background=BG2, foreground=DIM)
        st.configure("Val.TLabel", background=BG2, foreground=FG,
                     font=("Segoe UI", 10, "bold"))
        st.configure("TButton", padding=(12, 7), background="#233040",
                     foreground=FG, borderwidth=0)
        st.map("TButton", background=[("active", "#2d3d50"),
                                      ("disabled", "#1a2430")],
               foreground=[("disabled", "#5a6673")])
        st.configure("Go.TButton", background=ACCENT, foreground="#06121f",
                     font=("Segoe UI", 10, "bold"))
        st.map("Go.TButton", background=[("active", "#6db4ff")])
        st.configure("Danger.TButton", background="#5b2020", foreground="#ffb3ae")
        st.map("Danger.TButton", background=[("active", "#7a2a2a")])
        st.configure("TCombobox", padding=6)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=BG2, foreground=DIM,
                     padding=(16, 8))
        st.map("TNotebook.Tab", background=[("selected", "#22303f")],
               foreground=[("selected", FG)])
        st.configure("Green.Horizontal.TProgressbar", background=OK,
                     troughcolor=BG2, borderwidth=0)

    def _build(self) -> None:
        head = ttk.Frame(self, padding=(18, 14, 18, 6))
        head.pack(fill="x")
        ttk.Label(head, text="FlashPhysician", style="Head.TLabel").pack(side="left")
        ttk.Label(head, text="  Diagnose · Repair · Recover",
                  style="Dim.TLabel").pack(side="left")

        # drive picker
        pick = ttk.Frame(self, padding=(18, 4, 18, 6))
        pick.pack(fill="x")
        ttk.Label(pick, text="Drive").pack(side="left", padx=(0, 8))
        self.combo = ttk.Combobox(pick, textvariable=self.device, width=62,
                                  state="readonly")
        self.combo.pack(side="left")
        ttk.Button(pick, text="Refresh", command=self.refresh_drives).pack(
            side="left", padx=6)
        ttk.Button(pick, text="Open image file...",
                   command=self.browse_file).pack(side="left")
        self.btn_analyse = ttk.Button(pick, text="Analyse", style="Go.TButton",
                                      command=self.analyse)
        self.btn_analyse.pack(side="left", padx=(14, 0))

        # card
        card = ttk.Frame(self, style="Card.TFrame", padding=14)
        card.pack(fill="x", padx=18, pady=(8, 4))
        self.f_health = self._stat(card, "HEALTH", "--", 0)
        self.f_grade = self._stat(card, "GRADE", "--", 1)
        self.f_table = self._stat(card, "PARTITION TABLE", "--", 2)
        self.f_fs = self._stat(card, "FILESYSTEM", "--", 3)
        self.f_verdict = self._stat(card, "VERDICT", "--", 4, wide=True)
        for i in range(5):
            card.columnconfigure(i, weight=(3 if i == 4 else 1))

        self.prog = ttk.Progressbar(self, style="Green.Horizontal.TProgressbar",
                                    mode="determinate", maximum=100)
        self.prog.pack(fill="x", padx=18, pady=(4, 0))

        # actions
        act = ttk.Frame(self, padding=(18, 8, 18, 4))
        act.pack(fill="x")
        self.actions = []
        for text, cmd, style in (
            ("Repair", self.do_repair, "Go.TButton"),
            ("Surface scan", self.do_scan, "TButton"),
            ("Capacity test", self.do_capacity, "TButton"),
            ("Check filesystem", self.do_fsck, "TButton"),
            ("Recover files", self.do_recover, "TButton"),
            ("Back up to image", self.do_image, "TButton"),
            ("Safe eject", self.do_eject, "TButton"),
        ):
            b = ttk.Button(act, text=text, command=cmd, style=style)
            b.pack(side="left", padx=(0, 7))
            b.state(["disabled"])
            self.actions.append(b)

        # notebook
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=18, pady=(6, 4))
        self.txt = self._pane("Report")
        self.advice = self._pane("What this means")
        self.raw = self._pane("Detail")

        ttk.Label(self, textvariable=self.status, style="Dim.TLabel",
                  padding=(20, 2, 20, 10)).pack(fill="x", anchor="w")

    def _pane(self, title: str) -> tk.Text:
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=title)
        txt = tk.Text(frame, bg=BG2, fg=FG, insertbackground=FG, wrap="word",
                      relief="flat", font=("Consolas" if os.name == "nt"
                                           else "DejaVu Sans Mono", 10),
                      padx=14, pady=10, borderwidth=0)
        sb = ttk.Scrollbar(frame, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.tag_configure("ok", foreground=OK)
        txt.tag_configure("info", foreground=ACCENT)
        txt.tag_configure("warn", foreground=WARN)
        txt.tag_configure("error", foreground=ERR)
        txt.tag_configure("fatal", foreground=DEAD, font=("Segoe UI", 10, "bold"))
        txt.tag_configure("head", foreground=FG, font=("Segoe UI", 12, "bold"))
        txt.tag_configure("dim", foreground=DIM)
        txt.configure(state="disabled")
        return txt

    def _stat(self, parent, label: str, value: str, col: int,
              wide: bool = False) -> ttk.Label:
        f = ttk.Frame(parent, style="Card.TFrame")
        f.grid(row=0, column=col, sticky="nsew", padx=(0, 16))
        ttk.Label(f, text=label, style="Sub.TLabel").pack(anchor="w")
        lbl = ttk.Label(f, text=value, style="Val.TLabel", wraplength=380)
        lbl.pack(anchor="w")
        return lbl

    # -- helpers ---------------------------------------------------------
    def _write(self, widget: tk.Text, text: str, tag: str = "") -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text, tag or ())
        widget.configure(state="disabled")

    def _busy(self, on: bool, msg: str = "") -> None:
        self.busy = on
        for b in self.actions:
            b.state(["disabled"] if (on or not self.report) else ["!disabled"])
        self.btn_analyse.state(["disabled"] if on else ["!disabled"])
        if msg:
            self.status.set(msg)
        self.update_idletasks()

    def _drain(self) -> None:
        """All UI updates happen here, on the main thread.

        Worker threads only ever put callables on the queue -- touching Tk from
        another thread is the classic way a Tkinter app dies at random.
        """
        try:
            while True:
                fn = self.q.get_nowait()
                try:
                    fn()
                except Exception as exc:
                    self.status.set(f"error: {exc}")
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _progress(self):
        def cb(done, total, stage=""):
            pct = (done / total * 100) if total else 0
            self.q.put(lambda p=pct, s=stage: (
                self.prog.configure(value=p),
                self.status.set(f"{s}  {p:.0f}%")))
        return cb

    def _worker(self, fn, done_msg: str = "") -> None:
        def run():
            try:
                result = fn()
                self.q.put(lambda: self._finish(result, done_msg))
            except Exception as exc:
                import traceback
                tb = traceback.format_exc()
                self.q.put(lambda: (
                    self._busy(False, "failed"),
                    self._write(self.txt, f"Error:\n\n{tb}", "error")))
        threading.Thread(target=run, daemon=True).start()

    # -- actions ---------------------------------------------------------
    def refresh_drives(self) -> None:
        try:
            all_drives = discover_drives(include_system=True)
        except Exception as exc:
            self.status.set(f"could not enumerate drives: {exc}")
            return
        flash = [d for d in all_drives if d.is_flash and not d.is_loop]
        others = [d for d in all_drives
                  if d not in flash and not d.is_loop and d.size_bytes]
        self.drives = flash + others
        labels = []
        for d in self.drives:
            tags = []
            if d.is_flash:
                tags.append("USB/SD")
            else:
                tags.append("internal")
            if d.readonly:
                tags.append("READ-ONLY")
            if d.mounted:
                tags.append("mounted")
            labels.append(f"{d.path}  —  {human(d.size_bytes)}  "
                          f"[{', '.join(tags)}]  {d.fstype or '?'}"
                          + (f"  {d.label}" if d.label else ""))
        self.combo["values"] = labels
        if labels and not self.device.get():
            self.combo.current(0)
            self.device.set(labels[0])
        self.status.set(f"{len(flash)} removable device(s) found. "
                        + ("Select one and press Analyse."
                           if labels else "No drives detected."))

    def browse_file(self) -> None:
        p = filedialog.askopenfilename(
            title="Open a disk image or device",
            filetypes=[("Disk images", "*.img *.dd *.iso *.bin"),
                       ("All files", "*.*")])
        if not p:
            return
        vals = list(self.combo["values"])
        label = f"{p}  —  (image file)"
        vals.append(label)
        self.combo["values"] = vals
        self.combo.set(label)
        self.device.set(label)

    def _selected_path(self) -> str:
        raw = self.device.get()
        if not raw:
            return ""
        return raw.split("  —  ")[0].strip()

    def analyse(self) -> None:
        path = self._selected_path()
        if not path:
            messagebox.showinfo("FlashPhysician", "Pick a drive first.")
            return
        if not os.path.exists(path):
            messagebox.showerror("FlashPhysician", f"{path} is not accessible.")
            return
        self._busy(True, f"analysing {path} ...")
        self._write(self.txt, "Reading partition table and filesystem metadata...",
                    "dim")

        def work():
            return path, quick_diagnose(path)

        self._worker(work)

    def _apply_report(self, path: str, rep) -> None:
        self.report = rep
        self.f_health.configure(text=f"{rep.health}/100")
        self.f_health.configure(foreground=(
            OK if rep.health >= 75 else WARN if rep.health >= 45 else ERR))
        self.f_grade.configure(text=rep.grade)
        self.f_table.configure(text=rep.table.value.upper())
        self.f_fs.configure(text=(rep.fs.fs.value.upper() if rep.fs else "?"))
        self.f_verdict.configure(text=rep.verdict)

        text = self.txt
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("end", f"{path}\n", "head")
        text.insert("end", f"{human(rep.size_bytes)}  ·  scanned "
                           f"{rep.duration_s * 1000:.0f} ms\n\n", "dim")
        for f in rep.findings:
            icon = {"ok": "  OK  ", "info": " INFO ", "warn": " WARN ",
                    "error": " FAIL ", "fatal": " DEAD "}[f.severity.value]
            text.insert("end", f"[{icon}] ", f.severity.value)
            text.insert("end", f"{f.title}\n")
            if f.detail:
                text.insert("end", f"          {f.detail}\n", "dim")
        text.configure(state="disabled")

        self._write(self.advice, self._advice_for(rep))
        self._write(self.raw, format_report(rep, color=False))
        self.status.set(f"{rep.health}/100 — {rep.verdict}")
        for b in self.actions:
            b.state(["!disabled"])

    def _advice_for(self, rep) -> str:
        codes = {f.code for f in rep.findings}
        lines: list[str] = []
        add = lines.append

        if rep.raw.get("orphan_fs_offset") is not None:
            off = rep.raw["orphan_fs_offset"]
            add("YOUR FILES ARE PROBABLY FINE.")
            add("")
            add(f"The partition table is missing, but a complete filesystem was")
            add(f"found at {human(off)}. Nothing has overwritten it. Press")
            add("Repair: it writes the table back so the drive mounts again.")
            add("")
            add("Do NOT format the drive and do not let Windows 'initialise' it.")
            add("Both would write over the data you are trying to keep.")
            return "\n".join(lines)

        if "FAT_DIRTY" in codes:
            add("The drive was not safely removed.")
            add("")
            add("A write was interrupted, so the filesystem was never closed")
            add("properly. This is usually harmless: press Repair to clear the")
            add("flag, or Check filesystem for a deeper check. Copy anything")
            add("important off first.")
            add("")
        if "FAT_TYPE_MISMATCH" in codes:
            add("The filesystem type and its own layout disagree.")
            add("")
            add("This is why Windows shows the drive as RAW. Press Repair -- it")
            add("corrects the label to match the real layout. The data region is")
            add("not touched.")
            add("")
        if "FAT_BADCLUSTER" in codes:
            add("Some storage cells are already marked bad.")
            add("")
            add("The controller has remapped them, so they are not usable. If")
            add("this number grows between checks, the flash is wearing out:")
            add("copy your data off and replace the drive.")
            add("")
        if "FAT_COPIES_DIFF" in codes:
            add("The two copies of the allocation table disagree.")
            add("")
            add("Windows uses the first copy, which is the one this report read")
            add("as authoritative. Run Check filesystem to reconcile them.")
            add("")
        if "GPT_PROTECTIVE_ONLY" in codes or "GPT_BACKUP_RECOVERABLE" in codes:
            add("The GPT partition table is damaged but its backup survived.")
            add("")
            add("Press Repair: the backup at the end of the drive is used to")
            add("rebuild the primary table. No data is moved.")
            add("")
        if rep.fs and rep.fs.fs == FsType.RAW and not rep.raw.get("orphan_fs_offset"):
            add("No filesystem was found anywhere on the drive.")
            add("")
            add("Either the drive was formatted or everything was overwritten.")
            add("Try Recover files -- it scans for file signatures, which can")
            add("find photos and documents even with no filesystem left.")
            add("")
        if "DEAD_ZERO" in codes:
            add("The drive answers, but reports zero capacity.")
            add("")
            add("That is a hardware or controller failure, not a filesystem one.")
            add("Software cannot repair it. If the data matters, stop using it")
            add("and take it to a professional recovery service.")
            return "\n".join(lines)

        if not lines:
            if rep.health >= 85:
                add("This drive looks healthy.")
                add("")
                add("No filesystem or partition problems were found. If files are")
                add("still disappearing, run Surface scan to check the storage")
                add("cells, and Capacity test to check the drive is honest about")
                add("its size -- fake capacity is a common cause of vanishing")
                add("files on cheap drives.")
            else:
                add(rep.verdict)
                add("")
                add("Read the Report tab for the specific findings. Back up")
                add("anything important before attempting repairs.")
        return "\n".join(lines)

    # -- destructive -----------------------------------------------------
    def _require_report(self) -> bool:
        if not self.report:
            messagebox.showinfo("FlashPhysician", "Press Analyse first.")
            return False
        return True

    def _confirm(self, title: str, body: str) -> bool:
        return messagebox.askyesno(title, body, icon="warning")

    def do_repair(self) -> None:
        if not self._require_report():
            return
        path = self._selected_path()
        rep = self.report
        codes = {f.code for f in rep.findings}

        plans = []
        if "MBR_SIG" in codes or "TABLE_MISSING" in codes:
            plans.append(("Restore the MBR boot signature",
                          plan_restore_mbr_signature(path)))
        if "FAT_BOOTBACKUP_MISMATCH" in codes:
            plans.append(("Restore the FAT32 boot sector from its backup",
                          plan_restore_fat32_boot_backup(path)))
        if "FAT_TYPE_MISMATCH" in codes:
            plans.append(("Correct the filesystem type label",
                          plan_fix_fat_type_label(path)))
        if "FAT_DIRTY" in codes:
            plans.append(("Clear the dirty flag", plan_clear_fat_dirty(path)))
        if "GPT_PROTECTIVE_ONLY" in codes or "GPT_HEADER" in codes:
            plans.append(("Rebuild the GPT from its backup",
                          plan_restore_gpt_from_backup(path)))
        if rep.raw.get("orphan_fs_offset") is not None:
            fst = rep.raw.get("orphan_fs_type", "fat32")
            try:
                ftype = FsType(fst)
            except ValueError:
                ftype = FsType.FAT32
            plans.append(("Rebuild the partition table (filesystem found at "
                          f"{human(rep.raw['orphan_fs_offset'])})",
                          plan_rebuild_mbr_from_fs(path, rep.raw["orphan_fs_offset"],
                                                   ftype)))

        # keep the widest repair per target byte range, exactly as the CLI does
        usable = [(l, p) for l, p in plans if p.ok]
        supersedes = {"Restore the MBR boot signature": "Rebuild the partition table"}
        final = []
        for label, plan in usable:
            offs = {op.offset for op in plan.ops}
            beaten = False
            for other_label, other_plan in usable:
                if other_label == label:
                    continue
                want = supersedes.get(label)
                if want and want in other_label \
                        and offs & {op.offset for op in other_plan.ops}:
                    beaten = True
                    break
            if not beaten:
                final.append((label, plan))

        if not final:
            messagebox.showinfo(
                "FlashPhysician",
                "No automatically repairable problem was identified.\n\n"
                "Try Surface scan to check the hardware, or Check filesystem.")
            return

        detail = "\n\n".join(
            "• " + l + "\n   " + "\n   ".join(o.describe() for o in p.ops)
            for l, p in final)
        if not self._confirm(
                "Apply repair?",
                f"FlashPhysician will write to:\n{path}\n\n{detail}\n\n"
                "A copy of every sector that gets overwritten is saved first, "
                "so this can be undone.\n\nProceed?"):
            return

        self._busy(True, "repairing ...")

        def work():
            results = []
            for label, plan in final:
                results.append((label, plan.apply()))
            return ("repair", results, quick_diagnose(path))

        self._worker(work)

    def do_scan(self) -> None:
        if not self._require_report():
            return
        path = self._selected_path()
        if not self._confirm(
                "Surface scan",
                f"FlashPhysician will write test patterns to {path} and read them "
                "back to verify the storage cells hold data.\n\n"
                "This DESTROYS the contents of the tested area.\n\n"
                "Test the whole drive?"):
            return
        self._busy(True, "surface scan ...")
        self._worker(lambda: ("scan", surface_test(
            path, 0, None, progress=self._progress())))

    def do_capacity(self) -> None:
        if not self._require_report():
            return
        path = self._selected_path()
        if not self._confirm(
                "Capacity test",
                f"FlashPhysician will write markers across {path} to check whether "
                "the advertised size is real.\n\n"
                "This DESTROYS the contents of the drive.\n\nProceed?"):
            return
        self._busy(True, "capacity test ...")
        self._worker(lambda: ("capacity", capacity_test(
            path, progress=self._progress())))

    def do_fsck(self) -> None:
        if not self._require_report():
            return
        path = self._selected_path()
        repair = self._confirm(
            "Filesystem check",
            f"Run the operating system's filesystem checker on {path}?\n\n"
            "Yes  = FIX any errors found\n"
            "No   = check only, change nothing")
        self._busy(True, "filesystem check ...")
        be = get_backend()

        def work():
            if getattr(be, "mount_points", None):
                be.unmount(path)
            return ("fsck", be.fsck(path, fix=repair))

        self._worker(work)

    def do_recover(self) -> None:
        if not self._require_report():
            return
        path = self._selected_path()
        dest = filedialog.askdirectory(
            title="Where should recovered files go?")
        if not dest:
            return
        if os.path.abspath(dest).startswith(os.path.abspath(path)):
            messagebox.showerror(
                "FlashPhysician",
                "The destination is inside the drive being recovered.\n\n"
                "Pick a different folder -- writing to the damaged drive can "
                "overwrite the very files you are trying to save.")
            return
        carve = messagebox.askyesno(
            "Recover files",
            f"Recover files from {path} into:\n{dest}\n\n"
            "Also carve by signature? That finds pictures and documents even "
            "when there is no filesystem left, but recovered files lose their "
            "names and folders.")
        self._busy(True, "recovering ...")

        def work():
            rep = recover_files(path, dest, progress=self._progress())
            carved = None
            if carve:
                carved = carve_files(path, os.path.join(dest, "_carved"),
                                     progress=self._progress())
            return ("recover", rep, carved)

        self._worker(work)

    def do_image(self) -> None:
        if not self._require_report():
            return
        path = self._selected_path()
        out = filedialog.asksaveasfilename(
            title="Save the image as", defaultextension=".img",
            filetypes=[("Disk image", "*.img"), ("All files", "*.*")])
        if not out:
            return
        self._busy(True, "imaging ...")
        from .core.imaging import Source, copy_range

        def work():
            with Source(path) as src:
                size = src.size
            return ("image", copy_range(Source(path), out, 0, size,
                                        progress=self._progress()))

        self._worker(work)

    def do_eject(self) -> None:
        if not self._require_report():
            return
        path = self._selected_path()
        be = get_backend()
        self._busy(True, "ejecting ...")
        self._worker(lambda: ("eject", be.eject(path)))

    # -- report rendering for non-diagnose results ------------------------
    def _finish(self, result, msg: str) -> None:      # noqa: F811
        self._busy(False, msg or "done")
        self.prog.configure(value=100)
        if result is None:
            return

        # diagnose returns (path, report)
        if isinstance(result, tuple) and len(result) == 2 \
                and hasattr(result[1], "findings"):
            self._apply_report(result[0], result[1])
            return

        kind = result[0] if isinstance(result, tuple) else ""
        if kind == "repair":
            self._show_repair(result[1], result[2])
        elif kind == "scan":
            self._show_scan(result[1])
        elif kind == "capacity":
            self._show_capacity(result[1])
        elif kind == "fsck":
            self._show_command(result[1], "Filesystem check")
        elif kind == "eject":
            self._show_command(result[1], "Safe removal")
        elif kind == "recover":
            self._show_recover(result[1], result[2] if len(result) > 2 else None)
        elif kind == "image":
            self._show_image(result[1])

    def _show_repair(self, results, after) -> None:
        lines = []
        for label, res in results:
            state = "ok  " if res["ok"] else "FAIL"
            lines.append(f"[{state}] {label}")
            if res.get("backups"):
                lines.append(f"        backup: {res['backups'][0]}")
            for e in res.get("errors", []):
                lines.append(f"        error: {e}")
        lines.append("")
        lines.append(f"Health after repair: {after.health}/100  ({after.verdict})")
        ok = all(r["ok"] for _, r in results)
        self._write(self.txt, "\n".join(lines), "ok" if ok else "warn")
        self._apply_report(self._selected_path(), after)
        self.nb.select(0)
        self.status.set(f"repair finished -- health {after.health}/100")

    def _show_scan(self, rep) -> None:
        lines = [rep.verdict(), "",
                 f"Tested  : {human(rep.tested_bytes)}",
                 f"Write   : {rep.avg_write_mbps:.1f} MB/s",
                 f"Read    : {rep.avg_read_mbps:.1f} MB/s",
                 f"Duration: {rep.duration_s:.1f} s", ""]
        if rep.faults:
            lines.append(f"{len(rep.faults)} faulty sector(s):")
            lines += [f"  - {f.describe()}" for f in rep.faults[:40]]
        self._write(self.txt, "\n".join(lines),
                    "ok" if rep.clean else "error")
        self._write(self.advice, self._advice_scan(rep))
        self._write(self.raw, json.dumps(
            {"tested": rep.tested_bytes, "faults": len(rep.faults),
             "write_errors": rep.write_errors, "read_errors": rep.read_errors,
             "verify_errors": rep.verify_errors, "aborted": rep.aborted}, indent=2))
        self.status.set(rep.verdict())

    def _advice_scan(self, rep) -> str:
        if rep.clean:
            return ("Every sector held its data.\n\n"
                    "The storage cells are fine, so any problem you are seeing "
                    "is a filesystem or partition issue, not failing hardware.")
        if rep.verify_errors and not rep.write_errors:
            return ("Sectors accepted data and then failed to keep it.\n\n"
                    "That is a failing flash chip: the cells no longer hold a "
                    "charge. Do not trust this drive with anything important.\n\n"
                    "Back up whatever still reads, then replace it.")
        return ("The drive could not be written reliably.\n\n"
                "Either the medium is worn out or write-protected. Check for a "
                "physical lock switch, then try again. If it persists, the "
                "drive is failing.")

    def _show_capacity(self, rep) -> None:
        lines = [rep.verdict(), "",
                 f"Advertised: {human(rep.advertised_bytes)}",
                 f"Real      : {human(rep.genuine_bytes)}"]
        if rep.is_fake:
            lines.append(f"Fake      : {rep.fake_fraction * 100:.0f}%")
        lines.append("")
        lines += [f"- {n}" for n in rep.notes]
        self._write(self.txt, "\n".join(lines),
                    "error" if rep.is_fake else "ok")
        if rep.is_fake:
            self._write(self.advice,
                        "This drive lies about its size.\n\n"
                        "It claims more storage than it has. Once you write past "
                        "the real end, the new data silently overwrites the "
                        "start of the drive -- which is why files disappear and "
                        "folders come back corrupted.\n\n"
                        "No repair is possible: the capacity does not exist. "
                        "Copy off whatever reads correctly and buy a "
                        "replacement from a seller you trust.")
        else:
            self._write(self.advice,
                        "The advertised capacity is genuine.\n\n"
                        "Every marker written came back from its own address, so "
                        "the drive really can hold what it says.")
        self.status.set(rep.verdict())

    def _show_command(self, res, title: str) -> None:
        head = f"{title}\n\n$ {' '.join(res.argv)}\n"
        if res.missing:
            body = res.stderr
            self._write(self.txt, head + "\n" + body, "warn")
            self._write(self.advice, body)
            self.status.set("not available")
            return
        body = res.combined or "(no output)"
        self._write(self.txt, head + "\n" + body,
                    "ok" if res.returncode == 0 else "warn")
        self._write(self.advice,
                    "The checker exited with code "
                    f"{res.returncode}.\n\n"
                    + ("No errors were reported." if res.returncode == 0 else
                       "Errors were found or the check could not complete. "
                       "Run it again with repair enabled to fix what it found."))
        self.status.set(f"exit code {res.returncode}")

    def _show_recover(self, rep, carved) -> None:
        lines = [f"{len(rep.recovered)} file(s) recovered "
                 f"({human(rep.total_bytes)})", f"Destination: {rep.dest}", ""]
        lines += [f"  {os.path.basename(n)}  {human(sz)}"
                  for n, sz in rep.recovered[:60]]
        if rep.failed:
            lines.append("")
            lines.append("Could not read:")
            lines += [f"  {n}: {w}" for n, w in rep.failed[:20]]
        if carved is not None:
            lines.append("")
            lines.append(f"Carved by signature: {len(carved.carved)} file(s) "
                         f"in {rep.dest}/_carved")
        self._write(self.txt, "\n".join(lines),
                    "ok" if rep.recovered else "warn")
        adv = []
        if rep.recovered:
            adv += ["Files recovered.", "",
                    "Check them before doing anything else with the drive. "
                    "Recovered files keep their contents but a deleted file's "
                    "name loses its first character -- that is how the "
                    "filesystem works, not a bug.", ""]
        if carved is not None and carved.carved:
            adv += ["Carved files have no names or folders.",
                    "They were found by their contents, so check each one.", ""]
        if rep.failed:
            adv += ["Some files could not be read at all.",
                    "Those clusters are damaged. If they matter, stop using the "
                    "drive now and consider professional recovery.", ""]
        self._write(self.advice, "\n".join(adv) or "Nothing was recovered.")
        self.status.set(f"{len(rep.recovered)} recovered")

    def _show_image(self, res) -> None:
        lines = [f"Image written to {res['path']}",
                 f"{human(res['bytes_written'])} copied"]
        if res["bad_count"]:
            lines += ["", f"{res['bad_count']} unreadable block(s) were "
                          "zero-filled:"]
            lines += [f"  {human(o)}" for o in res["bad_offsets"][:30]]
        self._write(self.txt, "\n".join(lines),
                    "warn" if res["bad_count"] else "ok")
        self._write(self.advice,
                    "An image is now saved as a file.\n\n"
                    + ("It contains everything that could be read; the "
                       "unreadable regions are zeroed. Work on the image from "
                       "now on -- that way the failing drive is only read "
                       "once." if res["bad_count"] else
                       "It is a complete, exact copy. Recovered files can be "
                       "taken from the image instead of the drive."))
        self.status.set("image written")


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())