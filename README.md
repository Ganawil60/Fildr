# Fildr

**Fill. Folder. Done.**

A modern Windows app that organizes messy folders in one click. Fildr sorts files into folders like Pictures, Videos, and Docs, can keep chosen folders (like Downloads) tidy in the background, and comes with tools for finding duplicates and cleaning up large files.

Built with Python and [customtkinter](https://github.com/TomSchimansky/CustomTkinter).

> **Windows only.** Fildr runs on Windows 10 and 11 PCs. It does not work on Android, iPhone, or Mac.

## Download

Get the latest `Fildr.exe` from the [Releases](../../releases) page. No installation needed.

<!-- Add a screenshot of the app here:
![Fildr screenshot](screenshot.png)
-->

## Features

- **One-click organizing**: pick a folder and sort its files into category folders. Nothing moves until you press Organize now.
- **Two-view preview**: *Items in folder* shows every file and where it will go. After organizing, *Organized folders* shows your folders as tiles with item counts and what's new.
- **Open and edit from the tiles**: double-click a folder to open it in File Explorer, or right-click it to edit its icon.
- **Auto-manage folders (background)**: choose folders such as Downloads that are sorted automatically, even when the window is closed. Each folder has its own on/off switch. Fildr lives in the system tray and can start with Windows.
- **Pop-up notifications**: a small box in the bottom-right corner tells you what moved and where. Its *Open folder* link opens the folder with the moved files already selected. It closes by itself after 3 seconds, or with the X button.
- **Folder icons from your own icon set**: pick icons from the project's `icons` folder for your managed folder and any category folder (Windows).
- **Custom categories**: add, edit, or remove categories and choose which file extensions belong to each.
- **Sort by date**: optionally group files by year, or by year and month (for example `Pictures\2026\11 - November`).
- **Rename files**: add a date prefix (`2026-11-01_name.jpg`) or a number prefix (`001_name.jpg`).
- **Include subfolders**: pull files out of subfolders and sort them too (manual organizing only).
- **Ignore list**: skip files by pattern, such as `*.tmp` or `~*`.
- **Exclusions**: choose files or whole folders that are never moved. Files you restore (Undo or Activity log) are excluded automatically, so the background organizer doesn't move them again.
- **Duplicate finder**: finds files with identical content, keeps the oldest copy, and lets you move or delete the extras.
- **Large files finder**: lists the biggest files so you can clean up space.
- **Undo and activity log**: undo the last batch, or restore older moves from the saved log.
- **Modern look**: a sidebar with pages, rounded cards and switches, in light or dark theme.
- **Safe by design**: existing folders are merged, and files with the same name are renamed (`photo (1).jpg`) instead of overwritten.

## Default categories

| Folder | Examples |
|---|---|
| Pictures | `.jpg`, `.png`, `.gif`, `.webp`, `.svg` |
| Videos | `.mp4`, `.mkv`, `.avi`, `.mov` |
| Music | `.mp3`, `.wav`, `.flac` |
| Docs | `.pdf`, `.docx`, `.txt`, `.xlsx`, `.pptx` |
| Archives | `.zip`, `.rar`, `.7z` |
| Programs | `.exe`, `.msi`, `.apk` |
| Code | `.py`, `.js`, `.html`, `.json`, `.sql` |
| Others | everything else |

You can change all of these under **Settings > Custom categories**.

## How to use

1. On the **Organize** page, click **Browse** (Ctrl+O) and choose the folder you want to manage.
2. Check **Items in folder** to see where each file will go.
3. Adjust **Settings** if you want (sort by date, rename files, categories, ignore list).
4. Press **Organize now** (Ctrl+R). The view switches to **Organized folders**.
5. Changed your mind? Press **Undo** (Ctrl+Z), or open **Tools > Activity log** to restore older moves.

### Pages

| Page | What's inside |
|---|---|
| Organize | Choose a folder, preview, organize, undo |
| Auto-manage | Folders sorted in the background, with a switch for each one |
| Tools | Duplicate finder, large files finder, activity log |
| Settings | Subfolders, sort by date, rename, categories, ignore list, exclusions, folder icons, start with Windows, tray, notifications, theme |
| Help | A short tour, and the icons folder |

### Background mode

1. Open **Auto-manage** and click **Add folder** for each folder you want sorted automatically.
2. Turn on **Start with Windows** in **Settings** if you want it to run from login.
3. Close the window. Fildr keeps running in the system tray (next to the clock). Right-click the tray icon to open it, pause background sorting, or exit.

Good to know:
- Adding a folder sorts the files already in it right away.
- The background mode only moves files directly inside each folder, never files in subfolders.
- Files that are still downloading (`.crdownload`, `.part`, `.tmp`) or still open in another program (like a Word document you are editing) are skipped until they're ready.
- Whole drives, system folders, and your entire user folder are refused.
- Everything moved in the background is saved to the activity log, so it can be restored.

### Folder icons

1. Developers: put your `.ico` files in an `icons` folder next to `file_organizer.py`. They get bundled into the .exe.
   Users of the .exe can add their own with **Help > Open icons folder**, which opens `%APPDATA%\Fildr\my-icons`.
2. Right-click a folder tile under **Organized folders** and choose **Edit folder icon**.
3. Pick an icon, then choose where it applies:
   - **This folder only**: just that folder (for example `Memories\Video`). Other folders with the same name are left alone.
   - **Several folders**: tick the folders you want from a list. The folder you right-clicked is already ticked, every folder shows its full path, and **Select all** ticks everything. If an older folder is missing, press **Find more folders...**.
   - **All folders with that name**: every one of them, including the ones the app creates later.

**Settings > Folder icons** edits the folders inside the folder you're managing, and **Reset icon...** on a tile offers the same three choices. If Windows still shows the old icon, press F5 in File Explorer or restart it.

### Exclusions

- Right-click an item under **Items in folder** and choose **Exclude this file**, or **Exclude the folder** to leave everything inside it alone. Excluded items show as "Skipped (excluded)".
- **Settings > Exclusions** lists everything excluded, where you can add files or folders, remove entries, or clear the list.
- A restored file is added to the list automatically. Turn this off with the switch in the same window. After **Undo**, restored files stay excluded until you remove them there (or right-click them and choose **Include in organizing again**).

## For developers

### Requirements

- Python 3.9 or newer
- Windows (the app starts elsewhere, but folder icons, the tray icon, and Explorer features are Windows-only)

### Run from source

1. Clone or download this repository.
2. Install the dependencies:
   ```
   python -m pip install customtkinter pystray pillow
   ```
3. Create an `icons` folder next to `file_organizer.py` and put your `.ico` files in it.
4. Run the app:
   ```
   python file_organizer.py
   ```

### Build the .exe

1. Install PyInstaller:
   ```
   python -m pip install pyinstaller
   ```
2. Make sure the `icons` folder exists next to `file_organizer.py` with at least one `.ico` file, and that `fildr.ico` (the logo) is in the same folder.
3. Build:
   ```
   python -m PyInstaller --onefile --windowed --collect-all customtkinter --hidden-import pystray._win32 --add-data "icons;icons" --icon fildr.ico --name Fildr file_organizer.py
   ```
4. Your app is at `dist\Fildr.exe`.

> Windows Defender or other antivirus software may flag a PyInstaller-built .exe as suspicious. This is a common false positive. You can build it yourself from the source in this repository.

## Where settings are stored

Settings, the activity log, and copies of your chosen icons are saved in `%APPDATA%\Fildr`. If you used an earlier version (File Organizer), your settings are copied over the first time Fildr starts. Deleting the folder resets the app to its defaults. The **Start with Windows** option adds one entry to your user's login items, and turning it off removes it.

## Roadmap

- [ ] Drag and drop a folder onto the window to select it

## Tech stack

- Python
- customtkinter, tkinter, and Pillow
- pystray (system tray)
- PyInstaller (for the .exe build)
