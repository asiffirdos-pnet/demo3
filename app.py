"""Tkinter desktop UI for the YouTube downloader.

Run with:  python3 app.py
"""

import os
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from downloader import HAS_FFMPEG, QUALITY_FORMATS, Downloader, ensure_safe_url
from url_utils import InvalidURLError

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
POLL_MS = 100


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Downloader")
        self.geometry("620x300")
        self.minsize(520, 280)

        # Worker-thread callbacks push events here; the Tk thread drains it.
        self._events = queue.Queue()
        self.downloader = Downloader(
            on_progress=lambda pct, text: self._events.put(("progress", pct, text)),
            on_status=lambda text: self._events.put(("status", text)),
            on_done=lambda ok, msg: self._events.put(("done", ok, msg)),
        )

        self.url_var = tk.StringVar()
        self.dir_var = tk.StringVar(value=DEFAULT_DIR if os.path.isdir(DEFAULT_DIR) else os.getcwd())
        self.quality_var = tk.StringVar(value="Best available")
        self.status_var = tk.StringVar(value="Paste a YouTube link and press Download.")
        self.detail_var = tk.StringVar(value="")

        self._build_ui()
        self.after(POLL_MS, self._drain_events)

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Video link:").grid(row=0, column=0, sticky="w", **pad)
        url_entry = ttk.Entry(frm, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        url_entry.bind("<Return>", lambda _e: self._on_download())
        url_entry.focus_set()

        ttk.Label(frm, text="Save to:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.dir_var, state="readonly").grid(
            row=1, column=1, sticky="ew", **pad)
        self.browse_btn = ttk.Button(frm, text="Browse…", command=self._choose_dir)
        self.browse_btn.grid(row=1, column=2, sticky="e", **pad)

        ttk.Label(frm, text="Quality:").grid(row=2, column=0, sticky="w", **pad)
        self.quality_box = ttk.Combobox(
            frm, textvariable=self.quality_var, values=list(QUALITY_FORMATS),
            state="readonly", width=18)
        self.quality_box.grid(row=2, column=1, sticky="w", **pad)

        self.progress = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.progress.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)

        ttk.Label(frm, textvariable=self.status_var).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=12)
        ttk.Label(frm, textvariable=self.detail_var, foreground="gray").grid(
            row=5, column=0, columnspan=3, sticky="w", padx=12)

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(12, 0), padx=12)
        self.download_btn = ttk.Button(btns, text="Download", command=self._on_download)
        self.download_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        if not HAS_FFMPEG:
            self.detail_var.set("Note: ffmpeg not found — using single-file formats (max ~720p, no MP3).")

    # --------------------------------------------------------------- actions
    def _choose_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get(), title="Choose download folder")
        if chosen:
            self.dir_var.set(chosen)

    def _on_download(self):
        if self.downloader.busy:
            return

        # 1. Check the user's link and rebuild it as an encoded URL first.
        #    The raw text is never sent anywhere else.
        try:
            safe_url = ensure_safe_url(self.url_var.get())
        except InvalidURLError as exc:
            messagebox.showerror("Invalid link", str(exc))  # fixed messages only
            return

        out_dir = self.dir_var.get()
        if not os.path.isdir(out_dir):
            messagebox.showerror("Folder missing", "Please choose an existing download folder.")
            return

        # 2. Only the validated, canonical URL is passed to the downloader.
        try:
            self.downloader.start(safe_url, out_dir, self.quality_var.get())
        except (InvalidURLError, ValueError, RuntimeError) as exc:
            messagebox.showerror("Cannot start download", str(exc))
            return

        self.url_var.set(safe_url)  # show the user the cleaned-up link
        self.progress["value"] = 0
        self.status_var.set("Starting…")
        self.detail_var.set("")
        self._set_busy(True)

    def _on_cancel(self):
        self.downloader.cancel()
        self.status_var.set("Cancelling…")

    def _set_busy(self, busy: bool):
        self.download_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.browse_btn.config(state="disabled" if busy else "normal")
        self.quality_box.config(state="disabled" if busy else "readonly")

    # ------------------------------------------------- worker -> UI bridging
    def _drain_events(self):
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, pct, text = event
                    self.progress["value"] = pct
                    self.status_var.set(f"Downloading… {pct:.1f}%")
                    self.detail_var.set(text)
                elif kind == "status":
                    self.status_var.set(event[1])
                elif kind == "done":
                    _, ok, msg = event
                    self._set_busy(False)
                    self.status_var.set(msg)
                    if ok:
                        self.progress["value"] = 100
                        self.detail_var.set(f"Saved in: {self.dir_var.get()}")
                    else:
                        self.progress["value"] = 0
        except queue.Empty:
            pass
        self.after(POLL_MS, self._drain_events)


if __name__ == "__main__":
    App().mainloop()
