"""
File Organizer
Sorts files into folders (Pictures, Videos, Docs, ...) with a modern UI.

Features: two-view preview (items / organized folders), double-click and right-click
on folders, folder icons picked from the project's "icons" folder, auto-manage folders
in the background with a system tray icon, start with Windows, pop-up notifications
with an "open folder" link, custom categories, sort by date, rename by pattern, ignore
list, duplicate finder, large files finder, activity log with restore, undo,
light/dark theme.

Requires:  pip install customtkinter pystray pillow
Icons:     put your .ico files in an "icons" folder next to this file (they get bundled into the .exe).
           Users of the .exe can add their own in %APPDATA%\\FileOrganizer\\my-icons (Help > Open icons folder).
Build exe: python -m PyInstaller --onefile --windowed --collect-all customtkinter --hidden-import pystray._win32 --add-data "icons;icons" --name FileOrganizer file_organizer.py
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import zlib
import hashlib
import fnmatch
import threading
import subprocess
import ctypes
from datetime import datetime
from collections import Counter, defaultdict

APP_NAME = "FileOrganizer"
OTHERS = "Others"
DUPES = "Duplicates"
ICON_FILE = ".foldericon.ico"
TEMP_EXTS = {".crdownload", ".part", ".tmp", ".download", ".partial"}
WATCH_INTERVAL_MS = 5000
NOTIFY_SECONDS = 3
SKIP_NAMES = {"desktop.ini", ICON_FILE}
BACKGROUND_START = "--background" in sys.argv
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

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
    "date_mode": "off",          # off | year | month
    "rename_mode": "off",        # off | date | number
    "include_sub": False,
    "icons": {},                 # category name -> stored .ico path; only set by "apply to all folders"
    "folder_icons": {},          # normalised folder path -> {"icon", "name"}, or None = no icon on purpose
    "known_folders": [],         # category folders the app created or manages (for the icon scope list)
    "icon_scheme": 2,
    "excluded": [],              # files/folders that are never organized
    "exclude_restored": True,    # restored files are added to the exclusion list
    "watch_folders": [],         # [{"path": str, "enabled": bool}]
    "bg_enabled": True,          # master switch for background organizing
    "start_with_windows": False,
    "close_to_tray": True,
    "notify": True,
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
            data = json.load(f)
        if data.get("icon_scheme") != 2:
            # older versions applied one icon to every folder with the same name; start clean
            data["icons"] = {}
            data.pop("managed_icons", None)
            data.pop("icon_names", None)
            data["icon_scheme"] = 2
        cfg.update(data)
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


def short_text(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def short_path(path, limit):
    """Shorten a long path from the left, so the folders that tell places apart stay visible."""
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


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


def raw_files(root, cfg):
    """Every file the app could touch, before the ignore list is applied."""
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
    return [p for p in found if os.path.basename(p).lower() not in SKIP_NAMES]


def collect_files(root, cfg):
    return [p for p in raw_files(root, cfg) if not skip_reason(p, cfg)]


def is_busy_file(path):
    """True if another program still has the file open (renaming it onto itself fails then)."""
    try:
        os.rename(path, path)
        return False
    except OSError:
        return True


def count_files(path, limit=100000):
    n = 0
    for _dp, _dn, fn in os.walk(path):
        n += sum(1 for f in fn if f.lower() not in SKIP_NAMES)
        if n >= limit:
            break
    return n


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
    """Moves files. Returns (moves, errors); each move is (new_path, old_path, category)."""
    moves, errors = [], 0
    total = len(plan)
    for i, item in enumerate(plan, 1):
        try:
            os.makedirs(item["dest_dir"], exist_ok=True)
            ensure_category_icon(root, item["category"], cfg)
            dest = unique_path(os.path.join(item["dest_dir"], item["name"]))
            shutil.move(item["src"], dest)
            moves.append((dest, item["src"], item["category"]))
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


def build_notifications(root, moves, max_popups=3):
    """Turn a batch of moves into pop-up descriptions (one per category folder)."""
    groups = defaultdict(list)
    for dest, _src, cat in moves:
        groups[cat].append(dest)
    notes = []
    if not groups:
        return notes
    if len(groups) > max_popups:
        notes.append({"title": "Files organized",
                      "text": f"{len(moves)} files moved into {len(groups)} folders",
                      "folder": root, "select": list(groups)})
        return notes
    for cat, dests in groups.items():
        dirs = {os.path.dirname(d) for d in dests}
        if len(dirs) == 1:
            folder = next(iter(dirs))
            select = [os.path.basename(d) for d in dests]
        else:
            folder = os.path.join(root, cat)
            select = []
        if len(dests) == 1:
            text = f"{short_text(os.path.basename(dests[0]), 40)} moved to {cat}"
        else:
            text = f"{len(dests)} files moved to {cat}"
        notes.append({"title": "Files organized", "text": text, "folder": folder, "select": select})
    return notes


def unsafe_watch_folder(path):
    """Returns a reason string if a folder should never be auto-managed, else ''."""
    p = os.path.normpath(path)
    if os.path.dirname(p) == p:
        return "a whole drive"
    low = os.path.normcase(p)
    for env in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)"):
        bad = os.environ.get(env)
        if bad and low.startswith(os.path.normcase(os.path.normpath(bad))):
            return "a system folder"
    if low == os.path.normcase(os.path.expanduser("~")):
        return "your whole user folder"
    return ""


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


# ---------- icon library (the "icons" folder of the project) ----------

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def user_icons_dir():
    """Where users drop their own .ico files: %APPDATA%\\FileOrganizer\\my-icons (C: drive, out of sight)."""
    return os.path.join(CONFIG_DIR, "my-icons")


def icon_library_dirs():
    """Folders searched for icons: the user's own folder, one next to the app, and the copy bundled in the .exe."""
    dirs = [user_icons_dir(), os.path.join(app_dir(), "icons")]
    bundled = os.path.join(getattr(sys, "_MEIPASS", app_dir()), "icons")
    if bundled not in dirs:
        dirs.append(bundled)
    return dirs


def list_library_icons():
    found = {}
    for d in reversed(icon_library_dirs()):  # earlier folders win: user's own, then next to the app, then bundled
        try:
            for n in os.listdir(d):
                if n.lower().endswith(".ico"):
                    found[n.lower()] = (n, os.path.join(d, n))
        except OSError:
            pass
    return sorted(found.values(), key=lambda t: t[0].lower())


def store_icon(path):
    """Copy an icon into the app's own storage (named by its content) so folder icons survive if the source moves."""
    os.makedirs(ICON_DIR, exist_ok=True)
    with open(path, "rb") as f:
        data = f.read()
    dest = os.path.join(ICON_DIR, "ico_" + hashlib.md5(data).hexdigest()[:12] + ".ico")
    if not os.path.exists(dest):
        with open(dest, "wb") as f:
            f.write(data)
    return dest


# ---------- Windows helpers ----------

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


def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


def register_category_folders(cfg, root, names):
    """Remember the category folders the app creates or manages. Returns True if the list grew."""
    known = cfg.setdefault("known_folders", [])
    have = {norm_path(p) for p in known}
    grew = False
    for name in names:
        p = os.path.normpath(os.path.join(root, name))
        if norm_path(p) not in have:
            known.append(p)
            have.add(norm_path(p))
            grew = True
    return grew


def same_name_folders(known, name, current):
    """The folder being edited first, then every other known folder with the same name that still exists."""
    first = os.path.normpath(current)
    out, have = [first], {norm_path(first)}
    for p in sorted(known, key=lambda x: x.lower()):
        q = os.path.normpath(p)
        if os.path.basename(q).lower() == name.lower() and os.path.isdir(q) and norm_path(q) not in have:
            out.append(q)
            have.add(norm_path(q))
    return out


