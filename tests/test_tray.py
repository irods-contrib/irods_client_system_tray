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
import time
from threading import Barrier, Lock, Thread

import pytest
from PySide6.QtCore import QEvent, QObject

from irods_client_system_tray import config
from irods_client_system_tray.config import (
    AppConfig,
    IRODSEnvironment,
    MonitoredDirectory,
    RegexFilterConfig,
    RetryConfig,
)

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
    for slot in tray_controller._upload_workers:
        slot.dispatcher.upload.disconnect(slot.worker.upload_file)

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
    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", "trash", ""))
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


def _stored_folders(controller) -> list[tuple[str, str]]:
    return [
        (directory.source_directory, directory.target_collection)
        for directory in controller.config.monitored_directories
    ]


def _controller_with_saved_folders(tmp_path, monkeypatch, qapp, directories, *, active=True):
    config_path = tmp_path / "app_state.json"
    config.ConfigStore(config_path).save(
        AppConfig(is_monitoring_active=active, monitored_directories=directories)
    )
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "IRODS_ENVIRONMENT_PATH", tmp_path / "irods_environment.json")

    from irods_client_system_tray.tray import TrayController

    controller = TrayController(qapp)
    for slot in controller._upload_workers:
        slot.dispatcher.upload.disconnect(slot.worker.upload_file)
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    return controller


# --- adding folders -------------------------------------------------------------


def test_tray_menu_opens_the_main_window(controller):
    assert controller.open_settings_action.text() == "Main Window"
    assert controller.sign_in_action.text() == "Main Window"


def test_tray_menu_labels_the_sign_out_action_logout(controller):
    assert controller.sign_out_action.text() == "Logout"


def test_added_folder_target_is_anchored_under_the_zone(controller, watched):
    # Checks the controller fixes up what the dialog hands it. The user types a bare
    # collection path like "home/alice" and it must be stored under their zone.
    assert _stored_folders(controller) == [(str(watched), "/tempZone/home/alice")]


def test_duplicate_folder_is_rejected(controller, watched):
    # Checks a duplicate is refused with a message, and the original settings are
    # left alone rather than being overwritten by the second attempt.
    controller.add_directory(MonitoredDirectory(str(watched), "home/somewhere-else", "trash", ""))

    assert _stored_folders(controller) == [(str(watched), "/tempZone/home/alice")]
    assert "Already monitoring" in controller.window.status_label.text()


def test_folder_inside_a_recursive_watch_is_rejected(controller, watched):
    # Checks overlapping watches are blocked. The parent is already watching
    # subfolders, so adding one would upload every file inside it twice.
    nested = watched / "nested"
    nested.mkdir()

    controller.add_directory(MonitoredDirectory(str(nested), "home/nested", "trash", ""))

    assert len(controller.config.monitored_directories) == 1
    assert "overlaps monitored folder" in controller.window.status_label.text()


def test_sibling_with_similar_name_is_not_an_overlap(controller, watched, tmp_path):
    sibling = tmp_path / "watched-other"
    sibling.mkdir()

    controller.add_directory(MonitoredDirectory(str(sibling), "home/other", "trash", ""))

    assert len(controller.config.monitored_directories) == 2
    assert controller.config.monitored_directories[1].source_directory == str(sibling)


@pytest.mark.parametrize(
    ("filter_mode", "expected_files"),
    [
        ("allow", {"report.csv", "nested/deep.csv"}),
        ("deny", {"notes.txt"}),
    ],
)
def test_adding_folder_scans_existing_files_recursively_through_filters(
    controller, queued_uploads, qapp, monkeypatch, tmp_path, filter_mode, expected_files
):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    folder = tmp_path / "watched"
    (folder / "nested").mkdir(parents=True)
    for relative_path in ("report.csv", "nested/deep.csv", "notes.txt"):
        (folder / relative_path).write_text("data", encoding="utf-8")
    (tmp_path / "unrelated.csv").write_text("outside", encoding="utf-8")

    controller.add_directory(
        MonitoredDirectory(
            str(folder),
            "home/alice",
            regex_filter=RegexFilterConfig(mode=filter_mode, patterns=[r"\.csv$"]),
        )
    )
    for _ in range(20):
        qapp.processEvents()
        if not controller._initial_scan_iterators:
            break

    assert not controller._initial_scan_iterators
    assert {str((folder / name).resolve()) for name in expected_files} == {
        upload[0] for upload in queued_uploads
    }
    assert all(upload[1:4] == (str(folder), "/tempZone/home/alice", "delete") for upload in queued_uploads)

    queued_uploads.clear()
    controller._sync_from_config()
    qapp.processEvents()
    assert queued_uploads == []


