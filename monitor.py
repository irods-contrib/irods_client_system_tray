"""Background filesystem monitoring primitives built on watchdog and Qt signals."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

from config import MonitoredDirectory


class EventBridge(QObject):
    """Expose watchdog activity as Qt signals that the GUI layer can consume safely.

    Watchdog callbacks do not talk directly to widgets. This bridge lets the monitor
    manager forward background events into Qt's signal/slot system instead.
    """

    file_event = Signal(str, str, bool)
    ingest_requested = Signal(str)
    directory_relocated = Signal(str, str)
    directory_deleted = Signal(str)
    monitor_error = Signal(str)


class IngestionEventHandler(FileSystemEventHandler):
    """Translate every watchdog filesystem event into the shared event bridge."""

    def __init__(self, bridge: EventBridge) -> None:
        """Keep a reference to the bridge used for cross-thread event forwarding."""

        super().__init__()
        self.bridge = bridge

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Emit a compact Qt signal for any file or directory change watchdog sees."""

        self.bridge.file_event.emit(event.event_type, event.src_path, event.is_directory)

    def on_created(self, event: FileSystemEvent) -> None:
        """Forward created file paths for background ingestion."""

        super().on_created(event)
        if not event.is_directory:
            self.bridge.ingest_requested.emit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Forward moved destination paths for background ingestion."""

        super().on_moved(event)
        if not event.is_directory:
            self.bridge.ingest_requested.emit(event.dest_path)


class ParentDirectoryEventHandler(FileSystemEventHandler):
    """Surface monitored-folder rename, move, and delete events from parent watches."""

    def __init__(self, bridge: EventBridge) -> None:
        """Keep a reference to the bridge used for cross-thread event forwarding."""

        super().__init__()
        self.bridge = bridge

    def on_moved(self, event: FileSystemEvent) -> None:
        """Forward moved directory paths so the manager can classify them."""

        super().on_moved(event)
        print(
            "[parent-monitor] moved event "
            f"is_directory={event.is_directory} src={event.src_path!r} "
            f"dest={getattr(event, 'dest_path', None)!r}",
            flush=True,
        )
        self.bridge.directory_relocated.emit(event.src_path, event.dest_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Forward deleted directory paths so the manager can stop stale watches."""

        super().on_deleted(event)
        print(
            "[parent-monitor] deleted event "
            f"is_directory={event.is_directory} src={event.src_path!r}",
            flush=True,
        )
        self.bridge.directory_deleted.emit(event.src_path)


