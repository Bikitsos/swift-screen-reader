"""Full-screen overlay to drag-select the capture area, and a short highlight of the current area."""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen, QScreen
from PySide6.QtWidgets import QWidget

from .capture import Monitor, monitors
from .config import Area

MIN_SIZE = 4
ACCENT = QColor("#1e88e5")


def _monitor_for(screen: QScreen) -> Monitor:
    """Physical rectangle of a Qt screen (Qt only exposes per-screen scaled 'logical' coordinates)."""
    try:
        found = monitors()
    except Exception:  # noqa: BLE001 - non-Windows or API failure: fall back to the scale factor
        found = []
    for monitor in found:
        if monitor.device.lower() == screen.name().lower():
            return monitor
    geo, ratio = screen.geometry(), screen.devicePixelRatio()
    return Monitor(screen.name(), round(geo.x() * ratio), round(geo.y() * ratio),
                   round((geo.x() + geo.width()) * ratio), round((geo.y() + geo.height()) * ratio), False)


def _scale(screen: QScreen, monitor: Monitor) -> tuple[float, float]:
    geo = screen.geometry()
    return monitor.width / max(1, geo.width()), monitor.height / max(1, geo.height())


def to_physical(screen: QScreen, rect: QRect) -> Area:
    """Widget-local logical rect on `screen` -> physical virtual-desktop area."""
    monitor = _monitor_for(screen)
    sx, sy = _scale(screen, monitor)
    left, top = monitor.left + round(rect.left() * sx), monitor.top + round(rect.top() * sy)
    right = monitor.left + round((rect.left() + rect.width()) * sx)
    bottom = monitor.top + round((rect.top() + rect.height()) * sy)
    return Area(left, top, right - left, bottom - top)


def to_logical(area: Area) -> QRect | None:
    """Physical area -> global logical rect, using the screen that contains the area's centre."""
    cx, cy = area.x + area.width // 2, area.y + area.height // 2
    for screen in QGuiApplication.screens():
        monitor = _monitor_for(screen)
        if monitor.contains(cx, cy):
            sx, sy = _scale(screen, monitor)
            geo = screen.geometry()
            return QRect(
                geo.x() + round((area.x - monitor.left) / sx),
                geo.y() + round((area.y - monitor.top) / sy),
                max(1, round(area.width / sx)),
                max(1, round(area.height / sy)),
            )
    return None


class AreaSelector(QObject):
    """Covers every screen; emits the dragged area in physical pixels, or None if cancelled."""

    finished = Signal(object)

    def __init__(self, current: Area | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        current_rect = to_logical(current) if current else None
        self._overlays = [_Overlay(self, screen, current_rect) for screen in QGuiApplication.screens()]
        self._done = False

    def start(self) -> None:
        for overlay in self._overlays:
            overlay.show()
            overlay.setGeometry(overlay.screen_.geometry())
        under_mouse = QGuiApplication.screenAt(QCursor.pos())
        target = next((o for o in self._overlays if o.screen_ is under_mouse), self._overlays[0])
        target.activateWindow()
        target.raise_()

    def finish(self, area: Area | None) -> None:
        if self._done:
            return
        self._done = True
        for overlay in self._overlays:
            overlay.close()
            overlay.deleteLater()
        self.finished.emit(area)


class _Overlay(QWidget):
    def __init__(self, selector: AreaSelector, screen: QScreen, current: QRect | None) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.selector = selector
        self.screen_ = screen
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(screen.geometry())
        self._origin: QPoint | None = None
        self._rect = QRect()
        geo = screen.geometry()
        self._current = current.translated(-geo.x(), -geo.y()) if current and geo.intersects(current) else None

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self._current is not None and self._rect.isNull():
            p.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.PenStyle.DashLine))
            p.drawRect(self._current.adjusted(0, 0, -1, -1))
        if not self._rect.isNull():
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            # Alpha 1, not 0: fully transparent pixels would let mouse clicks fall through on Windows.
            p.fillRect(self._rect, QColor(0, 0, 0, 1))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(ACCENT, 2))
            p.drawRect(self._rect.adjusted(0, 0, -1, -1))
            area = to_physical(self.screen_, self._rect)
            self._label(p, f"{area.width} x {area.height}  at  {area.x}, {area.y}", self._rect.bottomLeft() + QPoint(0, 6))
        else:
            self._label(p, "Drag to select the capture area.  Esc to cancel.", QPoint(20, 20))
        p.end()

    def _label(self, p: QPainter, text: str, pos: QPoint) -> None:
        metrics = p.fontMetrics()
        box = metrics.boundingRect(text).adjusted(-8, -5, 8, 5)
        box.moveTopLeft(pos)
        if box.bottom() > self.height():
            box.moveBottom(self._rect.top() - 6 if not self._rect.isNull() else self.height() - 6)
        p.fillRect(box, QColor(30, 30, 30, 220))
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._rect = QRect()
        elif event.button() == Qt.MouseButton.RightButton:
            self.selector.finish(None)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._origin is not None:
            pos = event.position().toPoint()
            dx, dy = pos.x() - self._origin.x(), pos.y() - self._origin.y()
            # Built from size, not QRect(p1, p2), whose inclusive corners make it 1px too big.
            rect = QRect(min(pos.x(), self._origin.x()), min(pos.y(), self._origin.y()), abs(dx), abs(dy))
            self._rect = rect.intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        self._origin = None
        if self._rect.width() < MIN_SIZE or self._rect.height() < MIN_SIZE:
            self._rect = QRect()
            self.update()
            return
        self.selector.finish(to_physical(self.screen_, self._rect))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.selector.finish(None)

    def closeEvent(self, event) -> None:
        self.selector.finish(None)
        super().closeEvent(event)


