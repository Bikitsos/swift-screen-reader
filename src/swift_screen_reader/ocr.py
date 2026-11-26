"""Read text from a screenshot with the OCR engine built into Windows, and find the amount next to a label.

Windows OCR sometimes drops a number or reads "00" as the letters "O O", especially for light text on a
dark background. So the image is read in several prepared variants, letters that look like digits are
fixed inside numbers, truncated reads ("45.") are rejected, and an amount is only used when the reads
agree (two variants with the same amount, or a single clear read nothing contradicts).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

# Windows OCR needs letters at least ~10 px high; small captures are enlarged to about this height.
TARGET_HEIGHT = 120
MAX_SCALE = 4
AGREEING_READS = 2

_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "ø": "0", "Ø": "0", "e": "0",
                             "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})
_CURRENCY = re.compile(r"[€$£¥]|EUR|USD|GBP", re.IGNORECASE)
# Spaces OCR puts inside a number ("45. O O", "1 ,234.56").
_INNER_SPACE = re.compile(r"(?<=[0-9OoDQøØ.,])\s+(?=[0-9OoDQøØ.,])")
_DIGITISH = "0-9OoDQøØeIl|SB"
_TOKEN = re.compile(rf"[-−]?[{_DIGITISH}][{_DIGITISH}.,']*")
_TWO_DECIMALS = re.compile(r"-?\d{1,3}(?:[.,']\d{3})*[.,]\d{2}|-?\d+[.,]\d{2}")


class OcrError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    value: Decimal
    text: str
    strong: bool  # has exactly two decimals, like every POS amount


@dataclass
class OcrResult:
    lines: list[str]
    amount: Decimal | None
    amount_text: str
    ms: int
    reads: list[tuple[str, str, str]] = field(default_factory=list)  # (variant, text read, amount found)
    reason: str = ""

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


def _normalize(text: str) -> str:
    text = _INNER_SPACE.sub("", _CURRENCY.sub(" ", text))
    # Decimal part with spaces or misread zeros: "45. ee" / "7 . O O" -> "45.ee" / "7.OO".
    text = re.sub(rf"([.,])\s*([{_DIGITISH}])\s*([{_DIGITISH}])(?![A-Za-z0-9])", r"\1\2\3", text)
    # Decimal point read as a hyphen: "II-oo" / "22-40".
    return re.sub(rf"(?<=[{_DIGITISH}])-(?=[{_DIGITISH}]{{2}}(?![A-Za-z0-9]))", ".", text)


def candidates(text: str) -> list[Candidate]:
    """Money amounts in a piece of OCR text, with look-alike letters inside numbers fixed."""
    text = _normalize(text)
    found = []
    for match in _TOKEN.finditer(text):
        raw = match.group()
        before = text[match.start() - 1] if match.start() else " "
        after = text[match.end()] if match.end() < len(text) else " "
        if before.isalnum() or before in ".," or after.isalnum():
            continue  # glued to an unreadable character: part of the number is missing
        token = raw.translate(_LOOKALIKES).replace("−", "-").rstrip("'")
        if token[-1] in ".,":
            continue  # truncated: "45." lost its decimals
        strong = bool(_TWO_DECIMALS.fullmatch(token))
        if not any(c.isdigit() for c in raw) and not strong:
            continue  # an ordinary word; all-look-alike reads only count in amount form ("I.ee" = 1.00)
        value = parse_number(token)
        if value is not None:
            found.append(Candidate(value, token, strong))
    return found


def find_amount(lines: list[str], label: str) -> Candidate | None:
    """The amount after `label` (same line, else the next lines, else the line just before it);
    without a label match, the last amount."""
    label_key = label.strip().lower()
    if label_key:
        for index, line in enumerate(lines):
            position = line.lower().find(label_key)
            if position == -1:
                continue
            # OCR sometimes lists a larger amount before its label.
            for chunk in (line[position + len(label_key):], *lines[index + 1:], *reversed(lines[:index])):
                found = candidates(chunk)
                if found:
                    return found[0]
            return None
    found = [c for line in lines for c in candidates(line)]
    return found[-1] if found else None


def choose(reads: list[Candidate | None]) -> tuple[Candidate | None, str]:
    """Pick the amount the reads agree on; (None, reason) when they don't or none is clear."""
    strong = Counter(c.value for c in reads if c and c.strong)
    weak = Counter(c.value for c in reads if c and not c.strong)
    if len(strong) == 1 and strong.most_common(1)[0][1] >= AGREEING_READS:
        weak.clear()  # two clean reads agree; a read without decimals ("25 O O") can't outvote them
    values = set(strong) | set(weak)
    if len(values) > 1:
        return None, "the reads disagree (" + ", ".join(str(v) for v in sorted(values)) + ")"
    if not values:
        return None, "no amount found"
    value = values.pop()
    if strong[value] >= 1 or weak[value] >= AGREEING_READS:
        best = next(c for c in reads if c and c.value == value and (c.strong or not strong[value]))
        return best, ""
    return None, f"unclear read ({value})"


