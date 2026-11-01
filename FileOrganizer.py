"""
File Organizer
Sorts files into folders (Pictures, Videos, Docs, ...) with a modern UI.

Features: folder picker, custom categories, sort by date, rename by pattern,
ignore list, duplicate finder, large files finder, auto-organize, activity log
with restore, undo, custom folder icons (Windows), light/dark theme.

Requires:  pip install customtkinter
Build exe: python -m PyInstaller --onefile --windowed --collect-all customtkinter --name FileOrganizer file_organizer.py
"""

import os
import re
import json
import shutil
import hashlib
import fnmatch
import threading
import subprocess
import ctypes
import time
from datetime import datetime
from collections import Counter, defaultdict

APP_NAME = "FileOrganizer"
OTHERS = "Others"
DUPES = "Duplicates"
ICON_FILE = ".foldericon.ico"
TEMP_EXTS = {".crdownload", ".part", ".tmp", ".download", ".partial"}
AUTO_INTERVAL_MS = 5000
SKIP_NAMES = {"desktop.ini", ICON_FILE}

DEFAULT_CATEGORIES = {
    "Pictures": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".svg", ".ico", ".heic", ".raw"],
    "Videos": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpeg", ".mpg", ".3gp"],
    "Music": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"],
    "Docs": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx", ".csv",
             ".ppt", ".pptx", ".md", ".epub"],
    "Archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".iso"],
    "Programs": [".exe", ".msi", ".apk", ".bat", ".dmg"],
    "Code": [".py", ".js", ".html", ".css", ".java", ".c", ".cpp", ".cs", ".php", ".json",
             ".xml", ".sql", ".ts", ".sh"],
}

DEFAULT_CONFIG = {
    "theme": "System",
    "last_folder": "",
    "categories": DEFAULT_CATEGORIES,
    "ignore": ["*.tmp", "~*", "Thumbs.db"],
    "date_mode": "off",      # off | year | month
    "rename_mode": "off",    # off | date | number
    "include_sub": False,
    "icons": {},             # category name -> stored .ico path
    "managed_icons": {},     # managed folder path -> stored .ico path
}

CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "activity_log.jsonl")
ICON_DIR = os.path.join(CONFIG_DIR, "icons")

DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")
NUM_PREFIX_RE = re.compile(r"^\d{3,}_")


# =====================================================================
# ENGINE (no GUI code here)
# =====================================================================

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def append_log(entries):
    if not entries:
        return
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    except OSError:
        pass


