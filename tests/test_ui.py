"""Tests for the settings window, the login dialog, and the zone-anchored path input.

Widgets are built for real under Qt's offscreen platform, set in conftest.py, so no
display is required. No iRODS server is contacted: login validation and error
classification both finish before any background sign-in starts, and the heartbeat
probe is aimed at a local socket.
"""

from __future__ import annotations

import socket
import sys
import threading
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QLabel, QListWidget, QPushButton, QSpinBox

from irods_client_system_tray.activity_log import ActivityLog
import irods_client_system_tray.config as config_module
from irods_client_system_tray.config import (
    DEFAULT_POST_UPLOAD_ACTION,
    IRODSEnvironment,
    MonitoredDirectory,
)
from irods_client_system_tray.ui import (
    AddDirectoryDialog,
    LoginDialog,
    LoginWorker,
    RegexEditorDialog,
    SettingsWindow,
    ZoneRootLineEdit,
)
import irods_client_system_tray.ui as ui_module

pytestmark = pytest.mark.usefixtures("qapp")

INVALID_DIRECTORY_COLOR = "#b42318"


@pytest.fixture
def login_dialog():
    return LoginDialog(IRODSEnvironment())


@pytest.fixture
def activity_log(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    yield log
    log.close()


@pytest.fixture
def settings_window(activity_log):
    return SettingsWindow(activity_log)


def test_settings_window_uses_irods_ingest_name(settings_window):
    assert settings_window.windowTitle() == "iRODS Ingest"
    assert settings_window.title_label.text() == "iRODS Ingest"
    assert not hasattr(settings_window, "subtitle_label")


def test_session_summary_is_right_aligned_in_the_window_header(settings_window):
    settings_window.set_irods_environment(
        IRODSEnvironment(
            irods_user_name="alice",
            irods_zone_name="tempZone",
            irods_host="irods.example.org",
            irods_port=1247,
        )
    )

    assert settings_window.session_summary_label.text() == "alice#tempZone"
    assert settings_window.session_host_label.text() == "irods.example.org:1247"
    assert not settings_window.session_host_label.isHidden()
    assert settings_window.session_summary_label.parent() is settings_window
    assert settings_window.tabs.widget(0).findChildren(QLabel, "sessionUserZoneLabel") == []
    assert settings_window.session_summary_label.alignment() & Qt.AlignmentFlag.AlignRight
    assert settings_window.session_host_label.alignment() & Qt.AlignmentFlag.AlignRight
    assert settings_window.upload_summary_label.parent() is settings_window
    assert settings_window.upload_summary_label.alignment() & Qt.AlignmentFlag.AlignRight
    assert settings_window.upload_queue_label.alignment() & Qt.AlignmentFlag.AlignRight
    assert settings_window.upload_throughput_label.alignment() & Qt.AlignmentFlag.AlignRight


def test_header_shows_worker_queue_and_recent_speed(settings_window):
    settings_window.set_upload_summary(
        3, 2, 5, 12 * 1024 * 1024,
        remaining_bytes=12 * 1024 * 1024 + 512 * 1024, eta_seconds=3723,
    )

    assert settings_window.upload_summary_label.text() == "Active Workers: 2/3"
    assert settings_window.upload_queue_label.text() == (
        "Queue: 5 (12.5 MiB remaining, ETA 1h 2m 3s)"
    )
    assert settings_window.upload_throughput_label.text() == "Throughput: 12.0 MiB/s"

    settings_window.set_upload_summary(2, 0, 0, None, remaining_bytes=0, eta_seconds=0)
    assert settings_window.upload_summary_label.text() == "Active Workers: 0/2"
    assert settings_window.upload_queue_label.text() == "Queue: 0 (0 B remaining, ETA 0s)"
    assert settings_window.upload_throughput_label.text() == "Throughput: --"

    settings_window.set_upload_summary(2, 1, 0, 2048, remaining_bytes=1024, eta_seconds=4)
    assert settings_window.upload_queue_label.text() == "Queue: 0 (1.0 KiB remaining, ETA 4s)"
    assert settings_window.upload_throughput_label.text() == "Throughput: 2.0 KiB/s"

    settings_window.set_upload_summary(2, 1, 1, None)
    assert settings_window.upload_queue_label.text() == "Queue: 1 (size unknown, ETA --)"


@pytest.mark.parametrize(
    ("seconds", "formatted"),
    [(0, "0s"), (42, "42s"), (60, "1m"), (62, "1m 2s"),
     (3600, "1h"), (3602, "1h 2s"), (3723, "1h 2m 3s")],
)
def test_queue_eta_omits_zero_time_units(settings_window, seconds, formatted):
    settings_window.set_upload_summary(2, 1, 1, 1, remaining_bytes=10, eta_seconds=seconds)

    assert settings_window.upload_queue_label.text().endswith(f"ETA {formatted})")


def test_session_summary_clears_when_no_connection_is_configured(settings_window):
    settings_window.set_irods_environment(
        IRODSEnvironment(irods_user_name="alice", irods_host="irods.example.org")
    )

    settings_window.set_irods_environment(IRODSEnvironment())

    assert settings_window.session_summary_label.text() == "No iRODS session is configured"
    assert settings_window.session_host_label.text() == ""
    assert settings_window.session_host_label.isHidden()


def test_status_line_is_hidden_without_a_message(settings_window):
    assert settings_window.status_label.isHidden()

    settings_window.set_status_message("Upload failed.", is_error=True)
    assert not settings_window.status_label.isHidden()

    settings_window.set_status_message("")
    assert settings_window.status_label.isHidden()


@pytest.fixture
def one_shot_server():
    """Start a local socket that answers a single request with a fixed payload."""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve(payload: bytes) -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                connection.recv(1024)
                connection.sendall(payload)
        except OSError:  # pragma: no cover - only on teardown races
            pass

    def start(payload: bytes) -> tuple[str, int]:
        threading.Thread(target=serve, args=(payload,), daemon=True).start()
        return listener.getsockname()

    yield start
    listener.close()


# --- login error messages -------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # Cannot reach the box at all -> point at the connection fields.
        (
            OSError("Could not reach iRODS server at irods.example.org:1247"),
            "Could not connect. Check the host, port, and zone.",
        ),
        # Server answered and said no -> point at the credentials.
        (
            Exception("CAT_INVALID_AUTHENTICATION"),
            "Sign-in failed. Check the username and password.",
        ),
        # Anything else -> show it verbatim rather than guessing wrong.
        (Exception("disk quota exceeded"), "Sign-in failed: disk quota exceeded"),
    ],
)
def test_login_errors_are_categorized_for_the_user(login_dialog, error, expected):
    # Checks the three-way split between "wrong address", "wrong credentials", and
    # "something else". Getting this backwards sends someone off resetting a password
    # when their real problem is a typo in the hostname.
    assert login_dialog._format_login_error(error) == expected


