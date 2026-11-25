# PyInstaller spec: build with `uv run --group build pyinstaller --noconfirm --clean screen-reader.spec`
import re
import subprocess
from datetime import datetime
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)
from PySide6.QtGui import QGuiApplication

from swift_screen_reader import APP_NAME, CLI_EXE_NAME, COMPANY, CREDITS, EXE_NAME, __version__
from swift_screen_reader.gui import make_icon

root = Path(SPECPATH)
build_dir = root / "build"
build_dir.mkdir(exist_ok=True)

# Stamp the build date and git commit into the package so the About screen can show them.
try:
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    commit = ""
(root / "src" / "swift_screen_reader" / "_build_info.py").write_text(
    f'BUILD_DATE = "{datetime.now():%Y-%m-%d %H:%M}"\nGIT_COMMIT = "{commit}"\n', encoding="utf-8"
)

# Render the tray icon to an .ico so the exe uses the same artwork.
_app = QGuiApplication.instance() or QGuiApplication([])
icon_path = str(build_dir / "app.ico")
make_icon(True).pixmap(64, 64).save(icon_path)

numbers = tuple(int(n) for n in re.findall(r"\d+", __version__)[:3])
file_version = (numbers + (0, 0, 0, 0))[:4]


def version_file(exe_name: str, description: str) -> str:
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=file_version, prodvers=file_version),
        kids=[
            StringFileInfo([
                StringTable("040904B0", [
                    StringStruct("CompanyName", COMPANY),
                    StringStruct("FileDescription", description),
                    StringStruct("FileVersion", __version__),
                    StringStruct("InternalName", exe_name),
                    StringStruct("LegalCopyright", CREDITS),
                    StringStruct("OriginalFilename", f"{exe_name}.exe"),
                    StringStruct("ProductName", APP_NAME),
                    StringStruct("ProductVersion", __version__),
                ])
            ]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
    path = build_dir / f"version_{exe_name}.txt"
    path.write_text(str(info), encoding="utf-8")
    return str(path)


gui = Analysis(
    [str(root / "scripts" / "gui_entry.py")],
    pathex=[str(root / "src")],
    excludes=["tkinter"],
)
# The console tool never imports Qt, which keeps its start-up fast.
cli = Analysis(
    [str(root / "scripts" / "cli_entry.py")],
    pathex=[str(root / "src")],
    excludes=["tkinter", "PySide6", "shiboken6"],
)

# One-folder build: one-file exes unpack Python to %TEMP% on every start, which is slow and
# antivirus and temp cleaners can break ("Failed to load Python DLL").
gui_exe = EXE(
    PYZ(gui.pure),
    gui.scripts,
    exclude_binaries=True,
    name=EXE_NAME,
    console=False,
    icon=icon_path,
    version=version_file(EXE_NAME, APP_NAME),
    upx=False,
)
cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    exclude_binaries=True,
    name=CLI_EXE_NAME,
    console=True,
    icon=icon_path,
    version=version_file(CLI_EXE_NAME, f"{APP_NAME} command line"),
    upx=False,
)
coll = COLLECT(
    gui_exe, cli_exe,
    gui.binaries, gui.datas,
    cli.binaries, cli.datas,
    name=EXE_NAME,
    upx=False,
)