class MonitorManager(QObject):
    """Own the watchdog observer and keep watched directories in sync with config.

    The tray controller hands this manager the latest directory list and global active
    flag whenever settings change. The manager is responsible for starting, stopping,
    scheduling, and unscheduling watches without requiring an application restart.
    """

    file_event = Signal(str, str, bool)
    ingest_requested = Signal(str)
    monitored_directory_renamed = Signal(str, str)
    monitored_directory_moved = Signal(str, str)
    monitored_directory_deleted = Signal(str)
    monitor_error = Signal(str)

    def __init__(self) -> None:
        """Create the observer-facing state and wire internal signals outward."""

        super().__init__()
        self._observer: Observer | None = None
        self._bridge = EventBridge()
        self._handler = IngestionEventHandler(self._bridge)
        self._parent_handler = ParentDirectoryEventHandler(self._bridge)
        self._bridge.file_event.connect(self.file_event.emit)
        self._bridge.ingest_requested.connect(self.ingest_requested.emit)
        self._bridge.directory_relocated.connect(self._handle_directory_relocated)
        self._bridge.directory_deleted.connect(self._handle_directory_deleted)
        self._bridge.monitor_error.connect(self.monitor_error.emit)
        self._directory_watches: dict[str, ObservedWatch] = {}
        self._parent_watches: dict[str, ObservedWatch] = {}
        self._parent_children: dict[str, set[str]] = {}

    def sync(self, directories: list[MonitoredDirectory], is_active: bool) -> None:
        """Reconcile active filesystem watches with the latest application state.

        When monitoring is disabled this tears everything down. When enabled, it keeps
        existing watches that still belong, removes obsolete ones, and schedules new
        watches for directories that currently exist.
        """

        if not is_active:
            self.stop()
            return

        if not self._ensure_running():
            return

        # Track each configured directory alongside whether its watchdog should recurse.
        expected_directories = {
            directory.source_directory: directory.recursive for directory in directories
        }
        expected_parents = self._build_parent_map(directories)

        for directory, watch in list(self._directory_watches.items()):
            expected_recursive = expected_directories.get(directory)
            # Recreate the watch if the folder vanished or its recursive setting changed.
            if (
                expected_recursive is not None
                and watch.is_recursive == expected_recursive
                and Path(directory).is_dir()
            ):
                continue
            try:
                self._observer.unschedule(watch)
            except KeyError:
                pass
            del self._directory_watches[directory]

        for parent, watch in list(self._parent_watches.items()):
            if parent in expected_parents and Path(parent).is_dir():
                continue
            try:
                self._observer.unschedule(watch)
            except KeyError:
                pass
            del self._parent_watches[parent]
            self._parent_children.pop(parent, None)

        for directory in directories:
            if directory.source_directory in self._directory_watches:
                continue
            if not Path(directory.source_directory).is_dir():
                continue
            try:
                # Apply the per-folder recursive setting from app_state.json / the GUI.
                watch = self._observer.schedule(
                    self._handler,
                    directory.source_directory,
                    recursive=directory.recursive,
                )
            except OSError as exc:
                self.monitor_error.emit(
                    f"Failed to watch {directory.source_directory}: {exc}"
                )
                continue
            self._directory_watches[directory.source_directory] = watch

        for parent, children in expected_parents.items():
            self._parent_children[parent] = children
            if parent in self._parent_watches:
                continue
            try:
                watch = self._observer.schedule(self._parent_handler, parent, recursive=False)
            except OSError as exc:
                self.monitor_error.emit(f"Failed to watch parent folder {parent}: {exc}")
                continue
            self._parent_watches[parent] = watch

    def stop(self) -> None:
        """Stop the observer thread and clear all in-memory watch registrations."""

        if self._observer is None:
            return

        self._observer.stop()
        self._observer.join(timeout=5)
        self._observer = None
        self._directory_watches.clear()
        self._parent_watches.clear()
        self._parent_children.clear()

    def shutdown(self) -> None:
        """Provide a semantic shutdown hook for application exit paths."""

        self.stop()

    def _ensure_running(self) -> bool:
        """Start the watchdog observer if needed and report startup failures.

        Returning ``False`` lets callers skip further scheduling work when the monitor
        backend could not be started, while still keeping the rest of the application
        responsive.
        """

        if self._observer is not None and self._observer.is_alive():
            return True

        try:
            self._observer = Observer()
            self._observer.start()
        except OSError as exc:
            self._observer = None
            self.monitor_error.emit(f"Failed to start background monitor: {exc}")
            return False

        return True

    def _handle_directory_relocated(self, source_path: str, destination_path: str) -> None:
        """Classify monitored-folder parent events as renames or untrackable moves."""

        normalized_source = str(Path(source_path).expanduser().resolve(strict=False))
        normalized_destination = str(Path(destination_path).expanduser().resolve(strict=False))
        source_parent = str(Path(normalized_source).parent)
        destination_parent = str(Path(normalized_destination).parent)

        print(
            "[parent-monitor] classify moved "
            f"source={normalized_source!r} destination={normalized_destination!r} "
            f"source_parent={source_parent!r} destination_parent={destination_parent!r} "
            f"tracked_children={sorted(self._parent_children.get(source_parent, set()))!r}",
            flush=True,
        )

        if normalized_source not in self._parent_children.get(source_parent, set()):
            print(
                "[parent-monitor] ignoring moved event for untracked directory "
                f"{normalized_source!r}",
                flush=True,
            )
            return

        self._remove_directory_watch(normalized_source)

        if source_parent == destination_parent:
            print(
                "[parent-monitor] classified as rename "
                f"old={normalized_source!r} new={normalized_destination!r}",
                flush=True,
            )
            self.monitored_directory_renamed.emit(normalized_source, normalized_destination)
            return

        print(
            "[parent-monitor] classified as move-away "
            f"old={normalized_source!r} new={normalized_destination!r}",
            flush=True,
        )
        self.monitored_directory_moved.emit(normalized_source, normalized_destination)

    def _handle_directory_deleted(self, path: str) -> None:
        """Surface monitored folders deleted or moved away as unavailable."""

        normalized_path = str(Path(path).expanduser().resolve(strict=False))
        parent = str(Path(normalized_path).parent)

        print(
            "[parent-monitor] classify deleted "
            f"path={normalized_path!r} parent={parent!r} "
            f"tracked_children={sorted(self._parent_children.get(parent, set()))!r}",
            flush=True,
        )

        if normalized_path not in self._parent_children.get(parent, set()):
            print(
                "[parent-monitor] ignoring deleted event for untracked directory "
                f"{normalized_path!r}",
                flush=True,
            )
            return

        self._remove_directory_watch(normalized_path)
        print(
            f"[parent-monitor] classified as deleted path={normalized_path!r}",
            flush=True,
        )
        self.monitored_directory_deleted.emit(normalized_path)

    def _build_parent_map(
        self,
        directories: list[MonitoredDirectory],
    ) -> dict[str, set[str]]:
        """Collect existing parent folders that should be watched for directory moves."""

        parents: dict[str, set[str]] = {}
        for directory in directories:
            directory_path = Path(directory.source_directory)
            if not directory_path.is_dir():
                continue
            parent_path = directory_path.parent
            if parent_path == directory_path or not parent_path.is_dir():
                continue
            normalized_directory = str(directory_path.expanduser().resolve(strict=False))
            normalized_parent = str(parent_path.expanduser().resolve(strict=False))
            parents.setdefault(normalized_parent, set()).add(normalized_directory)

        return parents

    def _remove_directory_watch(self, directory: str) -> None:
        """Unschedule a tracked directory watch when its target path disappears."""

        watch = self._directory_watches.pop(directory, None)
        if watch is None or self._observer is None:
            print(
                "[parent-monitor] no directory watch to remove "
                f"directory={directory!r} observer_running={self._observer is not None}",
                flush=True,
            )
            return

        try:
            self._observer.unschedule(watch)
            print(
                f"[parent-monitor] removed directory watch for {directory!r}",
                flush=True,
            )
        except KeyError:
            print(
                f"[parent-monitor] directory watch already missing for {directory!r}",
                flush=True,
            )