class AreaHighlight(QWidget):
    """Click-through frame drawn around the capture area for a moment."""

    def __init__(self, area: Area, seconds: float = 2.5) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        rect = to_logical(area)
        self.valid = rect is not None
        if rect is not None:
            self.setGeometry(rect.adjusted(-3, -3, 3, 3))
        QTimer.singleShot(int(seconds * 1000), self.close)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setPen(QPen(QColor("#e53935"), 3))
        p.drawRect(self.rect().adjusted(1, 1, -2, -2))
        p.end()


class PointPicker(QObject):
    """Covers every screen; emits the tapped point (x, y) in physical pixels, or None if cancelled."""

    finished = Signal(object)

    def __init__(self, message: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._covers = [_PickCover(self, screen, message) for screen in QGuiApplication.screens()]
        self._done = False

    def start(self) -> None:
        for cover in self._covers:
            cover.show()
            cover.setGeometry(cover.screen_.geometry())
        under_mouse = QGuiApplication.screenAt(QCursor.pos())
        target = next((c for c in self._covers if c.screen_ is under_mouse), self._covers[0])
        target.activateWindow()
        target.raise_()

    def finish(self, point: tuple[int, int] | None) -> None:
        if self._done:
            return
        self._done = True
        for cover in self._covers:
            cover.close()
            cover.deleteLater()
        self.finished.emit(point)


class _PickCover(QWidget):
    def __init__(self, picker: PointPicker, screen: QScreen, message: str) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.picker = picker
        self.screen_ = screen
        self.message = message
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(screen.geometry())

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        # Light tint so the POS buttons stay visible; alpha above 0 so taps don't fall through.
        p.fillRect(self.rect(), QColor(0, 0, 0, 60))
        metrics = p.fontMetrics()
        box = metrics.boundingRect(self.message).adjusted(-14, -9, 14, 9)
        box.moveCenter(QPoint(self.width() // 2, 40))
        p.fillRect(box, QColor(30, 30, 30, 230))
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.message)
        p.end()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.picker.finish(None)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        area = to_physical(self.screen_, QRect(pos.x(), pos.y(), 1, 1))
        self.picker.finish((area.x, area.y))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.picker.finish(None)

    def closeEvent(self, event) -> None:
        self.picker.finish(None)
        super().closeEvent(event)


class PointHighlight(QWidget):
    """Click-through target marker drawn around a point for a moment."""

    RADIUS = 28

    def __init__(self, x: int, y: int, seconds: float = 2.5) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        rect = to_logical(Area(x, y, 1, 1))
        self.valid = rect is not None
        if rect is not None:
            r = self.RADIUS
            self.setGeometry(rect.x() - r, rect.y() - r, 2 * r + 1, 2 * r + 1)
        QTimer.singleShot(int(seconds * 1000), self.close)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.RADIUS
        p.setPen(QPen(QColor("#e53935"), 3))
        p.drawEllipse(QPoint(r, r), r - 3, r - 3)
        p.drawLine(r, 4, r, 2 * r - 4)
        p.drawLine(4, r, 2 * r - 4, r)
        p.end()
