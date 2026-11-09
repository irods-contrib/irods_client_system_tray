"""Offline tests for the version selection and asynchronous update request."""

import json

import pytest
from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtNetwork import QNetworkReply

from irods_client_system_tray.update_check import (
    CHECK_INTERVAL_MS,
    PYPI_URL,
    UpdateChecker,
    installed_version,
    latest_compatible_version,
)


def test_latest_compatible_version_ignores_incompatible_yanked_and_prereleases():
    releases = {
        "0.1.0": [{"yanked": False}],
        "0.2.0": [{"yanked": False, "requires_python": ">=3.10"}],
        "0.3.0rc1": [{"yanked": False}],
        "0.3.0": [{"yanked": True}],
        "0.4.0": [{"yanked": False, "requires_python": ">=99"}],
        "0.5.0": [],
        "invalid": [{"yanked": False}],
    }
    payload = {"releases": releases}

    assert latest_compatible_version(payload, "0.1.0") == "0.2.0"
    assert latest_compatible_version(payload, "0.2.0") is None
    with pytest.raises(ValueError, match="metadata is missing"):
        latest_compatible_version({}, "0.1.0")


def test_source_checkout_reports_the_project_version_not_stale_editable_metadata():
    from irods_client_system_tray import update_check

    pyproject = update_check.Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert installed_version() == update_check.tomllib.loads(
        pyproject.read_text(encoding="utf-8")
    )["project"]["version"]


class FakeReply(QObject):
    finished = Signal()

    def __init__(self, payload, *, failed=False):
        super().__init__()
        self.payload = payload
        self.network_failed = failed

    def error(self):
        return (
            QNetworkReply.NetworkError.ConnectionRefusedError if self.network_failed
            else QNetworkReply.NetworkError.NoError
        )

    def readAll(self):
        return QByteArray(json.dumps(self.payload).encode())


def test_update_check_runs_asynchronously_and_limits_requests(qapp):
    checker = UpdateChecker("0.1.0")
    checker.startup_timer.stop()
    results = []
    checker.checked.connect(results.append)
    replies = []

    class FakeManager:
        def get(self, request):
            assert request.url().toString() == PYPI_URL
            reply = FakeReply({"releases": {"0.2.0": [{"yanked": False}]}})
            replies.append(reply)
            return reply

    checker.manager = FakeManager()
    assert checker.timer.interval() == CHECK_INTERVAL_MS
    checker.check()
    assert len(replies) == 1
    assert results == []  # No synchronous HTTP or UI blocking.
    checker.check()
    assert len(replies) == 1  # Never overlap requests.
    replies[0].finished.emit()
    assert results == ["0.2.0"]
    assert checker._pending is None
    checker.timer.stop()


def test_update_check_handles_network_errors_without_reporting_an_update(qapp):
    checker = UpdateChecker("0.1.0")
    checker.startup_timer.stop()
    checker.timer.stop()
    failures = []
    checker.failed.connect(lambda: failures.append(True))
    reply = FakeReply({}, failed=True)
    checker.manager = type("FakeManager", (), {"get": lambda _self, _request: reply})()

    checker.check()
    reply.finished.emit()

    assert failures == [True]
    assert checker._pending is None


def test_periodic_checks_are_enabled_unless_explicitly_disabled(qapp, monkeypatch):
    disabled = UpdateChecker("0.1.0")
    assert not disabled.timer.isActive()
    assert not disabled.startup_timer.isActive()

    monkeypatch.delenv("IRODS_CLIENT_SYSTEM_TRAY_DISABLE_UPDATE_CHECK")
    enabled = UpdateChecker("0.1.0")
    assert enabled.timer.isActive()
    assert enabled.startup_timer.isActive()
    enabled.timer.stop()
    enabled.startup_timer.stop()
