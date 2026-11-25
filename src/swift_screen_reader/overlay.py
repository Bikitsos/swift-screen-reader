"""Full-screen "Processing..." cover shown on every monitor for a few seconds after each capture."""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QFont, QGuiApplication, QPainter, QPaintEvent, QScreen
from PySide6.QtWidgets import QWidget

WDA_EXCLUDEFROMCAPTURE = 0x11


class ProcessingOverlay(QObject):
    """Covers all screens and swallows mouse and keyboard input until the time is up."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._covers: list[_Cover] = []
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self.hide)

    @property
    def visible(self) -> bool:
        return bool(self._covers)

    def show(self, text: str, seconds: float) -> None:
        """Show (or keep showing, restarting the countdown) for `seconds`."""
        if not self._covers:
            self._covers = [_Cover(screen, text) for screen in QGuiApplication.screens()]
            for cover in self._covers:
                cover.show()
            self._covers[0].activateWindow()
            self._covers[0].grabKeyboard()
        else:
            for cover in self._covers:
                cover.set_text(text)
        self._timer.start(int(seconds * 1000))

    def hide(self) -> None:
        self._timer.stop()
        covers, self._covers = self._covers, []
        for cover in covers:
            cover.releaseKeyboard()
            cover.finish()


class _Cover(QWidget):
    def __init__(self, screen: QScreen, text: str) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self._text = text
        self._closing = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.WaitCursor)
        self.setGeometry(screen.geometry())
        self.winId()  # create the native window now so its capture exclusion is set before it is shown
        if sys.platform == "win32":
            # Keep the cover out of screenshots (Windows 10 2004+), so a capture during it shows the real screen.
            ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(int(self.winId())), WDA_EXCLUDEFROMCAPTURE)

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def finish(self) -> None:
        self._closing = True
        self.close()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(15, 15, 15, 235))
        font = QFont(self.font())
        font.setPointSize(36)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
        p.end()

    # Swallow input so nothing underneath can be used while processing.
    def mousePressEvent(self, event) -> None:
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        event.accept()

    def wheelEvent(self, event) -> None:
        event.accept()

    def keyPressEvent(self, event) -> None:
        event.accept()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            super().closeEvent(event)
        else:
            event.ignore()  # e.g. Alt+F4
