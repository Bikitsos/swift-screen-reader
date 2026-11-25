"""System-wide keyboard shortcuts (Win32 RegisterHotKey) that work while the app is hidden in the tray."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QKeySequence

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_QUIT, WM_HOTKEY = 0x0012, 0x0312
PM_NOREMOVE = 0
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_K = Qt.Key
_SPECIAL_KEYS = {
    _K.Key_Print: 0x2C, _K.Key_Pause: 0x13, _K.Key_Insert: 0x2D, _K.Key_Delete: 0x2E,
    _K.Key_Home: 0x24, _K.Key_End: 0x23, _K.Key_PageUp: 0x21, _K.Key_PageDown: 0x22,
    _K.Key_Left: 0x25, _K.Key_Up: 0x26, _K.Key_Right: 0x27, _K.Key_Down: 0x28, _K.Key_Space: 0x20,
}
# Keys that may be used without a modifier (they don't interfere with typing).
_STANDALONE = {0x2C, 0x13}


def _vk(key: Qt.Key) -> int | None:
    value = int(key.value if hasattr(key, "value") else key)
    if ord("A") <= value <= ord("Z") or ord("0") <= value <= ord("9"):
        return value
    f1 = int(_K.Key_F1.value)
    if f1 <= value <= f1 + 23:
        return 0x70 + value - f1
    return _SPECIAL_KEYS.get(key)


def parse(text: str) -> tuple[int, int]:
    """'Ctrl+Shift+F9' -> (Win32 modifiers, virtual key). Raises ValueError for unusable shortcuts."""
    sequence = QKeySequence.fromString(text, QKeySequence.SequenceFormat.PortableText)
    if sequence.isEmpty():
        raise ValueError(f"Invalid shortcut {text!r}")
    combo = sequence[0]
    vk = _vk(combo.key())
    if vk is None:
        raise ValueError(f"{text} can't be used: use a letter, digit, F1-F24, Print, Pause or a navigation key")
    qt_mods = combo.keyboardModifiers()
    mods = 0
    for qt_mod, win_mod in ((Qt.KeyboardModifier.ControlModifier, MOD_CONTROL), (Qt.KeyboardModifier.AltModifier, MOD_ALT),
                            (Qt.KeyboardModifier.ShiftModifier, MOD_SHIFT), (Qt.KeyboardModifier.MetaModifier, MOD_WIN)):
        if qt_mods & qt_mod:
            mods |= win_mod
    is_function_key = 0x70 <= vk <= 0x87
    if not mods & (MOD_CONTROL | MOD_ALT | MOD_WIN) and not is_function_key and vk not in _STANDALONE:
        raise ValueError(f"{text} needs Ctrl, Alt or Win, or it would block normal typing")
    return mods, vk


def display(text: str) -> str:
    return QKeySequence.fromString(text, QKeySequence.SequenceFormat.PortableText).toString(
        QKeySequence.SequenceFormat.NativeText
    )


if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


class _Worker(threading.Thread):
    """Owns the hotkey registrations: they belong to the thread whose message queue receives WM_HOTKEY."""

    def __init__(self, bindings: dict[int, tuple[int, int]], on_hotkey) -> None:
        super().__init__(name="hotkeys", daemon=True)
        self.bindings = bindings
        self.on_hotkey = on_hotkey
        self.errors: dict[int, int] = {}
        self.ready = threading.Event()
        self.thread_id = 0

    def run(self) -> None:
        self.thread_id = _kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)  # creates the message queue
        registered = []
        for hotkey_id, (mods, vk) in self.bindings.items():
            if _user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                registered.append(hotkey_id)
            else:
                self.errors[hotkey_id] = ctypes.get_last_error()
        self.ready.set()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.on_hotkey(int(msg.wParam))
        finally:
            for hotkey_id in registered:
                _user32.UnregisterHotKey(None, hotkey_id)

    def stop(self) -> None:
        if self.thread_id:
            _user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.join(2)


class HotkeyManager(QObject):
    """Registers named shortcuts; emits `triggered(name)` on the GUI thread when one is pressed."""

    triggered = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker: _Worker | None = None
        self._names: dict[int, str] = {}

    def apply(self, shortcuts: dict[str, str]) -> dict[str, str]:
        """Replace all registrations with `shortcuts` (name -> 'Ctrl+Shift+F9'; empty = off).

        Returns name -> error message for shortcuts that could not be registered.
        """
        self.stop()
        errors: dict[str, str] = {}
        bindings: dict[int, tuple[int, int]] = {}
        names: dict[int, str] = {}
        seen: dict[tuple[int, int], str] = {}
        for index, (name, text) in enumerate(shortcuts.items(), start=1):
            if not text:
                continue
            try:
                binding = parse(text)
            except ValueError as exc:
                errors[name] = str(exc)
                continue
            if binding in seen:
                errors[name] = f"{display(text)} is already used for {seen[binding]}"
                continue
            seen[binding] = name
            bindings[index] = binding
            names[index] = name
        if sys.platform != "win32":
            errors.update({name: "Global shortcuts are only supported on Windows" for name in names.values()})
            return errors
        if not bindings:
            return errors

        self._names = names
        self._worker = _Worker(bindings, self._on_hotkey)
        self._worker.start()
        self._worker.ready.wait(2)
        for hotkey_id, code in self._worker.errors.items():
            name = names[hotkey_id]
            text = display(shortcuts[name])
            if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                errors[name] = f"{text} is already used by another program"
            else:
                errors[name] = f"{text} could not be registered (error {code})"
        for hotkey_id, name in names.items():
            if hotkey_id not in self._worker.errors:
                logging.info("Shortcut %s: %s", display(shortcuts[name]), name)
        for message in errors.values():
            logging.warning("Shortcut: %s", message)
        return errors

    def stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker = None

    def _on_hotkey(self, hotkey_id: int) -> None:
        # Runs on the worker thread; the signal is delivered queued to the GUI thread.
        name = self._names.get(hotkey_id)
        if name:
            self.triggered.emit(name)
