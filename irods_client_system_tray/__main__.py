"""Run the iRODS client system tray application as a module."""

from __future__ import annotations

import sys

from .main import main


if __name__ == "__main__":
    sys.exit(main())
