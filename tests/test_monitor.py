"""Tests for watchdog event translation and monitored-folder relocation.

``MonitorManager`` turns raw watchdog events into the signals the rest of the app
listens to: files to ingest, and watched folders that were renamed, moved away, or
deleted. Those classification helpers are exercised directly. ``sync()`` needs a live
observer thread and real filesystem events, so it is not covered here.
"""

from __future__ import annotations

from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileMovedEvent,
)

from irods_client_system_tray.config import MonitoredDirectory
from irods_client_system_tray.monitor import EventBridge, IngestionEventHandler, MonitorManager


def _manager_watching(*paths: Path) -> tuple[MonitorManager, list[tuple]]:
    """Return a manager that believes it already watches ``paths``, and a signal log."""

    manager = MonitorManager()
    manager._parent_children = manager._build_parent_map(
        [MonitoredDirectory(source_directory=str(path)) for path in paths]
    )

    received: list[tuple] = []
    manager.monitored_directory_renamed.connect(
        lambda old, new: received.append(("renamed", old, new))
    )
    manager.monitored_directory_moved.connect(
        lambda old, new: received.append(("moved", old, new))
    )
    manager.monitored_directory_deleted.connect(
        lambda path: received.append(("deleted", path))
    )
    return manager, received


def test_parent_map_groups_siblings_and_skips_missing_folders(tmp_path):
    # Two watched folders sharing a parent must produce one parent watch, not two,
    # and a configured folder that no longer exists must not create a watch at all.
    parent = (tmp_path / "parent").resolve()
    (parent / "a").mkdir(parents=True)
    (parent / "b").mkdir()

    manager = MonitorManager()
    parent_map = manager._build_parent_map(
        [
            MonitoredDirectory(source_directory=str(parent / "a")),
            MonitoredDirectory(source_directory=str(parent / "b")),
            MonitoredDirectory(source_directory=str(tmp_path / "deleted-by-user")),
        ]
    )

    assert list(parent_map) == [str(parent)]
    assert parent_map[str(parent)] == {str(parent / "a"), str(parent / "b")}


def test_move_within_the_same_parent_is_a_rename(tmp_path):
    parent = (tmp_path / "parent").resolve()
    watched = parent / "a"
    watched.mkdir(parents=True)
    manager, received = _manager_watching(watched)

    manager._handle_directory_relocated(str(watched), str(parent / "renamed"))

    assert received == [("renamed", str(watched), str(parent / "renamed"))]


def test_move_out_of_the_parent_is_reported_as_moved(tmp_path):
    # A renamed folder can still be followed; one moved somewhere else cannot. The
    # two are reported differently so the app can keep watching the renamed one.
    parent = (tmp_path / "parent").resolve()
    watched = parent / "a"
    watched.mkdir(parents=True)
    destination = (tmp_path / "elsewhere").resolve()
    manager, received = _manager_watching(watched)

    manager._handle_directory_relocated(str(watched), str(destination))

    assert received == [("moved", str(watched), str(destination))]


def test_untracked_sibling_moves_are_ignored(tmp_path):
    # The parent watch sees every child of the parent folder, so unrelated siblings
    # generate events that must not be mistaken for the watched folder moving.
    parent = (tmp_path / "parent").resolve()
    watched = parent / "a"
    watched.mkdir(parents=True)
    manager, received = _manager_watching(watched)

    manager._handle_directory_relocated(str(parent / "sibling"), str(parent / "moved"))

    assert received == []


def test_only_tracked_folders_report_deletion(tmp_path):
    parent = (tmp_path / "parent").resolve()
    watched = parent / "a"
    watched.mkdir(parents=True)
    manager, received = _manager_watching(watched)

    manager._handle_directory_deleted(str(parent / "sibling"))
    assert received == []

    manager._handle_directory_deleted(str(watched))
    assert received == [("deleted", str(watched))]


def test_sync_with_monitoring_disabled_starts_no_observer(tmp_path):
    watched = tmp_path.resolve()
    manager = MonitorManager()

    manager.sync([MonitoredDirectory(source_directory=str(watched))], is_active=False)

    assert manager._observer is None
    assert manager._directory_watches == {}


def test_ingestion_handler_queues_files_but_not_directories(tmp_path):
    # Only files are uploadable, so directory creation and directory moves must not
    # reach the ingest queue. Moves are queued by destination, not source.
    bridge = EventBridge()
    queued: list[str] = []
    bridge.ingest_requested.connect(queued.append)
    handler = IngestionEventHandler(bridge)

    handler.on_created(DirCreatedEvent(str(tmp_path / "new-folder")))
    handler.on_moved(DirMovedEvent(str(tmp_path / "old"), str(tmp_path / "new")))
    assert queued == []

    handler.on_created(FileCreatedEvent(str(tmp_path / "created.txt")))
    handler.on_moved(FileMovedEvent(str(tmp_path / "before.txt"), str(tmp_path / "after.txt")))
    assert queued == [str(tmp_path / "created.txt"), str(tmp_path / "after.txt")]


def test_ingestion_handler_forwards_every_event_to_the_activity_signal(tmp_path):
    # file_event feeds the activity log, which lists folder changes as well as file
    # changes, so it fires for every event the watcher reports.
    bridge = EventBridge()
    seen: list[tuple[str, str, bool]] = []
    bridge.file_event.connect(lambda kind, path, is_directory: seen.append((kind, path, is_directory)))
    handler = IngestionEventHandler(bridge)

    handler.on_any_event(DirCreatedEvent(str(tmp_path / "new-folder")))

    assert seen == [("created", str(tmp_path / "new-folder"), True)]