def is_excluded(path, excluded):
    """True if the path, or any folder above it, is in the exclusion list."""
    p = norm_path(path)
    for e in excluded:
        ne = norm_path(e)
        if p == ne or p.startswith(ne.rstrip(os.sep) + os.sep):
            return True
    return False


def add_exclusions(cfg, paths):
    """Add files or folders to the exclusion list. Returns how many were new."""
    ex = cfg.setdefault("excluded", [])
    have = {norm_path(p) for p in ex}
    added = 0
    for p in paths:
        key = norm_path(p)
        if key not in have:
            ex.append(os.path.normpath(p))
            have.add(key)
            added += 1
    return added


def skip_reason(path, cfg):
    """Why a file is left alone: 'ignore list', 'excluded', or '' if it can be organized."""
    if is_ignored(os.path.basename(path), cfg["ignore"]):
        return "ignore list"
    if is_excluded(path, cfg.get("excluded", [])):
        return "excluded"
    return ""


def register_existing_categories(cfg, root):
    """Remember the category folders that already exist inside a root folder."""
    names = [c for c in list(cfg["categories"]) + [OTHERS, DUPES] if os.path.isdir(os.path.join(root, c))]
    return register_category_folders(cfg, root, names)


def find_named_folders(base, name, max_depth=3, limit=300):
    """Folders called `name` inside base (up to max_depth levels down). Used to find older app folders."""
    base = os.path.normpath(base)
    out = []
    start = base.rstrip(os.sep).count(os.sep)
    for dp, dn, _fn in os.walk(base):
        depth = dp.rstrip(os.sep).count(os.sep) - start
        for d in dn:
            if d.lower() == name.lower():
                out.append(os.path.join(dp, d))
        dn[:] = [d for d in dn if d.lower() != name.lower() and not d.startswith((".", "$"))]
        if depth >= max_depth - 1 or len(out) >= limit:
            dn[:] = []
        if len(out) >= limit:
            break
    return out


def ensure_category_icon(root, category, cfg):
    """Give a category folder its icon after the app creates it.
    A folder's own choice (even 'no icon') wins; otherwise the 'all folders named X' default is used."""
    folder = os.path.join(root, category)
    entry = cfg.get("folder_icons", {}).get(norm_path(folder), "unset")
    if isinstance(entry, dict):
        icon = entry.get("icon")
    elif entry is None:
        icon = None
    else:
        icon = cfg["icons"].get(category)
    if icon and os.path.isfile(icon) and os.path.isdir(folder) and not has_our_icon(folder):
        try:
            set_folder_icon(folder, icon)
        except OSError:
            pass


def _shell_select(folder, names):
    """Open a folder in Explorer with several items selected (Windows shell API)."""
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILFindLastID.restype = ctypes.c_void_p
        shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
        shell32.ILFree.restype = None
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                       ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitialize(None)
        folder_pidl = shell32.ILCreateFromPathW(os.path.normpath(folder))
        if not folder_pidl:
            return False
        full, children = [], []
        for n in names:
            pidl = shell32.ILCreateFromPathW(os.path.normpath(os.path.join(folder, n)))
            if pidl:
                full.append(pidl)
                children.append(shell32.ILFindLastID(pidl))
        ok = False
        if children:
            arr = (ctypes.c_void_p * len(children))(*children)
            ok = shell32.SHOpenFolderAndSelectItems(folder_pidl, len(children), arr, 0) == 0
        for p in full:
            shell32.ILFree(p)
        shell32.ILFree(folder_pidl)
        return ok
    except Exception:
        return False


def open_in_explorer(folder, select=None):
    """Open a folder in File Explorer, optionally selecting items (names inside that folder)."""
    if os.name != "nt" or not folder or not os.path.isdir(folder):
        return
    select = [n for n in (select or []) if os.path.exists(os.path.join(folder, n))]
    if select:
        if _shell_select(folder, select):
            return
        subprocess.Popen(["explorer", f"/select,{os.path.normpath(os.path.join(folder, select[0]))}"])
    else:
        os.startfile(folder)


def open_icons_folder():
    path = user_icons_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    if os.name == "nt" and os.path.isdir(path):
        os.startfile(path)


def startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return f'"{exe}" "{os.path.abspath(__file__)}" --background'


def set_startup(enabled):
    """Add or remove the app from this user's Windows login items. Returns True on success."""
    if os.name != "nt":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


_mutex_handle = None


def already_running():
    """True if another copy of the app is already running (Windows only)."""
    global _mutex_handle
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        _mutex_handle = k32.CreateMutexW(None, False, "Local\\FileOrganizerSingleInstance")
        return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def work_area(root):
    """(left, top, right, bottom) of the screen without the taskbar."""
    if os.name == "nt":
        try:
            from ctypes import wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


# ===== UI =====
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk
from PIL import Image, ImageDraw

try:
    import pystray
    HAS_TRAY = True
except Exception:
    pystray = None
    HAS_TRAY = False

CARD_STYLES = [
    {"bg": ("#E6F1FB", "#0C447C"), "fg": ("#0C447C", "#B5D4F4")},
    {"bg": ("#FAEEDA", "#633806"), "fg": ("#633806", "#FAC775")},
    {"bg": ("#EAF3DE", "#27500A"), "fg": ("#27500A", "#C0DD97")},
    {"bg": ("#F1EFE8", "#444441"), "fg": ("#444441", "#D3D1C7")},
]
FOLDER_COLORS = ["#378ADD", "#BA7517", "#639922", "#7F77DD", "#D85A30", "#1D9E75", "#D4537E", "#888780"]
INVALID_NAME_CHARS = set('\\/:*?"<>|')
VIEW_ITEMS = "Items in folder"
VIEW_FOLDERS = "Organized folders"
FOLDER_COLS = 4
ACCENT = "#1f6aa5"
NEUTRAL_BORDER = ("gray75", "gray35")


def darken(hex_color, factor=0.78):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02x%02x%02x" % (int(r * factor), int(g * factor), int(b * factor))


def make_folder_glyph(color, size=64):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size / 64.0
    d.rounded_rectangle([4 * s, 12 * s, 28 * s, 26 * s], radius=3 * s, fill=darken(color))
    d.rounded_rectangle([4 * s, 20 * s, 60 * s, 54 * s], radius=5 * s, fill=color)
    return im


