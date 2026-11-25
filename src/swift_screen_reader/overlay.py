"""Full-screen payment screens on every monitor: "Processing..." while working, then a result to tap away."""

from __future__ import annotations

import ctypes
import sys
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QGuiApplication, QPainter, QPaintEvent, QPen, QScreen
from PySide6.QtWidgets import QWidget

WDA_EXCLUDEFROMCAPTURE = 0x11
# Taps this soon after the result appears are ignored, so a tap meant for the POS doesn't close it unseen.
DISMISS_GUARD_SECONDS = 0.6

PROCESSING, SUCCESS, FAILURE, WARNING = "processing", "success", "failure", "warning"
COLORS = {SUCCESS: QColor("#2e7d32"), FAILURE: QColor("#c62828"), WARNING: QColor("#ef6c00"), PROCESSING: QColor("#1e88e5")}


class PaymentScreen(QObject):
    """Covers all screens and swallows input; a result screen emits `dismissed` when tapped anywhere."""

    dismissed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._covers: list[_Cover] = []
        self.kind = ""
        self._shown_at = 0.0
        self._spin = QTimer(self, interval=40)
        self._spin.timeout.connect(self._tick)
        self._angle = 0

    @property
    def visible(self) -> bool:
        return bool(self._covers)

    def show_processing(self, title: str, detail: str = "") -> None:
        self._show(PROCESSING, title, detail, "")
        self._spin.start()

    def show_result(self, kind: str, title: str, detail: str, hint: str = "Tap anywhere to continue") -> None:
        self._spin.stop()
        self._show(kind, title, detail, hint)

    def hide(self) -> None:
        self._spin.stop()
        covers, self._covers = self._covers, []
        for cover in covers:
            cover.releaseKeyboard()
            cover.finish()
        self.kind = ""

    def _show(self, kind: str, title: str, detail: str, hint: str) -> None:
        self.kind = kind
        self._shown_at = time.monotonic()
        if not self._covers:
            self._covers = [_Cover(self, screen) for screen in QGuiApplication.screens()]
            for cover in self._covers:
                cover.show()
            self._covers[0].activateWindow()
            self._covers[0].grabKeyboard()
        for cover in self._covers:
            cover.set_content(kind, title, detail, hint)

    def _tick(self) -> None:
        self._angle = (self._angle + 12) % 360
        for cover in self._covers:
            cover.angle = self._angle
            cover.update()

    def _on_input(self) -> None:
        if self.kind and self.kind != PROCESSING and time.monotonic() - self._shown_at >= DISMISS_GUARD_SECONDS:
            self.hide()
            self.dismissed.emit()


class _Cover(QWidget):
    def __init__(self, owner: PaymentScreen, screen: QScreen) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.owner = owner
        self.kind = PROCESSING
        self.title = self.detail = self.hint = ""
        self.angle = 0
        self._closing = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setGeometry(screen.geometry())
        self.winId()  # create the native window now so its capture exclusion is set before it is shown
        if sys.platform == "win32":
            # Keep the cover out of screenshots (Windows 10 2004+).
            ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(int(self.winId())), WDA_EXCLUDEFROMCAPTURE)

    def set_content(self, kind: str, title: str, detail: str, hint: str) -> None:
        self.kind, self.title, self.detail, self.hint = kind, title, detail, hint
        self.setCursor(Qt.CursorShape.WaitCursor if kind == PROCESSING else Qt.CursorShape.PointingHandCursor)
        self.update()

    def finish(self) -> None:
        self._closing = True
        self.close()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(18, 18, 18, 240))
        color = COLORS.get(self.kind, COLORS[PROCESSING])
        unit = max(8.0, min(self.width(), self.height()) / 60)
        cx, cy = self.width() / 2, self.height() / 2

        r = unit * 5
        badge = QRectF(cx - r, cy - r * 3.2, r * 2, r * 2)
        x, y = badge.center().x(), badge.center().y()
        if self.kind == PROCESSING:
            p.setPen(QPen(QColor(255, 255, 255, 40), unit * 0.8))
            p.drawEllipse(badge)
            pen = QPen(color, unit * 0.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(badge, -self.angle * 16, 100 * 16)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawEllipse(badge)
            pen = QPen(QColor("white"), unit * 0.9)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            if self.kind == SUCCESS:
                p.drawPolyline([QPointF(x - 0.45 * r, y + 0.02 * r), QPointF(x - 0.12 * r, y + 0.35 * r),
                                QPointF(x + 0.48 * r, y - 0.3 * r)])
            elif self.kind == FAILURE:
                p.drawLine(QPointF(x - 0.38 * r, y - 0.38 * r), QPointF(x + 0.38 * r, y + 0.38 * r))
                p.drawLine(QPointF(x + 0.38 * r, y - 0.38 * r), QPointF(x - 0.38 * r, y + 0.38 * r))
            else:
                p.drawLine(QPointF(x, y - 0.5 * r), QPointF(x, y + 0.12 * r))
                p.drawPoint(QPointF(x, y + 0.48 * r))

        def text(value: str, size: float, top: float, bold: bool, color: QColor) -> None:
            if not value:
                return
            font = QFont(self.font())
            font.setPixelSize(max(10, round(size)))
            font.setBold(bold)
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRectF(unit * 2, top, self.width() - unit * 4, size * 2.6),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, value)

        text(self.title, unit * 3.2, cy - unit * 0.5, True, QColor("white"))
        text(self.detail, unit * 2.4, cy + unit * 4.5, False, QColor(255, 255, 255, 220))
        text(self.hint, unit * 1.6, cy + unit * 11, False, QColor(255, 255, 255, 150))
        p.end()

    # Swallow input so nothing underneath can be used; on a result screen any tap or key closes it.
    def mousePressEvent(self, event) -> None:
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        event.accept()
        self.owner._on_input()

    def mouseDoubleClickEvent(self, event) -> None:
        event.accept()

    def wheelEvent(self, event) -> None:
        event.accept()

    def keyPressEvent(self, event) -> None:
        event.accept()
        if not event.isAutoRepeat():
            self.owner._on_input()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            super().closeEvent(event)
        else:
            event.ignore()  # e.g. Alt+F4
