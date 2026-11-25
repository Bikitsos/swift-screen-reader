"""PyInstaller entry point for SwiftScreenReader.exe (tray app; `--capture` takes a screenshot and exits)."""

import sys

from swift_screen_reader.launcher import main

sys.exit(main())
