"""Tests for watchdog event translation and monitored-folder relocation.

``MonitorManager`` turns raw watchdog events into the signals the rest of the app
listens to: files to ingest, and watched folders that were renamed, moved away, or
deleted. Those classification helpers are exercised directly; ``sync()`` is tested
with a stub observer instead of starting a real filesystem watcher.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileMovedEvent,
)
from watchdog.observers.api import ObservedWatch

from irods_client_system_tray.config import MonitoredDirectory
import irods_client_system_tray.monitor as monitor_module
from irods_client_system_tray.monitor import (
    EventBridge, IngestionEventHandler, MonitorManager, ParentDirectoryEventHandler,
)


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


def test_sync_replaces_non_recursive_watch_and_schedules_recursive_folders(
    tmp_path, monkeypatch
):
    watched = tmp_path / "watched"
    watched.mkdir()
    manager = MonitorManager()
    observer = Mock()
    observer.schedule.side_effect = lambda handler, path, recursive: ObservedWatch(
        path, recursive=recursive
    )
    manager._observer = observer
    monkeypatch.setattr(manager, "_ensure_running", lambda: True)
    old_watch = ObservedWatch(str(watched), recursive=False)
    manager._directory_watches[str(watched)] = old_watch

    manager.sync([MonitoredDirectory(source_directory=str(watched))], is_active=True)

    observer.unschedule.assert_called_once_with(old_watch)
    assert manager._directory_watches[str(watched)].is_recursive
    assert manager._parent_watches[str(tmp_path)].is_recursive is False


def test_sync_reuses_existing_watches_then_removes_unconfigured_folder(tmp_path, monkeypatch):
    watched = tmp_path / "watched"
    watched.mkdir()
    manager = MonitorManager()
    observer = Mock()
    observer.schedule.side_effect = lambda _handler, path, recursive: ObservedWatch(
        path, recursive=recursive
    )
    manager._observer = observer
    monkeypatch.setattr(manager, "_ensure_running", lambda: True)
    directories = [MonitoredDirectory(str(watched))]

    manager.sync(directories, is_active=True)
    manager.sync(directories, is_active=True)
    assert observer.schedule.call_count == 2  # Folder and parent, not duplicates.

    manager.sync([], is_active=True)
    assert observer.unschedule.call_count == 2
    assert manager._directory_watches == {}
    assert manager._parent_watches == {}
    assert manager._parent_children == {}


def test_sync_reports_folder_watch_errors_but_still_watches_parent(tmp_path, monkeypatch):
    watched = tmp_path / "watched"
    watched.mkdir()
    manager = MonitorManager()
    observer = Mock()

    def schedule(_handler, path, recursive):
        if recursive:
            raise OSError("out of watches")
        return ObservedWatch(path, recursive=False)

    observer.schedule.side_effect = schedule
    manager._observer = observer
    monkeypatch.setattr(manager, "_ensure_running", lambda: True)
    errors = []
    manager.monitor_error.connect(errors.append)

    manager.sync([MonitoredDirectory(str(watched))], is_active=True)

    assert errors == [f"Failed to watch {watched}: out of watches"]
    assert manager._directory_watches == {}
    assert str(tmp_path) in manager._parent_watches


def test_sync_reports_parent_watch_errors_without_dropping_folder(tmp_path, monkeypatch):
    watched = tmp_path / "watched"
    watched.mkdir()
    manager = MonitorManager()
    observer = Mock()

    def schedule(_handler, path, recursive):
        if not recursive:
            raise OSError("permission denied")
        return ObservedWatch(path, recursive=True)

    observer.schedule.side_effect = schedule
    manager._observer = observer
    monkeypatch.setattr(manager, "_ensure_running", lambda: True)
    errors = []
    manager.monitor_error.connect(errors.append)

    manager.sync([MonitoredDirectory(str(watched))], is_active=True)

    assert errors == [f"Failed to watch parent folder {tmp_path}: permission denied"]
    assert str(watched) in manager._directory_watches
    assert manager._parent_watches == {}


def test_observer_startup_failure_is_reported_without_scheduling(monkeypatch, tmp_path):
    observer = Mock()
    observer.start.side_effect = OSError("watch service unavailable")
    monkeypatch.setattr(monitor_module, "Observer", lambda: observer)
    manager = MonitorManager()
    errors = []
    manager.monitor_error.connect(errors.append)

    manager.sync([MonitoredDirectory(str(tmp_path))], is_active=True)

    assert errors == ["Failed to start background monitor: watch service unavailable"]
    assert manager._observer is None
    observer.schedule.assert_not_called()


def test_stop_clears_watches_and_joins_the_observer(tmp_path):
    manager = MonitorManager()
    observer = Mock()
    manager._observer = observer
    manager._directory_watches[str(tmp_path)] = ObservedWatch(str(tmp_path), recursive=True)
    manager._parent_watches[str(tmp_path.parent)] = ObservedWatch(
        str(tmp_path.parent), recursive=False
    )
    manager._parent_children[str(tmp_path.parent)] = {str(tmp_path)}

    manager.shutdown()

    observer.stop.assert_called_once()
    observer.join.assert_called_once_with(timeout=5)
    assert manager._observer is None
    assert manager._directory_watches == manager._parent_watches == manager._parent_children == {}


def test_parent_handler_forwards_move_and_delete_events(tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    manager, received = _manager_watching(watched)
    handler = ParentDirectoryEventHandler(manager._bridge)

    handler.on_moved(DirMovedEvent(str(watched), str(tmp_path / "renamed")))
    handler.on_deleted(DirDeletedEvent(str(watched)))

    assert received == [
        ("renamed", str(watched), str(tmp_path / "renamed")),
        ("deleted", str(watched)),
    ]


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