def test_login_scans_saved_folders_for_files_added_while_app_was_closed(
    tmp_path, monkeypatch, qapp
):
    folder = tmp_path / "watched"
    (folder / "nested").mkdir(parents=True)
    expected = {folder / "report.csv", folder / "nested" / "deep.csv"}
    for path in expected:
        path.write_text("data", encoding="utf-8")
    (folder / "ignored.txt").write_text("data", encoding="utf-8")
    controller = _controller_with_saved_folders(
        tmp_path, monkeypatch, qapp,
        [MonitoredDirectory(
            str(folder), "/tempZone/home/alice",
            regex_filter=RegexFilterConfig(mode="allow", patterns=[r"\.csv$"]),
        )],
    )
    sent = []
    controller.queue_upload.connect(lambda *args: sent.append(args))

    try:
        qapp.processEvents()
        assert sent == []  # Sign-in must happen before uploads can be queued.

        controller._complete_login(IRODSEnvironment(
            irods_host="irods.example.org", irods_user_name="alice",
            irods_password="alicepass", irods_zone_name="tempZone",
        ))
        for _ in range(20):
            qapp.processEvents()
            if not controller._initial_scan_iterators:
                break

        assert {upload[0] for upload in sent} == {str(path) for path in expected}
        assert not controller._pending_initial_scans

        sent.clear()
        controller._queued_uploads.clear()
        controller._sync_from_config()
        qapp.processEvents()
        assert sent == []  # Refreshing settings must not repeat the startup pass.
    finally:
        controller.shutdown()


def test_saved_folder_scan_waits_until_monitoring_resumes(tmp_path, monkeypatch, qapp):
    folder = tmp_path / "watched"
    folder.mkdir()
    existing = folder / "offline.txt"
    existing.write_text("data", encoding="utf-8")
    controller = _controller_with_saved_folders(
        tmp_path, monkeypatch, qapp,
        [MonitoredDirectory(str(folder), "/tempZone/home/alice")], active=False,
    )
    sent = []
    controller.queue_upload.connect(lambda *args: sent.append(args))

    try:
        controller._complete_login(IRODSEnvironment(
            irods_host="irods.example.org", irods_user_name="alice",
            irods_password="alicepass", irods_zone_name="tempZone",
        ))
        qapp.processEvents()
        assert sent == []

        controller.set_monitoring_active(True)
        qapp.processEvents()
        assert [upload[0] for upload in sent] == [str(existing)]
    finally:
        controller.shutdown()


