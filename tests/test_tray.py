"""Tests for the tray controller: folder rules, upload queueing, and settings persistence.

``TrayController`` owns the running application state: it loads and saves both JSON
stores, drives the folder monitor, feeds the upload worker, and updates the settings
window.

The ``controller`` fixture points both stores at ``tmp_path``. A controller built
with its defaults writes ``app_state.json`` and ``irods_environment.json`` under the
user config directory. No iRODS connection is made: the upload worker runs on its own thread
but only ever receives queued_uploads paths.
"""

from __future__ import annotations

import json

import pytest

from irods_client_system_tray import config
from irods_client_system_tray.config import IRODSEnvironment, MonitoredDirectory

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture
def controller(tmp_path, monkeypatch, qapp):
    """A signed-in controller whose state files live in tmp_path, not the repo.

    Monitoring starts disabled so ``_sync_from_config`` does not spin up a real watchdog
    observer; the tests that care about the monitoring flag set it directly.
    """

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "app_state.json")
    monkeypatch.setattr(config, "IRODS_ENVIRONMENT_PATH", tmp_path / "irods_environment.json")

    from irods_client_system_tray.tray import TrayController

    tray_controller = TrayController(qapp)

    # The controller normally hands queued paths to the upload worker, which lives on
    # its own thread and would attempt a real transfer to irods.example.org. Detaching
    # it keeps these tests offline and deterministic; what the controller queues is
    # still observable through the queue_upload signal itself.
    tray_controller.queue_upload.disconnect(tray_controller.upload_worker.upload_file)

    tray_controller._is_authenticated = True
    tray_controller.config.is_monitoring_active = False
    tray_controller.environment = IRODSEnvironment(
        irods_host="irods.example.org",
        irods_user_name="alice",
        irods_password="alicepass",
        irods_zone_name="tempZone",
    )
    yield tray_controller
    tray_controller.shutdown()


@pytest.fixture
def watched(controller, tmp_path):
    """A folder already registered with the controller, recursive, targeting home/alice."""

    folder = tmp_path / "watched"
    folder.mkdir()
    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", True, "keep", ""))
    return folder


@pytest.fixture
def queued_uploads(controller):
    """Capture everything the controller hands to the upload worker, with monitoring on."""

    sent: list[tuple[str, str, str]] = []
    controller.queue_upload.connect(lambda *args: sent.append(args))
    controller.config.is_monitoring_active = True
    return sent


@pytest.fixture
def watched_file(watched):
    """A file inside the watched folder, ready to hand to ``_queue_ingestion``."""

    path = watched / "report.csv"
    path.write_text("data", encoding="utf-8")
    return path


def _stored_folders(controller) -> list[tuple[str, str, bool]]:
    return [
        (directory.source_directory, directory.target_collection, directory.recursive)
        for directory in controller.config.monitored_directories
    ]


# --- adding folders -------------------------------------------------------------


def test_added_folder_target_is_anchored_under_the_zone(controller, watched):
    # Checks the controller fixes up what the dialog hands it. The user types a bare
    # collection path like "home/alice" and it must be stored under their zone.
    assert _stored_folders(controller) == [(str(watched), "/tempZone/home/alice", True)]


def test_duplicate_folder_is_rejected(controller, watched):
    # Checks a duplicate is refused with a message, and the original settings are
    # left alone rather than being overwritten by the second attempt.
    controller.add_directory(MonitoredDirectory(str(watched), "home/somewhere-else", True, "keep", ""))

    assert _stored_folders(controller) == [(str(watched), "/tempZone/home/alice", True)]
    assert "Already monitoring" in controller.window.status_label.text()


def test_folder_inside_a_recursive_watch_is_rejected(controller, watched):
    # Checks overlapping watches are blocked. The parent is already watching
    # subfolders, so adding one would upload every file inside it twice.
    nested = watched / "nested"
    nested.mkdir()

    controller.add_directory(MonitoredDirectory(str(nested), "home/nested", True, "keep", ""))

    assert len(controller.config.monitored_directories) == 1
    assert "already monitored through recursive watch" in controller.window.status_label.text()


def test_subfolder_of_a_non_recursive_watch_is_allowed(controller, tmp_path):
    # A folder may sit inside a watch that covers only its top level. That parent
    # never sees the subfolder's files, so nothing would be uploaded twice.
    top_level = tmp_path / "watched"
    (top_level / "nested").mkdir(parents=True)
    controller.add_directory(MonitoredDirectory(str(top_level), "home/alice", False, "keep", ""))

    controller.add_directory(MonitoredDirectory(str(top_level / "nested"), "home/nested", True, "keep", ""))

    assert len(controller.config.monitored_directories) == 2


