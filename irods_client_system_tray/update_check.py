"""Asynchronous, read-only checks for newer stable PyPI releases."""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


PACKAGE_NAME = "irods-client-system-tray"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000


def installed_version() -> str | None:
    """Prefer current source metadata over a stale editable-install record."""

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.is_file():
        try:
            source_version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
            Version(source_version)
            return source_version
        except (OSError, KeyError, TypeError, ValueError):
            pass

    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return None


def latest_compatible_version(payload: object, current: str) -> str | None:
    """Find a newer, non-yanked stable release compatible with this Python."""

    installed = Version(current)
    if not isinstance(payload, dict) or not isinstance(payload.get("releases"), dict):
        raise ValueError("PyPI release metadata is missing")

    python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    newest: Version | None = None
    for release, artifacts in payload["releases"].items():
        try:
            candidate = Version(release)
        except (InvalidVersion, TypeError):
            continue
        if candidate.is_prerelease or candidate.is_devrelease or candidate <= installed:
            continue
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("yanked"):
                continue
            required = artifact.get("requires_python")
            try:
                compatible = not required or python in SpecifierSet(required)
            except (InvalidSpecifier, TypeError):
                continue
            if compatible:
                if newest is None or candidate > newest:
                    newest = candidate
                break
    return str(newest) if newest is not None else None


class UpdateChecker(QObject):
    """Fetch PyPI metadata on Qt's event loop, never blocking the GUI thread."""

    checked = Signal(object)  # Newer version, or None when already up to date.
    failed = Signal()

    def __init__(self, current_version: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.current_version = current_version
        self.manager = QNetworkAccessManager(self)
        self._pending: QNetworkReply | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(CHECK_INTERVAL_MS)
        self.timer.timeout.connect(self.check)
        self.startup_timer = QTimer(self)
        self.startup_timer.setSingleShot(True)
        self.startup_timer.timeout.connect(self.check)
        if os.environ.get("IRODS_CLIENT_SYSTEM_TRAY_DISABLE_UPDATE_CHECK") != "1":
            self.timer.start()
            self.startup_timer.start(10_000)

    def check(self) -> None:
        if self._pending is not None:
            return
        request = QNetworkRequest(QUrl(PYPI_URL))
        request.setTransferTimeout(10_000)
        reply = self.manager.get(request)
        self._pending = reply
        reply.finished.connect(lambda: self._handle_reply(reply))

    def _handle_reply(self, reply: QNetworkReply) -> None:
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self.failed.emit()
                return
            payload = json.loads(bytes(reply.readAll()))
            self.checked.emit(latest_compatible_version(payload, self.current_version))
        except (ValueError, TypeError):
            self.failed.emit()
        finally:
            self._pending = None
            reply.deleteLater()
