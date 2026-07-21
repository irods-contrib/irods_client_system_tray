"""System tray coordinator tying together config, monitoring, and the settings UI."""

from __future__ import annotations

from pathlib import Path
from threading import Thread

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal, QThread
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QSystemTrayIcon, QStyle

LOGO_PATH = Path(__file__).resolve().with_name("irods_logo.svg")

from config import (
    ConfigStore,
    IRODSEnvironmentStore,
    MonitoredDirectory,
    normalize_directory,
    normalize_irods_zone_name,
    normalize_monitored_directories,
    normalize_target_collection_for_zone,
    rezone_target_collection,
)
from irods_worker import IRODSUploadWorker
from monitor import MonitorManager
from ui import SettingsWindow


class TrayController(QObject):
    """Own the long-lived application state and system tray interactions.

    This controller is the central integration point for the app: it loads and saves
    configuration, updates the watchdog monitor, reacts to GUI events, and handles tray
    icon behavior so monitoring can continue while the window stays hidden.
    """

    queue_upload = Signal(str, str, str)
    notification_open_requested = Signal()

    def __init__(self, app: QApplication) -> None:
        """Build the tray icon, menu, monitor, and settings window for the app."""

        super().__init__()
        self.app = app
        self.app.setQuitOnLastWindowClosed(False)

        self.config_store = ConfigStore()
        self.irods_environment_store = IRODSEnvironmentStore()
        self.irods_environment_store.ensure_exists()
        self.config = self.config_store.load()
        self.environment = self.irods_environment_store.load()
        self.monitor = MonitorManager()
        self.window = SettingsWindow()
        self._queued_uploads: dict[str, str] = {}
        self._is_shutting_down = False

        self._align_directory_targets_with_zone()

        self.upload_thread = QThread(self)
        self.upload_worker = IRODSUploadWorker(self.irods_environment_store)
        self.upload_worker.moveToThread(self.upload_thread)
        self.upload_thread.start()

        self.monitor_toggle_action = QAction("Toggle Monitoring", self)
        self.monitor_toggle_action.setCheckable(True)
        self.monitor_toggle_action.toggled.connect(self.set_monitoring_active)
        self.menu = QMenu()

        tray_icon = QIcon(self._build_icon())
        self.tray_icon = QSystemTrayIcon(tray_icon, self)
        self.tray_icon.setToolTip("iRODS Ingest")
        self.window.setWindowIcon(tray_icon)
        self.tray_icon.activated.connect(self._handle_tray_activation)

        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self.toggle_window)

        self._build_menu()
        self._connect_signals()
        self._sync_from_config()
        self.window.set_irods_environment(self.environment)
        self.tray_icon.show()
        self.app.aboutToQuit.connect(self.shutdown)

    def show_window(self) -> None:
        """Show and focus the settings window from the tray or startup path."""

        self.window.showNormal()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def toggle_window(self) -> None:
        """Hide the settings window if visible, otherwise show and focus it."""

        if self.window.isVisible():
            self.window.hide()
            return
        self.show_window()

    def add_directory(self, source_directory: str, target_collection: str, recursive: bool) -> None:
        """Normalize and persist a new monitored directory from the UI."""

        normalized_source = normalize_directory(source_directory)
        normalized_target = normalize_target_collection_for_zone(
            target_collection,
            self.environment.irods_zone_name,
        )
        if any(
            directory.source_directory == normalized_source
            for directory in self.config.monitored_directories
        ):
            self.window.set_status_message(f"Already monitoring {normalized_source}")
            return

        # Prevent the user from adding a folder inside a recursively watched folder.
        source_path = Path(normalized_source)
        for directory in self.config.monitored_directories:
            if not directory.recursive:
                continue

            watched_path = Path(directory.source_directory)
            try:
                source_path.relative_to(watched_path)
            except ValueError:
                continue

            self.window.set_status_message(
                f"{normalized_source} is already monitored through recursive watch {directory.source_directory}",
                is_error=True,
            )
            return

        self.config.monitored_directories.append(
            MonitoredDirectory(
                source_directory=normalized_source,
                target_collection=normalized_target,
                recursive=recursive,
            )
        )
        self._persist_and_sync()
        self.window.set_status_message(f"Added {normalized_source}")

    def remove_directory(self, path: str) -> None:
        """Remove a monitored directory, then persist and resync background watches."""

        self.config.monitored_directories = [
            directory
            for directory in self.config.monitored_directories
            if directory.source_directory != path
        ]
        self._persist_and_sync()
        self.window.set_status_message(f"Removed {path}")

    def set_monitoring_active(self, is_active: bool) -> None:
        """Apply the global monitoring toggle from either the tray or the window."""

        self.config.is_monitoring_active = is_active
        self._persist_and_sync()

    def exit_application(self) -> None:
        """Save state, stop background monitoring, and quit the Qt application cleanly."""

        self.shutdown()
        self.app.quit()

    def shutdown(self) -> None:
        """Stop background services once so any quit path uses the same cleanup."""

        if self._is_shutting_down:
            return

        self._is_shutting_down = True
        self.config_store.save(self.config)
        self.monitor.shutdown()
        self.upload_thread.quit()
        self.upload_thread.wait(5000)
        self.window.hide()
        self.tray_icon.hide()

    def save_irods_settings(self) -> None:
        """Persist the iRODS session settings entered in the settings window."""

        environment = self.window.get_irods_environment()
        if not all(
            [
                environment.irods_host,
                environment.irods_user_name,
                environment.irods_password,
                environment.irods_zone_name,
            ]
        ):
            self.window.set_status_message(
                "Complete all iRODS fields before saving.",
                is_error=True,
            )
            return

        old_zone_name = self.environment.irods_zone_name
        new_zone_name = environment.irods_zone_name
        zone_changed = normalize_irods_zone_name(old_zone_name) != normalize_irods_zone_name(
            new_zone_name
        )
        if zone_changed:
            self._rezone_directory_targets(old_zone_name, new_zone_name)

        self.irods_environment_store.save(environment)
        self.environment = self.irods_environment_store.load()
        self.window.set_irods_environment(self.environment)
        if zone_changed:
            self.config_store.save(self.config)
            self._sync_from_config(show_status=False)
        self.window.set_status_message("Saved iRODS settings.")
        self.window.append_activity(
            f"saved iRODS settings for {environment.irods_user_name} at {environment.irods_host}:{environment.irods_port}"
        )
        if zone_changed:
            self.window.append_activity(
                f"updated monitored folder targets to use /{normalize_irods_zone_name(new_zone_name)}"
            )

    def _build_icon(self) -> QPixmap:
        """Rasterize the iRODS logo SVG."""

        renderer = QSvgRenderer(str(LOGO_PATH))
        size = renderer.defaultSize().scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio)
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(
            painter,
            QRectF((32 - size.width()) / 2, (32 - size.height()) / 2, size.width(), size.height()),
        )
        painter.end()
        return pixmap

    def _build_menu(self) -> None:
        """Create the tray context menu and wire actions to controller methods."""

        open_action = self.menu.addAction("Open Settings")
        open_action.triggered.connect(lambda _checked=False: self.show_window())
        self.menu.addAction(self.monitor_toggle_action)
        self.menu.addSeparator()
        exit_action = self.menu.addAction("Exit")
        exit_action.triggered.connect(lambda _checked=False: self.exit_application())
        self.tray_icon.setContextMenu(self.menu)

    def _connect_signals(self) -> None:
        """Connect UI and monitor signals so changes flow through one controller."""

        self.window.add_folder_requested.connect(self.add_directory)
        self.window.remove_folder_requested.connect(self.remove_directory)
        self.window.save_irods_requested.connect(self.save_irods_settings)
        self.window.monitoring_toggled.connect(self.set_monitoring_active)
        self.notification_open_requested.connect(self.show_window)
        self.monitor.file_event.connect(self._handle_file_event)
        self.monitor.ingest_requested.connect(self._queue_ingestion)
        self.monitor.monitored_directory_renamed.connect(self._handle_monitored_directory_renamed)
        self.monitor.monitored_directory_moved.connect(self._handle_monitored_directory_moved)
        self.monitor.monitored_directory_deleted.connect(self._handle_monitored_directory_deleted)
        self.monitor.monitor_error.connect(self._handle_monitor_error)
        self.queue_upload.connect(self.upload_worker.upload_file)
        self.upload_worker.upload_started.connect(self._handle_upload_started)
        self.upload_worker.upload_debug.connect(self.window.append_activity)
        self.upload_worker.upload_cancelled.connect(self._handle_upload_cancelled)
        self.upload_worker.upload_paths_resolved.connect(self._handle_upload_paths_resolved)
        self.upload_worker.upload_progress.connect(self._handle_upload_progress)
        self.upload_worker.upload_finished.connect(self._handle_upload_finished)
        self.upload_worker.upload_failed.connect(self._handle_upload_failed)

    def _sync_from_config(self, *, show_status: bool = True) -> None:
        """Push the current config into the monitor, tray menu, and visible window.

        This keeps every surface of the app consistent after startup or after any user
        action that changes directories or the global enabled state.
        """

        self.monitor.sync(self.config.monitored_directories, self.config.is_monitoring_active)

        invalid_directories = {
            directory.source_directory
            for directory in self.config.monitored_directories
            if not Path(directory.source_directory).is_dir()
        }
        available_directories = [
            directory.source_directory
            for directory in self.config.monitored_directories
            if directory.source_directory not in invalid_directories
        ]
        directories_missing_targets = [
            directory.source_directory
            for directory in self.config.monitored_directories
            if not directory.target_collection
        ]

        for directory in available_directories:
            self.upload_worker.allow_directory_uploads(directory)

        self.window.set_monitoring_active(self.config.is_monitoring_active)
        previous = self.monitor_toggle_action.blockSignals(True)
        self.monitor_toggle_action.setChecked(self.config.is_monitoring_active)
        self.monitor_toggle_action.blockSignals(previous)
        self.window.set_directories(self.config.monitored_directories, invalid_directories)

        if show_status:
            if not self.config.is_monitoring_active:
                self.window.set_status_message("Monitoring paused.")
            elif directories_missing_targets:
                self.window.set_status_message(
                    f"{len(directories_missing_targets)} monitored folder(s) need a target collection before uploads can run.",
                    is_error=True,
                )
            elif invalid_directories:
                self.window.set_status_message(
                    f"Monitoring active for available folders. {len(invalid_directories)} folder(s) are missing.",
                    is_error=True,
                )
            else:
                self.window.set_status_message("Monitoring active.")

    def _persist_and_sync(self) -> None:
        """Save the latest state and immediately refresh monitoring and UI widgets."""

        self.config.monitored_directories = normalize_monitored_directories(
            self.config.monitored_directories
        )
        self.config_store.save(self.config)
        self._sync_from_config()

    def _handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Interpret tray clicks so single and double clicks both toggle the window.

        A short timer delays the single-click action long enough to detect a possible
        double-click without triggering both behaviors.
        """

        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._single_click_timer.start(self.app.doubleClickInterval())
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._single_click_timer.stop()
            self.toggle_window()

    def _handle_file_event(self, event_type: str, path: str, is_directory: bool) -> None:
        """Format background file events into readable activity log entries."""

        entry_type = "folder" if is_directory else "file"
        self.window.append_activity(f"{event_type}: {entry_type} -> {path}")

    def _queue_ingestion(self, path: str) -> None:
        """Forward created and moved files to the iRODS worker thread once per path."""

        if not self.config.is_monitoring_active:
            return

        normalized_path = str(Path(path).expanduser().resolve(strict=False))
        monitored_directory = self._match_monitored_directory(normalized_path)
        if monitored_directory is None:
            return
        if not monitored_directory.target_collection:
            message = (
                f"No target collection configured for {monitored_directory.source_directory}."
            )
            self.window.set_status_message(message, is_error=True)
            self.window.append_activity(f"warning: {message}")
            return
        if normalized_path in self._queued_uploads:
            return

        self._queued_uploads[normalized_path] = monitored_directory.source_directory
        self.window.append_activity(f"queued upload -> {normalized_path}")
        self.queue_upload.emit(
            normalized_path,
            monitored_directory.source_directory,
            monitored_directory.target_collection,
        )

    def _handle_monitor_error(self, message: str) -> None:
        """Surface monitoring failures in both the status area and activity log."""

        self.window.set_status_message(message, is_error=True)
        self.window.append_activity(f"warning: {message}")

    def _handle_monitored_directory_renamed(self, old_path: str, new_path: str) -> None:
        """Persist a new folder path when a watched directory is renamed in place."""

        updated = False
        for directory in self.config.monitored_directories:
            if directory.source_directory != old_path:
                continue
            directory.source_directory = new_path
            updated = True

        if not updated:
            return

        self._persist_and_sync()
        self.window.set_status_message(f"Updated monitored folder to {new_path}")
        self.window.append_activity(f"folder renamed -> {old_path} to {new_path}")

    def _handle_monitored_directory_moved(self, old_path: str, new_path: str) -> None:
        """Refresh the UI and notify the user when a watched folder leaves its parent."""
        
        print(
            "[tray] handle monitored directory moved "
            f"old_path={old_path} ",
            f"new_path={new_path} ",
            flush=True,
        )

        if not any(
            directory.source_directory == old_path
            for directory in self.config.monitored_directories
        ):
            print("[tray] path NOT in monitored_directories ", flush=True,)
            return

        print("[tray] path in monitored_directories ", flush=True,)
        self._cancel_uploads_for_directory(old_path)
        self._sync_from_config(show_status=False)
        self.window.set_status_message(
            f"{old_path} was moved and is no longer being tracked.",
            is_error=True,
        )
        self.window.append_activity(f"folder moved -> {old_path} to {new_path}")
        self._show_moved_folder_notification(old_path)

    def _handle_monitored_directory_deleted(self, path: str) -> None:
        """Refresh the UI when a watched folder is deleted and can no longer be read."""
        
        print(
            "[tray] handle monitored directory deleted "
            f"path={path} ",
            flush=True,
        )

        if not any(
            directory.source_directory == path
            for directory in self.config.monitored_directories
        ):
            print("[tray] path NOT in monitored_directories ", flush=True,)
            return

        print("[tray] path in monitored_directories ", flush=True,)

        self._cancel_uploads_for_directory(path)
        self._sync_from_config(show_status=False)
        self.window.set_status_message(
            f"{path} is no longer available and can no longer be tracked.",
            is_error=True,
        )
        self.window.append_activity(f"folder deleted -> {path}")
        self._show_moved_folder_notification(path)

    def _handle_upload_started(self, local_path: str, logical_path: str) -> None:
        """Surface the start of an iRODS upload in the tray window."""

        self.window.set_status_message(f"Uploading {Path(local_path).name} to iRODS...")
        self.window.append_activity(f"uploading -> {local_path} to {logical_path}")

    def _handle_upload_progress(
        self,
        local_path: str,
        _logical_path: str,
        bytes_sent: int,
        total_bytes: int,
    ) -> None:
        """Show coarse-grained upload progress without blocking the UI thread."""

        if total_bytes <= 0:
            self.window.set_status_message(f"Uploading {Path(local_path).name}...")
            return

        percent_complete = int((bytes_sent / total_bytes) * 100)
        self.window.set_status_message(
            f"Uploading {Path(local_path).name}: {percent_complete}%"
        )

    def _handle_upload_paths_resolved(self, local_path: str, logical_path: str) -> None:
        """Record the final paths used for the imminent iRODS put operation."""

        self.window.append_activity(
            f"iRODS put paths -> local={local_path} logical={logical_path}"
        )

    def _handle_upload_finished(self, local_path: str, logical_path: str) -> None:
        """Clear queue tracking and log successful background uploads."""

        self._queued_uploads.pop(local_path, None)
        self.window.set_status_message(f"Uploaded {Path(local_path).name} to iRODS.")
        self.window.append_activity(f"uploaded -> {local_path} to {logical_path}")

    def _handle_upload_failed(self, local_path: str, message: str) -> None:
        """Clear queue tracking and surface upload failures to the user."""

        self._queued_uploads.pop(local_path, None)
        self.window.set_status_message(message, is_error=True)
        self.window.append_activity(f"upload failed: {local_path} ({message})")

    def _handle_upload_cancelled(self, local_path: str, message: str) -> None:
        """Drop queued uploads cleanly once a monitored folder becomes unavailable."""

        self._queued_uploads.pop(local_path, None)
        self.window.append_activity(f"upload cancelled: {local_path} ({message})")

    def _match_monitored_directory(self, path: str) -> MonitoredDirectory | None:
        """Return the configured watch root that contains the given file path."""

        candidate = Path(path).expanduser().resolve(strict=False)
        best_match: MonitoredDirectory | None = None

        for directory in self.config.monitored_directories:
            directory_path = Path(directory.source_directory).expanduser().resolve(strict=False)
            try:
                candidate.relative_to(directory_path)
            except ValueError:
                continue

            if best_match is None or len(directory.source_directory) > len(best_match.source_directory):
                best_match = directory

        return best_match

    def _cancel_uploads_for_directory(self, directory: str) -> None:
        """Stop any later queued uploads for a monitored folder that vanished."""

        self.upload_worker.cancel_directory_uploads(directory)
        self.window.append_activity(
            f"cancelling queued uploads for unavailable folder -> {directory}"
        )

    def _align_directory_targets_with_zone(self) -> None:
        """Ensure every stored target collection starts at the current zone root."""

        updated = False
        for directory in self.config.monitored_directories:
            normalized_target = normalize_target_collection_for_zone(
                directory.target_collection,
                self.environment.irods_zone_name,
            )
            if normalized_target == directory.target_collection:
                continue
            directory.target_collection = normalized_target
            updated = True

        if updated:
            self.config_store.save(self.config)

    def _rezone_directory_targets(self, old_zone_name: str, new_zone_name: str) -> None:
        """Rewrite stored target collections to follow a newly saved zone name."""

        for directory in self.config.monitored_directories:
            directory.target_collection = rezone_target_collection(
                directory.target_collection,
                old_zone_name,
                new_zone_name,
            )

    def _show_moved_folder_notification(self, directory: str) -> None:
        """Send a Windows toast when a monitored folder can no longer be tracked."""

        try:
            from win11toast import toast
        except ImportError:
            self.window.append_activity(
                "warning: win11toast is unavailable; could not show folder notification"
            )
            return

        message = (
            "A folder monitored for iRODS ingest has been moved or deleted and can no longer be "
            "tracked. You may need to reselect your monitored folder(s) to compensate."
        )

        Thread(
            target=self._run_moved_folder_notification,
            args=(toast, f"{Path(directory).name}: {message}"),
            daemon=True,
        ).start()

    def _handle_notification_click(self, _args=None) -> None:
        """Bring the configuration window to the foreground from a toast click."""

        self.notification_open_requested.emit()

    def _run_moved_folder_notification(self, toast, body: str) -> None:
        """Run the blocking toast callback loop away from the Qt GUI thread."""

        try:
            toast(
                "Monitored folder unavailable",
                body,
                duration="long",
                on_click=self._handle_notification_click,
                on_dismissed=lambda _args: None,
                on_failed=lambda _args: None,
            )
        except Exception as exc:
            print(f"warning: failed to show folder notification ({exc})", flush=True)
