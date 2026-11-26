"""Administrator rights: detection, restarting elevated, and starting elevated at logon via a scheduled task.

A POS program that runs as administrator only accepts simulated taps from programs that also run as
administrator (Windows "UIPI"), so Swift Screen Reader can restart itself elevated and start elevated
at logon without a UAC prompt through a Task Scheduler task.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import winreg
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from . import EXE_NAME

TASK_NAME = "Swift Screen Reader"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = EXE_NAME
ERROR_CANCELLED = 1223
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
GA_ROOT = 2
CREATE_NO_WINDOW = 0x08000000
SW_SHOWNORMAL = 1

if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]


@dataclass(frozen=True)
class WindowOwner:
    pid: int
    name: str
    elevated: bool | None  # None = unknown


def is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(_shell32.IsUserAnAdmin())
    except AttributeError:
        return False


def process_elevated(pid: int) -> bool | None:
    """True/False if the process runs as administrator; None if it can't be told."""
    process = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return None
    try:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            # A normal program may not read an administrator program's token: that itself means elevated.
            return True if not is_elevated() and ctypes.get_last_error() == 5 else None
        try:
            elevation = wintypes.DWORD()
            size = wintypes.DWORD()
            if not _advapi32.GetTokenInformation(token, TOKEN_ELEVATION, ctypes.byref(elevation),
                                                 ctypes.sizeof(elevation), ctypes.byref(size)):
                return None
            return bool(elevation.value)
        finally:
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(process)


def _process_name(pid: int) -> str:
    process = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return f"process {pid}"
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if _kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
        return f"process {pid}"
    finally:
        _kernel32.CloseHandle(process)


def window_owner_at(x: int, y: int) -> WindowOwner | None:
    """The program whose window is at a screen point (physical pixels)."""
    if sys.platform != "win32":
        return None
    hwnd = _user32.WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return None
    hwnd = _user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    return WindowOwner(pid.value, _process_name(pid.value), process_elevated(pid.value))


def app_command(*args: str) -> tuple[str, list[str]]:
    """How to start the tray app: (program, arguments)."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).with_name(f"{EXE_NAME}.exe")), list(args)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else sys.executable), ["-m", "swift_screen_reader.launcher", *args]


def _quote(arg: str) -> str:
    return subprocess.list2cmdline([arg])


def relaunch_elevated(*args: str) -> str:
    """Start the app as administrator (UAC prompt). Returns "" on success, else an error message."""
    program, arguments = app_command(*args)
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.lpVerb = "runas"
    info.lpFile = program
    info.lpParameters = " ".join(_quote(a) for a in arguments)
    info.lpDirectory = str(Path(program).parent)
    # Not SW_HIDE: Windows applies this to the new copy's first window, which would then stay invisible.
    info.nShow = SW_SHOWNORMAL
    if _shell32.ShellExecuteExW(ctypes.byref(info)):
        return ""
    error = ctypes.get_last_error()
    return "cancelled" if error == ERROR_CANCELLED else f"error {error}"


def wait_for_exit(pid: int, timeout_seconds: float) -> None:
    process = _kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if process:
        _kernel32.WaitForSingleObject(process, int(timeout_seconds * 1000))
        _kernel32.CloseHandle(process)


# -- start at logon --------------------------------------------------------------


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)


def startup_task_exists() -> bool:
    if sys.platform != "win32":
        return False
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0


def _task_xml(user: str, program: str, arguments: list[str]) -> str:
    args = " ".join(_quote(a) for a in arguments)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts Swift Screen Reader as administrator at logon, without a UAC prompt.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(program)}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(str(Path(program).parent))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _set_run_entry(enabled: bool) -> None:
    """The plain (non-administrator) start-at-logon entry, which the scheduled task replaces."""
    roots = [winreg.HKEY_CURRENT_USER] + ([winreg.HKEY_LOCAL_MACHINE] if not enabled else [])
    for root in roots:
        try:
            with winreg.OpenKey(root, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    program, arguments = app_command("--minimized")
                    winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, " ".join(_quote(a) for a in [program, *arguments]))
                else:
                    winreg.DeleteValue(key, RUN_VALUE)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logging.warning("Could not update the startup entry: %s", exc)


def set_startup_task(enabled: bool) -> str:
    """Create or remove the logon task (needs administrator rights). Returns "" or an error message."""
    if enabled:
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
        program, arguments = app_command("--minimized")
        path = Path(tempfile.gettempdir()) / f"{EXE_NAME}-task.xml"
        path.write_text(_task_xml(user, program, arguments), encoding="utf-16")
        try:
            result = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(path), "/F")
        finally:
            path.unlink(missing_ok=True)
        if result.returncode != 0:
            return (result.stderr or result.stdout).strip() or f"schtasks failed ({result.returncode})"
        _set_run_entry(False)
        logging.info("Start at logon: scheduled task '%s' created for %s (as administrator)", TASK_NAME, user)
        return ""
    result = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if result.returncode != 0 and startup_task_exists():
        return (result.stderr or result.stdout).strip() or f"schtasks failed ({result.returncode})"
    _set_run_entry(True)
    logging.info("Start at logon: scheduled task removed; starting normally (not as administrator) again")
    return ""