def test_removed_folder_is_dropped_from_the_saved_file(controller, watched, tmp_path):
    # Checks removal is persisted, not just cleared from the in-memory list, so the
    # folder does not come back on the next launch.
    controller.remove_directory(str(watched))

    assert controller.config.monitored_directories == []
    saved = json.loads((tmp_path / "app_state.json").read_text(encoding="utf-8"))
    assert saved["monitored_directories"] == []


# --- queueing uploads -----------------------------------------------------------


def test_file_uses_the_deepest_matching_folder_settings(controller, queued_uploads, tmp_path):
    # Checks the right folder wins when watches are nested. The file sits inside both,
    # and using the outer one would upload it to the wrong collection under the wrong
    # subfolder path.
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    controller.add_directory(MonitoredDirectory(str(outer), "home/outer", False, "keep", ""))
    controller.add_directory(MonitoredDirectory(str(inner), "home/inner", True, "keep", ""))
    created = inner / "report.csv"
    created.write_text("data", encoding="utf-8")

    controller._queue_ingestion(str(created))

    # The worker is handed the deepest folder's own settings, cleanup policy included.
    assert queued_uploads == [(str(created), str(inner), "/tempZone/home/inner", "keep", "")]


def test_duplicate_events_queue_a_file_once(controller, queued_uploads, watched_file):
    # Checks duplicate events are ignored. Saving a file in some editors fires both a
    # "created" and a "moved" event for the same file, which would otherwise upload
    # it twice.
    controller._queue_ingestion(str(watched_file))
    controller._queue_ingestion(str(watched_file))

    assert len(queued_uploads) == 1


def test_file_outside_every_watched_folder_is_ignored(controller, queued_uploads, watched, tmp_path):
    # Checks the app only uploads from folders the user actually chose.
    unrelated = tmp_path / "unrelated.csv"
    unrelated.write_text("data", encoding="utf-8")

    controller._queue_ingestion(str(unrelated))

    assert queued_uploads == []


def test_nothing_uploads_while_monitoring_is_paused(controller, queued_uploads, watched_file):
    # Checks the pause toggle actually stops uploads, not just the folder watching.
    controller.config.is_monitoring_active = False

    controller._queue_ingestion(str(watched_file))

    assert queued_uploads == []


def test_nothing_uploads_before_the_user_signs_in(controller, queued_uploads, watched_file):
    # Checks files are not queued while signed out. There are no credentials yet, so
    # anything queued now would just pile up as failures.
    controller._is_authenticated = False

    controller._queue_ingestion(str(watched_file))

    assert queued_uploads == []


def test_folder_without_a_target_collection_warns(
    controller, queued_uploads, tmp_path
):
    # Checks a half-configured folder tells the user what is missing rather than
    # silently doing nothing or uploading somewhere arbitrary.
    folder_without_target = tmp_path / "watched"
    folder_without_target.mkdir()
    controller.config.monitored_directories.append(
        MonitoredDirectory(source_directory=str(folder_without_target), target_collection="")
    )
    created = folder_without_target / "report.csv"
    created.write_text("data", encoding="utf-8")

    controller._queue_ingestion(str(created))

    assert queued_uploads == []
    assert "No target collection configured" in controller.window.status_label.text()


# --- iRODS settings -------------------------------------------------------------


def _fill_settings_form(controller, *, host="irods.example.org", user="alice", zone="tempZone"):
    controller.window.irods_host_input.setText(host)
    controller.window.irods_user_name_input.setText(user)
    controller.window.irods_zone_name_input.setText(zone)
    controller.window.irods_password_input.clear()


def test_blank_password_field_keeps_the_saved_password(controller):
    # Checks saving other settings does not wipe the password. The box is always shown
    # empty, so blank has to mean "leave it alone" — treating it as "clear it" would
    # break uploads every time the user edited an unrelated field.
    _fill_settings_form(controller)

    controller.save_irods_settings()

    assert controller.environment.irods_password == "alicepass"


def test_saving_settings_never_writes_the_password_to_disk(controller, tmp_path):
    # Checks the password does not reach the settings file, by name or by value.
    _fill_settings_form(controller)

    controller.save_irods_settings()

    written = (tmp_path / "irods_environment.json").read_text(encoding="utf-8")
    assert "irods_password" not in written
    assert "alicepass" not in written


def test_saved_snapshot_has_no_password(controller):
    # The environment handed to the saver has the password blanked out. The saver
    # also leaves the field out when writing, so two independent steps keep the
    # password off disk.
    snapshot = controller._without_password(
        IRODSEnvironment(
            irods_host="irods.example.org",
            irods_port=1250,
            irods_user_name="alice",
            irods_password="alicepass",
            irods_zone_name="tempZone",
        )
    )

    assert snapshot == IRODSEnvironment(
        irods_host="irods.example.org",
        irods_port=1250,
        irods_user_name="alice",
        irods_password="",
        irods_zone_name="tempZone",
    )


