"""Client for the running tray service: one JSON line in, one JSON line out over a per-user named pipe.

Deliberately Qt-free so `SwiftScreenReaderCli.exe` starts in a fraction of a second.
"""

from __future__ import annotations

import json
import sys
import time

from . import pipe_name

ERROR_PIPE_BUSY = 231
MAX_REPLY = 1024 * 1024


def pipe_path() -> str:
    return rf"\\.\pipe\{pipe_name()}"


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
