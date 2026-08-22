"""Tests for the upload worker's local logic: path mapping, validation, and cancellation.

``IRODSUploadWorker`` creates its ``iRODSSession`` in one place, ``_stream_upload``,
and ``_ensure_collection_exists`` receives the session as an argument. Everything
outside those two runs without a live iRODS zone: collection handling is driven
through a stub session, and the remaining paths fail or return before a connection
is attempted.
"""

from __future__ import annotations

from contextlib import suppress
from unittest.mock import MagicMock

import pytest

from irods_client_system_tray import irods_worker
from conftest import IRODS_TEST_COLLECTION, requires_irods_server
from irods_client_system_tray.config import IRODSEnvironment
from irods_client_system_tray.irods_worker import IRODSUploadWorker


def test_subfolder_structure_is_preserved_in_irods(tmp_path):
    # Checks a file in a subfolder lands in a matching subcollection. Without this,
    # recursive monitoring would dump every nested file into one flat folder and
    # same-named files from different subfolders would collide.
    worker = IRODSUploadWorker()
    root = (tmp_path / "watched").resolve()
    (root / "sub").mkdir(parents=True)

    logical = worker._build_logical_path(
        root / "sub" / "report.csv", root, "/tempZone/home/alice"
    )

    assert logical == "/tempZone/home/alice/sub/report.csv"


def test_file_outside_the_watched_folder_uploads_by_name(tmp_path):
    # Checks an unexpected path does not abort the upload. Working out the subfolder
    # fails for a file outside the watched folder, so the worker just uses the file
    # name instead of raising.
    worker = IRODSUploadWorker()
    root = (tmp_path / "watched").resolve()
    root.mkdir()

    logical = worker._build_logical_path(
        tmp_path / "elsewhere" / "report.csv", root, "/tempZone/home/alice"
    )

    assert logical == "/tempZone/home/alice/report.csv"


def test_incomplete_irods_settings_are_reported_all_at_once(irods_environment):
    # Checks the error names every missing field together. Reporting them one at a
    # time would make the user fix, retry, and fail again for each one.
    worker = IRODSUploadWorker()

    with pytest.raises(ValueError) as excinfo:
        worker._validate_environment(IRODSEnvironment(irods_host="   "))

    message = str(excinfo.value)
    assert "irods_host" in message
    assert "irods_user_name" in message
    assert "irods_password" in message

    assert worker._validate_environment(irods_environment) is None


def test_missing_or_non_file_paths_are_rejected(tmp_path):
    # Checks the worker gives up cleanly when the thing it was told to upload is
    # gone or was never a file.
    worker = IRODSUploadWorker()

    with pytest.raises(FileNotFoundError):
        worker._wait_for_stable_file(tmp_path / "deleted-before-upload.txt")

    with pytest.raises(ValueError):
        worker._wait_for_stable_file(tmp_path)


def test_waiting_stops_once_the_file_size_settles(tmp_path, monkeypatch):
    # Checks the worker stops waiting once a file has finished being written. It
    # waits so half-written files are not uploaded, but a file that is already
    # complete must not sit through the whole waiting window.
    sleeps: list[float] = []
    monkeypatch.setattr(irods_worker.time, "sleep", sleeps.append)
    stable_file = tmp_path / "stable.txt"
    stable_file.write_text("already written", encoding="utf-8")

    IRODSUploadWorker()._wait_for_stable_file(stable_file)

    assert len(sleeps) == 2


def test_missing_subcollections_are_created_automatically():
    # Uploading into a subfolder creates the matching iRODS collection on demand, so
    # the user does not have to create it first. The fake session used here treats
    # every collection as already existing, including the configured target.
    session = MagicMock()

    IRODSUploadWorker()._ensure_collection_exists(
        session, "/tempZone/home/alice", "/tempZone/home/alice/sub/report.csv"
    )

    session.collections.create.assert_called_once_with("/tempZone/home/alice/sub", recurse=True)


def test_target_collection_is_not_recreated():
    # Checks a file going straight into the configured target does not trigger a
    # pointless create call to the server.
    session = MagicMock()

    IRODSUploadWorker()._ensure_collection_exists(
        session, "/tempZone/home/alice", "/tempZone/home/alice/report.csv"
    )

    session.collections.create.assert_not_called()


def test_missing_target_collection_raises_instead_of_creating():
    # Checks the app refuses to invent the target collection. Creating it silently
    # would turn a typo in the settings window into a real folder full of files
    # nobody can find.
    session = MagicMock()
    session.collections.get.side_effect = KeyError("/tempZone/home/typo")

    with pytest.raises(RuntimeError, match="does not exist"):
        IRODSUploadWorker()._ensure_collection_exists(
            session, "/tempZone/home/typo", "/tempZone/home/typo/report.csv"
        )

    session.collections.create.assert_not_called()


