"""Client for the running tray service: one JSON line in, one JSON line out over a per-user named pipe.

Deliberately Qt-free so `SwiftScreenReaderCli.exe` starts in a fraction of a second.
"""

from __future__ import annotations

import json
import sys
import time

from . import pipe_name

ERROR_PIPE_BUSY = 231
ERROR_ALREADY_EXISTS = 183
MAX_REPLY = 1024 * 1024


def pipe_path() -> str:
    return rf"\\.\pipe\{pipe_name()}"


_instance_lock = None


def acquire_instance_lock() -> bool:
    """Per-user named mutex held for the life of the process; False if another copy holds it.

    Needed because Windows lets several processes listen on the same pipe name.
    """
    global _instance_lock
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, f"Local\\{pipe_name()}")
    if not handle:
        return True  # can't tell; fall back to the pipe check
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _instance_lock = handle
    return True


def request(message: dict, busy_timeout: float = 5.0) -> dict | None:
    """Send a command to the service; returns its reply, or None if the service is not running."""
    if sys.platform != "win32":
        return None
    deadline = time.monotonic() + busy_timeout
    while True:
        try:
            pipe = open(pipe_path(), "r+b", buffering=0)
            break
        except FileNotFoundError:
            return None
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_PIPE_BUSY and time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            return None

    with pipe:
        pipe.write(json.dumps(message).encode("utf-8") + b"\n")
        reply = b""
        while b"\n" not in reply and len(reply) < MAX_REPLY:
            try:
                chunk = pipe.read(65536)
            except (BrokenPipeError, OSError):
                break
            if not chunk:
                break
            reply += chunk
    try:
        data = json.loads(reply.split(b"\n", 1)[0])
    except ValueError:
        return {"ok": False, "error": "The service sent an invalid reply."}
    return data if isinstance(data, dict) else {"ok": False, "error": "The service sent an invalid reply."}