def folder_color(name):
    return FOLDER_COLORS[zlib.crc32(name.encode("utf-8")) % len(FOLDER_COLORS)]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        ctk.set_appearance_mode(self.cfg["theme"])
        ctk.set_default_color_theme("blue")

        self.title("File Organizer")
        self.geometry("980x720")
        self.minsize(860, 620)

        self.folder = self.cfg.get("last_folder", "")
        self.plan = []
        self.last_moves = []
        self.recent = Counter()
        self.item_paths = {}
        self.item_reason = {}
        self.tile_widgets = []
        self.toasts = []
        self._img_cache = {}
        self._tray_hint_shown = False
        self.busy = False
        self.tray = None
        self.watch_thread = None
        self.work_lock = threading.Lock()
        self.ui_queue = queue.Queue()

        self.var_sub = tk.BooleanVar(value=self.cfg["include_sub"])
        self.var_date = tk.StringVar(value=self.cfg["date_mode"])
        self.var_rename = tk.StringVar(value=self.cfg["rename_mode"])
        self.var_theme = tk.StringVar(value=self.cfg["theme"])
        self.var_path = tk.StringVar(value=self.folder or "No folder selected")
        self.var_bg = tk.BooleanVar(value=self.cfg["bg_enabled"])
        self.var_startup = tk.BooleanVar(value=self.cfg["start_with_windows"])
        self.var_tray = tk.BooleanVar(value=self.cfg["close_to_tray"])
        self.var_notify = tk.BooleanVar(value=self.cfg["notify"])
        self.var_exclude_restored = tk.BooleanVar(value=self.cfg["exclude_restored"])

        self._build_menu()
        self._build_ui()
        self.apply_tree_style()

        self.bind("<Control-o>", lambda e: self.select_folder())
        self.bind("<Control-r>", lambda e: self.organize())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<F5>", lambda e: self.refresh())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        found_new = False
        for w in self.cfg["watch_folders"]:
            if os.path.isdir(w.get("path", "")):
                found_new |= register_existing_categories(self.cfg, w["path"])
        if found_new:
            save_config(self.cfg)
        if self._folder_ok():
            self.refresh()
        else:
            self.status.configure(text="Select a folder to begin (File > Select folder).")
            self.refresh_folders()
        self._update_bg_label()

        self._start_tray()
        if self.cfg["start_with_windows"]:
            set_startup(True)  # keeps the login entry pointing at this copy of the app
        if BACKGROUND_START and self.tray:
            self.withdraw()

        self.after(200, self._poll_queue)
        self.after(WATCH_INTERVAL_MS, self._watch_tick)

    # ------------------------------------------------------------ menus / layout
    def _build_menu(self):
        bar = tk.Menu(self)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Select folder...", accelerator="Ctrl+O", command=self.select_folder)
        m.add_command(label="Open folder in Explorer", command=lambda: self.open_path(self.folder))
        m.add_separator()
        m.add_command(label="Exit", command=self._quit)
        bar.add_cascade(label="File", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Organize now", accelerator="Ctrl+R", command=self.organize)
        m.add_command(label="Undo last organize", accelerator="Ctrl+Z", command=self.undo)
        m.add_command(label="Refresh", accelerator="F5", command=self.refresh)
        bar.add_cascade(label="Actions", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Find duplicate files...", command=self.open_duplicates)
        m.add_command(label="Find large files...", command=self.open_large_files)
        m.add_command(label="Activity log...", command=self.open_log)
        bar.add_cascade(label="Tools", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Auto-manage folders...", command=self.open_watch)
        m.add_checkbutton(label="Auto-organize in background", variable=self.var_bg, command=self._on_bg_toggle)
        m.add_separator()
        m.add_checkbutton(label="Start with Windows", variable=self.var_startup, command=self._on_startup_toggle)
        m.add_checkbutton(label="Keep running in the tray when the window is closed",
                          variable=self.var_tray, command=self._on_tray_toggle)
        m.add_checkbutton(label="Show pop-up notifications", variable=self.var_notify, command=self._on_notify_toggle)
        m.add_separator()
        m.add_command(label="Send a test notification", command=self.test_notification)
        bar.add_cascade(label="Background", menu=m)

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
        m.add_command(label="Exclusions...", command=self.open_exclusions)
        m.add_command(label="Folder icons...", command=self.open_icons)
        m.add_separator()
        theme_menu = tk.Menu(m, tearoff=0)
        for label in ("System", "Light", "Dark"):
            theme_menu.add_radiobutton(label=label, value=label, variable=self.var_theme, command=self._on_theme)
        m.add_cascade(label="Theme", menu=theme_menu)
        bar.add_cascade(label="Options", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="How it works", command=self.show_help)
        m.add_command(label="Open icons folder", command=open_icons_folder)
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
            name.pack(fill="x", padx=12, pady=(8, 0))
            count = ctk.CTkLabel(card, text="0", text_color=style["fg"], font=ctk.CTkFont(size=22, weight="bold"), anchor="w")
            count.pack(fill="x", padx=12, pady=(0, 8))
            self.card_name.append(name)
            self.card_count.append(count)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(8, 0))
        self.view_btn = ctk.CTkSegmentedButton(bar, values=[VIEW_ITEMS, VIEW_FOLDERS], command=self._switch_view)
        self.view_btn.pack(side="left")
        self.view_btn.set(VIEW_ITEMS)
        self.view_hint = ctk.CTkLabel(bar, text="", text_color=("gray35", "gray70"), anchor="w")
        self.view_hint.pack(side="left", padx=12)

        self.mid = ctk.CTkFrame(self, fg_color="transparent")
        self.mid.pack(fill="both", expand=True, padx=16, pady=8)

        self.items_frame = ctk.CTkFrame(self.mid, fg_color="transparent")
        self.tree = ttk.Treeview(self.items_frame, columns=("file", "dest"), show="headings", selectmode="browse")
        self.tree.heading("file", text="File", anchor="w")
        self.tree.heading("dest", text="Goes to", anchor="w")
        self.tree.column("file", width=560)
        self.tree.column("dest", width=240)
        self.tree.tag_configure("skipped", foreground="#888888")
        self.tree.bind("<Double-1>", self._on_item_double)
        self.tree.bind("<Button-3>", self._on_item_menu)
        sb = ttk.Scrollbar(self.items_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        self.folders_frame = ctk.CTkScrollableFrame(self.mid, fg_color="transparent")
        for c in range(FOLDER_COLS):
            self.folders_frame.grid_columnconfigure(c, weight=1, uniform="tile")

        self.items_frame.pack(fill="both", expand=True)
        self._update_view_hint(VIEW_ITEMS)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(0, 16))
        self.progress = ctk.CTkProgressBar(bottom, height=8)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(0, 6))
        row = ctk.CTkFrame(bottom, fg_color="transparent")
        row.pack(fill="x")
        self.status = ctk.CTkLabel(row, text="", anchor="w", text_color=("gray35", "gray70"))
        self.status.pack(side="left", fill="x", expand=True)
        self.bg_label = ctk.CTkLabel(row, text="", text_color=("gray35", "gray70"))
        self.bg_label.pack(side="left", padx=8)
        ctk.CTkCheckBox(row, text="Include subfolders", variable=self.var_sub,
                        command=self._on_sub_toggle).pack(side="left", padx=8)
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
        st.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", background=head, foreground=fg, relief="flat",
                     font=("Segoe UI", 10, "bold"))
        st.map("Treeview.Heading", background=[("active", head)])

    # ------------------------------------------------------------ views
    def _update_view_hint(self, value):
        if value == VIEW_ITEMS:
            self.view_hint.configure(text="Everything in the folder and where it will go. Double-click a file to show it in Explorer.")
        else:
            self.view_hint.configure(text="Double-click a folder to open it. Right-click for more options.")

    def _switch_view(self, value):
        if value == VIEW_ITEMS:
            self.folders_frame.pack_forget()
            self.items_frame.pack(fill="both", expand=True)
        else:
            self.items_frame.pack_forget()
            self.folders_frame.pack(fill="both", expand=True)
        self._update_view_hint(value)

    def show_view(self, value):
        self.view_btn.set(value)
        self._switch_view(value)

    def _on_item_double(self, _event):
        sel = self.tree.selection()
        if sel and sel[0] in self.item_paths:
            p = self.item_paths[sel[0]]
            open_in_explorer(os.path.dirname(p), [os.path.basename(p)])

    def _on_item_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self.item_paths:
            return
        self.tree.selection_set(iid)
        path = self.item_paths[iid]
        why = self.item_reason.get(iid, "")
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Show in File Explorer",
                      command=lambda: open_in_explorer(os.path.dirname(path), [os.path.basename(path)]))
        m.add_separator()
        if why == "excluded":
            m.add_command(label="Include in organizing again", command=lambda: self.include_again(path))
        elif not why:
            m.add_command(label="Exclude this file from organizing", command=lambda: self.exclude_paths([path]))
            folder = os.path.dirname(path)
            if os.path.normcase(os.path.normpath(folder)) != os.path.normcase(os.path.normpath(self.folder)):
                m.add_command(label=f"Exclude the folder '{os.path.basename(folder)}' (everything inside)",
                              command=lambda: self.exclude_paths([folder]))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def exclude_paths(self, paths):
        added = add_exclusions(self.cfg, paths)
        save_config(self.cfg)
        self.refresh(status=False)
        self.status.configure(text=f"{added} item(s) excluded from organizing. Manage them under Options > Exclusions.")

    def include_again(self, path):
        key = norm_path(path)
        before = len(self.cfg["excluded"])
        self.cfg["excluded"] = [e for e in self.cfg["excluded"] if norm_path(e) != key]
        if len(self.cfg["excluded"]) == before:
            owner = next((e for e in self.cfg["excluded"] if is_excluded(path, [e])), "")
            messagebox.showinfo("Exclusions", f"This file is excluded because its folder is excluded:\n{owner}\n\n"
                                              "Remove that folder under Options > Exclusions.")
            return
        save_config(self.cfg)
        self.refresh()

    def open_path(self, path):
        if path and os.path.isdir(path):
            open_in_explorer(path)
        else:
            messagebox.showwarning("File Organizer", "That folder isn't there any more.")

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
            self.recent = Counter()
            self.refresh()
            self.show_view(VIEW_ITEMS)

    def refresh(self, status=True):
        self.tree.delete(*self.tree.get_children())
        self.item_paths = {}
        self.item_reason = {}
        if not self._folder_ok():
            self.plan = []
            self._update_cards()
            self.refresh_folders()
            return
        skipped = []
        try:
            self.plan = build_plan(self.folder, self.cfg)
            for p in raw_files(self.folder, self.cfg):
                why = skip_reason(p, self.cfg)
                if why:
                    skipped.append((p, why))
        except OSError as e:
            self.plan = []
            self.status.configure(text=f"Couldn't read the folder: {e}")
        limit = 3000
        shown = 0
        for item in self.plan[:limit]:
            iid = self.tree.insert("", "end", values=(os.path.relpath(item["src"], self.folder), item["rel"]))
            self.item_paths[iid] = item["src"]
            shown += 1
        for p, why in skipped[:max(0, limit - shown)]:
            iid = self.tree.insert("", "end", values=(os.path.relpath(p, self.folder), f"Skipped ({why})"),
                                   tags=("skipped",))
            self.item_paths[iid] = p
            self.item_reason[iid] = why
        self._update_cards()
        self.refresh_folders()
        self.progress.set(0)
        if status:
            n = len(self.plan)
            extra = f", {len(skipped)} skipped" if skipped else ""
            more = f" (showing first {limit})" if n + len(skipped) > limit else ""
            self.status.configure(text=f"{n} file(s) ready to organize{extra}{more}.")

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

    # ------------------------------------------------------------ organized folders (tiles)
    def load_icon_image(self, path, size):
        try:
            key = (path, size, os.path.getmtime(path))
        except OSError:
            key = (path, size, 0)
        if key in self._img_cache:
            return self._img_cache[key]
        try:
            im = Image.open(path).convert("RGBA").resize((size, size))
        except Exception:
            im = make_folder_glyph("#888780")
        img = ctk.CTkImage(light_image=im, dark_image=im, size=(size, size))
        self._img_cache[key] = img
        return img

    def folder_image(self, name, path, size=52):
        """The tile picture: the icon the folder really has, else a drawn folder."""
        icon_file = os.path.join(path, ICON_FILE)
        if has_our_icon(path) and os.path.isfile(icon_file):
            return self.load_icon_image(icon_file, size)
        key = ("glyph", name, size)
        if key not in self._img_cache:
            im = make_folder_glyph(folder_color(name))
            self._img_cache[key] = ctk.CTkImage(light_image=im, dark_image=im, size=(size, size))
        return self._img_cache[key]

    def refresh_folders(self):
        for w in self.tile_widgets:
            try:
                w.destroy()
            except Exception:
                pass
        self.tile_widgets = []
        if not self._folder_ok():
            names = []
        else:
            candidates = list(self.cfg["categories"]) + [OTHERS, DUPES]
            names = [c for c in candidates if os.path.isdir(os.path.join(self.folder, c))]
            if register_category_folders(self.cfg, self.folder, names):
                save_config(self.cfg)
        if not names:
            hint = ctk.CTkLabel(self.folders_frame, text_color=("gray35", "gray70"),
                                text="No organized folders yet.\nPress Organize now and they will show up here.")
            hint.grid(row=0, column=0, columnspan=FOLDER_COLS, pady=40)
            self.tile_widgets.append(hint)
            return
        for i, name in enumerate(names):
            self._make_tile(i // FOLDER_COLS, i % FOLDER_COLS, name)

    def _make_tile(self, r, c, name):
        path = os.path.join(self.folder, name)
        count = count_files(path)
        new = self.recent.get(name, 0)
        tile = ctk.CTkFrame(self.folders_frame, corner_radius=10, border_width=2 if new else 1,
                            border_color=ACCENT if new else NEUTRAL_BORDER)
        tile.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
        pic = ctk.CTkLabel(tile, text="", image=self.folder_image(name, path))
        pic.pack(pady=(14, 4))
        title = ctk.CTkLabel(tile, text=short_text(name, 18), font=ctk.CTkFont(size=13, weight="bold"))
        title.pack()
        info = f"{count} item(s)" + (f"   +{new} new" if new else "")
        sub = ctk.CTkLabel(tile, text=info, font=ctk.CTkFont(size=11),
                           text_color=(ACCENT, "#6cb0ff") if new else ("gray35", "gray70"))
        sub.pack(pady=(0, 12))
        for w in (tile, pic, title, sub):
            w.bind("<Double-Button-1>", lambda e, p=path: self.open_path(p))
            w.bind("<Button-3>", lambda e, n=name: self._tile_menu(e, n))
        self.tile_widgets.append(tile)

    def _tile_menu(self, event, name):
        path = os.path.join(self.folder, name)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Open in File Explorer", command=lambda: self.open_path(path))
        m.add_separator()
        m.add_command(label="Edit folder icon...", command=lambda: self.edit_folder_icon(name))
        m.add_command(label="Reset icon...", command=lambda: self.reset_folder_icon_for(name))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def edit_folder_icon(self, name):
        path = os.path.join(self.folder, name)

        def picked(lib_path, lib_name):
            self.open_icon_scope(name, path, lib_path, lib_name)

        self.pick_library_icon(picked, f"Icon for {name}")

    def reset_folder_icon_for(self, name):
        self.open_icon_scope(name, os.path.join(self.folder, name), None, None)

    # ------------------------------------------------------------ icon bookkeeping
    def current_icon_for(self, path):
        """The icon a folder shows right now, as {'path', 'name'}, or None."""
        entry = self.cfg["folder_icons"].get(norm_path(path), "unset")
        if isinstance(entry, dict):
            return {"path": entry["icon"], "name": entry["name"]}
        if entry is None:
            return None
        icon_file = os.path.join(path, ICON_FILE)
        if has_our_icon(path) and os.path.isfile(icon_file):
            return {"path": icon_file, "name": "Custom icon"}
        return None

    def apply_icon_to(self, paths, lib_path, lib_name, default_for=None):
        """Give each folder in paths the icon. default_for=<name> also makes it the default for new folders
        with that name ('all' scope). Returns a list of error messages."""
        try:
            stored = store_icon(lib_path)
        except OSError as e:
            return [f"Couldn't read the icon: {e}"]
        errors = []
        if default_for:
            self.cfg["icons"][default_for] = stored
        for p in paths:
            self.cfg["folder_icons"][norm_path(p)] = {"icon": stored, "name": lib_name}
            try:
                if os.path.isdir(p):
                    set_folder_icon(p, stored)
            except OSError as e:
                errors.append(f"{p}: {e}")
        save_config(self.cfg)
        return errors

    def reset_icons(self, paths, drop_default_for=None):
        """Remove the icon from each folder in paths. drop_default_for=<name> also removes the 'all' default."""
        errors = []
        if drop_default_for:
            self.cfg["icons"].pop(drop_default_for, None)
        for p in paths:
            try:
                reset_folder_icon(p)
            except OSError as e:
                errors.append(f"{p}: {e}")
            key = norm_path(p)
            if self.cfg["icons"].get(os.path.basename(os.path.normpath(p))):
                self.cfg["folder_icons"][key] = None   # keep it bare even though a default exists
            else:
                self.cfg["folder_icons"].pop(key, None)
        save_config(self.cfg)
        return errors

    def open_icon_scope(self, name, current_path, lib_path, lib_name):
        """Ask which folders an icon change applies to: this one, several ticked ones, or every folder with this name."""
        resetting = lib_path is None
        folders = same_name_folders(self.cfg["known_folders"], name, current_path)
        win = self._toplevel(("Reset icon: " if resetting else "Apply icon: ") + name, 700, 720)

        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 6))
        if resetting:
            ctk.CTkLabel(head, text=f"Remove the custom icon from '{name}' folders",
                         font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        else:
            img = self.load_icon_image(lib_path, 40)
            win._keep_img = img
            ctk.CTkLabel(head, text="", image=img).pack(side="left")
            ctk.CTkLabel(head, text=f"Use {lib_name} for:", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left", padx=10)

        var = tk.StringVar(value="single")
        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16)
        muted = ("gray35", "gray70")
        list_holder = ctk.CTkFrame(body, fg_color="transparent")
        sel_all_var = tk.BooleanVar(value=False)
        state = {"rows": [], "checks": []}
        ticked = {norm_path(folders[0])}   # the folder you right-clicked starts ticked

        def sync():
            sel_all_var.set(bool(state["checks"]) and all(v.get() for _, v in state["checks"]))

        def toggle_all():
            val = sel_all_var.get()
            for _, v in state["checks"]:
                v.set(val)

        sel_all = ctk.CTkCheckBox(list_holder, text="", variable=sel_all_var, command=toggle_all)
        listbox = ctk.CTkScrollableFrame(list_holder, height=230)
        find_row = ctk.CTkFrame(list_holder, fg_color="transparent")
        find_note = ctk.CTkLabel(find_row, text="", text_color=muted, anchor="w")
        extra = "" if resetting else " and the ones the app creates later"
        all_radio = ctk.CTkRadioButton(body, text="", variable=var, value="all", command=lambda: update_list())

        def rebuild():
            for r in state["rows"]:
                r.destroy()
            state["rows"], state["checks"] = [], []
            for i, p in enumerate(folders):
                v = tk.BooleanVar(value=norm_path(p) in ticked)
                row = ctk.CTkFrame(listbox, fg_color="transparent")
                row.pack(fill="x", pady=(2, 6))
                parent_name = os.path.basename(os.path.dirname(p)) or p
                label = f"{parent_name}  ›  {name}" + ("    (the one you right-clicked)" if i == 0 else "")
                ctk.CTkCheckBox(row, text=label, variable=v, command=sync).pack(anchor="w")
                ctk.CTkLabel(row, text=p, text_color=muted, font=ctk.CTkFont(size=11), anchor="w",
                             justify="left", wraplength=560).pack(anchor="w", padx=(28, 0))
                state["rows"].append(row)
                state["checks"].append((p, v))
            sel_all.configure(text=f"Select all ({len(folders)})")
            all_radio.configure(text=f"All folders named '{name}' ({len(folders)} found){extra}")
            sync()

        def find_more():
            path = filedialog.askdirectory(parent=win, title=f"Look for '{name}' folders in...")
            if not path:
                return
            found = find_named_folders(path, name)
            added = sum(1 for p in found if register_category_folders(
                self.cfg, os.path.dirname(p), {os.path.basename(p)}))
            save_config(self.cfg)
            ticked.clear()
            ticked.update(norm_path(p) for p, v in state["checks"] if v.get())
            folders[:] = same_name_folders(self.cfg["known_folders"], name, current_path)
            rebuild()
            find_note.configure(text=f"Found {len(found)} in that location, {added} new.")

        ctk.CTkButton(find_row, text="Find more folders...", width=160, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=find_more).pack(side="left")
        find_note.pack(side="left", padx=10)

        parts = [(sel_all, dict(anchor="w", padx=(26, 0), pady=(4, 2))),
                 (listbox, dict(fill="x", padx=(26, 0), pady=(2, 4))),
                 (find_row, dict(anchor="w", padx=(26, 0), pady=(0, 6)))]

        def update_list():
            for w, _ in parts:
                w.pack_forget()
            if var.get() == "multiple":
                for w, opts in parts:
                    w.pack(**opts)

        ctk.CTkRadioButton(body, text="This folder only", variable=var, value="single",
                           command=update_list).pack(anchor="w", pady=(6, 0))
        ctk.CTkLabel(body, text=folders[0], text_color=muted, font=ctk.CTkFont(size=11), anchor="w",
                     justify="left", wraplength=600).pack(anchor="w", padx=26)
        ctk.CTkRadioButton(body, text=f"Several folders named '{name}'... (choose from a list)", variable=var,
                           value="multiple", command=update_list).pack(anchor="w", pady=(12, 0))
        list_holder.pack(fill="x")
        all_radio.pack(anchor="w", pady=(12, 0))
        ctk.CTkLabel(body, text="The list shows folders the app has organized into. Use 'Find more folders...' to "
                                "search another location for older ones.", text_color=muted, justify="left",
                     wraplength=620).pack(anchor="w", pady=(14, 0))
        rebuild()

        def do_apply():
            mode = var.get()
            if mode == "single":
                targets = [folders[0]]
            elif mode == "multiple":
                targets = [p for p, v in state["checks"] if v.get()]
                if not targets:
                    messagebox.showinfo("Folder icon", "Tick at least one folder.", parent=win)
                    return
            else:
                targets = list(folders)
            default_for = name if mode == "all" else None
            if resetting:
                errors = self.reset_icons(targets, drop_default_for=default_for)
            else:
                errors = self.apply_icon_to(targets, lib_path, lib_name, default_for=default_for)
            win.destroy()
            self.refresh_folders()
            if errors:
                messagebox.showwarning("Folder icon", "Some folders couldn't be changed:\n" + "\n".join(errors[:6]))
            else:
                self.status.configure(
                    text=f"Icon {'reset' if resetting else 'applied'} on {len(targets)} folder(s). "
                         "If Explorer still shows the old icon, press F5 there.")

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(8, 14))
        ctk.CTkButton(row, text="Reset icon" if resetting else "Apply", width=110, command=do_apply).pack(side="right")
        ctk.CTkButton(row, text="Cancel", width=80, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=win.destroy).pack(side="right", padx=8)

    def pick_library_icon(self, on_pick, title="Choose an icon"):
        """Pick one of the .ico files in the project's icons folder. on_pick(path, name)."""
        icons = list_library_icons()
        win = self._toplevel(title, 600, 480)
        if not icons:
            ctk.CTkLabel(win, justify="left", wraplength=540,
                         text="No icons found yet.\n\nPut your .ico files in this folder, then open this window again:\n\n"
                              + icon_library_dirs()[0]).pack(anchor="w", padx=20, pady=20)
            ctk.CTkButton(win, text="Open icons folder", command=open_icons_folder).pack(pady=6)
            return
        ctk.CTkLabel(win, text="Pick an icon from your icons folder. Double-click to use it right away.",
                     text_color=("gray35", "gray70")).pack(anchor="w", padx=16, pady=(14, 4))
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=4)
        cols = 5
        for c in range(cols):
            scroll.grid_columnconfigure(c, weight=1, uniform="ic")
        state = {"cell": None, "path": None, "name": None}
        keep = []

        def choose(cell, path, name):
            if state["cell"] is not None:
                try:
                    state["cell"].configure(border_width=1, border_color=NEUTRAL_BORDER)
                except Exception:
                    pass
            cell.configure(border_width=2, border_color=ACCENT)
            state.update(cell=cell, path=path, name=name)

        def use(path=None, name=None):
            p, n = path or state["path"], name or state["name"]
            if not p:
                messagebox.showinfo("Choose an icon", "Select an icon first.", parent=win)
                return
            win.destroy()
            on_pick(p, n)

        for i, (name, path) in enumerate(icons):
            cell = ctk.CTkFrame(scroll, corner_radius=8, border_width=1, border_color=NEUTRAL_BORDER)
            cell.grid(row=i // cols, column=i % cols, padx=4, pady=4, sticky="nsew")
            img = self.load_icon_image(path, 40)
            keep.append(img)
            pic = ctk.CTkLabel(cell, text="", image=img)
            pic.pack(pady=(8, 2))
            cap = ctk.CTkLabel(cell, text=short_text(name, 14), font=ctk.CTkFont(size=11))
            cap.pack(pady=(0, 8))
            for w in (cell, pic, cap):
                w.bind("<Button-1>", lambda e, c=cell, p=path, n=name: choose(c, p, n))
                w.bind("<Double-Button-1>", lambda e, p=path, n=name: use(p, n))
        win._keep_images = keep

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 14))
        ctk.CTkButton(row, text="Open icons folder", width=140, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=open_icons_folder).pack(side="left")
        ctk.CTkButton(row, text="Use this icon", width=120, command=use).pack(side="right")
        ctk.CTkButton(row, text="Cancel", width=80, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=win.destroy).pack(side="right", padx=8)

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

    def _on_bg_toggle(self):
        self.cfg["bg_enabled"] = self.var_bg.get()
        save_config(self.cfg)
        self._update_bg_label()
        if self.tray:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def _on_startup_toggle(self):
        want = self.var_startup.get()
        if not set_startup(want):
            self.var_startup.set(not want)
            messagebox.showwarning("Start with Windows", "Couldn't change the startup setting. This only works on Windows.")
            return
        self.cfg["start_with_windows"] = want
        save_config(self.cfg)
        if want and not self.tray:
            messagebox.showinfo("Start with Windows",
                                "The tray icon needs the 'pystray' library (pip install pystray). "
                                "Without it, the app will open its window at login instead of hiding.")

    def _on_tray_toggle(self):
        self.cfg["close_to_tray"] = self.var_tray.get()
        save_config(self.cfg)

    def _on_notify_toggle(self):
        self.cfg["notify"] = self.var_notify.get()
        save_config(self.cfg)

    def _update_bg_label(self):
        n = sum(1 for w in self.cfg["watch_folders"] if w.get("enabled", True))
        if not self.cfg["bg_enabled"]:
            text = "Background: off"
        elif n == 0:
            text = "Background: no folders yet"
        else:
            text = f"Background: watching {n} folder{'s' if n != 1 else ''}"
        self.bg_label.configure(text=text)

    # ------------------------------------------------------------ organize / undo (manual)
    def organize(self):
        if not self._folder_ok(warn=True) or self.busy:
            return
        self.refresh(status=False)
        if not self.plan:
            messagebox.showinfo("File Organizer", "No files to organize.")
            return
        if not messagebox.askyesno("File Organizer", f"Move {len(self.plan)} file(s) into category folders?"):
            return
        self._run_plan(self.plan)

    def _run_plan(self, plan):
        if not self.work_lock.acquire(blocking=False):
            messagebox.showinfo("File Organizer", "The background organizer is busy. Try again in a few seconds.")
            return
        self.busy = True
        self.progress.set(0)

        def cb(i, total):
            self.progress.set(i / total)
            self.status.configure(text=f"Moving files... {i} of {total}")
            if i % 5 == 0 or i == total:
                self.update_idletasks()

        try:
            moves, errors = execute_plan(self.folder, plan, self.cfg, cb)
        finally:
            self.busy = False
            self.work_lock.release()
        if moves:
            self.last_moves = moves
            self.recent = Counter(cat for _d, _s, cat in moves)
            append_log([log_entry("move", old, new) for new, old, _c in moves])
        self.refresh(status=False)
        self.progress.set(1 if moves else 0)
        self.status.configure(text=f"Done. Moved {len(moves)} file(s), {errors} error(s). Undo is available.")
        self.show_view(VIEW_FOLDERS)
        for note in build_notifications(self.folder, moves):
            self.show_toast(note)

    def undo(self):
        if not self.last_moves:
            messagebox.showinfo("File Organizer", "Nothing to undo.")
            return
        restored, entries, back = 0, [], []
        for new, old, _cat in reversed(self.last_moves):
            try:
                target = restore_file(new, old)
                if target:
                    restored += 1
                    back.append(target)
                    entries.append(log_entry("restore", new, target))
                    if self._folder_ok():
                        cleanup_empty_dirs(os.path.dirname(new), self.folder)
            except OSError:
                pass
        self.last_moves = []
        self.recent = Counter()
        append_log(entries)
        excluded = False
        if back and self.cfg.get("exclude_restored", True):
            excluded = add_exclusions(self.cfg, back) > 0
            save_config(self.cfg)
        self.refresh(status=False)
        note = " They are excluded from organizing now (Options > Exclusions)." if excluded else ""
        self.status.configure(text=f"Undo complete. Restored {restored} file(s).{note}")

    # ------------------------------------------------------------ background watcher
    def _watch_tick(self):
        try:
            if (self.cfg["bg_enabled"] and self.cfg["watch_folders"]
                    and not (self.watch_thread and self.watch_thread.is_alive())):
                self.watch_thread = threading.Thread(target=self._watch_once, daemon=True)
                self.watch_thread.start()
        except Exception:
            pass
        finally:
            self.after(WATCH_INTERVAL_MS, self._watch_tick)

    def _watch_once(self):
        """Runs in a worker thread: sorts new files in every enabled watched folder."""
        wcfg = dict(self.cfg)
        wcfg["include_sub"] = False  # the background watcher only touches files directly in the folder
        for item in list(self.cfg["watch_folders"]):
            if not item.get("enabled", True):
                continue
            root = item.get("path", "")
            if not root or not os.path.isdir(root):
                continue
            if not self.work_lock.acquire(blocking=False):
                return
            try:
                now = time.time()
                ready = []
                for f in collect_files(root, wcfg):
                    if os.path.splitext(f)[1].lower() in TEMP_EXTS:
                        continue
                    if now - _mtime(f) > 4 and not is_busy_file(f):
                        ready.append(f)
                if not ready:
                    continue
                moves, _errors = execute_plan(root, build_plan(root, wcfg, ready), wcfg)
                if moves:
                    append_log([log_entry("move", old, new) for new, old, _c in moves])
                    self.ui_queue.put(("moved", root, moves))
            except Exception:
                pass
            finally:
                self.work_lock.release()

    def _on_bg_moved(self, root, moves):
        if register_category_folders(self.cfg, root, {c for _d, _s, c in moves}):
            save_config(self.cfg)
        if os.path.normcase(os.path.normpath(root)) == os.path.normcase(os.path.normpath(self.folder or "")):
            self.recent = Counter(cat for _d, _s, cat in moves)
            self.refresh(status=False)
            self.status.configure(text=f"Auto-organized {len(moves)} new file(s).")
        for note in build_notifications(root, moves):
            self.show_toast(note)

    def _poll_queue(self):
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                try:
                    kind = msg[0]
                    if kind == "moved":
                        self._on_bg_moved(msg[1], msg[2])
                    elif kind == "show":
                        self.show_window()
                    elif kind == "toggle_bg":
                        self.var_bg.set(not self.var_bg.get())
                        self._on_bg_toggle()
                    elif kind == "quit":
                        self._quit()
                        return
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    # ------------------------------------------------------------ tray / window
    def _start_tray(self):
        if not HAS_TRAY:
            return
        try:
            image = make_folder_glyph("#378ADD")
            menu = pystray.Menu(
                pystray.MenuItem("Open File Organizer", lambda icon, item: self.ui_queue.put(("show",)), default=True),
                pystray.MenuItem("Auto-organize in background", lambda icon, item: self.ui_queue.put(("toggle_bg",)),
                                 checked=lambda item: self.cfg["bg_enabled"]),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", lambda icon, item: self.ui_queue.put(("quit",))),
            )
            self.tray = pystray.Icon(APP_NAME, image, "File Organizer", menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def show_window(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _on_close(self):
        watching = self.cfg["bg_enabled"] and any(w.get("enabled", True) for w in self.cfg["watch_folders"])
        if self.tray and self.cfg["close_to_tray"] and watching:
            self.withdraw()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.show_toast({"title": "File Organizer", "folder": "", "select": [],
                                 "text": "Still running in the tray. Right-click the tray icon to exit."})
            return
        self._quit()

    def _quit(self):
        self.cfg["last_folder"] = self.folder
        save_config(self.cfg)
        try:
            self.work_lock.acquire(timeout=5)
        except Exception:
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        for t in list(self.toasts):
            try:
                t["win"].destroy()
            except Exception:
                pass
        self.destroy()

    # ------------------------------------------------------------ pop-up notification
    def test_notification(self):
        folder = self.folder if self._folder_ok() else os.path.expanduser("~")
        self.show_toast({"title": "Files organized", "text": "3 files moved to Pictures",
                         "folder": folder, "select": []})

    def show_toast(self, note):
        if not self.cfg.get("notify", True):
            return
        dark = ctk.get_appearance_mode() == "Dark"
        bg = "#2b2b2b" if dark else "#ffffff"
        fg = "#f0f0f0" if dark else "#1a1a1a"
        sub = "#a8a8a8" if dark else "#555555"
        border = "#4a4a4a" if dark else "#c8c8c8"
        link = "#6cb0ff" if dark else ACCENT

        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=border)
        inner = tk.Frame(win, bg=bg, padx=14, pady=10)
        inner.pack(padx=1, pady=1, fill="both", expand=True)

        head = tk.Frame(inner, bg=bg)
        head.pack(fill="x")
        tk.Label(head, text=note["title"], bg=bg, fg=sub, font=("Segoe UI", 9)).pack(side="left")
        close_btn = tk.Label(head, text="×", bg=bg, fg=sub, font=("Segoe UI", 14), cursor="hand2")
        close_btn.pack(side="right")
        tk.Label(inner, text=note["text"], bg=bg, fg=fg, font=("Segoe UI", 10, "bold"),
                 anchor="w", justify="left", wraplength=300).pack(fill="x", pady=(2, 0))
        folder = note.get("folder", "")
        if folder:
            tk.Label(inner, text=short_path(folder, 58), bg=bg, fg=sub, font=("Segoe UI", 9),
                     anchor="w", justify="left", wraplength=300).pack(fill="x")
            open_lbl = tk.Label(inner, text="Open folder", bg=bg, fg=link, cursor="hand2",
                                font=("Segoe UI", 9, "underline"), anchor="w")
            open_lbl.pack(anchor="w", pady=(6, 0))
        else:
            open_lbl = None

        entry = {"win": win}
        state = {"job": None}

        def close(_e=None):
            if state["job"]:
                try:
                    win.after_cancel(state["job"])
                except Exception:
                    pass
                state["job"] = None
            if entry in self.toasts:
                self.toasts.remove(entry)
            try:
                win.destroy()
            except Exception:
                pass
            self._layout_toasts()

        def open_it(_e=None):
            open_in_explorer(folder, note.get("select"))
            close()

        def start_timer(_e=None):
            if state["job"]:
                try:
                    win.after_cancel(state["job"])
                except Exception:
                    pass
            state["job"] = win.after(NOTIFY_SECONDS * 1000, close)

        def stop_timer(_e=None):
            if state["job"]:
                try:
                    win.after_cancel(state["job"])
                except Exception:
                    pass
                state["job"] = None

        close_btn.bind("<Button-1>", close)
        if open_lbl is not None:
            open_lbl.bind("<Button-1>", open_it)
        for w in (win, inner, head, *inner.winfo_children(), *head.winfo_children()):
            w.bind("<Enter>", stop_timer, add="+")
            w.bind("<Leave>", start_timer, add="+")

        self.toasts.append(entry)
        win.update_idletasks()
        entry["h"] = win.winfo_reqheight()
        entry["w"] = win.winfo_reqwidth()
        self._layout_toasts()
        win.deiconify()
        start_timer()

    def _layout_toasts(self):
        """Stack the pop-ups in the bottom-right corner, newest at the bottom."""
        try:
            left, top, right, bottom = work_area(self)
        except Exception:
            return
        y = bottom - 16
        for entry in reversed(self.toasts):
            w, h = entry.get("w", 330), entry.get("h", 90)
            y -= h
            try:
                entry["win"].geometry(f"{w}x{h}+{right - w - 16}+{y}")
            except Exception:
                pass
            y -= 10

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

    # ------------------------------------------------------------ auto-manage folders
    def open_watch(self):
        win = self._toplevel("Auto-manage folders", 700, 500)
        ctk.CTkLabel(win, justify="left", wraplength=650, text_color=("gray35", "gray70"),
                     text="These folders are sorted automatically in the background, even when the window is "
                          "closed. Only files directly inside each folder are moved, and files that are still "
                          "downloading are skipped. They use the same categories and options as the rest of the app."
                     ).pack(anchor="w", padx=16, pady=(14, 6))
        frame, tree = self._make_tree(win, [("path", "Folder"), ("status", "Status")], [520, 120], selectmode="browse")
        frame.pack(fill="both", expand=True, padx=16, pady=6)

        def reload():
            tree.delete(*tree.get_children())
            for i, w in enumerate(self.cfg["watch_folders"]):
                if not os.path.isdir(w.get("path", "")):
                    status = "Missing"
                elif w.get("enabled", True):
                    status = "Watching"
                else:
                    status = "Paused"
                tree.insert("", "end", iid=str(i), values=(w.get("path", ""), status))
            self._update_bg_label()

        def commit():
            save_config(self.cfg)
            reload()

        def add():
            path = filedialog.askdirectory(parent=win, title="Choose a folder to manage automatically")
            if not path:
                return
            path = os.path.normpath(path)
            reason = unsafe_watch_folder(path)
            if reason:
                messagebox.showwarning("Auto-manage", f"That is {reason}. Choose a normal folder such as Downloads.", parent=win)
                return
            if any(os.path.normcase(w["path"]) == os.path.normcase(path) for w in self.cfg["watch_folders"]):
                messagebox.showinfo("Auto-manage", "That folder is already in the list.", parent=win)
                return
            if not messagebox.askyesno(
                    "Auto-manage",
                    f"Files already in this folder will be sorted right away, and new files will be sorted as "
                    f"they arrive:\n\n{path}\n\nContinue?", parent=win):
                return
            self.cfg["watch_folders"].append({"path": path, "enabled": True})
            register_existing_categories(self.cfg, path)
            commit()

        def selected_index():
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def remove():
            i = selected_index()
            if i is not None:
                del self.cfg["watch_folders"][i]
                commit()

        def toggle():
            i = selected_index()
            if i is not None:
                w = self.cfg["watch_folders"][i]
                w["enabled"] = not w.get("enabled", True)
                commit()
                tree.selection_set(str(i))

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 14))
        ctk.CTkButton(row, text="Add folder...", width=110, command=add).pack(side="left")
        ctk.CTkButton(row, text="Pause or resume", width=130, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=toggle).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Remove", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=remove).pack(side="left")
        ctk.CTkSwitch(row, text="Run in background", variable=self.var_bg,
                      command=lambda: (self._on_bg_toggle(), reload())).pack(side="right")
        reload()

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

    # ------------------------------------------------------------ exclusions
    def _on_exclude_restored_toggle(self):
        self.cfg["exclude_restored"] = self.var_exclude_restored.get()
        save_config(self.cfg)

    def open_exclusions(self):
        win = self._toplevel("Exclusions", 740, 540)
        ctk.CTkLabel(win, justify="left", wraplength=690, text_color=("gray35", "gray70"),
                     text="Excluded files and folders are never moved, by Organize now or by the background "
                          "organizer. Excluding a folder excludes everything inside it."
                     ).pack(anchor="w", padx=16, pady=(14, 6))
        frame, tree = self._make_tree(win, [("path", "Path"), ("kind", "Type")], [580, 90])
        frame.pack(fill="both", expand=True, padx=16, pady=6)

        def reload():
            tree.delete(*tree.get_children())
            for i, p in enumerate(self.cfg["excluded"]):
                kind = "Folder" if os.path.isdir(p) else ("File" if os.path.exists(p) else "Missing")
                tree.insert("", "end", iid=str(i), values=(p, kind))

        def commit():
            save_config(self.cfg)
            reload()
            self.refresh(status=False)

        def add_files():
            paths = filedialog.askopenfilenames(parent=win, title="Choose files to exclude")
            if paths:
                add_exclusions(self.cfg, paths)
                commit()

        def add_folder():
            path = filedialog.askdirectory(parent=win, title="Choose a folder to exclude")
            if path:
                add_exclusions(self.cfg, [path])
                commit()

        def remove():
            idx = sorted((int(i) for i in tree.selection()), reverse=True)
            for i in idx:
                del self.cfg["excluded"][i]
            if idx:
                commit()

        def remove_missing():
            self.cfg["excluded"] = [p for p in self.cfg["excluded"] if os.path.exists(p)]
            commit()

        def clear_all():
            if self.cfg["excluded"] and messagebox.askyesno("Exclusions", "Remove every exclusion?", parent=win):
                self.cfg["excluded"] = []
                commit()

        ctk.CTkSwitch(win, text="Automatically exclude files I restore (Undo and Activity log)",
                      variable=self.var_exclude_restored, command=self._on_exclude_restored_toggle
                      ).pack(anchor="w", padx=16, pady=(4, 6))
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(row, text="Add files...", width=100, command=add_files).pack(side="left")
        ctk.CTkButton(row, text="Add folder...", width=110, command=add_folder).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Remove selected", width=130, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=remove).pack(side="left")
        ctk.CTkButton(row, text="Clear all", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=clear_all).pack(side="right")
        ctk.CTkButton(row, text="Remove missing", width=120, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=remove_missing).pack(side="right", padx=6)
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

    # ------------------------------------------------------------ folder icons dialog
    def open_icons(self):
        if os.name != "nt":
            messagebox.showinfo("Folder icons", "Folder icons are only supported on Windows.")
            return
        if not self._folder_ok(warn=True):
            return
        win = self._toplevel("Folder icons", 660, 560)
        ctk.CTkLabel(win, justify="left", wraplength=610, text_color=("gray35", "gray70"),
                     text=f"Icons for the folders inside {short_path(self.folder, 50)}. These only change this "
                          "folder's own folders. To change several folders, or every folder with the same name, "
                          "right-click a folder under 'Organized folders'.").pack(anchor="w", padx=16, pady=(14, 4))
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=4)

        rows = [(self.folder, f"This folder ({os.path.basename(self.folder) or self.folder})")]
        rows += [(os.path.join(self.folder, c), c) for c in list(self.cfg["categories"]) + [OTHERS, DUPES]]
        initial = {p: self.current_icon_for(p) for p, _ in rows}
        pending = dict(initial)
        labels = {}

        def text_for(p):
            return short_text(pending[p]["name"], 20) if pending[p] else "Default icon"

        def choose(p):
            def picked(path, name):
                pending[p] = {"path": path, "name": name}
                labels[p].configure(text=text_for(p))
            self.pick_library_icon(picked)

        def clear(p):
            pending[p] = None
            labels[p].configure(text=text_for(p))

        for p, title in rows:
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text=short_text(title, 30), width=210, anchor="w").pack(side="left")
            labels[p] = ctk.CTkLabel(row, text=text_for(p), anchor="w", width=140, text_color=("gray35", "gray70"))
            labels[p].pack(side="left", padx=6)
            ctk.CTkButton(row, text="Choose...", width=84, height=30, command=lambda k=p: choose(k)).pack(side="left")
            ctk.CTkButton(row, text="Clear", width=60, height=30, fg_color="transparent", border_width=1,
                          text_color=("gray10", "gray90"), command=lambda k=p: clear(k)).pack(side="left", padx=6)

        def apply():
            errors = []
            for p, _ in rows:
                if pending[p] == initial[p]:
                    continue
                if pending[p]:
                    errors += self.apply_icon_to([p], pending[p]["path"], pending[p]["name"])
                else:
                    errors += self.reset_icons([p])
            if errors:
                messagebox.showwarning("Folder icons", "Some icons couldn't be applied:\n" + "\n".join(errors[:6]), parent=win)
            else:
                messagebox.showinfo("Folder icons",
                                    "Icons applied. If a folder still shows the old icon, press F5 in "
                                    "File Explorer or restart it.", parent=win)
            win.destroy()
            self.refresh_folders()

        def reset_all():
            for p in pending:
                pending[p] = None
                labels[p].configure(text=text_for(p))

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
            if sel:
                p = paths[sel[0]]
                open_in_explorer(os.path.dirname(p), [os.path.basename(p)])

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
            restored, out, back = 0, [], []
            for e in sel:
                try:
                    target = restore_file(e["dst"], e["src"])
                    if target:
                        restored += 1
                        out.append(log_entry("restore", e["dst"], target))
                        back.append(target)
                        if self._folder_ok():
                            cleanup_empty_dirs(os.path.dirname(e["dst"]), self.folder)
                except OSError:
                    pass
            append_log(out)
            msg = f"Restored {restored} of {len(sel)} file(s)."
            if back and self.cfg.get("exclude_restored", True):
                add_exclusions(self.cfg, back)
                save_config(self.cfg)
                msg += ("\n\nThey are now excluded from organizing (Options > Exclusions), so the background "
                        "organizer won't move them again.")
            messagebox.showinfo("Restore", msg, parent=win)
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
            "1. File > Select folder to choose a folder. The 'Items in folder' view shows where each file "
            "will go. Nothing moves until you press Organize now.\n"
            "2. After organizing, 'Organized folders' shows your folders. Double-click one to open it; "
            "right-click to change its icon.\n"
            "3. Background > Auto-manage folders lets you pick folders (like Downloads) that are sorted "
            "automatically, even when the window is closed.\n"
            "4. A small pop-up in the bottom-right corner tells you what moved, with a link that opens the folder.\n"
            "5. Undo puts the last batch back. The Activity log can restore older moves.")


if __name__ == "__main__":
    if already_running():
        if not BACKGROUND_START:
            _r = tk.Tk()
            _r.withdraw()
            messagebox.showinfo("File Organizer", "File Organizer is already running. "
                                                  "Look for its icon in the system tray, next to the clock.")
        sys.exit(0)
    App().mainloop()
