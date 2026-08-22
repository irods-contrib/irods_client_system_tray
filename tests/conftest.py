"""Shared test configuration for real-server integration tests.

Most tests stub the iRODS session and need these credentials only as inert values.
The tests that would contact a real server are gated behind ``IRODS_TEST_LIVE_SERVER``
and skip everywhere else.

To run them against a live zone, export the variables below and set ``IRODS_TEST_LIVE_SERVER=1``:

    IRODS_TEST_LIVE_SERVER  set to any non-empty value to enable the gated tests
    IRODS_TEST_HOST         hostname of the iRODS catalog provider
    IRODS_TEST_PORT         defaults to 1247
    IRODS_TEST_USER         iRODS user name
    IRODS_TEST_PASSWORD     iRODS password
    IRODS_TEST_ZONE         iRODS zone name
    IRODS_TEST_COLLECTION   an existing collection the user may write to

Nothing here reads a credential file or the app's own irods_environment.json, so a CI
job only has to supply these as secrets.
"""

from __future__ import annotations

import os

import pytest

# Must be set before any QApplication is constructed. The widget tests never render to
# a real display, so this keeps them runnable headlessly.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from irods_client_system_tray.config import IRODSEnvironment

# Imported directly by the gated upload test, so it stays a module-level constant.
IRODS_TEST_COLLECTION = os.environ.get("IRODS_TEST_COLLECTION", "/tempZone/home/alice")

requires_irods_server = pytest.mark.skipif(
    not os.environ.get("IRODS_TEST_LIVE_SERVER"),
    reason="Set IRODS_TEST_LIVE_SERVER=1 and the IRODS_TEST_* variables to run against a live zone.",
)


@pytest.fixture
def irods_environment() -> IRODSEnvironment:
    """Credentials for the worker: inert defaults locally, a real zone when configured."""

    return IRODSEnvironment(
        irods_host=os.environ.get("IRODS_TEST_HOST", "irods.example.org"),
        irods_port=int(os.environ.get("IRODS_TEST_PORT", "1247")),
        irods_user_name=os.environ.get("IRODS_TEST_USER", "alice"),
        irods_password=os.environ.get("IRODS_TEST_PASSWORD", "alicepass"),
        irods_zone_name=os.environ.get("IRODS_TEST_ZONE", "tempZone"),
    )


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session; Qt does not support creating a second."""

    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application