def test_initial_scan_waits_until_monitoring_is_enabled(controller, qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    folder = tmp_path / "watched"
    folder.mkdir()
    existing = folder / "already-here.txt"
    existing.write_text("data", encoding="utf-8")
    sent = []
    controller.queue_upload.connect(lambda *args: sent.append(args))

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice"))
    qapp.processEvents()
    assert sent == []

    controller.set_monitoring_active(True)
    qapp.processEvents()
    assert [upload[0] for upload in sent] == [str(existing)]


def test_scan_does_not_queue_a_file_already_seen_by_watchdog(
    controller, queued_uploads, qapp, monkeypatch, tmp_path
):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    folder = tmp_path / "watched"
    folder.mkdir()
    existing = folder / "report.csv"
    existing.write_text("data", encoding="utf-8")

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice"))
    controller._queue_ingestion(str(existing))
    qapp.processEvents()

    assert [upload[0] for upload in queued_uploads] == [str(existing)]


def test_initial_scan_continues_across_batches(
    controller, queued_uploads, qapp, monkeypatch, tmp_path
):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    folder = tmp_path / "watched"
    folder.mkdir()
    for index in range(105):
        (folder / f"{index}.txt").write_text("data", encoding="utf-8")

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice"))
    for _ in range(20):
        qapp.processEvents()
        if not controller._initial_scan_iterators:
            break

    assert not controller._initial_scan_iterators
    assert len(controller._queued_uploads) == 105
    assert len(queued_uploads) == controller.config.maximum_upload_workers

    while controller._active_uploads:
        path = next(iter(controller._active_uploads))
        controller._handle_upload_finished(path, "/tempZone/home/alice/file")
    assert len(queued_uploads) == 105


def test_queued_files_dispatch_only_when_a_worker_is_available(
    controller, queued_uploads, tmp_path
):
    folder = tmp_path / "watched"
    folder.mkdir()
    controller.config.monitored_directories = [
        MonitoredDirectory(str(folder), "/tempZone/home/alice")
    ]
    files = []
    for index in range(controller.config.maximum_upload_workers + 1):
        path = folder / f"{index}.txt"
        path.write_text("data", encoding="utf-8")
        files.append(str(path))
        controller._queue_ingestion(str(path))

    assert len(queued_uploads) == controller.config.maximum_upload_workers
    assert len(controller._active_uploads) == controller.config.maximum_upload_workers
    assert controller._queued_uploads[files[-1]].status == "queued"

    controller._handle_upload_finished(files[0], "/tempZone/home/alice/0.txt")

    assert [upload[0] for upload in queued_uploads] == files
    assert len(controller._active_uploads) == controller.config.maximum_upload_workers


def test_failed_upload_frees_a_worker_for_the_next_file(
    controller, queued_uploads, tmp_path
):
    controller.config.retry = RetryConfig(attempts=0)
    folder = tmp_path / "watched"
    folder.mkdir()
    controller.config.monitored_directories = [
        MonitoredDirectory(str(folder), "/tempZone/home/alice")
    ]
    files = []
    for index in range(controller.config.maximum_upload_workers + 1):
        path = folder / f"{index}.txt"
        path.write_text("data", encoding="utf-8")
        files.append(str(path))
        controller._queue_ingestion(str(path))

    controller._handle_upload_failed(files[0], "connection lost")

    assert [upload[0] for upload in queued_uploads] == files
    assert files[0] in controller._failed_uploads
    assert files[-1] in controller._active_uploads


def test_lowering_worker_limit_waits_for_active_uploads(
    controller, queued_uploads, tmp_path
):
    if controller.config.maximum_upload_workers < 2:
        pytest.skip("Needs at least two local CPUs")
    folder = tmp_path / "watched"
    folder.mkdir()
    controller.config.monitored_directories = [
        MonitoredDirectory(str(folder), "/tempZone/home/alice")
    ]
    files = []
    for index in range(3):
        path = folder / f"{index}.txt"
        path.write_text("data", encoding="utf-8")
        files.append(str(path))
        controller._queue_ingestion(str(path))
    assert len(queued_uploads) == 2

    controller.window.maximum_upload_workers_input.setValue(1)
    controller.window.save_settings_button.click()
    assert controller.config_store.load().maximum_upload_workers == 1

    controller._handle_upload_finished(files[0], "/tempZone/home/alice/0.txt")
    assert len(queued_uploads) == 2
    controller._handle_upload_finished(files[1], "/tempZone/home/alice/1.txt")
    assert [upload[0] for upload in queued_uploads] == files
    assert len(controller._active_uploads) == 1


def test_two_upload_workers_can_transfer_files_at_the_same_time(
    controller, queued_uploads, qapp, monkeypatch, tmp_path
):
    if controller.config.maximum_upload_workers < 2:
        pytest.skip("Needs at least two local CPUs")
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    folder = tmp_path / "watched"
    folder.mkdir()
    controller.config.monitored_directories = [
        MonitoredDirectory(str(folder), "/tempZone/home/alice")
    ]
    controller._complete_login(controller.environment)
    rendezvous = Barrier(2)
    lock = Lock()
    active = 0
    max_active = 0

    def fake_stream(*_args):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            rendezvous.wait(timeout=5)
            time.sleep(0.05)
        finally:
            with lock:
                active -= 1

    for slot in controller._upload_workers[:2]:
        slot.dispatcher.upload.connect(slot.worker.upload_file)
        monkeypatch.setattr(slot.worker, "_stream_upload", fake_stream)
        monkeypatch.setattr(slot.worker, "_wait_for_stable_file", lambda *_args: None)
        monkeypatch.setattr(slot.worker, "_run_post_upload_action", lambda *_args: None)

    for index in range(2):
        path = folder / f"{index}.txt"
        path.write_text("data", encoding="utf-8")
        controller._queue_ingestion(str(path))

    deadline = time.monotonic() + 10
    while controller._queued_uploads and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)

    assert len(queued_uploads) == 2
    assert controller._queued_uploads == {}
    assert max_active == 2


