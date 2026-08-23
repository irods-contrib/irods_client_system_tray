"""Persistent activity log helpers for the tray application."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import ACTIVITY_LOG_PATH


class UTCFormatter(logging.Formatter):
    """Format logging timestamps as the UTC values shown in the activity UI."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ActivityLog:
    """Append activity to a rotating file and read recent entries for display."""

    def __init__(
        self,
        path: Path = ACTIVITY_LOG_PATH,
        *,
        max_bytes: int = 1_000_000,
        backup_count: int = 5,
        visible_entry_count: int = 50,
    ) -> None:
        self.path = path
        self.backup_count = backup_count
        self.visible_entry_count = visible_entry_count
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(f"{__name__}.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers.clear()

        handler = RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(UTCFormatter("%(asctime)s - %(message)s"))
        self._logger.addHandler(handler)

    def append(self, message: str) -> None:
        """Write one activity message to the persistent rotating log."""

        self._logger.info(str(message).replace("\n", " ").replace("\r", " "))

    def read_recent(self) -> list[str]:
        """Return recent activity entries oldest-first for display."""

        lines: list[str] = []
        for path in self._ordered_log_paths():
            try:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
            except FileNotFoundError:
                continue

        entries = [line for line in lines if line.strip()]
        return entries[-self.visible_entry_count :]

    def _ordered_log_paths(self) -> list[Path]:
        """Return rotated log files oldest-first, followed by the current log."""

        backups = [
            self.path.with_name(f"{self.path.name}.{index}")
            for index in range(self.backup_count, 0, -1)
        ]
        return backups + [self.path]

    def close(self) -> None:
        """Close the underlying file handler."""

        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)
