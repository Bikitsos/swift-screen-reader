"""PyInstaller entry point for SwiftScreenReaderCli.exe (console; prints the saved screenshot path)."""

import sys

from swift_screen_reader.cli import main

sys.exit(main())
