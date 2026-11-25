"""Read text from a screenshot with the OCR engine built into Windows, and find the amount next to a label."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

# Windows OCR needs letters at least ~10 px high; small captures are enlarged to about this height.
TARGET_HEIGHT = 120
MAX_SCALE = 4

_NUMBER = re.compile(r"[-−]?\s?\d[\d.,' ]*")
_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})


class OcrError(RuntimeError):
    pass


@dataclass
class OcrResult:
    lines: list[str]
    amount: Decimal | None
    amount_text: str
    ms: int

    @property
    def text(self) -> str:
        return " | ".join(self.lines)


def parse_number(token: str) -> Decimal | None:
    """'1,234.56' / '1.234,56' / '12,50' / '0.00' / '€ 7.99' -> Decimal; None if it isn't a money amount."""
    s = token.strip().replace("−", "-").replace(" ", "").replace("'", "")
    negative = s.startswith("-")
    s = s.lstrip("-")
    if not s or not s[0].isdigit():
        return None
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    decimal_pos = max(last_dot, last_comma)
    # A separator followed by exactly 1-2 digits at the end is the decimal point; any others group thousands.
    if decimal_pos != -1 and 1 <= len(s) - decimal_pos - 1 <= 2:
        whole, fraction = s[:decimal_pos], s[decimal_pos + 1:]
    else:
        whole, fraction = s, ""
    whole = whole.replace(".", "").replace(",", "")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        return None
    try:
        value = Decimal(f"{whole}.{fraction or '0'}")
    except InvalidOperation:
        return None
    return -value if negative else value


def _clean_amount_text(text: str) -> str:
    """Fix letters OCR commonly confuses with digits, but only inside number-like text."""
    out = []
    for word in text.split():
        stripped = re.sub(r"[€$£¥]|EUR|USD|GBP", "", word, flags=re.IGNORECASE)
        if stripped and sum(c.isdigit() for c in stripped) >= max(1, len(stripped) // 2):
            stripped = stripped.translate(_LOOKALIKES)
        out.append(stripped)
    return " ".join(out)


def _numbers(text: str) -> list[tuple[str, Decimal]]:
    found = []
    for match in _NUMBER.finditer(_clean_amount_text(text)):
        value = parse_number(match.group())
        if value is not None:
            found.append((match.group().strip(), value))
    return found


def find_amount(lines: list[str], label: str) -> tuple[Decimal | None, str]:
    """The amount after `label` (same line, else the next lines); without a label match, the only/last amount."""
    label_key = label.strip().lower()
    if label_key:
        for index, line in enumerate(lines):
            position = line.lower().find(label_key)
            if position == -1:
                continue
            after = [line[position + len(label_key):], *lines[index + 1:]]
            for chunk in after:
                numbers = _numbers(chunk)
                if numbers:
                    return numbers[0][1], numbers[0][0]
            return None, ""
    numbers = [n for line in lines for n in _numbers(line)]
    if numbers:
        return numbers[-1][1], numbers[-1][0]
    return None, ""


def _engine():
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        engine = OcrEngine.try_create_from_language(Language("en-US"))
    if engine is None:
        raise OcrError("No Windows OCR language is installed (Settings > Time & language > Language).")
    return engine


async def _recognize(image: QImage) -> list[str]:
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    image = image.convertToFormat(QImage.Format.Format_ARGB32)  # BGRA byte order on Windows
    writer = DataWriter()
    writer.write_bytes(bytes(image.constBits())[: image.sizeInBytes()])
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, image.width(), image.height(), BitmapAlphaMode.PREMULTIPLIED
    )
    result = await _engine().recognize_async(bitmap)
    return [line.text for line in result.lines]


def warm_up() -> None:
    """Load the OCR engine once (about 1-2 s on first use), so the first real transaction is fast."""
    try:
        image = QImage(64, 32, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.white)
        asyncio.run(_recognize(image))
    except Exception as exc:  # noqa: BLE001 - only an optimisation; real errors surface on the first capture
        logging.warning("OCR warm-up failed: %s", exc)


def recognize(image: QImage, label: str) -> OcrResult:
    """OCR an image (safe to call from a worker thread) and pick out the amount after `label`."""
    if image.isNull():
        raise OcrError("The screenshot could not be loaded.")
    started = time.perf_counter()
    if image.height() < TARGET_HEIGHT:
        scale = min(MAX_SCALE, TARGET_HEIGHT / max(1, image.height()))
        image = image.scaled(round(image.width() * scale), round(image.height() * scale),
                             Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    try:
        lines = asyncio.run(_recognize(image))
    except ImportError as exc:
        raise OcrError(f"Windows OCR is not available: {exc}") from exc
    except OSError as exc:
        raise OcrError(f"Windows OCR failed: {exc}") from exc
    amount, amount_text = find_amount(lines, label)
    return OcrResult(lines, amount, amount_text, round((time.perf_counter() - started) * 1000))
