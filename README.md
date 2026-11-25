# Swift Screen Reader

A Windows tool that saves a **screenshot of a preselected screen area** every time it is called. It stays resident in the system tray as a small service, so a call returns the saved file in a fraction of a second, from the command line, from another program, or over a local HTTP endpoint.

Built with [PySide6](https://doc.qt.io/qtforpython/) and managed with [uv](https://docs.astral.sh/uv/).

## Features

- Drag-select the capture area once over a dimmed screen; it is remembered in physical pixels and works with multiple monitors and mixed scaling (100%, 150%, 200%...)
- Resident tray service, started with Windows by the installer, that answers capture calls immediately
- Three ways to call it:
  - `SwiftScreenReaderCli.exe`: console tool that prints the saved file path (Qt-free, starts fast)
  - `SwiftScreenReader.exe --capture`: same, for launchers and shortcuts that want a windowed exe
  - `http://127.0.0.1:8765/capture` (JSON with the path) or `/capture.png` (the image itself, nothing saved)
- If the service isn't running, the command line captures directly instead of failing
- PNG output to a folder of your choice, with a file name pattern (`{timestamp}`, `{date}`, `{time}`) or a fixed name such as `latest.png` that is overwritten on each capture
- Files are written atomically, so a program watching the folder never reads a half-written image
- Optional copy to the clipboard and tray notification after each capture
- "Show area" briefly outlines the current capture area on screen
- In-app updates from GitHub Releases

## Requirements

- Windows 10 or 11
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.10+ (uv can install it for you)

## Installation

```powershell
git clone <repo-url> swift-screen-reader
cd swift-screen-reader
uv sync
```

## Usage

### Tray app (the service)

```powershell
uv run screen-reader-gui              # open the window and the tray icon
uv run screen-reader-gui --minimized  # start hidden in the tray (what Windows startup uses)
uv run screen-reader-gui --select     # start by selecting the capture area
```

- **First run:** click **Select area...**, drag over the part of the screen to capture, release. Esc or right-click cancels. The dashed outline shows the previous area.
- **Tray icon:** blue means the service is running. Click it to open the window; right-click for Capture now, Select area..., Show area, Open screenshots folder, About and Quit.
- Closing the window keeps the service running in the tray. Only one copy runs per user; starting it again just opens the running window.
- You can also type the area (X, Y, width, height in physical pixels) directly in the window.

Settings are saved to `%APPDATA%\Swift Screen Reader\settings.json`.

### Taking a screenshot

```powershell
SwiftScreenReaderCli.exe                          # capture the saved area, print the file path
SwiftScreenReaderCli.exe -o C:\out\shot.png       # save to a specific file
SwiftScreenReaderCli.exe --area 0,0,800,600       # capture another area this time
SwiftScreenReaderCli.exe --json                   # {"ok": true, "path": "..."}
SwiftScreenReaderCli.exe select                   # choose the area (starts the app if needed)
SwiftScreenReaderCli.exe show                     # open the window
SwiftScreenReaderCli.exe status                   # is the service running?
SwiftScreenReaderCli.exe quit                     # stop the service
```

From source, use `uv run screen-reader` instead of `SwiftScreenReaderCli.exe`.

Exit codes: `0` success, `1` capture failed (for example no area selected yet), `2` invalid arguments, `3` the service is not running (`status` and `quit` only).

`SwiftScreenReader.exe --capture [-o FILE] [--area X,Y,W,H]` does the same as `capture` but has no console window, so check its exit code or use a fixed output file.

### HTTP endpoint

Enabled by default on `127.0.0.1:8765` (change the port or turn it off in the window). It only listens on the local machine.

| Request | Response |
| --- | --- |
| `GET /status` | `{"ok": true, "version": ..., "area": "x,y,w,h", "folder": ...}` |
| `GET /capture` | saves a screenshot, returns `{"ok": true, "path": ...}` |
| `GET /capture.png` | returns the PNG directly, without saving a file |

`/capture` and `/capture.png` accept `?area=X,Y,W,H` to override the saved area once.

```powershell
curl http://127.0.0.1:8765/capture
curl -o shot.png http://127.0.0.1:8765/capture.png
```

## Why a tray app and not a Windows service

Real Windows services run in session 0, which has no access to the user's desktop, so they cannot take screenshots. Instead the app runs as a resident per-user background process: the installer starts it with Windows, minimized to the tray, and calls are handed to it over a named pipe (`\\.\pipe\SwiftScreenReader-<user>`) or HTTP. Python and Qt are already loaded, so the capture itself takes only a few milliseconds.

## Building a Windows exe

```powershell
.\build.bat            # build with the current version
.\build.bat patch      # 0.1.0 -> 0.1.1, then build
.\build.bat minor      # 0.1.0 -> 0.2.0, then build
.\build.bat major      # 0.1.0 -> 1.0.0, then build
.\build.bat 2.3.4      # set an exact version, then build
```

`build.bat` installs the build dependencies, closes any running copy of the app, and builds the app folder `dist\SwiftScreenReader\` with:

- `SwiftScreenReader.exe`: the tray app (no console window); accepts `--minimized`, `--select` and `--capture`
- `SwiftScreenReaderCli.exe`: the console command line tool
- an `_internal` folder they share

It also builds a portable zip of that folder, `dist\SwiftScreenReader-X.Y.Z-portable.zip`. Python does not need to be installed on the target machine.

The app is built as a folder rather than a single exe on purpose. A single-file exe unpacks Python into `%TEMP%\_MEI...` every time it starts, which is slow, and antivirus scanners or temp cleaners can break it (*"Failed to load Python DLL"*).

If [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed (`winget install JRSoftware.InnoSetup`), it also builds an installer, `dist\SwiftScreenReader-Setup-<version>.exe`, from [installer.iss](installer.iss). The installer:

- installs for all users (admin) or only the current user (you choose in the setup dialog)
- adds Start Menu shortcuts (the app, *Select area*, *Capture*), plus an optional desktop shortcut
- starts the service with Windows, minimized to the tray (on by default)
- registers an uninstaller under **Settings → Apps** and in the Start Menu

Uninstalling closes the app and removes its files, shortcuts, startup entry and saved settings. Screenshots are kept.

To build only the exes by hand:

```powershell
uv run --group build pyinstaller --noconfirm --clean screen-reader.spec
```

### Versioning

The version is defined in one place: `__version__` in [src/swift_screen_reader/__init__.py](src/swift_screen_reader/__init__.py). The package metadata, the exe file properties, the installer file name, the window title, the **About** screen and `--version` all read it from there. Show or change it without building:

```powershell
uv run python scripts/bump_version.py          # show the current version
uv run python scripts/bump_version.py patch    # bump it
```

Each exe build also records its build date and git commit, shown in **Help → About**.

### Publishing a GitHub release

```powershell
.\release.bat patch    # or minor, major, or an exact X.Y.Z
```

`release.bat` checks that you are on `main` with no uncommitted changes, bumps the version, commits, creates the tag `vX.Y.Z` and pushes both. Pushing the tag starts the [Release workflow](.github/workflows/release.yml), which runs on a Windows runner and:

1. checks that the tag matches `__version__`
2. builds the exes and the installer with `build.bat`
3. publishes a GitHub release with auto-generated release notes, with `SwiftScreenReader-Setup-X.Y.Z.exe` and `SwiftScreenReader-X.Y.Z-portable.zip` attached

To test the pipeline without publishing, run the workflow by hand: `gh workflow run release.yml`.

### Updates

The installed app checks GitHub Releases at startup (can be turned off) and from **Help → Check for updates...**. It downloads the installer, verifies its size and SHA-256, runs it silently and restarts.

## Security

The named pipe is per user; other (non-administrator) users cannot send it commands. The HTTP endpoint listens on `127.0.0.1` only, but any program on the machine (including other users' sessions) can request a screenshot of the area through it; turn it off if that matters. Requests from web browsers (with an `Origin` or `Sec-Fetch-Site` header, or a foreign `Host`) are refused, so web pages cannot trigger captures. Over HTTP, screenshots are always saved to the configured folder; only the pipe accepts a custom output path.

## Project layout

```
src/swift_screen_reader/
  capture.py   Win32 GDI screen capture and PNG encoding (no Qt)
  config.py    settings (JSON in %APPDATA%), capture area, file names
  ipc.py       named pipe client (no Qt)
  cli.py       command line (SwiftScreenReaderCli.exe)
  launcher.py  SwiftScreenReader.exe entry: --capture, or the tray app
  service.py   resident service: named pipe server and HTTP endpoint
  selector.py  area selection overlay and area highlight
  gui.py       tray app and window
  updater.py   update check, download and install from GitHub Releases
scripts/
  bump_version.py  show or bump the version
```