def test_increasing_worker_limit_starts_additional_workers(
    controller, queued_uploads, tmp_path
):
    if config.maximum_upload_workers_limit() < 3:
        pytest.skip("Needs at least three local CPUs")
    controller.window.maximum_upload_workers_input.setValue(3)
    controller.window.save_settings_button.click()
    assert controller.config.maximum_upload_workers == 3
    assert len(controller._upload_workers) == 3
    for slot in controller._upload_workers[2:]:
        slot.dispatcher.upload.disconnect(slot.worker.upload_file)

    folder = tmp_path / "watched"
    folder.mkdir()
    controller.config.monitored_directories = [
        MonitoredDirectory(str(folder), "/tempZone/home/alice")
    ]
    for index in range(4):
        path = folder / f"{index}.txt"
        path.write_text("data", encoding="utf-8")
        controller._queue_ingestion(str(path))

    assert len(queued_uploads) == 3
    assert len(controller._active_uploads) == 3
    controller._handle_upload_finished(str(folder / "0.txt"), "/tempZone/home/alice/0.txt")
    assert len(queued_uploads) == 4


def test_pausing_an_initial_scan_preserves_remaining_files(
    controller, queued_uploads, qapp, monkeypatch, tmp_path
):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    folder = tmp_path / "watched"
    folder.mkdir()
    for name in ("one.txt", "two.txt"):
        (folder / name).write_text("data", encoding="utf-8")
    queue_file = controller._queue_ingestion

    def pause_after_first_file(path):
        queue_file(path)
        if len(queued_uploads) == 1:
            controller.set_monitoring_active(False)

    monkeypatch.setattr(controller, "_queue_ingestion", pause_after_first_file)
    controller.add_directory(MonitoredDirectory(str(folder), "home/alice"))
    qapp.processEvents()

    assert len(queued_uploads) == 1
    controller.set_monitoring_active(True)
    qapp.processEvents()
    assert {upload[0] for upload in queued_uploads} == {
        str(folder / "one.txt"), str(folder / "two.txt")
    }


def test_removing_a_folder_cancels_its_pending_scan(
    controller, queued_uploads, qapp, monkeypatch, tmp_path
):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    folder = tmp_path / "watched"
    folder.mkdir()
    (folder / "report.csv").write_text("data", encoding="utf-8")

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice"))
    controller.remove_directory(str(folder))
    qapp.processEvents()

    assert queued_uploads == []


def test_removed_folder_is_dropped_from_the_saved_file(controller, watched, tmp_path):
    # Checks removal is persisted, not just cleared from the in-memory list, so the
    # folder does not come back on the next launch.
    controller.remove_directory(str(watched))

    assert controller.config.monitored_directories == []
    saved = json.loads((tmp_path / "app_state.json").read_text(encoding="utf-8"))
    assert saved["monitored_directories"] == []