def test_empty_error_falls_back_to_the_exception_name():
    # Checks the user never sees a blank error. Some iRODS client exceptions carry
    # no text at all, so the worker falls back to the exception type name.
    worker = IRODSUploadWorker()

    assert worker._format_exception_message(ValueError("no route", "timed out")) == (
        "no route: timed out"
    )
    assert worker._format_exception_message(ValueError()) == "ValueError"
    assert worker._format_exception_message(ValueError("   ")) == "ValueError"


def test_cancelling_a_folder_stops_uploads_until_allowed_again(tmp_path):
    # Queued uploads for a folder that has gone away are dropped, rather than failing
    # one by one against files that are no longer there. Allowing the folder again
    # lifts the block, so a folder that comes back is not stuck.
    root = (tmp_path / "watched").resolve()
    root.mkdir()
    queued_file = root / "report.csv"
    queued_file.write_text("data", encoding="utf-8")

    worker = IRODSUploadWorker()
    events: list[tuple[str, str]] = []
    worker.upload_cancelled.connect(lambda path, message: events.append(("cancelled", message)))
    worker.upload_failed.connect(lambda path, message: events.append(("failed", message)))

    worker.cancel_directory_uploads(str(root))
    worker.upload_file(str(queued_file), str(root), "/tempZone/home/alice", "keep", "")

    assert [kind for kind, _ in events] == ["cancelled"]

    events.clear()
    worker.allow_directory_uploads(str(root))
    worker.upload_file(str(queued_file), str(root), "/tempZone/home/alice", "keep", "")

    # No longer cancelled, so it runs on and fails on the empty credentials instead.
    assert [kind for kind, _ in events] == ["failed"]


def test_failed_upload_reports_the_failing_step(tmp_path):
    # Checks the error tells the user where it broke, not just that it broke.
    root = (tmp_path / "watched").resolve()
    root.mkdir()
    queued_file = root / "report.csv"
    queued_file.write_text("data", encoding="utf-8")

    worker = IRODSUploadWorker()
    failures: list[str] = []
    worker.upload_failed.connect(lambda path, message: failures.append(message))

    worker.upload_file(str(queued_file), str(root), "/tempZone/home/alice", "keep", "")

    assert failures == [
        "validating iRODS settings: Missing iRODS settings: "
        "irods_host, irods_user_name, irods_password"
    ]


def test_credentials_are_copied_not_shared(
    irods_environment,
):
    # Checks the worker keeps its own copy of the credentials. It uploads in the
    # background, so if it shared them with the settings window, editing settings
    # during an upload could change the password partway through a transfer.
    worker = IRODSUploadWorker()
    handed_off_password = irods_environment.irods_password

    worker.set_environment(irods_environment)
    irods_environment.irods_password = "changed-after-handoff"
    assert worker._environment.irods_password == handed_off_password

    worker.clear_environment()
    assert worker._environment == IRODSEnvironment()

    with pytest.raises(TypeError):
        worker.set_environment({"irods_host": "irods.example.org"})


@requires_irods_server
def test_upload_file_round_trips_into_a_live_zone(tmp_path, irods_environment):
    """Perform a real put() against a configured zone, then clean up after itself."""

    from irods.session import iRODSSession

    session_settings = {
        "host": irods_environment.irods_host,
        "port": irods_environment.irods_port,
        "user": irods_environment.irods_user_name,
        "password": irods_environment.irods_password,
        "zone": irods_environment.irods_zone_name,
    }

    root = (tmp_path / "watched").resolve()
    root.mkdir()
    local_file = root / "integration-upload.txt"
    local_file.write_text("integration payload", encoding="utf-8")
    logical_path = f"{IRODS_TEST_COLLECTION}/integration-upload.txt"

    worker = IRODSUploadWorker(irods_environment)
    finished: list[tuple[str, str]] = []
    failures: list[str] = []
    worker.upload_finished.connect(lambda path, logical: finished.append((path, logical)))
    worker.upload_failed.connect(lambda path, message: failures.append(message))

    try:
        worker.upload_file(str(local_file), str(root), IRODS_TEST_COLLECTION, "keep", "")

        assert failures == []
        assert finished == [(str(local_file), logical_path)]

        with iRODSSession(**session_settings) as session:
            assert session.data_objects.exists(logical_path)
    finally:
        with suppress(Exception), iRODSSession(**session_settings) as session:
            session.data_objects.unlink(logical_path, force=True)


# --- post-upload cleanup --------------------------------------------------------


@pytest.fixture
def trashed_files(monkeypatch):
    """Record files sent to the recycle bin instead of really trashing them."""

    import send2trash

    sent: list[str] = []
    monkeypatch.setattr(send2trash, "send2trash", sent.append)
    return sent


def test_keep_leaves_the_local_file_in_place(tmp_path):
    # The "keep" policy uploads a copy and changes nothing on disk.
    root = (tmp_path / "watched").resolve()
    root.mkdir()
    uploaded = root / "report.csv"
    uploaded.write_text("data", encoding="utf-8")

    IRODSUploadWorker()._run_post_upload_action(uploaded, root, "keep", "")

    assert uploaded.exists()


