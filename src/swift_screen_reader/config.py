"""Settings shared by the tray service and the command line, stored as JSON in %APPDATA%."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from . import data_dir, default_screenshots_dir

DEFAULT_FILE_NAME = "screenshot_{timestamp}.png"
DEFAULT_HTTP_PORT = 8765
DEFAULT_HOTKEY_CAPTURE = "Ctrl+Shift+F9"
DEFAULT_HOTKEY_SELECT = "Ctrl+Shift+F10"
DEFAULT_PROCESSING_TEXT = "Processing..."


@dataclass(frozen=True)
class Area:
    """Screen rectangle in physical pixels, in virtual-desktop coordinates (may be negative)."""

    x: int
    y: int
    width: int
    height: int

    @property
    def valid(self) -> bool:
        return self.width > 0 and self.height > 0

    @classmethod
    def parse(cls, text: str) -> Area:
        parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
        if len(parts) != 4:
            raise ValueError(f"Expected X,Y,WIDTH,HEIGHT, got {text!r}")
        area = cls(*(int(p) for p in parts))
        if not area.valid:
            raise ValueError("Width and height must be greater than 0")
        return area

    def __str__(self) -> str:
        return f"{self.x},{self.y},{self.width},{self.height}"


@dataclass
class Settings:
    area: Area | None = None
    output_dir: str = ""
    file_name: str = DEFAULT_FILE_NAME
    copy_to_clipboard: bool = False
    notify: bool = False
    http_enabled: bool = True
    http_port: int = DEFAULT_HTTP_PORT
    hotkey_capture: str = DEFAULT_HOTKEY_CAPTURE
    hotkey_select: str = DEFAULT_HOTKEY_SELECT
    processing_enabled: bool = True
    processing_seconds: int = 5
    processing_text: str = DEFAULT_PROCESSING_TEXT
    check_updates: bool = True
    extra: dict = field(default_factory=dict, repr=False)

    def screenshots_dir(self) -> Path:
        return Path(self.output_dir) if self.output_dir else default_screenshots_dir()

    def output_path(self, override: str | Path | None = None) -> Path:
        """Where the next screenshot goes: `override`, or the folder + file name pattern."""
        if override:
            path = Path(override)
        else:
            path = self.screenshots_dir() / expand_file_name(self.file_name or DEFAULT_FILE_NAME)
        return path if path.suffix.lower() == ".png" else path.with_name(path.name + ".png")


def expand_file_name(pattern: str, now: datetime | None = None) -> str:
    """Fill {timestamp}, {date} and {time} in a file name pattern."""
    now = now or datetime.now()
    name = (
        pattern.replace("{timestamp}", f"{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}")
        .replace("{date}", f"{now:%Y-%m-%d}")
        .replace("{time}", f"{now:%H-%M-%S}")
    )
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "screenshot.png"
    return name


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load() -> Settings:
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        logging.warning("Could not read %s (%s); using defaults", path, exc)
        return Settings()

    known = {f.name for f in fields(Settings)} - {"area", "extra"}
    settings = Settings(**{k: v for k, v in data.items() if k in known})
    settings.extra = {k: v for k, v in data.items() if k not in known and k != "area"}
    area = data.get("area")
    if isinstance(area, dict):
        try:
            settings.area = Area(int(area["x"]), int(area["y"]), int(area["width"]), int(area["height"]))
        except (KeyError, TypeError, ValueError):
            settings.area = None
    return settings


def save(settings: Settings) -> None:
    data = {**settings.extra, **asdict(settings)}
    data.pop("extra", None)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