def test_monitoring_state_uses_checkbox_without_a_status_message(controller, monkeypatch):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)

    controller.set_monitoring_active(True)
    assert controller.window.monitor_toggle.isChecked()
    assert controller.window.status_label.isHidden()

    controller.set_monitoring_active(False)
    assert not controller.window.monitor_toggle.isChecked()
    assert controller.window.status_label.isHidden()


def test_missing_folder_warning_remains_visible_without_active_message(
    controller, monkeypatch, tmp_path
):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    controller.config.monitored_directories = [
        MonitoredDirectory(str(tmp_path / "missing"), "/tempZone/home/alice")
    ]
    controller.config.is_monitoring_active = True

    controller._sync_from_config()

    assert controller.window.status_label.text() == "1 monitored folder(s) are missing."
    assert not controller.window.status_label.isHidden()


# --- queueing uploads -----------------------------------------------------------


def test_legacy_nested_folders_use_the_deepest_matching_settings(
    controller, queued_uploads, tmp_path
):
    # Older configs can contain nested watches. Even though new overlapping folders
    # are rejected, uploads must still use the most specific saved destination.
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    controller.config.monitored_directories = [
        MonitoredDirectory(str(outer), "/tempZone/home/outer", "trash", ""),
        MonitoredDirectory(str(inner), "/tempZone/home/inner", "trash", ""),
    ]
    created = inner / "report.csv"
    created.write_text("data", encoding="utf-8")

    controller._queue_ingestion(str(created))

    assert queued_uploads == [(str(created), str(inner), "/tempZone/home/inner", "trash", "")]


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


# --- session and retry settings -------------------------------------------------


def test_saving_retry_settings_does_not_change_the_session(controller, tmp_path):
    controller.window.retry_attempts_input.setValue(3)
    controller.window.retry_first_delay_input.setValue(5)
    controller.window.retry_backoff_multiplier_input.setValue(2.0)

    controller.window.save_settings_button.click()

    assert controller.config.retry.attempts == 3
    assert controller.config.retry.first_delay_in_seconds == 5
    assert controller.config.retry.backoff_multiplier == 2.0
    assert controller.environment.irods_password == "alicepass"
    assert controller.config_store.load().retry_by_user[
        config.build_retry_settings_user_key(controller.environment)
    ] == controller.config.retry


def test_signing_in_never_writes_the_password_to_disk(controller, tmp_path):
    # The login path persists connection details but never the password.
    controller._complete_login(controller.environment)

    written = (tmp_path / "irods_environment.json").read_text(encoding="utf-8")
    assert "irods_password" not in written
    assert "alicepass" not in written


def test_sign_in_activity_identifies_user_zone_and_server(controller):
    controller._complete_login(controller.environment)

    assert any(
        "logged into iRODS as alice#tempZone to irods.example.org:1247" in entry
        for entry in controller.window.activity_log.read_recent()
    )


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


def test_signing_into_a_new_zone_repoints_saved_folders(controller, watched):
    # Connection changes now go through sign-in, which must repoint folder targets.
    controller._complete_login(
        IRODSEnvironment(
            irods_host="irods.example.org",
            irods_user_name="alice",
            irods_password="alicepass",
            irods_zone_name="otherZone",
        )
    )

    assert _stored_folders(controller) == [(str(watched), "/otherZone/home/alice")]
    assert controller.config_store.load().monitored_directories[0].target_collection == (
        "/otherZone/home/alice"
    )


# --- folder relocation ----------------------------------------------------------


def test_renamed_folder_stays_watched(controller, watched, tmp_path):
    # Checks a rename follows the folder instead of quietly stopping monitoring.
    renamed = tmp_path / "renamed"
    watched.rename(renamed)

    controller._handle_monitored_directory_renamed(str(watched), str(renamed))

    assert _stored_folders(controller) == [(str(renamed), "/tempZone/home/alice")]


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

    assert all(
        str(watched) in slot.worker._cancelled_monitored_roots
        for slot in controller._upload_workers
    )
    assert str(watched) in controller.window.status_label.text()