# --- login form validation ------------------------------------------------------


def test_login_has_heading_but_no_subtitle_or_initial_status(login_dialog):
    labels = [label.text() for label in login_dialog.findChildren(QLabel)]

    assert login_dialog.windowTitle() == "iRODS Ingest"
    assert "iRODS Login" in labels
    assert login_dialog.sign_in_button.text() == "Login"
    assert [button.text() for button in login_dialog.findChildren(QPushButton)] == ["Login"]
    assert not any("Enter your iRODS connection details" in text for text in labels)
    assert login_dialog.status_label.text() == ""
    assert login_dialog.status_label.isHidden()


@pytest.mark.parametrize("previous_login", [True, False])
def test_login_focuses_password_only_when_connection_fields_are_filled(
    qapp, previous_login
):
    environment = IRODSEnvironment(
        irods_host="irods.example.org" if previous_login else "",
        irods_user_name="alice",
        irods_zone_name="tempZone",
    )
    dialog = LoginDialog(environment)

    try:
        dialog.show()
        qapp.processEvents()

        if previous_login:
            assert dialog.focusWidget() is dialog.password_input
        else:
            assert dialog.focusWidget() is not dialog.password_input
        assert dialog.password_input.text() == ""
    finally:
        dialog.hide()


@pytest.mark.parametrize(
    ("host", "port", "zone", "user", "password", "expected"),
    [
        ("", "1247", "tempZone", "alice", "pw", "Enter host, port, zone, username, and password."),
        ("h", "not-a-port", "tempZone", "alice", "pw", "Enter a valid numeric port."),
        ("h", "70000", "tempZone", "alice", "pw", "Port must be between 1 and 65535."),
    ],
)
def test_invalid_login_details_are_rejected_locally(
    login_dialog, host, port, zone, user, password, expected
):
    # Each of these is wrong in a way the app can see for itself, so the dialog says
    # so straight away and leaves the form usable. Starting a background sign-in
    # first would make the user wait for a network round trip only to be told the
    # port was never a number.
    login_dialog.host_input.setText(host)
    login_dialog.port_input.setText(port)
    login_dialog.zone_name_input.setText(zone)
    login_dialog.user_name_input.setText(user)
    login_dialog.password_input.setText(password)

    login_dialog._attempt_login()

    assert login_dialog.status_label.text() == expected
    assert not login_dialog.status_label.isHidden()
    assert login_dialog._auth_thread is None
    assert login_dialog.sign_in_button.isEnabled()