@pytest.mark.parametrize("blank_field", ["host", "user", "zone"])
def test_incomplete_connection_settings_are_refused(controller, blank_field):
    # Checks any one missing field blocks the save, so the app never stores a
    # connection it cannot actually use.
    _fill_settings_form(controller, **{blank_field: ""})

    controller.save_irods_settings()

    assert controller.window.status_label.text() == "Complete host, user, and zone before saving."


def test_changing_the_zone_repoints_saved_folders(controller, watched):
    # Checks switching zones updates folders the user already set up. Targets are
    # stored as full paths, so without this they would keep pointing at the old zone
    # and every upload would fail.
    _fill_settings_form(controller, zone="otherZone")

    controller.save_irods_settings()

    assert _stored_folders(controller) == [(str(watched), "/otherZone/home/alice", True)]


# --- folder relocation ----------------------------------------------------------


def test_renamed_folder_stays_watched(controller, watched, tmp_path):
    # Checks a rename follows the folder instead of quietly stopping monitoring.
    renamed = tmp_path / "renamed"
    watched.rename(renamed)

    controller._handle_monitored_directory_renamed(str(watched), str(renamed))

    assert _stored_folders(controller) == [(str(renamed), "/tempZone/home/alice", True)]


@pytest.mark.parametrize("outcome", ["moved", "deleted"])
def test_vanished_folder_cancels_pending_uploads(controller, watched, tmp_path, outcome):
    # A folder that has gone away has its pending uploads cancelled, so the worker
    # drops them instead of failing on each missing file in turn.
    #
    # The cancellation only holds while the folder is really absent: the refresh that
    # runs straight afterwards re-enables uploads for every folder still on disk.
    if outcome == "moved":
        watched.rename(tmp_path / "elsewhere")
        controller._handle_monitored_directory_moved(str(watched), str(tmp_path / "elsewhere"))
    else:
        watched.rmdir()
        controller._handle_monitored_directory_deleted(str(watched))

    assert str(watched) in controller.upload_worker._cancelled_monitored_roots
    assert str(watched) in controller.window.status_label.text()


def test_folder_still_on_disk_is_not_left_cancelled(controller, watched):
    # A delete event for a folder that is still on disk is a false alarm. The refresh
    # that follows clears the cancellation, so uploads carry on.
    controller._handle_monitored_directory_deleted(str(watched))

    assert controller.upload_worker._cancelled_monitored_roots == set()


@pytest.mark.parametrize("outcome", ["moved", "deleted", "renamed"])
def test_events_for_unwatched_folders_are_ignored(controller, watched, tmp_path, outcome):
    # Checks the app only reacts to folders it is actually watching. The watcher also
    # sees sibling folders, so a unwatched_folder being moved or deleted must change nothing.
    unwatched_folder = str(tmp_path / "not-monitored")
    destination = str(tmp_path / "wherever")

    if outcome == "moved":
        controller._handle_monitored_directory_moved(unwatched_folder, destination)
    elif outcome == "deleted":
        controller._handle_monitored_directory_deleted(unwatched_folder)
    else:
        controller._handle_monitored_directory_renamed(unwatched_folder, destination)

    assert _stored_folders(controller) == [(str(watched), "/tempZone/home/alice", True)]
    assert unwatched_folder not in controller.upload_worker._cancelled_monitored_roots


# --- upload feedback ------------------------------------------------------------


@pytest.mark.parametrize(
    ("bytes_sent", "total_bytes", "expected"),
    [
        # A zero total would be a divide-by-zero; the status still has to say something.
        (0, 0, "Uploading report.csv..."),
        (1, 4, "Uploading report.csv: 25%"),
    ],
)
def test_upload_progress_is_reported_in_the_status_line(
    controller, bytes_sent, total_bytes, expected
):
    # Checks progress is shown, and that an unknown file size does not crash the
    # percentage calculation.
    controller._handle_upload_progress(
        "/watched/report.csv", "/tempZone/x", bytes_sent, total_bytes
    )

    assert controller.window.status_label.text() == expected


def test_finished_upload_is_released_for_retry(controller, queued_uploads, watched_file):
    # Checks the file is cleared from the in-progress list once done. The same list
    # blocks duplicates, so a file that never cleared could never be re-uploaded
    # after the user edited it.
    controller._queue_ingestion(str(watched_file))
    assert str(watched_file) in controller._queued_uploads

    controller._handle_upload_finished(str(watched_file), "/tempZone/home/alice/report.csv")

    assert str(watched_file) not in controller._queued_uploads