def test_folder_still_on_disk_is_not_left_cancelled(controller, watched):
    # A delete event for a folder that is still on disk is a false alarm. The refresh
    # that follows clears the cancellation, so uploads carry on.
    controller._handle_monitored_directory_deleted(str(watched))

    assert all(not slot.worker._cancelled_monitored_roots for slot in controller._upload_workers)


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

    assert _stored_folders(controller) == [(str(watched), "/tempZone/home/alice")]
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


def test_progress_updates_repaint_the_status_line_during_transfer(controller, qapp):
    painted_messages = []

    class PaintRecorder(QObject):
        def eventFilter(self, watched, event):  # noqa: N802
            if event.type() == QEvent.Type.Paint:
                painted_messages.append(watched.text())
            return False

    recorder = PaintRecorder()
    controller.window.status_label.installEventFilter(recorder)
    controller.window.show()
    qapp.processEvents()

    def send_progress():
        for amount in (0, 25, 50, 75, 100):
            controller.upload_worker.upload_progress.emit(
                "/watched/report.csv", "/tempZone/x", amount, 100
            )
            time.sleep(0.1)

    thread = Thread(target=send_progress)
    try:
        thread.start()
        while thread.is_alive():
            qapp.processEvents()
            time.sleep(0.005)
        thread.join()
        qapp.processEvents()

        assert "Uploading report.csv: 25%" in painted_messages
        assert "Uploading report.csv: 50%" in painted_messages
        assert "Uploading report.csv: 75%" in painted_messages
    finally:
        thread.join()
        controller.window.status_label.removeEventFilter(recorder)
        controller.window.hide()


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


def test_retry_timer_dispatches_a_second_attempt(controller, queued_uploads, watched_file):
    controller._queue_ingestion(str(watched_file))
    controller._handle_upload_failed(str(watched_file), "connection reset")

    controller._retry_upload_job(str(watched_file))

    assert len(queued_uploads) == 2
    assert controller._queued_uploads[str(watched_file)].attempt_number == 2
    assert controller._queued_uploads[str(watched_file)].status == "dispatched"


@pytest.mark.parametrize("condition", ["paused", "signed_out", "unwatched", "no_target"])
def test_retry_does_not_upload_when_prerequisites_change(
    controller, queued_uploads, watched_file, condition
):
    controller._queue_ingestion(str(watched_file))
    controller._handle_upload_failed(str(watched_file), "connection reset")

    if condition == "paused":
        controller.config.is_monitoring_active = False
    elif condition == "signed_out":
        controller._is_authenticated = False
    elif condition == "unwatched":
        controller.config.monitored_directories.clear()
    else:
        controller.config.monitored_directories[0].target_collection = ""

    controller._retry_upload_job(str(watched_file))

    assert len(queued_uploads) == 1
    assert str(watched_file) not in controller._queued_uploads
    if condition != "signed_out":
        assert str(watched_file) in controller._failed_uploads


def test_pausing_monitoring_moves_waiting_retries_to_failed_list(
    controller, queued_uploads, watched_file, monkeypatch
):
    monkeypatch.setattr(controller.monitor, "sync", lambda *_args: None)
    controller._queue_ingestion(str(watched_file))
    controller._handle_upload_failed(str(watched_file), "connection reset")

    controller.set_monitoring_active(False)

    assert str(watched_file) not in controller._queued_uploads
    assert controller._failed_uploads[str(watched_file)].status == "failed"
    assert controller._failed_uploads[str(watched_file)].retry_timer is None


def test_manual_retry_resets_a_failed_upload(controller, queued_uploads, watched_file):
    controller.config.retry = RetryConfig(attempts=0)
    controller._queue_ingestion(str(watched_file))
    controller._handle_upload_failed(str(watched_file), "connection reset")

    controller.retry_failed_upload(str(watched_file))

    assert len(queued_uploads) == 2
    assert str(watched_file) not in controller._failed_uploads
    assert controller._queued_uploads[str(watched_file)].attempt_number == 1


