"""Entry point of SwiftScreenReader.exe: `--capture` takes a screenshot without loading Qt, otherwise the tray app."""

from __future__ import annotations

import sys

from . import EXE_NAME


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--capture" in argv:
        from .cli import run

        rest = [a for a in argv if a != "--capture"]
        return run(["capture", *rest], prog=f"{EXE_NAME} --capture")

    from .gui import main as gui_main

    return gui_main(argv)


if __name__ == "__main__":
    sys.exit(main())
