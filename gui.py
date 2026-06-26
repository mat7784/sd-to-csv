"""
gui.py — tkinter GUI launcher for bin_to_csv.

Three input modes:
  1. Single .bin file
  2. Folder that directly contains .bin files
  3. Folder whose immediate sub-folders each contain .bin files (one level deep)

An optional output-folder picker overrides the default (same folder as input).
Conversion runs on a background thread so the GUI stays responsive.
All bin_to_csv stderr/stdout output is captured and shown in a scrollable log.
"""

import contextlib
import io
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import bin_to_csv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _discover_bin_files_one_level_deep(parent: str) -> list[str]:
    """Return all .bin files that are exactly one directory below *parent*.

    Layout expected:
        parent/run1/data_fast.bin
        parent/run2/data_slow.bin
    Files directly inside *parent* are NOT included (use mode 2 for that).
    """
    found: list[str] = []
    try:
        entries = sorted(os.scandir(parent), key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            try:
                children = sorted(os.listdir(entry.path))
            except OSError:
                continue
            for name in children:
                if name.lower().endswith('.bin'):
                    found.append(os.path.join(entry.path, name))
    return found


def _run_conversion(argv: list[str], on_done):
    """Run bin_to_csv.main(argv) on a background thread, capture output."""
    buf = io.StringIO()
    rc = 1
    try:
        with contextlib.redirect_stderr(buf):
            with contextlib.redirect_stdout(buf):
                rc = bin_to_csv.main(argv)
    except SystemExit as exc:
        # argparse / sys.exit may raise SystemExit
        rc = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001
        buf.write(f"\nUnexpected error: {exc}\n")
    on_done(rc, buf.getvalue())


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SD to CSV Converter")
        self.resizable(True, True)
        self.minsize(540, 420)

        # --- State ---
        self._input_paths: list[str] = []   # positional args for bin_to_csv
        self._outdir: str = ""

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        pad = dict(padx=10, pady=4)

        # ---- Input section ----
        input_frame = ttk.LabelFrame(self, text="Input")
        input_frame.pack(fill="x", **pad)

        ttk.Button(
            input_frame, text="Select .bin file",
            command=self._pick_bin_file,
        ).grid(row=0, column=0, sticky="ew", padx=6, pady=4)

        ttk.Button(
            input_frame, text="Select folder (contains .bin files)",
            command=self._pick_flat_folder,
        ).grid(row=1, column=0, sticky="ew", padx=6, pady=4)

        ttk.Button(
            input_frame, text="Select folder (contains sub-folders with .bin files)",
            command=self._pick_nested_folder,
        ).grid(row=2, column=0, sticky="ew", padx=6, pady=4)

        input_frame.columnconfigure(0, weight=1)

        # Selected-input label
        self._input_var = tk.StringVar(value="No input selected")
        ttk.Label(self, textvariable=self._input_var, foreground="grey",
                  wraplength=520, justify="left").pack(fill="x", padx=10)

        # ---- Output section ----
        out_frame = ttk.LabelFrame(self, text="Output folder (optional)")
        out_frame.pack(fill="x", **pad)

        self._outdir_var = tk.StringVar(value="Same folder as each input file")
        ttk.Label(out_frame, textvariable=self._outdir_var, foreground="grey",
                  wraplength=420, justify="left").grid(row=0, column=0,
                  sticky="ew", padx=6, pady=4)
        ttk.Button(
            out_frame, text="Browse...", command=self._pick_outdir,
        ).grid(row=0, column=1, padx=6, pady=4)
        ttk.Button(
            out_frame, text="Clear", command=self._clear_outdir,
        ).grid(row=0, column=2, padx=4, pady=4)
        out_frame.columnconfigure(0, weight=1)

        # ---- Convert button ----
        self._convert_btn = ttk.Button(
            self, text="Convert", command=self._start_conversion,
        )
        self._convert_btn.pack(pady=6)

        # ---- Log area ----
        log_frame = ttk.LabelFrame(self, text="Output log")
        log_frame.pack(fill="both", expand=True, **pad)

        self._log = scrolledtext.ScrolledText(
            log_frame, state="disabled", wrap="word",
            font=("Courier New", 9),
        )
        self._log.pack(fill="both", expand=True, padx=4, pady=4)

    # ------------------------------------------------------------------
    # Pickers
    # ------------------------------------------------------------------

    def _pick_bin_file(self):
        path = filedialog.askopenfilename(
            title="Select a .bin file",
            filetypes=[("Binary telemetry", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        self._input_paths = [path]
        self._input_var.set(f"File: {path}")

    def _pick_flat_folder(self):
        folder = filedialog.askdirectory(title="Select folder containing .bin files")
        if not folder:
            return
        # Pass the folder itself; bin_to_csv.gather_inputs expands it.
        self._input_paths = [folder]
        self._input_var.set(f"Folder: {folder}")

    def _pick_nested_folder(self):
        folder = filedialog.askdirectory(
            title="Select folder whose sub-folders contain .bin files"
        )
        if not folder:
            return
        files = _discover_bin_files_one_level_deep(folder)
        if not files:
            messagebox.showwarning(
                "No files found",
                f"No .bin files found one level deep inside:\n{folder}",
            )
            return
        self._input_paths = files
        summary = f"Folder of folders: {folder}\n({len(files)} .bin file(s) found)"
        self._input_var.set(summary)

    def _pick_outdir(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if not folder:
            return
        self._outdir = folder
        self._outdir_var.set(folder)

    def _clear_outdir(self):
        self._outdir = ""
        self._outdir_var.set("Same folder as each input file")

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def _start_conversion(self):
        if not self._input_paths:
            messagebox.showwarning("No input", "Please select a file or folder first.")
            return

        argv = list(self._input_paths)
        if self._outdir:
            argv += ["-o", self._outdir]

        self._convert_btn.configure(state="disabled", text="Converting...")
        self._log_clear()
        self._log_append(f"Running: bin_to_csv {' '.join(argv)}\n\n")

        def on_done(rc, text):
            # Called from background thread — use after() to touch the GUI safely.
            self.after(0, lambda: self._conversion_done(rc, text))

        t = threading.Thread(target=_run_conversion, args=(argv, on_done), daemon=True)
        t.start()

    def _conversion_done(self, rc: int, text: str):
        self._log_append(text)
        status = "Done (exit code 0)." if rc == 0 else f"Finished with errors (exit code {rc})."
        self._log_append(f"\n{status}\n")
        self._convert_btn.configure(state="normal", text="Convert")

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    def _log_clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _log_append(self, text: str):
        self._log.configure(state="normal")
        self._log.insert("end", text)
        self._log.see("end")
        self._log.configure(state="disabled")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = App()
    app.mainloop()
