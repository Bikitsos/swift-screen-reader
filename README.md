# Swift Screen Reader

A Windows tool for a point of sale: when called, it takes a **screenshot of a preselected screen area** (for example the POS subtotal), **reads the amount with OCR** and **takes the card payment** for it on a full-screen payment screen, then presses a POS button of your choice to continue. It stays resident in the system tray, so it reacts in a fraction of a second to a keyboard shortcut, the command line, another program or a local HTTP call.

The card terminal is simulated for now (every payment is approved); a real terminal will plug in when its API is available.

Built with [PySide6](https://doc.qt.io/qtforpython/) and managed with [uv](https://docs.astral.sh/uv/).

## Features

- Drag-select the capture area once over a dimmed screen; it is remembered in physical pixels and works with multiple monitors and mixed scaling (100%, 150%, 200%...)
- Resident tray service, started with Windows by the installer, that answers capture calls immediately
- Global keyboard shortcuts that work anywhere, also while the app is hidden in the tray (default **Ctrl+Shift+F9** capture, **Ctrl+Shift+F10** select area, **Ctrl+Shift+F11** pick the POS button; configurable)
- Other ways to call it:
  - `SwiftScreenReaderCli.exe`: console tool that prints the saved file path (Qt-free, starts fast)
  - `SwiftScreenReader.exe --capture`: same, for launchers and shortcuts that want a windowed exe
  - `http://127.0.0.1:8765/capture` (JSON with the path) or `/capture.png` (the image itself, nothing saved)
- With no arguments the command line uses all the app's settings, starting the app in the tray if it isn't running
- PNG output to a folder of your choice, with a file name pattern (`{timestamp}`, `{date}`, `{time}`) or a fixed name such as `latest.png` that is overwritten on each capture
- Files are written atomically, so a program watching the folder never reads a half-written image
- Optional copy to the clipboard and tray notification after each capture
- Reads the amount after a label (default `Subtotal`) with the OCR engine built into Windows: no extra software, about 30 ms
- Full-screen payment screen: "Processing payment..." then "Transaction successful", which closes by itself and presses a POS button you picked (Ctrl+Shift+F11); failures stay until the operator taps. Other captures are refused meanwhile, so nothing is charged twice
- A `.json` file next to each screenshot with the text read, the amount and the payment result
- Detailed activity log (`logs\activity.log`, one file per day, kept 90 days) of every capture, OCR result, payment step and setting change
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

- **First run:** on the **Home** page click **Select area...**, drag over the amount on the POS screen (include the `Subtotal` label), release. Esc or right-click cancels. The dashed outline shows the previous area. Then use **Payment → Test: read the amount now** to check that the amount is read correctly.
- **Window:** a sidebar with **Home** (status, Capture now, last transaction), **Capture** (area and screenshot folder), **Payment** (OCR and card terminal), **Shortcuts**, **Service** (HTTP endpoint, updates) and **Activity log**. Changes are saved immediately.
- **Tray icon:** blue means the service is running. Click it to open the window; right-click for Capture now, Select area..., Show area, Open screenshots folder, About and Quit.
- Closing the window keeps the service running in the tray. Only one copy runs per user; starting it again just opens the running window.
- You can also type the area (X, Y, width, height in physical pixels) on the **Capture** page.

Settings are saved to `%APPDATA%\Swift Screen Reader\settings.json`.

### Keyboard shortcuts

| Shortcut (default) | Action |
| --- | --- |
| **Ctrl+Shift+F9** | Capture the area now (asks you to select one first if none is set) |
| **Ctrl+Shift+F10** | Select a new capture area |
| **Ctrl+Shift+F11** | Pick the POS button to press after a successful payment |

They are system-wide: they work in any program while the app runs, even hidden in the tray. To change one, click its box in the window and press the new keys; the **×** turns it off and **Default** restores it. A shortcut needs Ctrl, Alt or Win plus a letter, digit, F-key or navigation key; F1-F24, Print and Pause also work on their own. If another program already uses a combination, the window shows it in red and you can pick a different one. The tray menu lists the active shortcuts.

### Payment flow

Every capture made through the running app (shortcut, **Capture now**, the command line or HTTP `/capture`) runs a transaction:

1. The screenshot is saved.
2. All monitors are covered with a dark screen: **Reading amount...**, then **Processing payment...** with the amount and a spinner. Mouse clicks and keys are swallowed.
3. The amount is read with Windows OCR: the number after the label (default `Subtotal`) on the same line or the next one. `12.50`, `1,234.56`, `1.234,56`, `€ 12,50` are all understood.
4. The amount is sent to the card terminal. For now this is a simulator that approves every payment after a short delay (default 2 s).
5. The result is shown:
   - **Transaction successful** (green) with the amount. After 2 seconds it closes by itself and the app **presses the POS button you picked** (see below), so the operator doesn't have to touch anything.
   - **Transaction declined / failed** (red), for when a real terminal is connected
   - **Nothing to pay** (orange) when the amount is 0.00; nothing is charged
   - **Could not read the amount** (orange) with the text that was read; nothing is charged

   Every result except success stays on screen until the operator **taps anywhere** (or presses a key), because someone has to see it.

Until the transaction is finished (the success screen has closed and the button was pressed, or another result was tapped away), new captures are refused (the shortcut is ignored, the command line gets an error, HTTP returns 409), so a double press never charges twice. The payment screen is hidden from screen capture (Windows 10 2004 or later). It is not a Windows lock: no password is needed, and system keys such as the Windows key and Ctrl+Alt+Del still work.

On the **Payment** page you can change the label, test the OCR on the current screen, choose the terminal and currency, set the simulated processing time, or turn the payment (or the OCR) off. With payment off, the amount is only read and recorded, without the payment screen. A command-line capture with `--direct` only saves the screenshot.

### Pressing a POS button after a successful payment

To continue on the POS automatically (for example its **Done** or **Card paid** button):

1. Press **Ctrl+Shift+F11** anywhere (or **Payment → Pick button...**, or **Pick POS button...** in the tray menu). The screen dims slightly and shows *Tap the POS button to press after a successful payment*.
2. Tap (or click) the button once. Its position is saved and briefly marked with a red target. Esc or right-click cancels.

After each approved payment the success screen closes after the time set in **After a successful payment** (default 2 s, 1-30 s), then the app presses that point with a simulated **touch** (or, if you choose, a **mouse click**; the mouse pointer is put back afterwards). **Show** marks the saved position, **Test tap** presses it now. Turn off *Close the success screen automatically* to go back to tapping it away by hand, or *Then press this POS button* to close it without pressing anything. Taps are only made after a success, never after a failure.

#### When the POS program runs as administrator

Windows does not let a normal program send taps or clicks to a program that runs as administrator. If your POS does, Swift Screen Reader must run as administrator too, otherwise the log shows *"... runs as administrator, so Windows blocks simulated taps"* and the button is not pressed. The app warns about this as soon as you pick a button in such a program, and on the **Home** page after a failed tap.

1. **Service → Restart as administrator** (or the button on **Home**). Windows asks for permission once. The window title then ends in *(Administrator)*.
2. Tick **Service → Start with Windows as administrator**. This creates a Task Scheduler task named *Swift Screen Reader* that starts the app as administrator at logon, without a permission prompt, and removes the normal startup entry. Updates restart the app through the same task. Untick it to go back to a normal start.

The POS can keep calling `SwiftScreenReaderCli.exe` as before; the command line also reaches the app when it runs as administrator. Uninstalling removes the task.

### Transaction records

Next to each screenshot a `.json` file with the same name records the transaction:

```json
{
  "id": "screenshot_20261125_130241_274",
  "status": "approved",
  "message": "Transaction successful",
  "source": "shortcut",
  "image": "C:\\...\\screenshot_20261125_130241_274.png",
  "area": "200,200,400,80",
  "started": "2026-11-25T13:02:41.276",
  "finished": "2026-11-25T13:02:43.450",
  "dismissed": "2026-11-25T13:02:45.643",
  "closed_by": "auto",
  "auto_tap": {"point": "860,680", "method": "touch", "error": null},
  "ocr": {"label": "Subtotal", "lines": ["Subtotal", "12.50"], "amount_text": "12.50", "ms": 28},
  "amount": "12.50",
  "currency": "EUR",
  "payment": {"requested": true, "terminal": "Simulated terminal (always approves)", "auth_code": "809778",
              "terminal_reference": "SIM-screenshot_20261125_130241_274", "ms": 2000}
}
```

`status` is one of `approved`, `declined`, `error`, `zero` (nothing to pay), `no_amount` (OCR found no amount) or `read` (payment turned off).

### Activity log

Everything the app does is written to `logs\activity.log` next to the exe (or `%LOCALAPPDATA%\Swift Screen Reader\logs` if that folder isn't writable), with millisecond timestamps: start-up, shortcut presses, captures with their source and area, the OCR text and amount, payment requests and results with auth codes, when the operator closed the result screen, refused captures, area selections and every setting change. A new file is started each day and 90 days are kept. The **Activity log** page shows it live and opens the file or folder.

```
13:02:41.276 INFO Screenshot saved (source: cli, area: 200,200,400,80): C:\...\screenshot_20261125_130241_274.png
13:02:41.449 INFO TX screenshot_20261125_130241_274: OCR read 'Subtotal | 12.50' in 129 ms
13:02:41.449 INFO TX screenshot_20261125_130241_274: payment request EUR 12.50 sent to Simulated terminal (always approves)
13:02:41.460 WARNING Capture from cli refused: a transaction is in progress
13:02:43.450 INFO TX screenshot_20261125_130241_274: payment APPROVED in 2000 ms (auth code 809778, ...): Transaction successful
13:02:45.643 INFO TX screenshot_20261125_130241_274: result screen closed by the operator after 2.2 s
```

### Taking a screenshot

```powershell
SwiftScreenReaderCli.exe                          # capture with all the app's settings, print the file path
SwiftScreenReaderCli.exe -o C:\out\shot.png       # save to a specific file
SwiftScreenReaderCli.exe --area 0,0,800,600       # capture another area this time
SwiftScreenReaderCli.exe --json                   # {"ok": true, "path": "..."}
SwiftScreenReaderCli.exe --direct                 # screenshot only: no app, no amount reading or payment
SwiftScreenReaderCli.exe select                   # choose the area (starts the app if needed)
SwiftScreenReaderCli.exe show                     # open the window
SwiftScreenReaderCli.exe status                   # is the service running?
SwiftScreenReaderCli.exe quit                     # stop the service
```

With no arguments the command line uses everything set in the app: the capture area, screenshot folder and file name, amount reading and the payment screen. It hands the capture to the running app; if the app isn't running, it starts it hidden in the tray first (a few seconds the first time) and then captures. Only if the app can't start does it fall back to saving the screenshot alone. `--area` and `-o` override the area or the file for that one capture. The command returns as soon as the screenshot is saved; the payment continues on screen.

From source, use `uv run screen-reader` instead of `SwiftScreenReaderCli.exe`.

Exit codes: `0` success, `1` capture failed (for example no area selected yet, or a transaction is still on screen), `2` invalid arguments, `3` the service is not running (`status` and `quit` only).

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

Uninstalling closes the app and removes its files, shortcuts, startup entry and saved settings. Screenshots, transaction records and logs are kept.

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
  hotkeys.py   system-wide keyboard shortcuts (RegisterHotKey)
  overlay.py   full-screen payment screens (processing, result)
  ocr.py       Windows OCR and finding the amount after the label
  payment.py   card terminal interface and the simulator
  tap.py       simulated touch / mouse click on the POS button
  elevation.py administrator detection, restart as administrator, start-at-logon task
  transaction.py  capture -> OCR -> payment run, logging and .json records
  selector.py  area selection overlay and area highlight
  gui.py       tray app and window
  updater.py   update check, download and install from GitHub Releases
scripts/
  bump_version.py  show or bump the version
```
