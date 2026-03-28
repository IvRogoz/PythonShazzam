from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk
from mutagen.mp3 import MP3
import requests

from music import TrackInfo, Shazam, gather_files, make_target_filename, safe_rename, shazam_identify_file, update_tags
from system_audio import AudioCaptureError, LiveAudioListener, LiveTrackResult, SOURCE_LABELS


@dataclass
class FileEntry:
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    has_cover: bool = False
    has_embedded: bool = False
    cover_data: bytes | None = None
    status: str = "Ready"
    pending_track: TrackInfo | None = None
    pending_name: str | None = None
    pending_cover_data: bytes | None = None


class PythonShazzamGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PythonShazzam")
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        start_h = int(screen_h * (2 / 3))
        start_w = min(1100, int(screen_w * 0.9))
        self.geometry(f"{start_w}x{start_h}")
        self.minsize(900, 620)

        self.folder_var = tk.StringVar(value=str(Path.home() / "Music"))
        self.timeout_var = tk.StringVar(value="40")
        self.recurse_var = tk.BooleanVar(value=True)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.auto_apply_var = tk.BooleanVar(value=True)
        self.dark_mode_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Choose a folder to scan")
        self.count_var = tk.StringVar(value="0 files")
        self.progress_var = tk.DoubleVar(value=0)

        self.scan_thread: threading.Thread | None = None
        self.run_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self.entries: list[FileEntry] = []
        self.row_by_path: dict[str, dict[str, object]] = {}
        self.row_photos: list[ImageTk.PhotoImage] = []
        self.menu_path: str | None = None
        self.selected_row_path: str | None = None
        self.colors: dict[str, str] = {}
        self.listen_modal: tk.Toplevel | None = None
        self.listen_source_var = tk.StringVar(value="system")
        self.listen_auto_preview_var = tk.BooleanVar(value=True)
        self.listen_canvas: tk.Canvas | None = None
        self.listen_caption_id: int | None = None
        self.listen_hint_var = tk.StringVar(value="Choose a source and press Listen.")
        self.listen_options_frame: tk.Frame | None = None
        self.listen_source_menu: ttk.Combobox | None = None
        self.listen_results_var = tk.StringVar(value=())
        self.listen_results_list: tk.Listbox | None = None
        self.listen_thread: threading.Thread | None = None
        self.listen_stop_event = threading.Event()
        self.listen_results: list[str] = []
        self.listen_wave_canvas: tk.Canvas | None = None
        self.listen_wave_levels: list[float] = [0.0] * 160
        self.listen_found_title_var = tk.StringVar(value="No track found yet")
        self.listen_found_meta_var = tk.StringVar(value="Start listening to show the latest match.")
        self.listen_found_link_var = tk.StringVar(value="")
        self.listen_found_cover_label: tk.Label | None = None
        self.listen_found_link_label: tk.Label | None = None
        self.listen_found_cover_photo: ImageTk.PhotoImage | None = None
        self.listen_found_url: str | None = None

        self.row_menu = tk.Menu(self, tearoff=0)
        self.row_menu.add_command(label="Open File", command=self._open_item_file)
        self.row_menu.add_command(label="Open Folder", command=self._open_item_folder)
        self.row_menu.add_separator()
        self.row_menu.add_command(label="Rescan Item", command=self._rescan_item)

        self._configure_style()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_events)

    def _configure_style(self) -> None:
        self.style = ttk.Style(self)
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        self._apply_theme()

    def _theme_colors(self) -> dict[str, str]:
        if self.dark_mode_var.get():
            return {
                "root_bg": "#0B1220",
                "card_bg": "#111A2D",
                "title_fg": "#E2E8F0",
                "hint_fg": "#94A3B8",
                "text_fg": "#CBD5E1",
                "status_fg": "#93C5FD",
                "canvas_bg": "#111A2D",
            }
        return {
            "root_bg": "#F3EFE7",
            "card_bg": "#FFFDF8",
            "title_fg": "#1F2937",
            "hint_fg": "#4B5563",
            "text_fg": "#0F172A",
            "status_fg": "#334155",
            "canvas_bg": "#FFFDF8",
        }

    def _apply_theme(self) -> None:
        self.colors = self._theme_colors()
        is_dark = self.dark_mode_var.get()
        btn_bg = "#1E293B" if is_dark else "#E5E7EB"
        btn_active = "#334155" if is_dark else "#D1D5DB"
        accent_bg = "#0F766E" if is_dark else "#0D9488"
        accent_active = "#115E59" if is_dark else "#0F766E"
        entry_border = "#334155" if is_dark else "#CBD5E1"
        trough_bg = "#0F172A" if is_dark else "#E2E8F0"

        self.configure(bg=self.colors["root_bg"])
        self.style.configure("Root.TFrame", background=self.colors["root_bg"])
        self.style.configure("Card.TFrame", background=self.colors["card_bg"])
        self.style.configure("Title.TLabel", background=self.colors["root_bg"], foreground=self.colors["title_fg"], font=("Segoe UI Semibold", 18))
        self.style.configure("Hint.TLabel", background=self.colors["root_bg"], foreground=self.colors["hint_fg"], font=("Segoe UI", 10))
        self.style.configure("CardLabel.TLabel", background=self.colors["card_bg"], foreground=self.colors["text_fg"], font=("Segoe UI", 10))
        self.style.configure("Status.TLabel", background=self.colors["root_bg"], foreground=self.colors["status_fg"], font=("Segoe UI", 10))
        self.style.configure("TCheckbutton", background=self.colors["card_bg"], foreground=self.colors["text_fg"], indicatorcolor=self.colors["card_bg"])
        self.style.map("TCheckbutton", background=[("active", self.colors["card_bg"])], foreground=[("active", self.colors["text_fg"])])

        self.style.configure(
            "TEntry",
            fieldbackground=self.colors["card_bg"],
            foreground=self.colors["text_fg"],
            insertcolor=self.colors["text_fg"],
            bordercolor=entry_border,
            lightcolor=entry_border,
            darkcolor=entry_border,
            padding=4,
            relief="flat",
        )
        self.style.map("TEntry", bordercolor=[("focus", "#14B8A6" if is_dark else "#0D9488")], lightcolor=[("focus", "#14B8A6" if is_dark else "#0D9488")], darkcolor=[("focus", "#14B8A6" if is_dark else "#0D9488")])

        self.style.configure("TButton", background=btn_bg, foreground=self.colors["text_fg"], bordercolor=entry_border, lightcolor=entry_border, darkcolor=entry_border, focusthickness=0, padding=(10, 6))
        self.style.map("TButton", background=[("active", btn_active), ("pressed", btn_active)], foreground=[("disabled", "#64748B")])

        self.style.configure("Accent.TButton", background=accent_bg, foreground="#F8FAFC", bordercolor=accent_bg, lightcolor=accent_bg, darkcolor=accent_bg, focusthickness=0, padding=(10, 6))
        self.style.map("Accent.TButton", background=[("active", accent_active), ("pressed", accent_active)], foreground=[("disabled", "#94A3B8")])

        self.style.configure("Horizontal.TProgressbar", troughcolor=trough_bg, background=accent_bg, bordercolor=entry_border, lightcolor=accent_bg, darkcolor=accent_bg)
        self.style.configure("Vertical.TScrollbar", background=btn_bg, troughcolor=trough_bg, bordercolor=entry_border, arrowcolor=self.colors["text_fg"], lightcolor=entry_border, darkcolor=entry_border)
        self.style.map("Vertical.TScrollbar", background=[("active", btn_active), ("pressed", btn_active)])

        if hasattr(self, "list_canvas"):
            self.list_canvas.configure(bg=self.colors["canvas_bg"])
        self.row_menu.configure(bg=self.colors["card_bg"], fg=self.colors["text_fg"], activebackground=accent_bg, activeforeground="#FFFFFF")
        self._refresh_listen_modal_theme()

    def _on_theme_toggle(self) -> None:
        self._apply_theme()
        if self.entries:
            self._render_file_list(self.entries)

    def _build_layout(self) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=16)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root, style="Root.TFrame")
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text="PythonShazzam", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            top,
            text="Rows show: cover, filename, and embedded details. Updates happen live while processing.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        controls = ttk.Frame(root, style="Card.TFrame", padding=12)
        controls.pack(fill="x", pady=(0, 10))

        ttk.Label(controls, text="Music Folder", style="CardLabel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.folder_var).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Button(controls, text="Browse", command=self._choose_folder).grid(row=1, column=1, sticky="ew", pady=(4, 8))
        self.scan_btn = ttk.Button(controls, text="Scan", command=self._scan_files)
        self.scan_btn.grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=(4, 8))

        ttk.Checkbutton(controls, text="Recurse subfolders", variable=self.recurse_var).grid(row=2, column=0, sticky="w", pady=(0, 8))
        ttk.Checkbutton(controls, text="Dry run", variable=self.dry_run_var).grid(row=2, column=1, sticky="w", pady=(0, 8))
        ttk.Checkbutton(controls, text="Auto apply changes", variable=self.auto_apply_var).grid(row=2, column=2, sticky="w", pady=(0, 8))
        ttk.Checkbutton(controls, text="Dark mode", variable=self.dark_mode_var, command=self._on_theme_toggle).grid(row=2, column=3, sticky="w", pady=(0, 8))

        ttk.Label(controls, text="Timeout (seconds)", style="CardLabel.TLabel").grid(row=3, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.timeout_var, width=10).grid(row=4, column=0, sticky="w", pady=(4, 6))

        actions = ttk.Frame(controls, style="Card.TFrame")
        actions.grid(row=4, column=1, columnspan=2, sticky="e")
        self.run_btn = ttk.Button(actions, text="Start Processing", command=self._start_run)
        self.run_btn.pack(side="left", padx=(0, 8))
        self.listen_btn = ttk.Button(actions, text="Listen Mode", command=self._open_listen_modal)
        self.listen_btn.pack(side="left", padx=(0, 8))
        self.apply_btn = ttk.Button(actions, text="Apply Pending", command=self._start_apply, state="disabled")
        self.apply_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(actions, text="Stop", command=self._stop_run, state="disabled")
        self.stop_btn.pack(side="left")
        controls.columnconfigure(0, weight=1)

        list_card = ttk.Frame(root, style="Card.TFrame", padding=10)
        list_card.pack(fill="both", expand=True)
        ttk.Label(list_card, text="File List", style="CardLabel.TLabel").pack(anchor="w", pady=(0, 8))

        self.list_canvas = tk.Canvas(list_card, bg=self.colors["canvas_bg"], highlightthickness=0)
        self.list_scroll = ttk.Scrollbar(list_card, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=self.list_scroll.set)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        self.list_scroll.pack(side="right", fill="y")

        self.list_frame = ttk.Frame(self.list_canvas, style="Card.TFrame")
        self.list_window = self.list_canvas.create_window((0, 0), window=self.list_frame, anchor="nw")

        self.list_frame.bind("<Configure>", self._on_list_frame_configure)
        self.list_canvas.bind("<Configure>", self._on_list_canvas_configure)
        self.list_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        footer = ttk.Frame(root, style="Root.TFrame")
        footer.pack(fill="x", pady=(10, 0))
        ttk.Progressbar(footer, maximum=100, variable=self.progress_var).pack(fill="x", pady=(0, 6))
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")
        ttk.Label(footer, textvariable=self.count_var, style="Status.TLabel").pack(anchor="w")

    def _on_list_frame_configure(self, _event: object) -> None:
        self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all"))

    def _on_list_canvas_configure(self, event: tk.Event) -> None:
        self.list_canvas.itemconfigure(self.list_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.list_canvas.winfo_exists():
            self.list_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _open_listen_modal(self) -> None:
        if self.listen_modal and self.listen_modal.winfo_exists():
            self.listen_modal.deiconify()
            self.listen_modal.lift()
            self.listen_modal.focus_force()
            return

        modal = tk.Toplevel(self)
        modal.title("Listen")
        modal.transient(self)
        modal.grab_set()
        modal.resizable(False, False)
        modal.protocol("WM_DELETE_WINDOW", self._close_listen_modal)
        modal.geometry("500x860")

        shell = tk.Frame(modal, bg=self.colors["root_bg"], padx=24, pady=24)
        shell.pack(fill="both", expand=True)

        tk.Label(
            shell,
            text="Listen",
            bg=self.colors["root_bg"],
            fg=self.colors["title_fg"],
            font=("Segoe UI Semibold", 20),
        ).pack(anchor="center", pady=(0, 18))

        canvas = tk.Canvas(shell, width=240, height=240, highlightthickness=0, bd=0, cursor="hand2")
        canvas.pack(pady=(0, 24))
        self.listen_canvas = canvas
        self._draw_listen_button()
        canvas.bind("<Button-1>", lambda _event: self._on_listen_pressed())

        wave_canvas = tk.Canvas(shell, width=320, height=72, highlightthickness=0, bd=0)
        wave_canvas.pack(pady=(0, 24))
        self.listen_wave_canvas = wave_canvas
        self._draw_listen_waveform()

        options = tk.Frame(shell, bg=self.colors["card_bg"], highlightthickness=1)
        options.pack(fill="x", pady=(0, 12))
        self.listen_options_frame = options

        tk.Label(
            options,
            text="Source",
            bg=self.colors["card_bg"],
            fg=self.colors["text_fg"],
            font=("Segoe UI Semibold", 11),
            padx=14,
            pady=12,
        ).pack(anchor="w")

        source_menu = ttk.Combobox(
            options,
            state="readonly",
            values=("Local listen", "System audio", "Both"),
            font=("Segoe UI", 11),
        )
        source_menu.pack(fill="x", padx=14, pady=(0, 10))
        source_menu.bind("<<ComboboxSelected>>", lambda _event: self._on_listen_source_change())
        source_menu.set(self._listen_source_label(self.listen_source_var.get()))
        self.listen_source_menu = source_menu

        preview_row = tk.Frame(options, bg=self.colors["card_bg"])
        preview_row.pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(
            preview_row,
            text="Auto preview",
            bg=self.colors["card_bg"],
            fg=self.colors["text_fg"],
            font=("Segoe UI", 10),
        ).pack(side="left")

        tk.Checkbutton(
            preview_row,
            text="Play sample before Shazam",
            variable=self.listen_auto_preview_var,
            bg=self.colors["card_bg"],
            fg=self.colors["text_fg"],
            selectcolor=self.colors["card_bg"],
            activebackground=self.colors["card_bg"],
            activeforeground=self.colors["text_fg"],
            font=("Segoe UI", 10),
            padx=8,
        ).pack(side="right")

        tk.Label(
            options,
            text="Capture from a local microphone, the current computer output, or both.",
            bg=self.colors["card_bg"],
            fg=self.colors["hint_fg"],
            font=("Segoe UI", 9),
            wraplength=360,
            justify="left",
            padx=14,
            pady=2,
        ).pack(fill="x", anchor="w")

        tk.Label(
            shell,
            textvariable=self.listen_hint_var,
            bg=self.colors["root_bg"],
            fg=self.colors["hint_fg"],
            font=("Segoe UI", 10),
            wraplength=340,
            justify="center",
            pady=8,
        ).pack()

        found_card = tk.Frame(shell, bg=self.colors["card_bg"], highlightthickness=1)
        found_card.pack(fill="x", pady=(0, 16))

        tk.Label(
            found_card,
            text="Found Audio",
            bg=self.colors["card_bg"],
            fg=self.colors["text_fg"],
            font=("Segoe UI Semibold", 11),
            padx=14,
            pady=12,
        ).pack(anchor="w")

        found_inner = tk.Frame(found_card, bg=self.colors["card_bg"])
        found_inner.pack(fill="x", padx=12, pady=(0, 12))

        cover_label = tk.Label(found_inner, bg=self.colors["card_bg"])
        cover_label.pack(side="left", padx=(0, 12))
        self.listen_found_cover_label = cover_label

        found_text = tk.Frame(found_inner, bg=self.colors["card_bg"])
        found_text.pack(side="left", fill="x", expand=True)

        tk.Label(
            found_text,
            textvariable=self.listen_found_title_var,
            bg=self.colors["card_bg"],
            fg=self.colors["text_fg"],
            font=("Segoe UI Semibold", 12),
            anchor="w",
            justify="left",
            wraplength=300,
        ).pack(anchor="w", fill="x")

        tk.Label(
            found_text,
            textvariable=self.listen_found_meta_var,
            bg=self.colors["card_bg"],
            fg=self.colors["hint_fg"],
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=300,
            pady=4,
        ).pack(anchor="w", fill="x")

        link_label = tk.Label(
            found_text,
            textvariable=self.listen_found_link_var,
            bg=self.colors["card_bg"],
            fg="#38BDF8" if self.dark_mode_var.get() else "#0369A1",
            font=("Segoe UI", 10, "underline"),
            cursor="hand2",
            anchor="w",
            justify="left",
            wraplength=300,
        )
        link_label.pack(anchor="w", fill="x")
        link_label.bind("<Button-1>", lambda _event: self._open_listen_found_link())
        self.listen_found_link_label = link_label
        self._refresh_listen_found_card()

        results_card = tk.Frame(shell, bg=self.colors["card_bg"], highlightthickness=1)
        results_card.pack(fill="both", expand=True, pady=(0, 12))

        tk.Label(
            results_card,
            text="Live Results",
            bg=self.colors["card_bg"],
            fg=self.colors["text_fg"],
            font=("Segoe UI Semibold", 11),
            padx=14,
            pady=12,
        ).pack(anchor="w")

        results_inner = tk.Frame(results_card, bg=self.colors["card_bg"])
        results_inner.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        results_list = tk.Listbox(
            results_inner,
            listvariable=self.listen_results_var,
            activestyle="none",
            relief="flat",
            highlightthickness=0,
            borderwidth=0,
            font=("Consolas", 10),
        )
        results_scroll = ttk.Scrollbar(results_inner, orient="vertical", command=results_list.yview)
        results_list.configure(yscrollcommand=results_scroll.set)
        results_list.pack(side="left", fill="both", expand=True)
        results_scroll.pack(side="right", fill="y")
        self.listen_results_list = results_list
        self._refresh_listen_results()

        actions = ttk.Frame(shell, style="Root.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="Clear Results", command=self._clear_listen_results).pack(side="left")
        ttk.Button(actions, text="Close", command=self._close_listen_modal).pack(side="right")

        self.listen_modal = modal
        self._refresh_listen_modal_theme()
        self.update_idletasks()
        x = self.winfo_rootx() + max((self.winfo_width() - 500) // 2, 0)
        y = self.winfo_rooty() + max((self.winfo_height() - 860) // 2, 0)
        modal.geometry(f"+{x}+{y}")

    def _draw_listen_button(self) -> None:
        if not self.listen_canvas or not self.listen_canvas.winfo_exists():
            return

        is_dark = self.dark_mode_var.get()
        listening = self.listen_thread is not None and self.listen_thread.is_alive()
        outer = "#B91C1C" if listening else ("#0D9488" if is_dark else "#0F766E")
        inner = "#EF4444" if listening else "#14B8A6"
        text_fg = "#F8FAFC"
        ring = "#FCA5A5" if listening else ("#5EEAD4" if is_dark else "#99F6E4")

        self.listen_canvas.configure(bg=self.colors["root_bg"])
        self.listen_canvas.delete("all")
        self.listen_canvas.create_oval(18, 18, 222, 222, fill=ring, outline="")
        self.listen_canvas.create_oval(30, 30, 210, 210, fill=outer, outline="")
        self.listen_canvas.create_oval(42, 42, 198, 198, fill=inner, outline="")
        self.listen_canvas.create_text(
            120,
            92,
            text="START",
            fill="#CCFBF1" if is_dark else "#E6FFFA",
            font=("Segoe UI", 10, "bold"),
        )
        self.listen_caption_id = self.listen_canvas.create_text(
            120,
            128,
            text="Stop" if listening else "Listen",
            fill=text_fg,
            font=("Segoe UI Semibold", 22),
        )

    def _draw_listen_waveform(self) -> None:
        if not self.listen_wave_canvas or not self.listen_wave_canvas.winfo_exists():
            return

        canvas = self.listen_wave_canvas
        canvas.configure(bg=self.colors["root_bg"])
        canvas.delete("all")

        width = int(canvas.cget("width"))
        height = int(canvas.cget("height"))
        mid = height // 2
        active = self.listen_thread is not None and self.listen_thread.is_alive()
        trace_color = "#5EEAD4" if active else "#94A3B8"
        line_color = "#1E293B" if self.dark_mode_var.get() else "#CBD5E1"

        canvas.create_line(0, mid, width, mid, fill=line_color, width=1)
        canvas.create_rectangle(0, 0, width - 1, height - 1, outline=line_color)

        if not self.listen_wave_levels:
            return
        points: list[float] = []
        denom = max(len(self.listen_wave_levels) - 1, 1)
        for idx, sample in enumerate(self.listen_wave_levels):
            x = (idx / denom) * (width - 1)
            y = mid - (float(sample) * (height * 0.42))
            points.extend((x, y))
        canvas.create_line(points, fill=trace_color, width=2, smooth=True)

    def _push_listen_level(self, level: float) -> None:
        self._draw_listen_waveform()

    def _set_listen_waveform(self, samples: list[float]) -> None:
        if not samples:
            self.listen_wave_levels = [0.0] * 160
        else:
            self.listen_wave_levels = [max(-1.0, min(1.0, float(sample))) for sample in samples]
        self._draw_listen_waveform()

    def _refresh_listen_found_card(self) -> None:
        photo = self.listen_found_cover_photo or self._placeholder_cover()
        if self.listen_found_cover_label and self.listen_found_cover_label.winfo_exists():
            self.listen_found_cover_label.configure(image=photo)
            self.listen_found_cover_label.image = photo

        if self.listen_found_link_label and self.listen_found_link_label.winfo_exists():
            self.listen_found_link_label.configure(cursor="hand2" if self.listen_found_url else "arrow")
            if not self.listen_found_url:
                self.listen_found_link_var.set("")

    def _listen_source_label(self, source: str) -> str:
        label_map = {
            "local": "Local listen",
            "system": "System audio",
            "both": "Both",
        }
        return label_map.get(source, source)

    def _listen_source_value(self, label: str) -> str:
        value_map = {
            "Local listen": "local",
            "System audio": "system",
            "Both": "both",
        }
        return value_map.get(label, "local")

    def _refresh_listen_modal_theme(self) -> None:
        if not self.listen_modal or not self.listen_modal.winfo_exists():
            return

        border = "#334155" if self.dark_mode_var.get() else "#CBD5E1"
        self.listen_modal.configure(bg=self.colors["root_bg"])

        for child in self.listen_modal.winfo_children():
            self._refresh_listen_modal_widget(child, border)

        self._draw_listen_button()
        self._draw_listen_waveform()
        self._refresh_listen_found_card()

    def _refresh_listen_modal_widget(self, widget: tk.Widget, border: str) -> None:
        if isinstance(widget, (tk.Frame, tk.LabelFrame)):
            bg = self.colors["root_bg"]
            if widget.winfo_children():
                bg = self.colors["card_bg"] if any(isinstance(c, tk.Radiobutton) for c in widget.winfo_children()) else self.colors["root_bg"]
            widget.configure(bg=bg)
            if bg == self.colors["card_bg"]:
                widget.configure(highlightbackground=border, highlightcolor=border)
        elif isinstance(widget, tk.Label):
            current_bg = str(widget.cget("bg"))
            is_card = current_bg == self.colors["card_bg"] or widget.master is self.listen_options_frame or (
                widget.master is not None and widget.master.master is self.listen_options_frame
            )
            widget.configure(bg=self.colors["card_bg"] if is_card else self.colors["root_bg"])
            if is_card:
                current_fg = str(widget.cget("fg"))
                if current_fg.lower() in {"#38bdf8", "#0369a1"}:
                    widget.configure(fg="#38BDF8" if self.dark_mode_var.get() else "#0369A1")
                else:
                    widget.configure(fg=self.colors["hint_fg"] if current_fg == self.colors["hint_fg"] else self.colors["text_fg"])
        elif isinstance(widget, tk.Listbox):
            widget.configure(
                bg=self.colors["card_bg"],
                fg=self.colors["text_fg"],
                selectbackground="#0D9488" if self.dark_mode_var.get() else "#99F6E4",
                selectforeground=self.colors["text_fg"],
            )

        for child in widget.winfo_children():
            self._refresh_listen_modal_widget(child, border)

    def _on_listen_source_change(self) -> None:
        if self.listen_source_menu and self.listen_source_menu.winfo_exists():
            self.listen_source_var.set(self._listen_source_value(self.listen_source_menu.get()))

        source = self.listen_source_var.get()
        label = self._listen_source_label(source)
        if source == "local":
            self.listen_hint_var.set("Local listen selected. This mode will use a microphone or line-in device.")
        elif source == "system":
            self.listen_hint_var.set("System audio selected. This mode will capture what the computer is currently playing.")
        elif source == "both":
            self.listen_hint_var.set("Both selected. This mode is intended to merge local input and system audio later.")
        else:
            self.listen_hint_var.set(f"Selected source: {label}.")

    def _on_listen_pressed(self) -> None:
        if self.listen_thread and self.listen_thread.is_alive():
            self._stop_listening()
            return

        source = self.listen_source_var.get()
        label = self._listen_source_label(source)
        self.status_var.set(f"Starting listen mode: {label}")
        self.listen_hint_var.set(f"Starting {label.lower()}...")
        self._start_listening()

    def _start_listening(self) -> None:
        if self.listen_thread and self.listen_thread.is_alive():
            return

        self.listen_stop_event = threading.Event()
        source = self.listen_source_var.get()
        auto_preview = self.listen_auto_preview_var.get()

        def worker() -> None:
            try:
                listener = LiveAudioListener(
                    source=source,
                    auto_preview=auto_preview,
                    stop_event=self.listen_stop_event,
                    on_status=lambda message: self.event_queue.put(("listen_status", message)),
                    on_result=lambda result: self.event_queue.put(("listen_result", result)),
                    on_level=lambda source_name, level: self.event_queue.put(("listen_level", {"source": source_name, "level": level})),
                    on_wave=lambda source_name, samples: self.event_queue.put(("listen_wave", {"source": source_name, "samples": samples})),
                    on_preview_ready=lambda source_name, path: None,
                )
                listener.run()
                self.event_queue.put(("listen_finished", {"stopped": True}))
            except AudioCaptureError as exc:
                self.event_queue.put(("listen_finished", {"stopped": False, "error": str(exc)}))
            except Exception as exc:
                self.event_queue.put(("listen_finished", {"stopped": False, "error": str(exc)}))

        self.listen_thread = threading.Thread(target=worker, daemon=True)
        self.listen_thread.start()
        self._draw_listen_button()

    def _stop_listening(self) -> None:
        if not (self.listen_thread and self.listen_thread.is_alive()):
            return
        self.listen_stop_event.set()
        self.listen_hint_var.set("Stopping listen mode...")
        self.status_var.set("Stopping listen mode...")
        self._draw_listen_button()
        self._draw_listen_waveform()

    def _add_listen_result(self, result: LiveTrackResult) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(result.detected_at))
        album = result.track.album or "Unknown album"
        source_label = SOURCE_LABELS.get(result.source, result.source.title())
        line = f"[{stamp}] {source_label}: {result.track.artist} - {result.track.title} | {album}"
        self.listen_results.insert(0, line)
        self.listen_results = self.listen_results[:40]
        self._refresh_listen_results()
        self.status_var.set(f"Matched {result.track.artist} - {result.track.title}")
        self.listen_hint_var.set(f"Detected on {result.device_name}: {result.track.artist} - {result.track.title}")
        self.listen_found_title_var.set(f"{result.track.artist} - {result.track.title}")
        self.listen_found_meta_var.set(f"{album} | {source_label} | {stamp}")
        self.listen_found_url = result.track.track_url
        self.listen_found_link_var.set(result.track.track_url or "")
        self.listen_found_cover_photo = self._cover_photo(self._fetch_cover_bytes(result.track.cover_url))
        self._refresh_listen_found_card()

    def _open_listen_found_link(self) -> None:
        if self.listen_found_url:
            webbrowser.open(self.listen_found_url)

    def _refresh_listen_results(self) -> None:
        items = self.listen_results if self.listen_results else ["No matches yet."]
        self.listen_results_var.set(tuple(items))
        if self.listen_results_list and self.listen_results_list.winfo_exists():
            self.listen_results_list.selection_clear(0, "end")

    def _clear_listen_results(self) -> None:
        self.listen_results.clear()
        self._refresh_listen_results()
        self.listen_hint_var.set("Results cleared. Choose a source and press Listen.")
        self.listen_found_title_var.set("No track found yet")
        self.listen_found_meta_var.set("Start listening to show the latest match.")
        self.listen_found_url = None
        self.listen_found_link_var.set("")
        self.listen_found_cover_photo = None
        self._refresh_listen_found_card()

    def _close_listen_modal(self) -> None:
        self._stop_listening()
        if self.listen_modal and self.listen_modal.winfo_exists():
            self.listen_modal.grab_release()
            self.listen_modal.destroy()
        self.listen_modal = None
        self.listen_canvas = None
        self.listen_caption_id = None
        self.listen_options_frame = None
        self.listen_source_menu = None
        self.listen_results_list = None
        self.listen_wave_canvas = None
        self.listen_found_cover_label = None
        self.listen_found_link_label = None

    def _show_row_menu(self, event: tk.Event, path: str) -> None:
        self._set_selected_row(path)
        self.menu_path = path
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()

    def _bind_row_interactions(self, widget: tk.Widget, path: str) -> None:
        widget.bind("<Button-3>", lambda event, p=path: self._show_row_menu(event, p))
        widget.bind("<Button-1>", lambda _event, p=path: self._set_selected_row(p))

    def _row_palette(self, selected: bool) -> tuple[str, str]:
        if self.dark_mode_var.get():
            if selected:
                return "#1E293B", "#14B8A6"
            return self.colors["card_bg"], "#263449"
        if selected:
            return "#E6F2FF", "#3B82F6"
        return self.colors["card_bg"], "#D6D3D1"

    def _paint_row(self, path: str) -> None:
        row = self.row_by_path.get(path)
        if not row:
            return

        selected = path == self.selected_row_path
        bg, border = self._row_palette(selected)

        row_frame = row.get("row_frame")
        text_col = row.get("text_col")
        status_row = row.get("status_row")
        cover_label = row.get("cover_label")
        name_label = row.get("name_label")
        details_label = row.get("details_label")
        status_prefix = row.get("status_prefix")
        status_label = row.get("status_label")
        status_var = row.get("status_var")

        if isinstance(row_frame, tk.Frame):
            row_frame.configure(bg=bg, highlightbackground=border, highlightcolor=border)
        if isinstance(text_col, tk.Frame):
            text_col.configure(bg=bg)
        if isinstance(status_row, tk.Frame):
            status_row.configure(bg=bg)
        if isinstance(cover_label, tk.Label):
            cover_label.configure(bg=bg)
        if isinstance(name_label, tk.Label):
            name_label.configure(bg=bg, fg=self.colors["text_fg"])
        if isinstance(details_label, tk.Label):
            details_label.configure(bg=bg, fg=self.colors["text_fg"])
        if isinstance(status_prefix, tk.Label):
            status_prefix.configure(bg=bg, fg=self.colors["text_fg"])
        if isinstance(status_label, tk.Label):
            status_label.configure(bg=bg)
            self._apply_status_to_label(status_label, status_var.get() if isinstance(status_var, tk.StringVar) else "")

    def _set_selected_row(self, path: str | None) -> None:
        previous = self.selected_row_path
        self.selected_row_path = path
        if previous:
            self._paint_row(previous)
        if path:
            self._paint_row(path)

    def _open_path(self, path: Path) -> None:
        if os.name == "nt":
            os.startfile(str(path))
            return
        opener = "open" if os.name == "posix" else "xdg-open"
        subprocess.Popen([opener, str(path)])

    def _open_item_file(self) -> None:
        if not self.menu_path:
            return
        path = Path(self.menu_path)
        if not path.exists():
            messagebox.showerror("Missing file", f"File not found: {path}")
            return
        self._open_path(path)

    def _open_item_folder(self) -> None:
        if not self.menu_path:
            return
        path = Path(self.menu_path)
        folder = path.parent if path.parent.exists() else path
        self._open_path(folder)

    def _rescan_item(self) -> None:
        if not self.menu_path:
            return
        if (self.run_thread and self.run_thread.is_alive()) or (self.scan_thread and self.scan_thread.is_alive()):
            messagebox.showinfo("Busy", "Stop current processing before rescanning an item.")
            return

        path = Path(self.menu_path)
        if not path.exists():
            messagebox.showerror("Missing file", f"File not found: {path}")
            return

        refreshed = self._read_embedded_info(path)
        refreshed.status = "Rescanned"
        self._apply_row_result({"old_path": str(path), "entry": refreshed, "status": "Rescanned"})
        self.status_var.set(f"Rescanned: {path.name}")

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.folder_var.get())
        if folder:
            self.folder_var.set(folder)
            self._scan_files()

    def _scan_files(self) -> None:
        if self.run_thread and self.run_thread.is_alive():
            messagebox.showinfo("Busy", "Stop processing before scanning again.")
            return

        folder = Path(self.folder_var.get()).expanduser()
        if not folder.exists() or not folder.is_dir():
            messagebox.showerror("Invalid folder", "Choose a valid folder.")
            return

        if self.scan_thread and self.scan_thread.is_alive():
            return

        self.scan_btn.config(state="disabled")
        self.status_var.set("Scanning MP3 files and embedded metadata...")
        self.progress_var.set(0)
        self.scan_thread = threading.Thread(target=self._scan_worker, args=(folder, self.recurse_var.get()), daemon=True)
        self.scan_thread.start()

    def _scan_worker(self, folder: Path, recurse: bool) -> None:
        files = gather_files(folder, recurse)
        total = len(files)
        entries: list[FileEntry] = []

        for index, file_path in enumerate(files, start=1):
            entries.append(self._read_embedded_info(file_path))
            pct = (index / total) * 100 if total else 100
            self.event_queue.put(("scan_progress", pct))

        self.event_queue.put(("scan_done", entries))

    def _read_embedded_info(self, file_path: Path) -> FileEntry:
        entry = FileEntry(path=file_path)
        try:
            audio = MP3(file_path)
            tags = audio.tags
            if tags:
                title = tags.get("TIT2")
                artist = tags.get("TPE1")
                album = tags.get("TALB")

                entry.title = str(title.text[0]) if title and getattr(title, "text", None) else ""
                entry.artist = str(artist.text[0]) if artist and getattr(artist, "text", None) else ""
                entry.album = str(album.text[0]) if album and getattr(album, "text", None) else ""

                apics = tags.getall("APIC")
                if apics:
                    entry.has_cover = True
                    entry.cover_data = apics[0].data

                entry.has_embedded = bool(entry.title or entry.artist or entry.album or entry.has_cover)
        except Exception:
            entry.status = "Unreadable tags"
        return entry

    def _placeholder_cover(self) -> ImageTk.PhotoImage:
        image = Image.new("RGB", (72, 72), color="#253047" if self.dark_mode_var.get() else "#E2E8F0")
        return ImageTk.PhotoImage(image)

    def _cover_photo(self, data: bytes | None) -> ImageTk.PhotoImage:
        if not data:
            return self._placeholder_cover()
        try:
            image = Image.open(BytesIO(data))
            image = image.convert("RGB")
            image.thumbnail((72, 72))
            thumb = Image.new("RGB", (72, 72), "#253047" if self.dark_mode_var.get() else "#E2E8F0")
            x = (72 - image.width) // 2
            y = (72 - image.height) // 2
            thumb.paste(image, (x, y))
            return ImageTk.PhotoImage(thumb)
        except Exception:
            return self._placeholder_cover()

    def _display_cover_data(self, entry: FileEntry) -> bytes | None:
        if entry.pending_cover_data:
            return entry.pending_cover_data
        return entry.cover_data

    def _fetch_cover_bytes(self, url: str | None) -> bytes | None:
        if not url:
            return None
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.content
        except Exception:
            return None

    def _details_text(self, entry: FileEntry) -> str:
        title = entry.title or "-"
        artist = entry.artist or "-"
        album = entry.album or "-"
        embedded = "yes" if entry.has_embedded else "no"
        cover = "yes" if entry.has_cover else "no"
        pending = "yes" if entry.pending_track else "no"
        return f"title: {title} | artist: {artist} | album: {album} | embedded: {embedded} | cover: {cover} | pending: {pending}"

    def _has_pending(self) -> bool:
        return any(e.pending_track is not None for e in self.entries)

    def _refresh_apply_button(self) -> None:
        running = self.run_thread is not None and self.run_thread.is_alive()
        self.apply_btn.config(state="normal" if (self._has_pending() and not running) else "disabled")

    def _replace_entry(self, old_path: str, new_entry: FileEntry) -> None:
        for idx, existing in enumerate(self.entries):
            if str(existing.path) == old_path:
                self.entries[idx] = new_entry
                return

    def _status_style(self, status: str) -> tuple[str, str]:
        s = status.lower()
        if self.dark_mode_var.get():
            base = {
                "error": "#F87171",
                "applying": "#38BDF8",
                "pending": "#C084FC",
                "applied": "#4ADE80",
                "not_identified": "#FBBF24",
                "identifying": "#60A5FA",
                "done": "#4ADE80",
                "dry": "#94A3B8",
                "default": "#CBD5E1",
            }
        else:
            base = {
                "error": "#B91C1C",
                "applying": "#0369A1",
                "pending": "#7C3AED",
                "applied": "#166534",
                "not_identified": "#B45309",
                "identifying": "#1D4ED8",
                "done": "#166534",
                "dry": "#475569",
                "default": "#334155",
            }
        if "error" in s:
            return base["error"], "bold"
        if "applying" in s:
            return base["applying"], "bold"
        if "pending" in s:
            return base["pending"], "bold"
        if "applied" in s:
            return base["applied"], "bold"
        if "not identified" in s:
            return base["not_identified"], "bold"
        if "identifying" in s:
            return base["identifying"], "bold"
        if "renamed" in s or "done" in s:
            return base["done"], "bold"
        if "dry run" in s:
            return base["dry"], "bold"
        return base["default"], "bold"

    def _apply_status_to_label(self, label: tk.Label, status: str) -> None:
        color, weight = self._status_style(status)
        label.configure(text=status, fg=color, font=("Segoe UI", 10, weight))

    def _render_file_list(self, entries: list[FileEntry]) -> None:
        previous_selected = self.selected_row_path
        self.entries = entries
        self.row_by_path.clear()
        self.row_photos.clear()
        self.selected_row_path = None

        for child in self.list_frame.winfo_children():
            child.destroy()

        for entry in entries:
            row = tk.Frame(self.list_frame, bg=self.colors["card_bg"], highlightthickness=1, highlightbackground="#000000", highlightcolor="#000000", padx=6, pady=6)
            row.pack(fill="x", pady=3)
            path_key = str(entry.path)

            photo = self._cover_photo(self._display_cover_data(entry))
            self.row_photos.append(photo)
            cover = tk.Label(row, image=photo, bg=self.colors["card_bg"], width=72, height=72)
            cover.pack(side="left", padx=(0, 10))

            text_col = tk.Frame(row, bg=self.colors["card_bg"])
            text_col.pack(side="left", fill="x", expand=True)

            name_var = tk.StringVar(value=entry.path.name)
            details_var = tk.StringVar(value=self._details_text(entry))
            status_var = tk.StringVar(value=entry.status)

            name_label = tk.Label(text_col, textvariable=name_var, bg=self.colors["card_bg"], fg=self.colors["text_fg"], anchor="w", font=("Segoe UI Semibold", 10))
            name_label.pack(anchor="w")
            details_label = tk.Label(text_col, textvariable=details_var, bg=self.colors["card_bg"], fg=self.colors["text_fg"], anchor="w", wraplength=830, justify="left", font=("Segoe UI", 10))
            details_label.pack(anchor="w", pady=(2, 0))
            status_row = tk.Frame(text_col, bg=self.colors["card_bg"])
            status_row.pack(anchor="w", pady=(2, 0), fill="x")
            status_prefix = tk.Label(status_row, text="status:", bg=self.colors["card_bg"], fg=self.colors["text_fg"], anchor="w", font=("Segoe UI", 10))
            status_prefix.pack(side="left")
            status_label = tk.Label(status_row, bg=self.colors["card_bg"], anchor="w")
            status_label.pack(side="left", padx=(4, 0))
            self._apply_status_to_label(status_label, status_var.get())

            for widget in (row, cover, text_col, name_label, details_label, status_row, status_prefix, status_label):
                self._bind_row_interactions(widget, path_key)

            self.row_by_path[path_key] = {
                "entry": entry,
                "row_frame": row,
                "text_col": text_col,
                "status_row": status_row,
                "name_var": name_var,
                "details_var": details_var,
                "status_var": status_var,
                "name_label": name_label,
                "details_label": details_label,
                "status_prefix": status_prefix,
                "status_label": status_label,
                "cover_label": cover,
            }

        count = len(entries)
        with_embedded = sum(1 for e in entries if e.has_embedded)
        with_cover = sum(1 for e in entries if e.has_cover)
        pending = sum(1 for e in entries if e.pending_track is not None)
        self.count_var.set(f"{count} files | embedded info: {with_embedded} | cover art: {with_cover} | pending: {pending}")
        self.status_var.set("Scan complete" if count else "No MP3 files found")
        self.list_canvas.yview_moveto(0)
        if previous_selected and previous_selected in self.row_by_path:
            self._set_selected_row(previous_selected)
        elif entries:
            self._set_selected_row(str(entries[0].path))
        self._refresh_apply_button()

    def _start_run(self) -> None:
        if self.run_thread and self.run_thread.is_alive():
            return
        if not self.entries:
            messagebox.showinfo("No files", "Scan a folder first.")
            return

        try:
            timeout = float(self.timeout_var.get().strip())
            if timeout <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror("Invalid timeout", "Timeout must be a positive number.")
            return

        self.stop_event.clear()
        self.run_btn.config(state="disabled")
        self.apply_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.scan_btn.config(state="disabled")
        self.progress_var.set(0)
        self.status_var.set("Processing files...")

        paths = [e.path for e in self.entries]
        opts = {
            "dry_run": self.dry_run_var.get(),
            "auto_apply": self.auto_apply_var.get(),
            "timeout": timeout,
        }
        self.run_thread = threading.Thread(target=self._run_worker, args=(paths, opts), daemon=True)
        self.run_thread.start()

    def _start_apply(self) -> None:
        if self.run_thread and self.run_thread.is_alive():
            return

        pending_paths = [e.path for e in self.entries if e.pending_track is not None]
        if not pending_paths:
            messagebox.showinfo("No pending changes", "There are no detected changes to apply.")
            return

        self.stop_event.clear()
        self.run_btn.config(state="disabled")
        self.apply_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.scan_btn.config(state="disabled")
        self.progress_var.set(0)
        self.status_var.set("Applying pending changes...")

        self.run_thread = threading.Thread(target=self._apply_worker, args=(pending_paths,), daemon=True)
        self.run_thread.start()

    def _stop_run(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping after current file...")

    def _run_worker(self, paths: list[Path], options: dict[str, object]) -> None:
        asyncio.run(self._run_async(paths, options))

    def _apply_worker(self, paths: list[Path]) -> None:
        asyncio.run(self._apply_async(paths))

    async def _run_async(self, paths: list[Path], options: dict[str, object]) -> None:
        dry_run = bool(options.get("dry_run", False))
        auto_apply = bool(options.get("auto_apply", True))
        timeout_raw = options.get("timeout", 40.0)
        timeout = float(timeout_raw) if isinstance(timeout_raw, (int, float, str)) else 40.0

        processed = 0
        skipped = 0
        errors = 0
        total = len(paths)

        shazam = Shazam()

        for idx, current_path in enumerate(paths, start=1):
            if self.stop_event.is_set():
                break

            self.event_queue.put(("row_update", {"path": str(current_path), "status": "Identifying"}))
            track = await shazam_identify_file(shazam, current_path, timeout=timeout)

            if not track:
                skipped += 1
                self.event_queue.put(("row_update", {"path": str(current_path), "status": "Not identified"}))
                self.event_queue.put(("run_progress", (idx / total) * 100 if total else 100))
                continue

            target_name = make_target_filename(track.artist, track.title, track.album)
            final_path = current_path
            renamed = False

            try:
                if auto_apply:
                    if target_name != current_path.name and not dry_run:
                        final_path = safe_rename(current_path, target_name)
                        renamed = True

                    if not dry_run:
                        update_tags(final_path, track)

                    refreshed = self._read_embedded_info(final_path)
                    status = "Done"
                    if dry_run:
                        status = "Dry run"
                    elif renamed:
                        status = "Renamed + tagged"
                else:
                    refreshed = self._read_embedded_info(current_path)
                    refreshed.title = track.title
                    refreshed.artist = track.artist
                    refreshed.album = track.album or ""
                    refreshed.pending_track = track
                    refreshed.pending_name = target_name
                    refreshed.pending_cover_data = self._fetch_cover_bytes(track.cover_url)
                    status = "Pending apply"

                self.event_queue.put(("row_result", {"old_path": str(current_path), "entry": refreshed, "status": status}))
                processed += 1
            except Exception as exc:
                errors += 1
                self.event_queue.put(("row_update", {"path": str(current_path), "status": f"Error: {exc}"}))

            self.event_queue.put(("run_progress", (idx / total) * 100 if total else 100))

        self.event_queue.put(
            (
                "run_done",
                {
                    "processed": processed,
                    "skipped": skipped,
                    "errors": errors,
                    "stopped": self.stop_event.is_set(),
                },
            )
        )

    async def _apply_async(self, paths: list[Path]) -> None:
        processed = 0
        skipped = 0
        errors = 0
        total = len(paths)

        for idx, current_path in enumerate(paths, start=1):
            if self.stop_event.is_set():
                break

            row = self.row_by_path.get(str(current_path))
            entry = row.get("entry") if row else None
            if not isinstance(entry, FileEntry) or entry.pending_track is None:
                skipped += 1
                self.event_queue.put(("run_progress", (idx / total) * 100 if total else 100))
                continue

            self.event_queue.put(("row_update", {"path": str(current_path), "status": "Applying"}))

            try:
                target_name = entry.pending_name or make_target_filename(
                    entry.pending_track.artist,
                    entry.pending_track.title,
                    entry.pending_track.album,
                )
                final_path = current_path
                if target_name != current_path.name:
                    final_path = safe_rename(current_path, target_name)

                update_tags(final_path, entry.pending_track)
                refreshed = self._read_embedded_info(final_path)
                refreshed.status = "Applied"
                refreshed.pending_track = None
                refreshed.pending_name = None
                refreshed.pending_cover_data = None
                self.event_queue.put(("row_result", {"old_path": str(current_path), "entry": refreshed, "status": "Applied"}))
                processed += 1
            except Exception as exc:
                errors += 1
                self.event_queue.put(("row_update", {"path": str(current_path), "status": f"Error: {exc}"}))

            self.event_queue.put(("run_progress", (idx / total) * 100 if total else 100))

        self.event_queue.put(
            (
                "run_done",
                {
                    "processed": processed,
                    "skipped": skipped,
                    "errors": errors,
                    "stopped": self.stop_event.is_set(),
                },
            )
        )

    def _apply_row_update(self, data: dict[str, object]) -> None:
        path = str(data.get("path", ""))
        status = str(data.get("status", ""))
        row = self.row_by_path.get(path)
        if not row:
            return
        status_var = row.get("status_var")
        status_label = row.get("status_label")
        if isinstance(status_var, tk.StringVar):
            status_var.set(status)
        if isinstance(status_label, tk.Label):
            self._apply_status_to_label(status_label, status)
        entry = row.get("entry")
        if isinstance(entry, FileEntry):
            entry.status = status

    def _apply_row_result(self, data: dict[str, object]) -> None:
        old_path = str(data.get("old_path", ""))
        status = str(data.get("status", "Done"))
        entry = data.get("entry")
        if not isinstance(entry, FileEntry):
            return

        row = self.row_by_path.pop(old_path, None)
        if not row:
            return

        row["entry"] = entry
        self.row_by_path[str(entry.path)] = row
        self._replace_entry(old_path, entry)
        if self.selected_row_path == old_path:
            self.selected_row_path = str(entry.path)

        name_var = row.get("name_var")
        details_var = row.get("details_var")
        status_var = row.get("status_var")
        status_label = row.get("status_label")
        cover_label = row.get("cover_label")

        entry.status = status

        if isinstance(name_var, tk.StringVar):
            name_var.set(entry.path.name)
        if isinstance(details_var, tk.StringVar):
            details_var.set(self._details_text(entry))
        if isinstance(status_var, tk.StringVar):
            status_var.set(status)
        if isinstance(status_label, tk.Label):
            self._apply_status_to_label(status_label, status)
        if isinstance(cover_label, tk.Label):
            photo = self._cover_photo(self._display_cover_data(entry))
            self.row_photos.append(photo)
            cover_label.configure(image=photo)

        self._paint_row(str(entry.path))

        count = len(self.entries)
        with_embedded = sum(1 for e in self.entries if e.has_embedded)
        with_cover = sum(1 for e in self.entries if e.has_cover)
        pending = sum(1 for e in self.entries if e.pending_track is not None)
        self.count_var.set(f"{count} files | embedded info: {with_embedded} | cover art: {with_cover} | pending: {pending}")
        self._refresh_apply_button()

    def _finish_run(self, result: dict[str, object]) -> None:
        self.run_btn.config(state="normal")
        self.scan_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._refresh_apply_button()

        processed_raw = result.get("processed", 0)
        skipped_raw = result.get("skipped", 0)
        errors_raw = result.get("errors", 0)

        processed = int(processed_raw) if isinstance(processed_raw, (int, float, str)) else 0
        skipped = int(skipped_raw) if isinstance(skipped_raw, (int, float, str)) else 0
        errors = int(errors_raw) if isinstance(errors_raw, (int, float, str)) else 0
        stopped = bool(result.get("stopped", False))

        if stopped:
            self.status_var.set(f"Stopped. Updated: {processed}, skipped: {skipped}, errors: {errors}")
        else:
            self.status_var.set(f"Done. Updated: {processed}, skipped: {skipped}, errors: {errors}")

    def _finish_listen(self, result: dict[str, object]) -> None:
        error = str(result.get("error", "")).strip()
        if error:
            self.status_var.set(f"Listen failed: {error}")
            self.listen_hint_var.set(f"Listen failed: {error}")
            if self.listen_modal and self.listen_modal.winfo_exists():
                messagebox.showerror("Listen failed", error)
        else:
            self.status_var.set("Listen mode stopped")
            self.listen_hint_var.set("Choose a source and press Listen.")

        self.listen_thread = None
        self.listen_stop_event = threading.Event()
        self.listen_wave_levels = [0.0] * 160
        self._draw_listen_button()
        self._draw_listen_waveform()
        self._refresh_listen_found_card()

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "scan_progress":
                self.progress_var.set(float(payload) if isinstance(payload, (int, float)) else 0.0)
            elif kind == "scan_done":
                self.scan_btn.config(state="normal")
                self.progress_var.set(100)
                if isinstance(payload, list):
                    self._render_file_list(payload)
            elif kind == "run_progress":
                self.progress_var.set(float(payload) if isinstance(payload, (int, float)) else 0.0)
            elif kind == "row_update" and isinstance(payload, dict):
                self._apply_row_update(payload)
            elif kind == "row_result" and isinstance(payload, dict):
                self._apply_row_result(payload)
            elif kind == "run_done" and isinstance(payload, dict):
                self._finish_run(payload)
            elif kind == "listen_status" and isinstance(payload, str):
                self.listen_hint_var.set(payload)
                self.status_var.set(payload)
            elif kind == "listen_result" and isinstance(payload, LiveTrackResult):
                self._add_listen_result(payload)
            elif kind == "listen_level" and isinstance(payload, dict):
                level = payload.get("level", 0.0)
                if isinstance(level, (int, float)):
                    self._push_listen_level(float(level))
            elif kind == "listen_wave" and isinstance(payload, dict):
                samples = payload.get("samples", [])
                if isinstance(samples, list):
                    self._set_listen_waveform(samples)
            elif kind == "listen_finished" and isinstance(payload, dict):
                self._finish_listen(payload)

        self.after(80, self._drain_events)

    def _on_close(self) -> None:
        if self.run_thread and self.run_thread.is_alive():
            if not messagebox.askyesno("Exit", "Processing is running. Stop and close?"):
                return
            self.stop_event.set()
        self._close_listen_modal()
        self.destroy()


def main() -> None:
    app = PythonShazzamGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