def test_login_progress_message_uses_logging_in(login_dialog, monkeypatch):
    login_dialog.host_input.setText("irods.example.org")
    login_dialog.user_name_input.setText("alice")
    login_dialog.password_input.setText("password")
    monkeypatch.setattr(login_dialog, "_start_authentication", lambda *_args: None)

    login_dialog._attempt_login()

    assert login_dialog.status_label.text() == "Logging in to iRODS..."


@pytest.mark.parametrize("outcome", ["success", "authentication_failed", "unreachable"])
def test_login_worker_reports_authentication_outcomes(monkeypatch, outcome):
    from irods import session as irods_session

    environment = IRODSEnvironment(
        irods_host="irods.example.org", irods_user_name="alice",
        irods_password="password", irods_zone_name="tempZone",
    )
    worker = LoginWorker(environment)
    fake_session = MagicMock()
    fake_session.__enter__.return_value = fake_session
    monkeypatch.setattr(irods_session, "iRODSSession", lambda **_kwargs: fake_session)
    if outcome == "authentication_failed":
        fake_session.users.get.side_effect = ValueError("invalid credentials")
    if outcome == "unreachable":
        def fail_probe():
            raise OSError("connection refused")
        monkeypatch.setattr(worker, "_probe_server_heartbeat", fail_probe)
    else:
        monkeypatch.setattr(worker, "_probe_server_heartbeat", lambda: None)
    results = []
    finished = []
    worker.authentication_finished.connect(lambda *args: results.append(args))
    worker.finished.connect(lambda: finished.append(True))

    worker.authenticate()

    assert finished == [True]
    assert len(results) == 1
    assert results[0][0] is (outcome == "success")
    if outcome != "success":
        assert isinstance(results[0][1], (OSError, ValueError))
    if outcome == "unreachable":
        fake_session.users.get.assert_not_called()
    else:
        fake_session.users.get.assert_called_once_with("alice", "tempZone")


def test_login_worker_reports_missing_irods_client(monkeypatch):
    worker = LoginWorker(IRODSEnvironment())
    monkeypatch.setitem(sys.modules, "irods.session", None)
    results = []
    worker.authentication_finished.connect(lambda *args: results.append(args))

    worker.authenticate()

    assert results[0][0] is False
    assert "python-irodsclient is not installed" in str(results[0][1])


