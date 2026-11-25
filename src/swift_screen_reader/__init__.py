"""Windows tray service that saves a screenshot of a preselected screen area whenever it is called."""

import getpass
import os
import re
import sys
from pathlib import Path

# Single source of truth for the version; bump with `python scripts/bump_version.py`.
__version__ = "0.1.0"

APP_NAME = "Swift Screen Reader"
EXE_NAME = "SwiftScreenReader"
CLI_EXE_NAME = "SwiftScreenReaderCli"
AUTHOR = "Bikitsos"
COMPANY = "db IMMIX"
CREDITS = f"Software built by {AUTHOR} for {COMPANY}"
GITHUB_REPO = "Bikitsos/swift-screen-reader"


def build_info() -> tuple[str, str]:
    """Return (build date, git commit) written at exe build time, or development placeholders."""
    try:
        from ._build_info import BUILD_DATE, GIT_COMMIT
    except ImportError:
        return "development build", ""
    return BUILD_DATE, GIT_COMMIT


def app_root() -> Path:
    """Folder of the exe when frozen, otherwise the project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Per-user folder for settings: %APPDATA%\\Swift Screen Reader."""
    base = Path(os.environ.get("APPDATA") or Path.home())
    return base / APP_NAME


def default_screenshots_dir() -> Path:
    """`<app root>/screenshots`, or a per-user folder if the app root is not writable."""
    preferred = app_root() / "screenshots"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write_test"
        probe.write_bytes(b"")
        probe.unlink()
        return preferred
    except OSError:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / APP_NAME / "screenshots"


def pipe_name() -> str:
    """Per-user local socket (named pipe) name the running service listens on."""
    user = re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser() or "user")
    return f"{EXE_NAME}-{user}"
