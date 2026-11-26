"""Simulated touch or mouse click at a screen point (physical pixels), used to press a POS button."""

from __future__ import annotations

import ctypes
import sys
import time

TOUCH, MOUSE = "touch", "mouse"
TAP_METHODS = {TOUCH: "Touch", MOUSE: "Mouse click"}

PT_TOUCH = 2
POINTER_FLAG_INRANGE, POINTER_FLAG_INCONTACT = 0x2, 0x4
POINTER_FLAG_DOWN, POINTER_FLAG_UP = 0x10000, 0x40000
TOUCH_MASK_CONTACTAREA, TOUCH_MASK_PRESSURE = 0x1, 0x4
TOUCH_FEEDBACK_DEFAULT = 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
HOLD_SECONDS = 0.06
CONTACT_RADIUS = 2


class TapError(OSError):
    pass


def parse_point(text: str) -> tuple[int, int] | None:
    try:
        x, y = (int(part) for part in text.replace(" ", "").split(","))
    except ValueError:
        return None
    return x, y


if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    class POINTER_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerType", wintypes.DWORD),
            ("pointerId", ctypes.c_uint32),
            ("frameId", ctypes.c_uint32),
            ("pointerFlags", ctypes.c_uint32),
            ("sourceDevice", wintypes.HANDLE),
            ("hwndTarget", wintypes.HWND),
            ("ptPixelLocation", wintypes.POINT),
            ("ptHimetricLocation", wintypes.POINT),
            ("ptPixelLocationRaw", wintypes.POINT),
            ("ptHimetricLocationRaw", wintypes.POINT),
            ("dwTime", wintypes.DWORD),
            ("historyCount", ctypes.c_uint32),
            ("InputData", ctypes.c_int32),
            ("dwKeyStates", wintypes.DWORD),
            ("PerformanceCount", ctypes.c_uint64),
            ("ButtonChangeType", ctypes.c_int),
        ]

    class POINTER_TOUCH_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerInfo", POINTER_INFO),
            ("touchFlags", ctypes.c_uint32),
            ("touchMask", ctypes.c_uint32),
            ("rcContact", wintypes.RECT),
            ("rcContactRaw", wintypes.RECT),
            ("orientation", ctypes.c_uint32),
            ("pressure", ctypes.c_uint32),
        ]

    _user32.InitializeTouchInjection.argtypes = [ctypes.c_uint32, wintypes.DWORD]
    _user32.InjectTouchInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO)]

_touch_ready = False


def _touch(x: int, y: int) -> None:
    global _touch_ready
    if not _touch_ready:
        if not _user32.InitializeTouchInjection(1, TOUCH_FEEDBACK_DEFAULT):
            raise TapError(f"Touch input is not available (error {ctypes.get_last_error()})")
        _touch_ready = True
    info = POINTER_TOUCH_INFO()
    info.pointerInfo.pointerType = PT_TOUCH
    info.pointerInfo.pointerId = 0
    info.pointerInfo.ptPixelLocation = wintypes.POINT(x, y)
    info.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_PRESSURE
    info.rcContact = wintypes.RECT(x - CONTACT_RADIUS, y - CONTACT_RADIUS, x + CONTACT_RADIUS, y + CONTACT_RADIUS)
    info.pressure = 32000
    for flags in (POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT, POINTER_FLAG_UP):
        info.pointerInfo.pointerFlags = flags
        if not _user32.InjectTouchInput(1, ctypes.byref(info)):
            raise TapError(f"Touch injection failed (error {ctypes.get_last_error()})")
        time.sleep(HOLD_SECONDS)


def _click(x: int, y: int) -> None:
    previous = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(previous))
    if not _user32.SetCursorPos(x, y):
        raise TapError(f"Could not move the mouse (error {ctypes.get_last_error()})")
    time.sleep(0.02)
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(HOLD_SECONDS)
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.02)
    _user32.SetCursorPos(previous.x, previous.y)


def tap(x: int, y: int, method: str = TOUCH) -> str:
    """Tap at (x, y). Falls back to a mouse click if touch injection is unavailable; returns the method used."""
    if sys.platform != "win32":
        raise TapError("Simulated taps are only supported on Windows.")
    if method == TOUCH:
        try:
            _touch(x, y)
            return TOUCH
        except TapError:
            _click(x, y)
            return MOUSE
    _click(x, y)
    return MOUSE
