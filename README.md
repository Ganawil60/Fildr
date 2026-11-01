File Organizer

A modern Python desktop app that organizes messy folders in one click. It sorts files into folders like Pictures, Videos, and Docs, and comes with tools for finding duplicates, cleaning up large files, and customizing how everything is sorted.

Built with Python and customtkinter.

<img width="1146" height="863" alt="image" src="https://github.com/user-attachments/assets/ab7f4983-7c55-4046-8841-7c5542908ac3" />

Features
- One-click organizing: pick a folder and sort its files into category folders. Nothing moves until you press Organize now.
- Live preview: see where each file will go before anything is moved, plus counts for each category.
- Custom categories: add, edit, or remove categories and choose which file extensions belong to each.
- Sort by date: optionally group files by year, or by year and month (for example Pictures\2026\11 - November).
- Rename files: add a date prefix (2026-11-01_name.jpg) or a number prefix (001_name.jpg).
- Include subfolders: pull files out of subfolders and sort them too.
- Ignore list: skip files by pattern, such as *.tmp or ~*.
- Duplicate finder: finds files with identical content, keeps the oldest copy, and lets you move or delete the extras.
- Large files finder: lists the biggest files so you can clean up space.
- Auto-organize: watches the selected folder and sorts new files as they arrive. Files that are still downloading are skipped.
- Undo and activity log: undo the last batch, or restore older moves from the saved log.
- Custom folder icons (Windows): give your managed folder and category folders their own .ico icons.
- Light and dark themes: follow the system theme or choose one.
- Safe by design: existing folders are merged, and files with the same name are renamed (photo (1).jpg) instead of overwritten.
  
Default categories
Folder	Examples
- Pictures:	.jpg, .png, .gif, .webp, .svg
- Videos:	.mp4, .mkv, .avi, .mov
- Music:	.mp3, .wav, .flac
- Docs:	.pdf, .docx, .txt, .xlsx, .pptx
- Archives:	.zip, .rar, .7z
- Programs:	.exe, .msi, .apk
- Code:	.py, .js, .html, .json, .sql
- Others:	everything else

You can change all of these from Options > Custom categories.

Requirements
-- Python 3.9 or newer
-- Windows (the app runs elsewhere, but folder icons and some Explorer features are Windows-only)
Run from source
Clone or download this repository.

Install the dependency:
   python -m pip install customtkinter
Run the app:
   python file_organizer.py
Build the .exe
Install PyInstaller:
   python -m pip install pyinstaller
Build:
   python -m PyInstaller --onefile --windowed --collect-all customtkinter --name FileOrganizer file_organizer.py
Your app is at dist\FileOrganizer.exe.

To use a custom icon for the .exe file itself, add --icon=myicon.ico to the build command.

Windows Defender or other antivirus software may flag a PyInstaller-built .exe as suspicious. This is a common false positive. You can build it yourself from the source in this repository.

How to use
Open File > Select folder (Ctrl+O) and choose the folder you want to manage.
Check the list to see where each file will go.
Adjust Options if you want (sort by date, rename files, categories, ignore list).
Press Organize now (Ctrl+R).
Changed your mind? Press Undo (Ctrl+Z), or open Tools > Activity log to restore older moves.
Menu overview
Menu	What's inside
File	Select folder, open folder in Explorer, exit
Actions	Organize now, undo, refresh, auto-organize
Tools	Duplicate finder, large files finder, activity log
Options	Include subfolders, sort by date, rename files, custom categories, ignore list, folder icons, theme
Help	How it works, about
Folder icons
Open Options > Folder icons.
Choose an .ico file for the managed folder or any category folder.
Press Apply icons.

If Windows still shows the old icon, press F5 in File Explorer or restart it. Only .ico files are supported.

Where settings are stored

Settings, the activity log, and copies of your chosen icons are saved in %APPDATA%\FileOrganizer. Deleting that folder resets the app to its defaults.

Roadmap
 Drag and drop a folder onto the window to select it
Tech stack
Python
customtkinter and tkinter
PyInstaller (for the .exe build)