def log_entry(action, src, dst=""):
    return {"t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "action": action, "src": src, "dst": dst}


def read_log(limit=1000):
    entries = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return entries[-limit:][::-1]


def clear_log():
    try:
        os.remove(LOG_PATH)
    except OSError:
        pass


def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def get_category(filename, categories):
    ext = os.path.splitext(filename)[1].lower()
    for cat, exts in categories.items():
        if ext in exts:
            return cat
    return OTHERS


def is_ignored(name, patterns):
    low = name.lower()
    return any(fnmatch.fnmatch(low, p.strip().lower()) for p in patterns if p.strip())


def collect_files(root, cfg):
    skip_dirs = set(cfg["categories"]) | {OTHERS, DUPES}
    found = []
    if cfg["include_sub"]:
        for dirpath, dirnames, filenames in os.walk(root):
            if os.path.normcase(dirpath) == os.path.normcase(root):
                dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for n in filenames:
                found.append(os.path.join(dirpath, n))
    else:
        for n in os.listdir(root):
            p = os.path.join(root, n)
            if os.path.isfile(p):
                found.append(p)
    return [p for p in found
            if os.path.basename(p).lower() not in SKIP_NAMES
            and not is_ignored(os.path.basename(p), cfg["ignore"])]


def date_subdir(path, mode):
    if mode == "off":
        return ""
    dt = datetime.fromtimestamp(os.path.getmtime(path))
    if mode == "year":
        return str(dt.year)
    return os.path.join(str(dt.year), dt.strftime("%m - %B"))


def build_plan(root, cfg, files=None):
    if files is None:
        files = collect_files(root, cfg)
    plan = []
    counter = 0
    for src in sorted(files):
        try:
            cat = get_category(src, cfg["categories"])
            sub = date_subdir(src, cfg["date_mode"])
            name = os.path.basename(src)
            mode = cfg["rename_mode"]
            if mode == "date" and not DATE_PREFIX_RE.match(name):
                stamp = datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d_")
                name = stamp + name
            elif mode == "number" and not NUM_PREFIX_RE.match(name):
                counter += 1
                name = f"{counter:03d}_{name}"
            rel = os.path.join(cat, sub) if sub else cat
            plan.append({"src": src, "dest_dir": os.path.join(root, rel), "name": name,
                         "category": cat, "rel": rel})
        except OSError:
            continue
    return plan


def execute_plan(root, plan, cfg, progress=None):
    moves, errors = [], 0
    total = len(plan)
    for i, item in enumerate(plan, 1):
        try:
            os.makedirs(item["dest_dir"], exist_ok=True)
            ensure_category_icon(root, item["category"], cfg)
            dest = unique_path(os.path.join(item["dest_dir"], item["name"]))
            shutil.move(item["src"], dest)
            moves.append((dest, item["src"]))
        except OSError:
            errors += 1
        if progress:
            progress(i, total)
    return moves, errors


def restore_file(dst, src):
    """Move dst back to src (never overwrites). Returns the final path or None."""
    if not os.path.exists(dst):
        return None
    os.makedirs(os.path.dirname(src), exist_ok=True)
    target = unique_path(src)
    shutil.move(dst, target)
    return target


def cleanup_empty_dirs(start_dir, root):
    d = os.path.normpath(start_dir)
    root = os.path.normpath(root)
    while d.lower() != root.lower() and d.lower().startswith(root.lower()):
        try:
            entries = [e.lower() for e in os.listdir(d)]
            if all(e in SKIP_NAMES for e in entries):
                reset_folder_icon(d)
                os.rmdir(d)
            else:
                break
        except OSError:
            break
        d = os.path.dirname(d)


# ---------- duplicates and large files ----------

def file_hash(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def find_duplicates(root):
    by_size = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != DUPES]
        for n in filenames:
            if n.lower() in SKIP_NAMES:
                continue
            p = os.path.join(dirpath, n)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size > 0:
                by_size[size].append(p)
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash = defaultdict(list)
        for p in paths:
            try:
                by_hash[file_hash(p)].append(p)
            except OSError:
                pass
        for plist in by_hash.values():
            if len(plist) > 1:
                plist.sort(key=_mtime)  # oldest first = the copy we keep
                groups.append((size, plist))
    groups.sort(key=lambda g: g[0] * (len(g[1]) - 1), reverse=True)
    return groups


def find_large_files(root, limit=50):
    items = []
    for dirpath, _dirs, filenames in os.walk(root):
        for n in filenames:
            p = os.path.join(dirpath, n)
            try:
                items.append((os.path.getsize(p), p))
            except OSError:
                pass
    items.sort(reverse=True)
    return items[:limit]


# ---------- folder icons (Windows) ----------

def _set_attr(path, attr):
    if os.name == "nt":
        ctypes.windll.kernel32.SetFileAttributesW(str(path), attr)


def _get_attr(path):
    if os.name == "nt":
        return ctypes.windll.kernel32.GetFileAttributesW(str(path))
    return -1


def _notify_shell():
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        except Exception:
            pass


def has_our_icon(folder):
    ini = os.path.join(folder, "desktop.ini")
    try:
        with open(ini, "r", encoding="utf-8", errors="ignore") as f:
            return ICON_FILE in f.read()
    except OSError:
        return False


def set_folder_icon(folder, ico_path):
    if os.name != "nt":
        raise OSError("Folder icons are only supported on Windows.")
    os.makedirs(folder, exist_ok=True)
    icon_dest = os.path.join(folder, ICON_FILE)
    ini = os.path.join(folder, "desktop.ini")
    for p in (icon_dest, ini):
        if os.path.exists(p):
            _set_attr(p, 0x80)
    try:
        shutil.copyfile(ico_path, icon_dest)
    except shutil.SameFileError:
        pass
    with open(ini, "w", encoding="utf-8") as f:
        f.write(f"[.ShellClassInfo]\nIconResource={ICON_FILE},0\nIconFile={ICON_FILE}\nIconIndex=0\n")
    _set_attr(icon_dest, 0x2 | 0x4)
    _set_attr(ini, 0x2 | 0x4)
    attr = _get_attr(folder)
    if attr != -1:
        _set_attr(folder, attr | 0x1)
    _notify_shell()


def reset_folder_icon(folder):
    if not has_our_icon(folder):
        return
    for name in ("desktop.ini", ICON_FILE):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            _set_attr(p, 0x80)
            try:
                os.remove(p)
            except OSError:
                pass
    attr = _get_attr(folder)
    if attr != -1:
        _set_attr(folder, attr & ~0x1)
    _notify_shell()


def ensure_category_icon(root, category, cfg):
    icon = cfg["icons"].get(category)
    folder = os.path.join(root, category)
    if icon and os.path.isfile(icon) and os.path.isdir(folder) and not has_our_icon(folder):
        try:
            set_folder_icon(folder, icon)
        except OSError:
            pass


# ===== UI =====
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

CARD_STYLES = [
    {"bg": ("#E6F1FB", "#0C447C"), "fg": ("#0C447C", "#B5D4F4")},
    {"bg": ("#FAEEDA", "#633806"), "fg": ("#633806", "#FAC775")},
    {"bg": ("#EAF3DE", "#27500A"), "fg": ("#27500A", "#C0DD97")},
    {"bg": ("#F1EFE8", "#444441"), "fg": ("#444441", "#D3D1C7")},
]
INVALID_NAME_CHARS = set('\\/:*?"<>|')


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        ctk.set_appearance_mode(self.cfg["theme"])
        ctk.set_default_color_theme("blue")

        self.title("File Organizer")
        self.geometry("920x660")
        self.minsize(800, 580)

        self.folder = self.cfg.get("last_folder", "")
        self.plan = []
        self.last_moves = []
        self.busy = False

        self.var_sub = tk.BooleanVar(value=self.cfg["include_sub"])
        self.var_auto = tk.BooleanVar(value=False)
        self.var_date = tk.StringVar(value=self.cfg["date_mode"])
        self.var_rename = tk.StringVar(value=self.cfg["rename_mode"])
        self.var_theme = tk.StringVar(value=self.cfg["theme"])
        self.var_path = tk.StringVar(value=self.folder or "No folder selected")

        self._build_menu()
        self._build_ui()
        self.apply_tree_style()

        self.bind("<Control-o>", lambda e: self.select_folder())
        self.bind("<Control-r>", lambda e: self.organize())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<F5>", lambda e: self.refresh())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self._folder_ok():
            self.refresh()
        else:
            self.status.configure(text="Select a folder to begin (File > Select folder).")
        self.after(AUTO_INTERVAL_MS, self._auto_tick)

    # ------------------------------------------------------------ menus / layout
    def _build_menu(self):
        bar = tk.Menu(self)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Select folder...", accelerator="Ctrl+O", command=self.select_folder)
        m.add_command(label="Open folder in Explorer", command=self.open_folder)
        m.add_separator()
        m.add_command(label="Exit", command=self._on_close)
        bar.add_cascade(label="File", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Organize now", accelerator="Ctrl+R", command=self.organize)
        m.add_command(label="Undo last organize", accelerator="Ctrl+Z", command=self.undo)
        m.add_command(label="Refresh list", accelerator="F5", command=self.refresh)
        m.add_separator()
        m.add_checkbutton(label="Auto-organize new files", variable=self.var_auto, command=self._on_auto_toggle)
        bar.add_cascade(label="Actions", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Find duplicate files...", command=self.open_duplicates)
        m.add_command(label="Find large files...", command=self.open_large_files)
        m.add_command(label="Activity log...", command=self.open_log)
        bar.add_cascade(label="Tools", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_checkbutton(label="Include subfolders", variable=self.var_sub, command=self._on_sub_toggle)
        date_menu = tk.Menu(m, tearoff=0)
        for label, val in (("Off", "off"), ("By year", "year"), ("By year and month", "month")):
            date_menu.add_radiobutton(label=label, value=val, variable=self.var_date, command=self._on_option_change)
        m.add_cascade(label="Sort by date", menu=date_menu)
        ren_menu = tk.Menu(m, tearoff=0)
        for label, val in (("Keep original names", "off"), ("Add date prefix (2026-11-01_name)", "date"),
                           ("Add number prefix (001_name)", "number")):
            ren_menu.add_radiobutton(label=label, value=val, variable=self.var_rename, command=self._on_option_change)
        m.add_cascade(label="Rename files", menu=ren_menu)
        m.add_separator()
        m.add_command(label="Custom categories...", command=self.open_categories)
        m.add_command(label="Ignore list...", command=self.open_ignore)
        m.add_command(label="Folder icons...", command=self.open_icons)
        m.add_separator()
        theme_menu = tk.Menu(m, tearoff=0)
        for label in ("System", "Light", "Dark"):
            theme_menu.add_radiobutton(label=label, value=label, variable=self.var_theme, command=self._on_theme)
        m.add_cascade(label="Theme", menu=theme_menu)
        bar.add_cascade(label="Options", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="How it works", command=self.show_help)
        m.add_command(label="About", command=lambda: messagebox.showinfo(
            "About", "File Organizer\nSorts files into folders by type.\nBuilt with Python and customtkinter."))
        bar.add_cascade(label="Help", menu=m)

        self.configure(menu=bar)

    def _build_ui(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(16, 8))
        ctk.CTkEntry(top, textvariable=self.var_path, state="readonly", height=36).pack(
            side="left", fill="x", expand=True)
        ctk.CTkButton(top, text="Browse", width=90, height=36, command=self.select_folder).pack(side="left", padx=(8, 0))

        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.pack(fill="x", padx=16, pady=4)
        self.card_name, self.card_count = [], []
        for i, style in enumerate(CARD_STYLES):
            cards.grid_columnconfigure(i, weight=1, uniform="card")
            card = ctk.CTkFrame(cards, fg_color=style["bg"], corner_radius=10)
            card.grid(row=0, column=i, padx=(0 if i == 0 else 6, 0 if i == 3 else 6), sticky="ew")
            name = ctk.CTkLabel(card, text="-", text_color=style["fg"], font=ctk.CTkFont(size=12), anchor="w")
            name.pack(fill="x", padx=12, pady=(10, 0))
            count = ctk.CTkLabel(card, text="0", text_color=style["fg"], font=ctk.CTkFont(size=24, weight="bold"), anchor="w")
            count.pack(fill="x", padx=12, pady=(0, 10))
            self.card_name.append(name)
            self.card_count.append(count)

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.pack(fill="both", expand=True, padx=16, pady=8)
        self.tree = ttk.Treeview(mid, columns=("file", "dest"), show="headings", selectmode="browse")
        self.tree.heading("file", text="File", anchor="w")
        self.tree.heading("dest", text="Goes to", anchor="w")
        self.tree.column("file", width=520)
        self.tree.column("dest", width=240)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(0, 16))
        self.progress = ctk.CTkProgressBar(bottom, height=8)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(0, 6))
        row = ctk.CTkFrame(bottom, fg_color="transparent")
        row.pack(fill="x")
        self.status = ctk.CTkLabel(row, text="", anchor="w", text_color=("gray35", "gray70"))
        self.status.pack(side="left", fill="x", expand=True)
        ctk.CTkCheckBox(row, text="Include subfolders", variable=self.var_sub,
                        command=self._on_sub_toggle).pack(side="left", padx=8)
        ctk.CTkSwitch(row, text="Auto-organize", variable=self.var_auto,
                      command=self._on_auto_toggle).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Undo", width=80, height=36, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self.undo).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Organize now", width=130, height=36, command=self.organize).pack(side="left", padx=(4, 0))

    def apply_tree_style(self):
        dark = ctk.get_appearance_mode() == "Dark"
        bg = "#2b2b2b" if dark else "#ffffff"
        fg = "#e6e6e6" if dark else "#1a1a1a"
        head = "#3a3a3a" if dark else "#eeeeee"
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Treeview", background=bg, fieldbackground=bg, foreground=fg, rowheight=28,
                     borderwidth=0, font=("Segoe UI", 10))
        st.map("Treeview", background=[("selected", "#1f6aa5")], foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", background=head, foreground=fg, relief="flat",
                     font=("Segoe UI", 10, "bold"))
        st.map("Treeview.Heading", background=[("active", head)])

    # ------------------------------------------------------------ basic actions
    def _folder_ok(self, warn=False):
        ok = bool(self.folder) and os.path.isdir(self.folder)
        if not ok and warn:
            messagebox.showwarning("File Organizer", "Select a folder first (File > Select folder).")
        return ok

    def select_folder(self):
        path = filedialog.askdirectory(title="Choose the folder to organize")
        if path:
            self.folder = os.path.normpath(path)
            self.var_path.set(self.folder)
            self.cfg["last_folder"] = self.folder
            save_config(self.cfg)
            self.last_moves = []
            self.refresh()

    def open_folder(self):
        if self._folder_ok(warn=True) and os.name == "nt":
            os.startfile(self.folder)

    def refresh(self, status=True):
        self.tree.delete(*self.tree.get_children())
        if not self._folder_ok():
            self.plan = []
            self._update_cards()
            return
        try:
            self.plan = build_plan(self.folder, self.cfg)
        except OSError as e:
            self.plan = []
            self.status.configure(text=f"Couldn't read the folder: {e}")
        shown = self.plan[:3000]
        for item in shown:
            rel_src = os.path.relpath(item["src"], self.folder)
            self.tree.insert("", "end", values=(rel_src, item["rel"]))
        self._update_cards()
        self.progress.set(0)
        if status:
            n = len(self.plan)
            extra = f" (showing first {len(shown)})" if n > len(shown) else ""
            self.status.configure(text=f"{n} file(s) ready to organize{extra}.")

    def _update_cards(self):
        counts = Counter(p["category"] for p in self.plan)
        top = [n for n, _ in counts.most_common(3)]
        for n in self.cfg["categories"]:
            if len(top) >= 3:
                break
            if n not in top:
                top.append(n)
        for i in range(3):
            if i < len(top):
                self.card_name[i].configure(text=top[i])
                self.card_count[i].configure(text=str(counts.get(top[i], 0)))
            else:
                self.card_name[i].configure(text="-")
                self.card_count[i].configure(text="")
        rest = sum(c for k, c in counts.items() if k not in top)
        self.card_name[3].configure(text="Everything else")
        self.card_count[3].configure(text=str(rest))

    # ------------------------------------------------------------ option handlers
    def _on_sub_toggle(self):
        self.cfg["include_sub"] = self.var_sub.get()
        save_config(self.cfg)
        self.refresh()

    def _on_option_change(self):
        self.cfg["date_mode"] = self.var_date.get()
        self.cfg["rename_mode"] = self.var_rename.get()
        save_config(self.cfg)
        self.refresh()

    def _on_theme(self):
        self.cfg["theme"] = self.var_theme.get()
        save_config(self.cfg)
        ctk.set_appearance_mode(self.cfg["theme"])
        self.after(80, self.apply_tree_style)

    def _on_auto_toggle(self):
        if self.var_auto.get():
            if not self._folder_ok(warn=True):
                self.var_auto.set(False)
                return
            self.status.configure(text="Auto-organize is on: new files are sorted every few seconds.")
        else:
            self.status.configure(text="Auto-organize is off.")

    def _on_close(self):
        self.cfg["last_folder"] = self.folder
        save_config(self.cfg)
        self.destroy()

    # ------------------------------------------------------------ organize / undo
    def organize(self):
        if not self._folder_ok(warn=True) or self.busy:
            return
        self.refresh(status=False)
        if not self.plan:
            messagebox.showinfo("File Organizer", "No files to organize.")
            return
        if not messagebox.askyesno("File Organizer", f"Move {len(self.plan)} file(s) into category folders?"):
            return
        self._run_plan(self.plan, source="manual")

    def _run_plan(self, plan, source):
        self.busy = True
        self.progress.set(0)

        def cb(i, total):
            self.progress.set(i / total)
            self.status.configure(text=f"Moving files... {i} of {total}")
            if i % 5 == 0 or i == total:
                self.update_idletasks()

        moves, errors = execute_plan(self.folder, plan, self.cfg, cb)
        self.busy = False
        if moves:
            self.last_moves = moves
            append_log([log_entry("move", old, new) for new, old in moves])
        self.refresh(status=False)
        self.progress.set(1 if moves else 0)
        if source == "auto":
            self.status.configure(text=f"Auto-organized {len(moves)} new file(s).")
        else:
            self.status.configure(text=f"Done. Moved {len(moves)} file(s), {errors} error(s). Undo is available.")
            messagebox.showinfo("File Organizer", f"Moved {len(moves)} file(s).\nErrors: {errors}")

    def undo(self):
        if not self.last_moves:
            messagebox.showinfo("File Organizer", "Nothing to undo.")
            return
        restored, entries = 0, []
        for new, old in reversed(self.last_moves):
            try:
                target = restore_file(new, old)
                if target:
                    restored += 1
                    entries.append(log_entry("restore", new, target))
                    if self._folder_ok():
                        cleanup_empty_dirs(os.path.dirname(new), self.folder)
            except OSError:
                pass
        self.last_moves = []
        append_log(entries)
        self.refresh(status=False)
        self.status.configure(text=f"Undo complete. Restored {restored} file(s).")

    def _auto_tick(self):
        try:
            if self.var_auto.get() and self._folder_ok() and not self.busy:
                now = time.time()
                ready = []
                for f in collect_files(self.folder, self.cfg):
                    if os.path.splitext(f)[1].lower() in TEMP_EXTS:
                        continue
                    if now - _mtime(f) > 4:
                        ready.append(f)
                if ready:
                    self._run_plan(build_plan(self.folder, self.cfg, ready), source="auto")
        except Exception:
            pass
        finally:
            self.after(AUTO_INTERVAL_MS, self._auto_tick)

    # ------------------------------------------------------------ helpers for dialogs
    def _toplevel(self, title, w, h):
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry(f"{w}x{h}")
        win.transient(self)
        win.after(150, win.lift)
        return win

    def _make_tree(self, parent, columns, widths, selectmode="extended", show="headings"):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show=show, selectmode=selectmode)
        for (cid, label), width in zip(columns, widths):
            tree.heading(cid, text=label, anchor="w")
            tree.column(cid, width=width, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        return frame, tree

    def run_bg(self, func, done):
        box = {}

        def worker():
            try:
                box["res"] = func()
            except Exception as e:  # noqa
                box["err"] = e
            box["done"] = True

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            if box.get("done"):
                done(box.get("res"), box.get("err"))
            else:
                self.after(150, poll)

        self.after(150, poll)

    # ------------------------------------------------------------ custom categories
    def open_categories(self):
        win = self._toplevel("Custom categories", 640, 460)
        ctk.CTkLabel(win, text="Files go to the first category that lists their extension. "
                               "Anything unlisted goes to Others.",
                     wraplength=590, justify="left", text_color=("gray35", "gray70")).pack(anchor="w", padx=16, pady=(14, 6))
        frame, tree = self._make_tree(win, [("name", "Folder"), ("exts", "Extensions")], [150, 440], selectmode="browse")
        frame.pack(fill="both", expand=True, padx=16, pady=6)

        def reload():
            tree.delete(*tree.get_children())
            for name, exts in self.cfg["categories"].items():
                tree.insert("", "end", iid=name, values=(name, ", ".join(exts)))

        def commit():
            save_config(self.cfg)
            reload()
            self.refresh()

        def edit_form(existing=None):
            form = self._toplevel("Edit category" if existing else "Add category", 460, 230)
            ctk.CTkLabel(form, text="Folder name").pack(anchor="w", padx=16, pady=(14, 2))
            e_name = ctk.CTkEntry(form, height=34)
            e_name.pack(fill="x", padx=16)
            ctk.CTkLabel(form, text="Extensions (comma separated, like .pptx, .docx)").pack(anchor="w", padx=16, pady=(10, 2))
            e_exts = ctk.CTkEntry(form, height=34)
            e_exts.pack(fill="x", padx=16)
            if existing:
                e_name.insert(0, existing)
                e_exts.insert(0, ", ".join(self.cfg["categories"][existing]))

            def save():
                name = e_name.get().strip()
                exts = []
                for token in re.split(r"[,\s;]+", e_exts.get().strip().lower()):
                    token = token.strip().lstrip(".")
                    if token:
                        exts.append("." + token)
                if not name or any(c in INVALID_NAME_CHARS for c in name):
                    messagebox.showwarning("Category", "Enter a valid folder name.", parent=form)
                    return
                if name in (OTHERS, DUPES):
                    messagebox.showwarning("Category", f"'{name}' is reserved.", parent=form)
                    return
                if name != existing and name in self.cfg["categories"]:
                    messagebox.showwarning("Category", "That folder name already exists.", parent=form)
                    return
                if not exts:
                    messagebox.showwarning("Category", "Add at least one extension.", parent=form)
                    return
                cats = {}
                for k, v in self.cfg["categories"].items():
                    if k == existing:
                        cats[name] = exts
                    else:
                        cats[k] = v
                if not existing:
                    cats[name] = exts
                self.cfg["categories"] = cats
                form.destroy()
                commit()

            ctk.CTkButton(form, text="Save", command=save).pack(pady=18)

        def selected():
            sel = tree.selection()
            return sel[0] if sel else None

        def on_edit():
            name = selected()
            if name:
                edit_form(name)

        def on_remove():
            name = selected()
            if name and messagebox.askyesno("Remove", f"Remove the '{name}' category?\n"
                                                      "Its folder and files are not touched.", parent=win):
                del self.cfg["categories"][name]
                commit()

        def on_reset():
            if messagebox.askyesno("Reset", "Restore the default categories?", parent=win):
                self.cfg["categories"] = json.loads(json.dumps(DEFAULT_CATEGORIES))
                commit()

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 14))
        ctk.CTkButton(row, text="Add", width=80, command=lambda: edit_form()).pack(side="left")
        ctk.CTkButton(row, text="Edit", width=80, command=on_edit).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Remove", width=80, command=on_remove).pack(side="left")
        ctk.CTkButton(row, text="Reset to defaults", width=130, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=on_reset).pack(side="right")
        reload()

    # ------------------------------------------------------------ ignore list
    def open_ignore(self):
        win = self._toplevel("Ignore list", 440, 400)
        ctk.CTkLabel(win, text="One pattern per line. Matching files are never moved.\n"
                               "Examples: *.tmp   ~*   notes.txt",
                     justify="left", text_color=("gray35", "gray70")).pack(anchor="w", padx=16, pady=(14, 6))
        box = ctk.CTkTextbox(win, height=220)
        box.pack(fill="both", expand=True, padx=16, pady=6)
        box.insert("1.0", "\n".join(self.cfg["ignore"]))

        def save():
            self.cfg["ignore"] = [ln.strip() for ln in box.get("1.0", "end").splitlines() if ln.strip()]
            save_config(self.cfg)
            win.destroy()
            self.refresh()

        ctk.CTkButton(win, text="Save", command=save).pack(pady=(6, 14))

    # ------------------------------------------------------------ folder icons
    def open_icons(self):
        if os.name != "nt":
            messagebox.showinfo("Folder icons", "Folder icons are only supported on Windows.")
            return
        win = self._toplevel("Folder icons", 600, 560)
        ctk.CTkLabel(win, text="Pick an .ico file for each folder, then press Apply icons.",
                     text_color=("gray35", "gray70")).pack(anchor="w", padx=16, pady=(14, 4))
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=4)

        managed_label = "Managed folder"
        if self._folder_ok():
            managed_label += f" ({os.path.basename(self.folder) or self.folder})"
        keys = [("__managed__", managed_label)]
        keys += [(c, c) for c in self.cfg["categories"]] + [(OTHERS, OTHERS), (DUPES, DUPES)]

        pending, labels = {}, {}
        for key, _ in keys:
            if key == "__managed__":
                pending[key] = self.cfg["managed_icons"].get(self.folder) if self.folder else None
            else:
                pending[key] = self.cfg["icons"].get(key)

        def text_for(key):
            return os.path.basename(pending[key]) if pending[key] else "Default icon"

        def choose(key):
            path = filedialog.askopenfilename(parent=win, title="Choose an icon",
                                              filetypes=[("Icon files", "*.ico")])
            if path:
                if not path.lower().endswith(".ico"):
                    messagebox.showwarning("Folder icons", "Please choose an .ico file.", parent=win)
                    return
                pending[key] = path
                labels[key].configure(text=text_for(key))

        def clear(key):
            pending[key] = None
            labels[key].configure(text=text_for(key))

        for key, title in keys:
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text=title, width=190, anchor="w").pack(side="left")
            labels[key] = ctk.CTkLabel(row, text=text_for(key), anchor="w", width=130,
                                       text_color=("gray35", "gray70"))
            labels[key].pack(side="left", padx=6)
            ctk.CTkButton(row, text="Choose...", width=84, height=30, command=lambda k=key: choose(k)).pack(side="left")
            ctk.CTkButton(row, text="Clear", width=60, height=30, fg_color="transparent", border_width=1,
                          text_color=("gray10", "gray90"), command=lambda k=key: clear(k)).pack(side="left", padx=6)

        def store_icon(path, key):
            os.makedirs(ICON_DIR, exist_ok=True)
            if key == "__managed__":
                safe = "managed_" + hashlib.md5(self.folder.encode("utf-8")).hexdigest()[:8]
            else:
                safe = re.sub(r"[^\w\-]+", "_", key)
            dest = os.path.join(ICON_DIR, safe + ".ico")
            try:
                shutil.copyfile(path, dest)
            except shutil.SameFileError:
                pass
            return dest

        def apply():
            errors = []
            for key, _ in keys:
                path = pending[key]
                try:
                    if key == "__managed__":
                        if not self._folder_ok():
                            continue
                        store, skey, target = self.cfg["managed_icons"], self.folder, self.folder
                    else:
                        store, skey = self.cfg["icons"], key
                        target = os.path.join(self.folder, key) if self._folder_ok() else None
                    if path:
                        store[skey] = store_icon(path, key)
                        if target and os.path.isdir(target):
                            set_folder_icon(target, store[skey])
                    else:
                        store.pop(skey, None)
                        if target and os.path.isdir(target):
                            reset_folder_icon(target)
                except OSError as e:
                    errors.append(f"{key}: {e}")
            save_config(self.cfg)
            if errors:
                messagebox.showwarning("Folder icons", "Some icons couldn't be applied:\n" + "\n".join(errors), parent=win)
            else:
                messagebox.showinfo("Folder icons",
                                    "Icons applied. If a folder still shows the old icon, press F5 in "
                                    "File Explorer or restart it.", parent=win)
            win.destroy()

        def reset_all():
            for key in pending:
                pending[key] = None
                labels[key].configure(text=text_for(key))

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 14))
        ctk.CTkButton(row, text="Reset all", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=reset_all).pack(side="left")
        ctk.CTkButton(row, text="Apply icons", width=120, command=apply).pack(side="right")
        ctk.CTkButton(row, text="Cancel", width=80, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=win.destroy).pack(side="right", padx=8)

    # ------------------------------------------------------------ duplicates
    def open_duplicates(self):
        if not self._folder_ok(warn=True):
            return
        root = self.folder
        win = self._toplevel("Duplicate files", 760, 560)
        status = ctk.CTkLabel(win, text="Scanning... this can take a while for big folders.", anchor="w")
        status.pack(fill="x", padx=16, pady=(14, 4))
        bar = ctk.CTkProgressBar(win, mode="indeterminate", height=6)
        bar.pack(fill="x", padx=16)
        bar.start()
        frame, tree = self._make_tree(win, [("path", "File"), ("size", "Size")], [560, 100], show="tree headings")
        tree.column("#0", width=40, stretch=False)
        frame.pack(fill="both", expand=True, padx=16, pady=8)
        groups_holder = {"groups": []}

        def extras():
            out = []
            for _size, paths in groups_holder["groups"]:
                out.extend(paths[1:])
            return out

        def done(groups, err):
            if not win.winfo_exists():
                return
            bar.stop()
            bar.pack_forget()
            if err:
                status.configure(text=f"Scan failed: {err}")
                return
            groups_holder["groups"] = groups
            saved = sum(size * (len(p) - 1) for size, p in groups)
            status.configure(text=f"{len(groups)} group(s) of identical files. Extra copies use {human_size(saved)}. "
                                  "The oldest copy in each group is kept.")
            for i, (size, paths) in enumerate(groups, 1):
                parent = tree.insert("", "end", text="", values=(f"Group {i}: {len(paths)} identical files", human_size(size)), open=True)
                for j, p in enumerate(paths):
                    label = os.path.relpath(p, root) + ("   (kept)" if j == 0 else "")
                    tree.insert(parent, "end", text="", values=(label, ""))

        self.run_bg(lambda: find_duplicates(root), done)

        def move_extras():
            items = extras()
            if not items:
                messagebox.showinfo("Duplicates", "No extra copies to move.", parent=win)
                return
            if not messagebox.askyesno("Duplicates", f"Move {len(items)} extra copy(ies) into the '{DUPES}' folder?", parent=win):
                return
            dest_dir = os.path.join(root, DUPES)
            os.makedirs(dest_dir, exist_ok=True)
            entries, moved = [], 0
            for p in items:
                try:
                    dest = unique_path(os.path.join(dest_dir, os.path.basename(p)))
                    shutil.move(p, dest)
                    entries.append(log_entry("move", p, dest))
                    moved += 1
                except OSError:
                    pass
            append_log(entries)
            messagebox.showinfo("Duplicates", f"Moved {moved} file(s) to '{DUPES}'.", parent=win)
            win.destroy()
            self.refresh()

        def delete_extras():
            items = extras()
            if not items:
                messagebox.showinfo("Duplicates", "No extra copies to delete.", parent=win)
                return
            if not messagebox.askyesno("Delete duplicates",
                                       f"Permanently delete {len(items)} extra copy(ies)?\nThis can't be undone.",
                                       icon="warning", parent=win):
                return
            entries, deleted = [], 0
            for p in items:
                try:
                    os.remove(p)
                    entries.append(log_entry("delete", p))
                    deleted += 1
                except OSError:
                    pass
            append_log(entries)
            messagebox.showinfo("Duplicates", f"Deleted {deleted} file(s).", parent=win)
            win.destroy()
            self.refresh()

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(row, text=f"Move extra copies to '{DUPES}'", command=move_extras).pack(side="left")
        ctk.CTkButton(row, text="Delete extra copies", fg_color="#b3261e", hover_color="#8c1d18",
                      command=delete_extras).pack(side="right")

    # ------------------------------------------------------------ large files
    def open_large_files(self):
        if not self._folder_ok(warn=True):
            return
        root = self.folder
        win = self._toplevel("Large files", 760, 520)
        status = ctk.CTkLabel(win, text="Scanning...", anchor="w")
        status.pack(fill="x", padx=16, pady=(14, 4))
        frame, tree = self._make_tree(win, [("size", "Size"), ("path", "File")], [100, 600])
        frame.pack(fill="both", expand=True, padx=16, pady=8)
        paths = {}

        def scan():
            status.configure(text="Scanning...")
            tree.delete(*tree.get_children())

            def done(items, err):
                if not win.winfo_exists():
                    return
                if err:
                    status.configure(text=f"Scan failed: {err}")
                    return
                paths.clear()
                for size, p in items:
                    iid = tree.insert("", "end", values=(human_size(size), os.path.relpath(p, root)))
                    paths[iid] = p
                status.configure(text=f"Top {len(items)} largest files in this folder and its subfolders.")

            self.run_bg(lambda: find_large_files(root), done)

        def show():
            sel = tree.selection()
            if sel and os.name == "nt":
                subprocess.Popen(["explorer", f"/select,{paths[sel[0]]}"])

        def delete():
            sel = tree.selection()
            if not sel:
                return
            if not messagebox.askyesno("Delete", f"Permanently delete {len(sel)} file(s)?\nThis can't be undone.",
                                       icon="warning", parent=win):
                return
            entries = []
            for iid in sel:
                try:
                    os.remove(paths[iid])
                    entries.append(log_entry("delete", paths[iid]))
                    tree.delete(iid)
                except OSError:
                    pass
            append_log(entries)
            self.refresh()

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(row, text="Show in Explorer", width=140, command=show).pack(side="left")
        ctk.CTkButton(row, text="Rescan", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=scan).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Delete selected", fg_color="#b3261e", hover_color="#8c1d18",
                      command=delete).pack(side="right")
        scan()

    # ------------------------------------------------------------ activity log
    def open_log(self):
        win = self._toplevel("Activity log", 900, 540)
        ctk.CTkLabel(win, text="Everything the app moved or deleted. Select moves and press Restore to put them back.",
                     anchor="w", text_color=("gray35", "gray70")).pack(fill="x", padx=16, pady=(14, 4))
        frame, tree = self._make_tree(win, [("time", "Time"), ("action", "Action"), ("src", "From"), ("dst", "To")],
                                      [140, 70, 330, 330])
        frame.pack(fill="both", expand=True, padx=16, pady=8)
        entries = {}

        def reload():
            tree.delete(*tree.get_children())
            entries.clear()
            for i, e in enumerate(read_log()):
                iid = str(i)
                entries[iid] = e
                tree.insert("", "end", iid=iid, values=(e.get("t", ""), e.get("action", ""), e.get("src", ""), e.get("dst", "")))

        def restore():
            sel = [entries[i] for i in tree.selection() if entries[i].get("action") == "move"]
            if not sel:
                messagebox.showinfo("Restore", "Select one or more 'move' entries first.", parent=win)
                return
            restored, out = 0, []
            for e in sel:
                try:
                    target = restore_file(e["dst"], e["src"])
                    if target:
                        restored += 1
                        out.append(log_entry("restore", e["dst"], target))
                        if self._folder_ok():
                            cleanup_empty_dirs(os.path.dirname(e["dst"]), self.folder)
                except OSError:
                    pass
            append_log(out)
            messagebox.showinfo("Restore", f"Restored {restored} of {len(sel)} file(s).", parent=win)
            reload()
            self.refresh()

        def clear():
            if messagebox.askyesno("Clear log", "Delete the whole activity log?", parent=win):
                clear_log()
                reload()

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(row, text="Restore selected", width=140, command=restore).pack(side="left")
        ctk.CTkButton(row, text="Refresh", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=reload).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Clear log", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=clear).pack(side="right")
        reload()

    # ------------------------------------------------------------ help
    def show_help(self):
        messagebox.showinfo(
            "How it works",
            "1. File > Select folder to choose the folder you want to manage.\n"
            "2. The list shows where each file will go. Nothing moves until you press Organize now.\n"
            "3. Options lets you sort by date, rename files, add custom categories, "
            "set an ignore list and change folder icons.\n"
            "4. Undo puts the last batch back. The Activity log can restore older moves.\n"
            "5. Auto-organize sorts new files in the folder every few seconds.")


if __name__ == "__main__":
    App().mainloop()