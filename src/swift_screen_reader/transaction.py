"""One capture -> OCR -> payment run, on a worker thread, logged step by step and saved as JSON next to the PNG."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from . import ocr
from .config import Settings
from .payment import APPROVED, DECLINED, ERROR, TERMINALS, create_terminal

READING = "reading"
PAYING = "paying"
READ = "read"  # OCR only, payment turned off
NO_AMOUNT = "no_amount"
ZERO = "zero"
FINISHED = {READ, NO_AMOUNT, ZERO, APPROVED, DECLINED, ERROR}


def format_amount(amount: Decimal | None, currency: str = "") -> str:
    if amount is None:
        return "-"
    text = format(amount, ",.2f")
    return f"{currency} {text}".strip()


@dataclass
class Transaction:
    id: str
    image: str
    source: str
    area: str
    currency: str
    label: str
    payment: bool
    started: datetime = field(default_factory=datetime.now)
    status: str = READING
    message: str = ""
    ocr_lines: list[str] = field(default_factory=list)
    ocr_ms: int = 0
    amount: Decimal | None = None
    amount_text: str = ""
    terminal: str = ""
    auth_code: str = ""
    terminal_reference: str = ""
    payment_ms: int = 0
    finished: datetime | None = None
    dismissed: datetime | None = None
    closed_by: str = ""  # "operator" (tapped) or "auto" (success screen timed out)
    tap_point: str = ""
    tap_method: str = ""
    tap_error: str = ""
    tap_target: str = ""

    @property
    def done(self) -> bool:
        return self.status in FINISHED

    @property
    def amount_display(self) -> str:
        return format_amount(self.amount, self.currency)

    @property
    def json_path(self) -> Path:
        return Path(self.image).with_suffix(".json")

    def to_json(self) -> dict:
        def stamp(value: datetime | None) -> str | None:
            return value.isoformat(timespec="milliseconds") if value else None

        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "source": self.source,
            "image": self.image,
            "area": self.area,
            "started": stamp(self.started),
            "finished": stamp(self.finished),
            "dismissed": stamp(self.dismissed),
            "closed_by": self.closed_by or None,
            "auto_tap": {"point": self.tap_point, "method": self.tap_method, "program": self.tap_target or None,
                         "error": self.tap_error or None}
            if self.tap_point else None,
            "ocr": {
                "label": self.label,
                "lines": self.ocr_lines,
                "amount_text": self.amount_text,
                "ms": self.ocr_ms,
            },
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "payment": {
                "requested": self.payment and self.status in (PAYING, APPROVED, DECLINED, ERROR) and bool(self.terminal),
                "terminal": self.terminal,
                "auth_code": self.auth_code,
                "terminal_reference": self.terminal_reference,
                "ms": self.payment_ms,
            },
        }


class TransactionRunner(QObject):
    """Emits `changed(tx)` at each step and `finished(tx)` once; both arrive on the GUI thread."""

    changed = Signal(object)
    finished = Signal(object)

    def start(self, image: str, source: str, area: str, settings: Settings) -> Transaction:
        tx = Transaction(
            id=Path(image).stem,
            image=image,
            source=source,
            area=area,
            currency=settings.currency.strip().upper(),
            label=settings.amount_label,
            payment=settings.payment_enabled,
        )
        terminal_kind, delay = settings.terminal, settings.simulated_seconds
        threading.Thread(target=self._run, args=(tx, terminal_kind, delay), name=f"tx-{tx.id}", daemon=True).start()
        return tx

    def dismiss(self, tx: Transaction, by: str = "operator") -> None:
        tx.dismissed = datetime.now()
        tx.closed_by = by
        seconds = (tx.dismissed - (tx.finished or tx.started)).total_seconds()
        if by == "auto":
            _log(tx, "result screen closed automatically after %.1f s", seconds)
        else:
            _log(tx, "result screen closed by the operator after %.1f s", seconds)
        _save(tx)

    def record_tap(self, tx: Transaction, point: str, method: str, error: str = "", target: str = "") -> None:
        tx.tap_point, tx.tap_method, tx.tap_error, tx.tap_target = point, method, error, target
        if error:
            logging.error("TX %s: tapping the POS button at %s failed: %s", tx.id, point, error)
        else:
            _log(tx, "tapped the POS button at %s in %s (%s)", point, target or "?", method)
        _save(tx)

    def _run(self, tx: Transaction, terminal_kind: str, delay: float) -> None:
        try:
            self._process(tx, terminal_kind, delay)
        except Exception as exc:  # noqa: BLE001 - a broken step must never leave the screen locked
            logging.exception("Transaction %s failed unexpectedly", tx.id)
            tx.status, tx.message = ERROR, f"Unexpected error: {exc}"
        tx.finished = datetime.now()
        _log(tx, "finished: %s%s", tx.status, f" ({tx.message})" if tx.message else "")
        _save(tx)
        self.finished.emit(tx)

    def _process(self, tx: Transaction, terminal_kind: str, delay: float) -> None:
        _log(tx, "started (source: %s, area: %s, image: %s)", tx.source, tx.area, tx.image)
        self.changed.emit(tx)
        try:
            result = ocr.recognize(QImage(tx.image), tx.label)
        except ocr.OcrError as exc:
            tx.status, tx.message = NO_AMOUNT, str(exc)
            return
        tx.ocr_lines, tx.ocr_ms, tx.amount, tx.amount_text = result.lines, result.ms, result.amount, result.amount_text
        _log(tx, "OCR read %r in %d ms", result.text, result.ms)
        if tx.amount is None:
            found = result.text or "no text"
            tx.status = NO_AMOUNT
            tx.message = f"No amount found after \u201c{tx.label}\u201d (read: {found})"
            return
        _log(tx, "amount %s (from %r)", tx.amount_display, tx.amount_text)
        if not tx.payment:
            tx.status, tx.message = READ, "Payment is turned off; amount recorded only"
            return
        if tx.amount <= 0:
            tx.status, tx.message = ZERO, f"{tx.label} is {tx.amount_display}; nothing to charge"
            return

        terminal = create_terminal(terminal_kind, delay)
        tx.terminal = TERMINALS.get(terminal_kind, terminal.name)
        tx.status = PAYING
        _log(tx, "payment request %s sent to %s", tx.amount_display, tx.terminal)
        self.changed.emit(tx)
        started = time.perf_counter()
        payment = terminal.charge(tx.amount, tx.currency, tx.id)
        tx.payment_ms = round((time.perf_counter() - started) * 1000)
        tx.status, tx.message = payment.status, payment.message
        tx.auth_code, tx.terminal_reference = payment.auth_code, payment.terminal_reference
        _log(tx, "payment %s in %d ms (auth code %s, reference %s): %s", payment.status.upper(), tx.payment_ms,
             payment.auth_code or "-", payment.terminal_reference or "-", payment.message)


def _log(tx: Transaction, message: str, *args) -> None:
    logging.info("TX %s: " + message, tx.id, *args)


def _save(tx: Transaction) -> None:
    path = tx.json_path
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(tx.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logging.error("TX %s: could not save %s: %s", tx.id, path, exc)
        tmp.unlink(missing_ok=True)