def test_delete_removes_the_local_file(tmp_path):
    # The "delete" policy removes the file outright — it does not go to the recycle
    # bin, so there is nothing to restore it from.
    root = (tmp_path / "watched").resolve()
    root.mkdir()
    uploaded = root / "report.csv"
    uploaded.write_text("data", encoding="utf-8")

    IRODSUploadWorker()._run_post_upload_action(uploaded, root, "delete", "")

    assert not uploaded.exists()


def test_recycle_sends_the_local_file_to_the_trash(tmp_path, trashed_files):
    # The "recycle" policy defers to the operating system's trash rather than deleting,
    # so the user can still get the file back.
    root = (tmp_path / "watched").resolve()
    root.mkdir()
    uploaded = root / "report.csv"
    uploaded.write_text("data", encoding="utf-8")

    IRODSUploadWorker()._run_post_upload_action(uploaded, root, "recycle", "")

    assert trashed_files == [str(uploaded)]


def test_move_preserves_the_subfolder_layout(tmp_path):
    # The "move" policy rebuilds the folder structure under the destination, so files
    # from different subfolders do not collide once they are all moved out.
    root = (tmp_path / "watched").resolve()
    (root / "invoices").mkdir(parents=True)
    uploaded = root / "invoices" / "report.csv"
    uploaded.write_text("data", encoding="utf-8")
    destination = tmp_path / "archive"

    IRODSUploadWorker()._run_post_upload_action(uploaded, root, "move", str(destination))

    assert (destination / "invoices" / "report.csv").read_text(encoding="utf-8") == "data"
    assert not uploaded.exists()


def test_move_refuses_to_overwrite_an_existing_file(tmp_path):
    # Moving on top of a file already at the destination would destroy it, so the move
    # stops and the local file stays put for the user to deal with.
    root = (tmp_path / "watched").resolve()
    root.mkdir()
    uploaded = root / "report.csv"
    uploaded.write_text("new", encoding="utf-8")
    destination = tmp_path / "archive"
    destination.mkdir()
    (destination / "report.csv").write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        IRODSUploadWorker()._run_post_upload_action(uploaded, root, "move", str(destination))

    assert uploaded.exists()
    assert (destination / "report.csv").read_text(encoding="utf-8") == "existing"


def test_failed_cleanup_warns_without_failing_the_upload(tmp_path, monkeypatch):
    # The file reached iRODS even if the local tidy-up did not work, so the transfer is
    # still reported as finished and the cleanup problem is raised separately. Treating
    # it as a failed upload would make the app retry a transfer that already succeeded.
    root = (tmp_path / "watched").resolve()
    root.mkdir()
    uploaded = root / "report.csv"
    uploaded.write_text("data", encoding="utf-8")
    occupied_destination = tmp_path / "archive"
    occupied_destination.mkdir()
    (occupied_destination / "report.csv").write_text("in the way", encoding="utf-8")

    worker = IRODSUploadWorker(
        IRODSEnvironment(
            irods_host="irods.example.org",
            irods_user_name="alice",
            irods_password="alicepass",
            irods_zone_name="tempZone",
        )
    )
    monkeypatch.setattr(worker, "_stream_upload", lambda *args, **kwargs: None)
    events: list[tuple[str, str]] = []
    worker.upload_finished.connect(lambda path, logical: events.append(("finished", logical)))
    worker.upload_warning.connect(lambda path, message: events.append(("warning", message)))
    worker.upload_failed.connect(lambda path, message: events.append(("failed", message)))

    worker.upload_file(str(uploaded), str(root), "/tempZone/home/alice", "move", str(occupied_destination))

    assert [kind for kind, _ in events] == ["finished", "warning"]
    assert "cleanup failed" in dict(events)["warning"]
    assert uploaded.exists()


def test_move_without_a_destination_does_not_write_to_the_working_directory(
    tmp_path, monkeypatch
):
    # A "move" with no destination falls back to the default action, so the move branch
    # never joins the file onto an empty path. The worker runs from a temporary working
    # directory, which is where such a path would resolve to, and nothing is written there.
    watched_folder = (tmp_path / "watched").resolve()
    watched_folder.mkdir()
    uploaded = watched_folder / "report.csv"
    uploaded.write_text("data", encoding="utf-8")
    working_directory = tmp_path / "somewhere-else"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)

    worker = IRODSUploadWorker(
        IRODSEnvironment(
            irods_host="irods.example.org",
            irods_user_name="alice",
            irods_password="alicepass",
            irods_zone_name="tempZone",
        )
    )
    monkeypatch.setattr(worker, "_stream_upload", lambda *args, **kwargs: None)

    worker.upload_file(str(uploaded), str(watched_folder), "/tempZone/home/alice", "move", "")

    assert list(working_directory.iterdir()) == []