def _engine():
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        engine = OcrEngine.try_create_from_language(Language("en-US"))
    if engine is None:
        raise OcrError("No Windows OCR language is installed (Settings > Time & language > Language).")
    return engine


async def _recognize(image: QImage, engine=None) -> list[str]:
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    image = image.convertToFormat(QImage.Format.Format_ARGB32)  # BGRA byte order on Windows
    writer = DataWriter()
    writer.write_bytes(bytes(image.constBits())[: image.sizeInBytes()])
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, image.width(), image.height(), BitmapAlphaMode.PREMULTIPLIED
    )
    result = await (engine or _engine()).recognize_async(bitmap)
    return [line.text for line in result.lines]


def _scaled(image: QImage, factor: float) -> QImage:
    return image.scaled(round(image.width() * factor), round(image.height() * factor),
                        Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def _fit(image: QImage, target: float = TARGET_HEIGHT) -> QImage:
    return _scaled(image, min(MAX_SCALE, target / max(1, image.height()))) if image.height() < target else image


def _dark_on_light(image: QImage) -> QImage:
    """Grayscale with dark text on a light background, which the OCR reads best."""
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    total = sum(bytes(gray.constBits())[: gray.sizeInBytes()])
    if total / max(1, gray.sizeInBytes()) < 128:
        gray.invertPixels()
    return gray.convertToFormat(QImage.Format.Format_RGB32)


def _padded(image: QImage, margin_ratio: float = 0.4) -> QImage:
    """Add a border: text that touches the edge (a right-aligned amount) is often skipped."""
    margin = max(8, round(image.height() * margin_ratio))
    corner = QColor(image.pixel(0, 0))
    out = QImage(image.width() + 2 * margin, image.height() + 2 * margin, QImage.Format.Format_RGB32)
    out.fill(corner)
    painter = QPainter(out)
    painter.drawImage(margin, margin, image)
    painter.end()
    return out


VARIANTS = (
    ("original", lambda img: _fit(img)),
    ("dark on light, padded", lambda img: _padded(_fit(_dark_on_light(img), TARGET_HEIGHT * 0.7))),
    ("dark on light", lambda img: _fit(_dark_on_light(img), TARGET_HEIGHT * 1.3)),
    ("padded", lambda img: _padded(_fit(img, TARGET_HEIGHT * 1.3))),
    ("dark on light, large", lambda img: _padded(_fit(_dark_on_light(img), TARGET_HEIGHT * 1.6))),
)


def warm_up() -> None:
    """Load the OCR engine once (about 1-2 s on first use), so the first real transaction is fast."""
    try:
        image = QImage(64, 32, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.white)
        asyncio.run(_recognize(image))
    except Exception as exc:  # noqa: BLE001 - only an optimisation; real errors surface on the first capture
        logging.warning("OCR warm-up failed: %s", exc)


async def _read_variants(image: QImage, label: str) -> tuple[list[tuple[str, list[str], Candidate | None]], Candidate | None, str]:
    engine = _engine()
    reads: list[tuple[str, list[str], Candidate | None]] = []
    for name, prepare in VARIANTS:
        lines = await _recognize(prepare(image), engine)
        reads.append((name, lines, find_amount(lines, label)))
        found = [r[2] for r in reads]
        strong = Counter(c.value for c in found if c and c.strong)
        if strong and strong.most_common(1)[0][1] >= AGREEING_READS:
            break  # two reads agree; the others can only confirm
    chosen, reason = choose([r[2] for r in reads])
    return reads, chosen, reason


def recognize(image: QImage, label: str) -> OcrResult:
    """OCR an image (safe to call from a worker thread) and pick out the amount after `label`."""
    if image.isNull():
        raise OcrError("The screenshot could not be loaded.")
    started = time.perf_counter()
    try:
        reads, chosen, reason = asyncio.run(_read_variants(image, label))
    except ImportError as exc:
        raise OcrError(f"Windows OCR is not available: {exc}") from exc
    except OSError as exc:
        raise OcrError(f"Windows OCR failed: {exc}") from exc
    ms = round((time.perf_counter() - started) * 1000)
    summary = [(name, " | ".join(lines), c.text if c else "-") for name, lines, c in reads]
    # Show the text of the read that gave the amount, else the one with the most text.
    best = next((lines for _, lines, c in reads if chosen and c and c.value == chosen.value),
                max((lines for _, lines, _ in reads), key=lambda ls: len(" ".join(ls))))
    return OcrResult(best, chosen.value if chosen else None, chosen.text if chosen else "", ms, summary, reason)