def test_login_probe_explains_connection_refusal(monkeypatch):
    def refuse(_address, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("irods_client_system_tray.ui.socket.create_connection", refuse)
    worker = LoginWorker(IRODSEnvironment(irods_host="irods.example.org"))

    with pytest.raises(OSError, match="Could not reach iRODS server at irods.example.org:1247"):
        worker._probe_server_heartbeat()


@pytest.mark.parametrize("outcome", ["no_result", "failure", "missing_state", "success"])
def test_login_dialog_finalizes_worker_outcomes(login_dialog, outcome):
    login_dialog.password_input.setText("password")
    login_dialog._pending_login = ("irods.example.org", 1247, "tempZone", "alice", "password")
    if outcome == "failure":
        login_dialog._auth_result = (False, ValueError("CAT_INVALID_AUTHENTICATION"))
    elif outcome in {"missing_state", "success"}:
        login_dialog._auth_result = (True, None)
    if outcome == "missing_state":
        login_dialog._pending_login = None

    login_dialog._finalize_authentication_attempt()

    assert login_dialog.sign_in_button.isEnabled()
    if outcome == "success":
        assert login_dialog.result() == login_dialog.DialogCode.Accepted
        assert login_dialog.authenticated_environment == IRODSEnvironment(
            irods_host="irods.example.org", irods_user_name="alice",
            irods_password="password", irods_zone_name="tempZone",
        )
    else:
        assert login_dialog.result() != login_dialog.DialogCode.Accepted
        assert login_dialog.status_label.text()
    if outcome == "failure":
        assert login_dialog.password_input.text() == ""
        assert "CAT_INVALID_AUTHENTICATION" in login_dialog.error_details_view.toPlainText()


# --- heartbeat probe ------------------------------------------------------------


def test_non_irods_endpoint_is_rejected(one_shot_server):
    # Checks a wrong-but-open port (a web server, say) is caught by the quick probe
    # rather than surfacing later as a confusing error from deep in the iRODS client.
    host, port = one_shot_server(b"HTTP/1.1 200 OK")
    worker = LoginWorker(IRODSEnvironment(irods_host=host, irods_port=port))

    with pytest.raises(RuntimeError, match="Heartbeat probe failed"):
        worker._probe_server_heartbeat()


def test_valid_heartbeat_reply_is_accepted(one_shot_server):
    # An endpoint that answers with the expected heartbeat payload passes the probe,
    # and sign-in continues on to the real iRODS client.
    host, port = one_shot_server(LoginWorker.HEARTBEAT_RESPONSE)
    worker = LoginWorker(IRODSEnvironment(irods_host=host, irods_port=port))

    assert worker._probe_server_heartbeat() is None


# --- zone-anchored collection input ---------------------------------------------


def test_changing_zone_rewrites_the_prefix_and_keeps_the_path():
    # Checks the user does not lose their work when the zone changes. The "/zone/"
    # part of the box is locked and swaps automatically; the path they typed after
    # it is preserved.
    field = ZoneRootLineEdit()
    field.set_zone_name("tempZone")
    assert field.text() == "/tempZone/"

    field.setText("/tempZone/home/alice")
    assert field.collection_path() == "/tempZone/home/alice"

    field.set_zone_name("otherZone")
    assert field.text() == "/otherZone/home/alice"


# --- add-folder dialog ----------------------------------------------------------


def test_add_folder_dialog_requires_a_source_folder():
    # Checks OK is refused, with an explanation, until a folder is chosen.
    dialog = AddDirectoryDialog("tempZone", [])
    assert not hasattr(dialog, "recursive_checkbox")

    dialog._accept_if_valid()

    assert dialog.validation_label.text() == "Select a source directory to monitor."
    assert not dialog.isVisible()


def test_add_folder_dialog_returns_the_entered_values(tmp_path):
    # Checks the dialog reports the user's choices unchanged, including trimming
    # stray spaces around a pasted path.
    dialog = AddDirectoryDialog("tempZone", [])
    dialog.source_directory_input.setText(f"  {tmp_path}  ")
    dialog.target_collection_input.setText("/tempZone/home/alice")

    directory = dialog.get_directory()

    assert directory == MonitoredDirectory(
        source_directory=str(tmp_path),
        target_collection="/tempZone/home/alice",
    )


def test_regex_editor_validates_lines_before_accepting():
    editor = RegexEditorDialog([r"\.csv$"])
    editor.pattern_input.setPlainText("  .*\\.csv$  \n  \n[")

    editor._accept_if_valid()

    assert "Invalid regex on line 3" in editor.validation_label.text()
    assert editor.patterns == [r"\.csv$"]
    assert editor.result() != QDialog.DialogCode.Accepted

    editor.pattern_input.setPlainText("  .*\\.csv$  \n  \n^report_")
    editor._accept_if_valid()

    assert editor.patterns == [r".*\.csv$", "^report_"]
    assert editor.validation_label.text() == ""
    assert editor.result() == QDialog.DialogCode.Accepted


def test_add_folder_dialog_requires_patterns_when_regex_filter_is_enabled(tmp_path):
    dialog = AddDirectoryDialog("tempZone", [])
    dialog.source_directory_input.setText(str(tmp_path))
    dialog.regex_filter_toggle.setChecked(True)

    dialog._accept_if_valid()
    assert "Add at least one regex pattern" in dialog.validation_label.text()

    dialog._regex_patterns = [r"\.csv$"]
    dialog.regex_allow_radio.setChecked(True)
    dialog._update_regex_summary()
    dialog._accept_if_valid()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.get_directory().regex_filter.mode == "allow"
    assert dialog.get_directory().regex_filter.patterns == [r"\.csv$"]
    assert "1 pattern configured" in dialog.regex_summary_label.text()


def test_add_folder_dialog_keeps_existing_patterns_when_editor_is_cancelled(monkeypatch):
    dialog = AddDirectoryDialog("tempZone", [])
    dialog._regex_patterns = ["old"]

    class CancelledEditor:
        def __init__(self, _patterns, _parent):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ui_module, "RegexEditorDialog", CancelledEditor)
    dialog._edit_regex_patterns()

    assert dialog._regex_patterns == ["old"]