def test_deleted_file_event_removes_failed_upload(controller, queued_uploads, watched_file):
    controller.config.retry = RetryConfig(attempts=0)
    controller._queue_ingestion(str(watched_file))
    controller._handle_upload_failed(str(watched_file), "connection reset")

    controller._handle_file_event("deleted", str(watched_file), False)

    assert str(watched_file) not in controller._failed_uploads
    assert any(
        "failed upload removed" in entry for entry in controller.window.activity_log.read_recent()
    )


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
    assert any("logged out" in entry for entry in controller.window.activity_log.read_recent())

    # sign_out reopens the login dialog; close it so the fixture can tear down cleanly.
    if controller._login_dialog is not None:
        controller._login_dialog.reject()


# --- post-upload cleanup rules --------------------------------------------------


def test_legacy_recycle_action_is_saved_as_trash(controller, tmp_path):
    folder = tmp_path / "watched"
    folder.mkdir()

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", "recycle", ""))

    assert controller.config.monitored_directories[0].post_upload_action == "trash"
    saved = json.loads((tmp_path / "app_state.json").read_text(encoding="utf-8"))
    assert saved["monitored_directories"][0]["post_upload_action"] == "trash"


@pytest.mark.parametrize(
    ("action", "destination"),
    [("move", ""), ("move", "   "), ("  MoVe ", "")],
)
def test_move_without_a_destination_is_refused(controller, tmp_path, action, destination):
    # Reject incomplete move settings before normalization can turn them into a
    # "delete" action. This also protects callers outside the add-folder dialog.
    folder = tmp_path / "watched"
    folder.mkdir()

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", action, destination))

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

    controller.add_directory(MonitoredDirectory(str(folder), "home/second", "move", str(target)))

    assert len(controller.config.monitored_directories) == 1
    assert "must be outside monitored folders" in controller.window.status_label.text()


def test_move_destination_outside_watched_folders_is_accepted(controller, tmp_path):
    folder = tmp_path / "watched"
    folder.mkdir()
    destination = tmp_path / "archive"
    destination.mkdir()

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", "move", str(destination)))

    stored = controller.config.monitored_directories[0]
    assert stored.post_upload_action == "move"
    assert stored.post_upload_destination == str(destination)


def test_destination_is_discarded_unless_the_action_is_move(controller, tmp_path):
    # Switching away from "move" clears the destination, so it cannot be acted on
    # later if the action is switched back without choosing a folder again.
    folder = tmp_path / "watched"
    folder.mkdir()

    controller.add_directory(MonitoredDirectory(str(folder), "home/alice", "delete", str(tmp_path / "archive")))

    stored = controller.config.monitored_directories[0]
    assert stored.post_upload_action == "delete"
    assert stored.post_upload_destination == ""


def test_recursive_watch_above_an_existing_folder_is_rejected(controller, watched, tmp_path):
    # The reverse of adding a subfolder to a recursive watch: a new recursive watch
    # that would swallow a folder already being monitored is refused, since every file
    # inside it would be uploaded by both.
    controller.add_directory(MonitoredDirectory(str(tmp_path), "home/parent", "trash", ""))

    assert _stored_folders(controller) == [(str(watched), "/tempZone/home/alice")]
    assert "overlaps monitored folder" in controller.window.status_label.text()


def test_cleanup_warning_is_shown_in_the_status_line(controller, queued_uploads, watched_file):
    # A warning tells the user their local file is still there, while the upload itself
    # stays finished and the file is released from the in-progress list.
    controller._queue_ingestion(str(watched_file))

    controller._handle_upload_warning(
        str(watched_file), "Upload succeeded, but post-upload move cleanup failed: no space"
    )

    assert "cleanup failed" in controller.window.status_label.text()
