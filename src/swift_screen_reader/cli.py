"""Command line: ask the tray app for a capture, so all its settings apply (area, folder, OCR, payment).

If the app isn't running, `capture` starts it hidden in the tray first; `--direct` only saves a screenshot.
Prints the saved file path on success. Exit codes: 0 ok, 1 capture/command failed, 2 bad arguments,
3 the service is not running (status/quit only).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from . import APP_NAME, EXE_NAME, __version__, ipc

COMMANDS = ("capture", "select", "show", "status", "quit")
START_TIMEOUT = 20.0


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=f"{APP_NAME}: capture the preselected screen area with the app's settings "
                    "(area, folder, amount reading, payment).",
        epilog="If the app isn't running, 'capture' starts it in the tray first (a few seconds), "
               "so all its settings apply. --direct only saves a screenshot, without starting the app.",
    )
    parser.add_argument("command", nargs="?", default="capture", choices=COMMANDS,
                        help="capture (default), select (choose the area), show (open the window), status, quit")
    parser.add_argument("-o", "--output", metavar="FILE", help="save to this .png file instead of the screenshots folder")
    parser.add_argument("--area", metavar="X,Y,W,H", help="capture this area (physical pixels) instead of the saved one")
    parser.add_argument("--direct", action="store_true",
                        help="only save a screenshot in this process: no app, no amount reading or payment")
    parser.add_argument("--json", action="store_true", help="print the full JSON result")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def start_service(timeout: float = START_TIMEOUT) -> bool:
    """Start the tray app hidden and wait until it answers on its pipe."""
    launch_gui("--minimized")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if ipc.request({"cmd": "ping"}) is not None:
            return True
    return False


def launch_gui(*args: str) -> None:
    """Start the tray app detached (used when the service isn't running)."""
    if getattr(sys, "frozen", False):
        cmd = [str(Path(sys.executable).with_name(f"{EXE_NAME}.exe")), *args]
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        cmd = [str(pythonw if pythonw.exists() else sys.executable), "-m", "swift_screen_reader.launcher", *args]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)


def _capture_direct(area_text: str | None, output: str | None) -> dict:
    from . import config
    from .capture import CaptureError, enable_dpi_awareness, take_screenshot

    enable_dpi_awareness()
    try:
        area = config.Area.parse(area_text) if area_text else None
        path = take_screenshot(config.load(), area, output)
    except (ValueError, CaptureError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": str(path), "direct": True}


def run(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = build_parser(prog).parse_args(argv)
    output = str(Path(args.output).resolve()) if args.output else None

    if args.command == "capture":
        message = {"cmd": "capture", "area": args.area, "output": output}
        result = None if args.direct else ipc.request(message)
        if result is None and not args.direct:
            if start_service():
                result = ipc.request(message)
            else:
                print(f"{APP_NAME} did not start; saving the screenshot only.", file=sys.stderr)
        if result is None:
            result = _capture_direct(args.area, output)
    elif args.command in ("select", "show"):
        result = ipc.request({"cmd": args.command})
        if result is None:
            launch_gui(*(["--select"] if args.command == "select" else []))
            result = {"ok": True, "started": True}
    else:
        result = ipc.request({"cmd": "ping" if args.command == "status" else "quit"})
        if result is None:
            _print({"ok": False, "running": False, "error": "The service is not running."}, args.json, sys.stderr)
            return 3

    if not result.get("ok"):
        _print(result, args.json, sys.stderr)
        return 1
    if args.json:
        _print(result, True)
    elif "path" in result:
        print(result["path"])
    elif args.command == "status":
        print(f"Running (version {result.get('version')}, pid {result.get('pid')})")
    return 0


def _print(result: dict, as_json: bool, stream=None) -> None:
    stream = stream or sys.stdout
    if stream is None:  # windowed exe: no console
        return
    if as_json:
        print(json.dumps(result), file=stream)
    else:
        print(result.get("error", ""), file=stream)


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
