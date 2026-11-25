"""Screen capture with the Win32 GDI API (no Qt), so a cold start of the command line stays fast."""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path

from .config import Area, Settings

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
MONITORINFOF_PRIMARY = 1
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
# Larger areas would need gigabytes of memory; nothing real is this big.
MAX_PIXELS = 100_000_000

_lock = threading.Lock()


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class Monitor:
    device: str
    left: int
    top: int
    right: int
    bottom: int
    primary: bool

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM]
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    _gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ]
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise CaptureError("Screen capture is only supported on Windows.")


def enable_dpi_awareness() -> None:
    """Use physical pixels for all coordinates. Qt apps do this themselves; call it in non-Qt processes."""
    if sys.platform != "win32":
        return
    try:
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return
    except AttributeError:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        _user32.SetProcessDPIAware()


def monitors() -> list[Monitor]:
    _require_windows()
    found: list[Monitor] = []

    def callback(handle, _hdc, _rect, _data) -> bool:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if _user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(Monitor(info.szDevice, r.left, r.top, r.right, r.bottom, bool(info.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    _user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    return found


def virtual_screen() -> Area:
    _require_windows()
    m = _user32.GetSystemMetrics
    return Area(m(SM_XVIRTUALSCREEN), m(SM_YVIRTUALSCREEN), m(SM_CXVIRTUALSCREEN), m(SM_CYVIRTUALSCREEN))


def grab(area: Area) -> bytes:
    """Top-down 32-bit BGRA pixels of the area (parts outside every monitor come back black)."""
    _require_windows()
    if not area.valid:
        raise CaptureError("The capture area is empty.")
    if area.width * area.height > MAX_PIXELS:
        raise CaptureError("The capture area is too large.")
    with _lock:
        screen = _user32.GetDC(None)
        if not screen:
            raise CaptureError("Cannot access the screen (is the desktop locked or is this a service session?).")
        memory = bitmap = old = None
        try:
            memory = _gdi32.CreateCompatibleDC(screen)
            bitmap = _gdi32.CreateCompatibleBitmap(screen, area.width, area.height)
            if not memory or not bitmap:
                raise CaptureError("Not enough memory to capture the screen.")
            old = _gdi32.SelectObject(memory, bitmap)
            if not _gdi32.BitBlt(memory, 0, 0, area.width, area.height, screen, area.x, area.y, SRCCOPY | CAPTUREBLT):
                raise CaptureError(f"Screen copy failed (error {ctypes.get_last_error()}).")
            _gdi32.SelectObject(memory, old)
            old = None

            info = BITMAPINFO()
            header = info.bmiHeader
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = area.width
            header.biHeight = -area.height  # negative = top-down rows
            header.biPlanes = 1
            header.biBitCount = 32
            buffer = ctypes.create_string_buffer(area.width * area.height * 4)
            if _gdi32.GetDIBits(memory, bitmap, 0, area.height, buffer, ctypes.byref(info), DIB_RGB_COLORS) != area.height:
                raise CaptureError("Reading the captured pixels failed.")
            return buffer.raw
        finally:
            if old:
                _gdi32.SelectObject(memory, old)
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            if memory:
                _gdi32.DeleteDC(memory)
            _user32.ReleaseDC(None, screen)


def encode_png(width: int, height: int, bgra: bytes, level: int = 3) -> bytes:
    """Encode top-down BGRA pixels as an RGB PNG."""
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    stride = width * 3
    rows = b"".join(b"\x00" + rgb[i:i + stride] for i in range(0, len(rgb), stride))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, level)) + chunk(b"IEND", b"")


def capture_png(area: Area) -> bytes:
    return encode_png(area.width, area.height, grab(area))


def save_png(data: bytes, path: Path) -> Path:
    """Write atomically so a program watching the folder never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def take_screenshot(settings: Settings, area: Area | None = None, output: str | Path | None = None) -> Path:
    """Capture `area` (default: the configured one) and save it as PNG; returns the file path."""
    area = area or settings.area
    if area is None:
        raise CaptureError("No capture area selected yet. Open the app and choose Select area.")
    return save_png(capture_png(area), settings.output_path(output))