def test_failed_upload_is_held_for_retry_and_reports_the_error(
    controller, queued_uploads, watched_file
):
    # A failed upload stays tracked so the retry schedule can pick it up again. The
    # status line reports the pending retry, and the reason is kept on the job rather
    # than swallowed, so it is still available when the attempts run out.
    controller._queue_ingestion(str(watched_file))

    controller._handle_upload_failed(str(watched_file), "streaming upload: connection reset")

    job = controller._queued_uploads[str(watched_file)]
    assert job.status == "retry_wait"
    assert job.last_error == "streaming upload: connection reset"
    assert controller.window.status_label.text().startswith("Retrying report.csv")


# --- sign out -------------------------------------------------------------------


def test_signing_out_locks_the_app_and_clears_the_queue(controller, queued_uploads, watched_file):
    # Checks signing out really locks things down: no credentials, nothing left in the
    # upload queue, and the settings menu hidden behind the sign-in prompt again.
    controller._queue_ingestion(str(watched_file))

    controller.sign_out()

    assert not controller._is_authenticated
    assert controller._queued_uploads == {}
    assert not controller.open_settings_action.isVisible()
    assert controller.sign_in_action.isVisible()

    # sign_out reopens the login dialog; close it so the fixture can tear down cleanly.
    if controller._login_dialog is not None:
        controller._login_dialog.reject()


# --- post-upload cleanup rules --------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="add_directory() normalizes before it validates, so the move-without-destination "
    "guard is unreachable and the folder is saved as 'delete' instead",
)
def test_move_without_a_destination_is_refused(controller, tmp_path):
    # add_directory() calls normalize_monitored_directories() before checking the action,
    # and that call has already rewritten a "move" with no destination into the default
    # "delete". The guard below it therefore never sees "move" and never fires, so a
    # folder the user asked to move files from is saved as one that deletes them.
    #
    # The add-folder dialog still blocks this, so it is not reachable from the GUI, but
    # the guard in add_directory() is dead code as written.
    folder = tmp_path / "watched"
    folder.mkdir()

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", True, "move", ""))

    assert controller.config.monitored_directories == []
    assert controller.window.status_label.text() == (
        "Choose a destination for files moved after upload."
    )


@pytest.mark.parametrize("destination", ["inside-itself", "inside-another-watch"])
def test_move_destination_inside_a_watched_folder_is_refused(
    controller, watched, tmp_path, destination
):
    # Moving uploaded files into a watched folder would have them picked up and
    # uploaded again, and moved again, round and round.
    folder = tmp_path / "second"
    folder.mkdir()
    target = folder / "archive" if destination == "inside-itself" else watched / "archive"

    controller.add_directory(MonitoredDirectory(str(folder), "home/second", True, "move", str(target)))

    assert len(controller.config.monitored_directories) == 1
    assert "must be outside monitored folders" in controller.window.status_label.text()


def test_move_destination_outside_watched_folders_is_accepted(controller, tmp_path):
    folder = tmp_path / "watched"
    folder.mkdir()
    destination = tmp_path / "archive"
    destination.mkdir()

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", True, "move", str(destination)))

    stored = controller.config.monitored_directories[0]
    assert stored.post_upload_action == "move"
    assert stored.post_upload_destination == str(destination)


def test_destination_is_discarded_unless_the_action_is_move(controller, tmp_path):
    # Switching away from "move" clears the destination, so it cannot be acted on
    # later if the action is switched back without choosing a folder again.
    folder = tmp_path / "watched"
    folder.mkdir()

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", True, "delete", str(tmp_path / "archive")))

    stored = controller.config.monitored_directories[0]
    assert stored.post_upload_action == "delete"
    assert stored.post_upload_destination == ""


def test_recursive_watch_above_an_existing_folder_is_rejected(controller, watched, tmp_path):
    # The reverse of adding a subfolder to a recursive watch: a new recursive watch
    # that would swallow a folder already being monitored is refused, since every file
    # inside it would be uploaded by both.
    controller.add_directory(MonitoredDirectory(str(tmp_path), "home/parent", True, "keep", ""))

    assert _stored_folders(controller) == [(str(watched), "/tempZone/home/alice", True)]
    assert "would overlap existing folder" in controller.window.status_label.text()


def test_cleanup_warning_is_shown_in_the_status_line(controller, queued_uploads, watched_file):
    # A warning tells the user their local file is still there, while the upload itself
    # stays finished and the file is released from the in-progress list.
    controller._queue_ingestion(str(watched_file))

    controller._handle_upload_warning(
        str(watched_file), "Upload succeeded, but post-upload move cleanup failed: no space"
    )

    assert "cleanup failed" in controller.window.status_label.text()
