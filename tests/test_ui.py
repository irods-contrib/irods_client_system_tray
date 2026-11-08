"""Tests for the settings window, the login dialog, and the zone-anchored path input.

Widgets are built for real under Qt's offscreen platform, set in conftest.py, so no
display is required. No iRODS server is contacted: login validation and error
classification both finish before any background sign-in starts, and the heartbeat
probe is aimed at a local socket.
"""

from __future__ import annotations

import socket
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel

from irods_client_system_tray.activity_log import ActivityLog
from irods_client_system_tray.config import (
    DEFAULT_POST_UPLOAD_ACTION,
    IRODSEnvironment,
    MonitoredDirectory,
)
from irods_client_system_tray.ui import (
    AddDirectoryDialog,
    LoginDialog,
    LoginWorker,
    SettingsWindow,
    ZoneRootLineEdit,
)

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
    assert login_dialog._auth_thread is None
    assert login_dialog.sign_in_button.isEnabled()


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


def test_setting_the_toggle_does_not_emit_a_change(
    settings_window,
):
    # Checks refreshing the window does not look like the user toggled monitoring.
    # If it did, every refresh would trigger a save and a restart of the watchers.
    toggles: list[bool] = []
    settings_window.monitoring_toggled.connect(toggles.append)

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


def test_activity_log_loads_existing_entries(activity_log):
    # Checks opening a new settings window restores recent activity from the log file
    # instead of relying on a list held by the previous widget instance.
    activity_log.append("older event")
    activity_log.append("newer event")

    window = SettingsWindow(activity_log)

    assert window.activity_list.count() == 2
    assert " - older event" in window.activity_list.item(0).text()
    assert " - newer event" in window.activity_list.item(1).text()


def test_settings_tab_only_contains_retry_controls(settings_window):
    settings_tab = settings_window.tabs.widget(1)

    assert settings_tab.findChild(QFrame, "retryCard") is not None
    assert settings_tab.findChild(QFrame, "irodsCard") is None
    assert not hasattr(settings_window, "irods_password_input")
    assert settings_window.save_retry_settings_button.text() == "Save Retry Settings"

    requested = []
    settings_window.save_retry_settings_requested.connect(lambda: requested.append(True))
    settings_window.save_retry_settings_button.click()
    assert requested == [True]


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