def test_add_folder_dialog_applies_edited_patterns(monkeypatch):
    dialog = AddDirectoryDialog("tempZone", [])

    class AcceptedEditor:
        def __init__(self, _patterns, _parent):
            self.patterns = ["one", "two"]

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui_module, "RegexEditorDialog", AcceptedEditor)
    dialog._edit_regex_patterns()

    assert dialog._regex_patterns == ["one", "two"]
    assert "2 patterns configured" in dialog.regex_summary_label.text()


@pytest.mark.parametrize("candidate", ["parent", "child", "same"])
def test_add_folder_dialog_rejects_overlapping_folders(tmp_path, candidate):
    watched = tmp_path / "parent" / "watched"
    (watched / "child").mkdir(parents=True)
    candidates = {
        "parent": watched.parent,
        "child": watched / "child",
        "same": watched,
    }
    dialog = AddDirectoryDialog("tempZone", [MonitoredDirectory(str(watched))])
    dialog.source_directory_input.setText(str(candidates[candidate]))

    dialog._accept_if_valid()

    assert "overlaps monitored folder" in dialog.validation_label.text()
    assert str(watched) in dialog.validation_label.text()
    assert dialog.result() != dialog.DialogCode.Accepted


def test_add_folder_dialog_allows_sibling_with_similar_name(tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    sibling = tmp_path / "watched-other"
    sibling.mkdir()
    dialog = AddDirectoryDialog("tempZone", [MonitoredDirectory(str(watched))])
    dialog.source_directory_input.setText(str(sibling))

    dialog._accept_if_valid()

    assert dialog.result() == dialog.DialogCode.Accepted


# --- settings window ------------------------------------------------------------


def test_folder_row_shows_target_and_cleanup_policy(settings_window):
    # Each row summarizes one watched folder: where it uploads to and what happens
    # to the local file afterwards. The cleanup policy is the only warning the app
    # gives that uploading removes the local copy.
    settings_window.set_directories(
        [
            MonitoredDirectory("/trashed", "/tempZone/x", "trash", ""),
            MonitoredDirectory("/wiped", "/tempZone/y", "delete", ""),
            MonitoredDirectory("/moved", "/tempZone/z", "move", "/archive"),
        ],
        set(),
    )

    rows = [settings_window.directory_list.item(row).text() for row in range(3)]

    assert "/tempZone/x" in rows[0] and "send to trash" in rows[0]
    assert "recursive" not in rows[0].lower()
    assert "recursive" not in settings_window.directory_list.item(0).toolTip().lower()
    assert "delete" in rows[1]
    assert "/archive" in rows[2]


def test_legacy_recycle_folder_is_described_as_trash(settings_window):
    settings_window.set_directories(
        [MonitoredDirectory("/legacy", "/tempZone/x", "recycle", "")], set()
    )

    assert "send to trash" in settings_window.directory_list.item(0).text()


def test_missing_monitored_folder_is_flagged_in_overview(settings_window):
    directory = MonitoredDirectory("/missing", "/tempZone/home/alice")

    settings_window.set_directories([directory], {"/missing"})

    item = settings_window.directory_list.item(0)
    assert "does not currently exist" in item.toolTip()
    assert item.foreground().color().name() == INVALID_DIRECTORY_COLOR


@pytest.mark.parametrize("accepted", [False, True])
def test_add_folder_request_is_emitted_only_for_accepted_dialog(
    settings_window, monkeypatch, accepted
):
    requested = []
    settings_window.add_folder_requested.connect(requested.append)
    directory = MonitoredDirectory("/watched", "/tempZone/home/alice")

    class FakeDialog:
        def __init__(self, zone, watched, parent):
            assert zone == "tempZone"
            assert parent is settings_window

        def exec(self):
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

        def get_directory(self):
            return directory

    monkeypatch.setattr(ui_module, "AddDirectoryDialog", FakeDialog)
    settings_window._emit_add_requested()

    assert requested == ([directory] if accepted else [])


def test_overview_buttons_emit_selected_folder_and_failed_upload(settings_window):
    removed = []
    retried = []
    settings_window.remove_folder_requested.connect(removed.append)
    settings_window.retry_failed_upload_requested.connect(retried.append)
    settings_window.set_directories(
        [MonitoredDirectory("/watched", "/tempZone/home/alice")], set()
    )
    settings_window.set_failed_uploads([("/watched/report.csv", "no route", 2, 3)])

    settings_window._emit_remove_selected()
    settings_window._emit_retry_failed_upload_requested()
    assert removed == retried == []

    settings_window.directory_list.setCurrentRow(0)
    settings_window.failed_uploads_list.setCurrentRow(0)
    assert settings_window.remove_button.isEnabled()
    assert settings_window.retry_failed_upload_button.isEnabled()
    settings_window._emit_remove_selected()
    settings_window._emit_retry_failed_upload_requested()

    assert removed == ["/watched"]
    assert retried == ["/watched/report.csv"]
    assert "no route" in settings_window.failed_uploads_list.item(0).toolTip()


def test_setting_the_toggle_does_not_emit_a_change(
    settings_window,
):
    # Checks refreshing the window does not look like the user toggled monitoring.
    # If it did, every refresh would trigger a save and a restart of the watchers.
    toggles: list[bool] = []
    settings_window.monitoring_toggled.connect(toggles.append)
    assert settings_window.monitor_toggle.text() == "Monitoring enabled"

    settings_window.set_monitoring_active(True)

    assert settings_window.monitor_toggle.isChecked()
    assert toggles == []


def test_activity_log_is_persisted_newest_last_and_capped(settings_window):
    # Checks recent activity is written to disk and the list cannot grow without
    # limit during a long monitoring session.
    for index in range(55):
        settings_window.append_activity(f"event {index}")

    assert settings_window.activity_list.count() == 50
    assert " - event 54" in settings_window.activity_list.item(49).text()
    assert " - event 54" in settings_window.activity_log.path.read_text(encoding="utf-8")


def test_recent_activity_lives_in_its_own_tab(settings_window):
    assert [settings_window.tabs.tabText(index) for index in range(settings_window.tabs.count())] == [
        "Overview", "Settings", "Activity", "About"
    ]
    assert settings_window.tabs.widget(0).findChild(QListWidget, "activityList") is None
    assert settings_window.tabs.widget(2).findChild(QListWidget, "activityList") is (
        settings_window.activity_list
    )
    assert settings_window.activity_list.maximumHeight() > 140


def test_about_tab_describes_the_app_and_shows_update_status(settings_window):
    about_tab = settings_window.tabs.widget(3)
    assert about_tab.findChild(QFrame, "aboutCard") is not None
    labels = [label.text() for label in about_tab.findChildren(QLabel)]
    assert any("hands-free ingest of instrument data" in text for text in labels)
    assert any("Version:" in text and "BSD-3-Clause" in text for text in labels)
    assert any("github.com/irods-contrib" in text for text in labels)

    status = settings_window.update_status_label
    settings_window._show_update_result("0.3.0")
    assert "Update available: version 0.3.0" in status.text()
    assert "pypi.org/project/irods-client-system-tray/0.3.0/" in status.text()
    settings_window._show_update_result(None)
    assert status.text() == "You're up to date."
    settings_window._show_update_failure()
    assert "Unable to check" in status.text()


def test_activity_log_loads_existing_entries(activity_log):
    # Checks opening a new settings window restores recent activity from the log file
    # instead of relying on a list held by the previous widget instance.
    activity_log.append("older event")
    activity_log.append("newer event")

    window = SettingsWindow(activity_log)

    assert window.activity_list.count() == 2
    assert " - older event" in window.activity_list.item(0).text()
    assert " - newer event" in window.activity_list.item(1).text()


def test_recent_activity_scrolls_to_bottom_after_first_show(activity_log, qapp):
    for index in range(55):
        activity_log.append(f"event {index}")
    window = SettingsWindow(activity_log)

    try:
        window.show()
        window.tabs.setCurrentIndex(2)
        qapp.processEvents()

        scrollbar = window.activity_list.verticalScrollBar()
        assert scrollbar.maximum() > 0
        assert scrollbar.value() == scrollbar.maximum()

        window.hide()
        scrollbar.setValue(0)
        window.show()
        qapp.processEvents()
        assert scrollbar.value() == 0
    finally:
        window.hide()


def test_activity_tab_scrolls_to_newest_on_selection(activity_log, qapp):
    for index in range(55):
        activity_log.append(f"event {index}")
    window = SettingsWindow(activity_log)

    try:
        window.show()
        window.tabs.setCurrentIndex(2)
        qapp.processEvents()
        scrollbar = window.activity_list.verticalScrollBar()
        scrollbar.setValue(0)
        window.tabs.setCurrentIndex(0)
        window.tabs.setCurrentIndex(2)
        qapp.processEvents()
        assert scrollbar.maximum() > 0
        assert scrollbar.value() == scrollbar.maximum()
    finally:
        window.hide()


def test_settings_tab_contains_upload_controls(settings_window):
    settings_tab = settings_window.tabs.widget(1)

    assert settings_tab.findChild(QFrame, "retryCard") is not None
    assert settings_tab.findChild(QFrame, "irodsCard") is None
    assert not hasattr(settings_window, "irods_password_input")
    assert settings_tab.findChild(QSpinBox, "maximumUploadWorkers") is (
        settings_window.maximum_upload_workers_input
    )
    assert settings_window.save_settings_button.text() == "Save Settings"

    requested = []
    settings_window.save_settings_requested.connect(lambda: requested.append(True))
    settings_window.save_settings_button.click()
    assert requested == [True]


def test_monitored_folders_list_has_a_dedicated_square_corner_style(settings_window):
    assert settings_window.directory_list.objectName() == "monitoredFoldersList"


def test_upload_modes_are_side_by_side_with_memory_cap_units(settings_window, qapp):
    settings_tab = settings_window.tabs.widget(1)
    automatic_card = settings_tab.findChild(QFrame, "automaticModeCard")
    manual_card = settings_tab.findChild(QFrame, "manualModeCard")
    assert automatic_card is not None
    assert manual_card is not None
    assert automatic_card.isAncestorOf(settings_window.automatic_mode_radio)
    assert automatic_card.isAncestorOf(settings_window.memory_cap_input)
    assert manual_card.isAncestorOf(settings_window.manual_mode_radio)
    assert manual_card.isAncestorOf(settings_window.maximum_upload_workers_input)
    assert manual_card.isAncestorOf(settings_window.parallel_upload_threads_input)
    assert "single-threaded PUT" in settings_window.parallel_upload_threads_input.toolTip()
    assert manual_card.isAncestorOf(settings_window.single_write_chunk_input)
    assert manual_card.isAncestorOf(settings_window.parallel_write_chunk_input)
    assert settings_window.memory_cap_description.text().endswith("automatically.")
    assert "parallel-write buffering" in settings_window.memory_cap_description.text()
    assert settings_window.memory_cap_unit_label.text() == "MiB"

    try:
        settings_window.tabs.setCurrentIndex(1)
        settings_window.show()
        qapp.processEvents()
        assert automatic_card.geometry().right() < manual_card.geometry().left()
        assert settings_window.memory_cap_input.geometry().right() < (
            settings_window.memory_cap_unit_label.geometry().left()
        )
        assert settings_window.save_settings_button.isVisible()
        assert settings_window.save_settings_button.geometry().bottom() <= settings_tab.height()
    finally:
        settings_window.hide()


def test_upload_worker_control_defaults_to_two_and_caps_at_cpu_count(
    monkeypatch, activity_log
):
    monkeypatch.setattr(config_module.os, "cpu_count", lambda: 4)
    window = SettingsWindow(activity_log)

    assert window.get_maximum_upload_workers() == 2
    assert window.maximum_upload_workers_input.maximum() == 4
    window.set_maximum_upload_workers(99)
    assert window.get_maximum_upload_workers() == 4

    monkeypatch.setattr(config_module.os, "cpu_count", lambda: 1)
    window.set_maximum_upload_workers(2)
    assert window.get_maximum_upload_workers() == 1
    assert window.maximum_upload_workers_input.maximum() == 1


def test_automatic_worker_control_shows_effective_count(settings_window):
    assert not settings_window.get_automatic_upload_workers()
    assert settings_window.maximum_upload_workers_input.isEnabled()
    assert settings_window.parallel_upload_threads_input.isEnabled()
    assert not settings_window.memory_cap_input.isEnabled()

    settings_window.set_parallel_upload_threads(2)
    assert settings_window.get_parallel_upload_threads() == min(
        2, config_module.maximum_upload_workers_limit()
    )

    settings_window.set_automatic_upload_workers(True, 512)
    settings_window.set_auto_worker_status(2)

    assert settings_window.get_automatic_upload_workers()
    assert settings_window.get_memory_cap() == 512
    assert not settings_window.maximum_upload_workers_input.isEnabled()
    assert not settings_window.parallel_upload_threads_input.isEnabled()
    assert settings_window.maximum_upload_workers_input.value() == 2
    assert settings_window.memory_cap_input.isEnabled()

    settings_window.set_automatic_upload_workers(False, 512)
    assert settings_window.maximum_upload_workers_input.isEnabled()
    assert settings_window.parallel_upload_threads_input.isEnabled()
    assert settings_window.maximum_upload_workers_input.value() == 2
    assert not settings_window.memory_cap_input.isEnabled()


def test_write_chunk_controls_keep_last_effective_values(settings_window):
    assert settings_window.get_write_chunk_settings() == (8, 4)
    assert settings_window.single_write_chunk_input.isEnabled()

    settings_window.set_write_chunk_settings(4, 16)
    settings_window.set_automatic_upload_workers(True, 256)
    settings_window.set_chunk_status(4, 8)

    assert settings_window.get_write_chunk_settings() == (4, 8)
    assert not settings_window.parallel_write_chunk_input.isEnabled()

    settings_window.set_automatic_upload_workers(False, 256)
    assert settings_window.get_write_chunk_settings() == (4, 8)
    assert settings_window.parallel_write_chunk_input.isEnabled()
    settings_window.set_chunk_status(8, 16)
    assert settings_window.get_write_chunk_settings() == (4, 8)


def test_automatic_write_selectors_show_effective_sizes_and_freeze_on_manual(
    settings_window,
):
    settings_window.set_write_chunk_settings(4, 16)
    settings_window.set_chunk_status(8, 4)
    assert settings_window.get_write_chunk_settings() == (4, 16)

    settings_window.automatic_mode_radio.setChecked(True)
    assert not settings_window.single_write_chunk_input.isEnabled()
    settings_window.set_chunk_status(8, 4)
    assert (
        settings_window.single_write_chunk_input.currentData(),
        settings_window.parallel_write_chunk_input.currentData(),
    ) == (8, 4)

    settings_window.set_chunk_status(16, 8)
    assert (
        settings_window.single_write_chunk_input.currentData(),
        settings_window.parallel_write_chunk_input.currentData(),
    ) == (16, 8)
    assert settings_window.get_write_chunk_settings() == (16, 8)

    settings_window.manual_mode_radio.setChecked(True)
    assert settings_window.get_write_chunk_settings() == (16, 8)
    assert settings_window.single_write_chunk_input.isEnabled()


# --- post-upload cleanup in the add-folder dialog -------------------------------


def test_add_folder_dialog_defaults_to_delete():
    # The dialog opens on the app's default action. It is currently "delete", so a user
    # who accepts the form unchanged is opting into permanent removal of local files.
    dialog = AddDirectoryDialog("tempZone", [])

    assert dialog.post_upload_action_input.currentData() == DEFAULT_POST_UPLOAD_ACTION
    assert dialog.post_upload_action_input.findData("recycle") == -1


def test_move_destination_box_appears_only_for_move():
    # Every other action leaves the destination meaningless, so the row is hidden until
    # "move" is chosen.
    dialog = AddDirectoryDialog("tempZone", [])
    assert not dialog.post_upload_destination_widget.isEnabled()

    dialog.post_upload_action_input.setCurrentIndex(
        dialog.post_upload_action_input.findData("move")
    )

    assert dialog.post_upload_destination_widget.isEnabled()


def test_add_folder_dialog_requires_a_move_destination(tmp_path):
    dialog = AddDirectoryDialog("tempZone", [])
    dialog.source_directory_input.setText(str(tmp_path))
    dialog.post_upload_action_input.setCurrentIndex(
        dialog.post_upload_action_input.findData("move")
    )

    dialog._accept_if_valid()

    assert dialog.validation_label.text() == "Select a destination for files moved after upload."
    assert not dialog.isVisible()


@pytest.mark.parametrize("destination", ["inside-itself", "inside-another-watch"])
def test_add_folder_dialog_refuses_a_destination_inside_a_watch(tmp_path, destination):
    # Files moved into a watched folder would be picked up and uploaded again, so the
    # dialog blocks it before the folder is ever saved.
    already_watched = tmp_path / "already"
    already_watched.mkdir()
    source = tmp_path / "watched"
    source.mkdir()
    target = source / "archive" if destination == "inside-itself" else already_watched / "archive"

    dialog = AddDirectoryDialog(
        "tempZone", [MonitoredDirectory(str(already_watched), "/tempZone/x", "trash", "")]
    )
    dialog.source_directory_input.setText(str(source))
    dialog.post_upload_action_input.setCurrentIndex(
        dialog.post_upload_action_input.findData("move")
    )
    dialog.post_upload_destination_input.setText(str(target))

    dialog._accept_if_valid()

    assert "must be outside monitored folders" in dialog.validation_label.text()
    assert not dialog.isVisible()


def test_add_folder_dialog_returns_the_cleanup_action(tmp_path):
    dialog = AddDirectoryDialog("tempZone", [])
    dialog.source_directory_input.setText(str(tmp_path))
    dialog.post_upload_action_input.setCurrentIndex(
        dialog.post_upload_action_input.findData("trash")
    )

    directory = dialog.get_directory()

    assert directory.post_upload_action == "trash"
    assert directory.post_upload_destination == ""
