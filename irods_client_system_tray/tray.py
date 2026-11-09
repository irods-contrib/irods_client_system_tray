"""System tray coordinator tying together config, monitoring, and the settings UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
import os
import re
import time
from math import ceil
from collections import deque
from pathlib import Path
from threading import Thread
from typing import Iterator

from PySide6.QtCore import QObject, Qt, Signal, QThread, QTimer
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QStyle

APP_ICON_RESOURCE = files("irods_client_system_tray").joinpath("app_icon.png")

from .config import (
    ConfigStore,
    IRODSEnvironment,
    IRODSEnvironmentStore,
    MonitoredDirectory,
    build_retry_settings_user_key,
    find_overlapping_monitored_directory,
    normalize_maximum_upload_workers,
    normalize_write_chunk_size,
    normalize_parallel_upload_threads,
    normalize_memory_cap,
    maximum_upload_workers_limit,
    DEFAULT_SINGLE_WRITE_CHUNK_MIB,
    DEFAULT_PARALLEL_WRITE_CHUNK_MIB,
    normalize_directory,
    normalize_retry_config,
    normalize_file_path,
    normalize_irods_zone_name,
    normalize_monitored_directories,
    normalize_post_upload_action,
    normalize_target_collection_for_zone,
    rezone_target_collection,
)
from .auto_workers import AutomaticUploadWorkers, TransferSample
from .eta_smoothing import EtaSmoother
from .throughput_history import ThroughputHistory
from .auto_chunks import AutomaticChunkSize, CHUNK_SIZES_MIB
from .irods_worker import IRODSUploadWorker
from .monitor import MonitorManager
from .ui import LoginDialog, SettingsWindow


@dataclass(slots=True)
class UploadJob:
    """Track the controller-side state for one queued or active upload."""

    local_path: str
    monitored_root: str
    target_collection: str
    post_upload_action: str = "delete"
    post_upload_destination: str = ""
    attempt_number: int = 1
    max_attempts: int = 1
    logical_path: str = ""
    last_error: str = ""
    status: str = "queued"
    started_at: float = 0.0
    size_bytes: int = 0
    bytes_sent: int = 0
    is_parallel: bool = False
    chunk_size_mib: int = 0
    parallel_upload_threads: int = 1
    retry_timer: QTimer | None = field(default=None, repr=False, compare=False)


class UploadDispatcher(QObject):
    """Deliver jobs to one worker's Qt thread without broadcasting to the pool."""

    upload = Signal(str, str, str, str, str)


@dataclass(slots=True)
class UploadWorkerSlot:
    worker: IRODSUploadWorker
    thread: QThread
    dispatcher: UploadDispatcher
    busy_path: str | None = None


class TrayController(QObject):
    """Own the long-lived application state and system tray interactions.

    This controller is the central integration point for the app: it loads and saves
    configuration, updates the watchdog monitor, reacts to GUI events, and handles tray
    icon behavior so monitoring can continue while the window stays hidden.
    """

    queue_upload = Signal(str, str, str, str, str)
    notification_open_requested = Signal()
    upload_environment_updated = Signal(object)
    upload_environment_cleared = Signal()
    THROUGHPUT_IDLE_SECONDS = 5
    PARALLEL_TRANSFER_THRESHOLD = 32 * 1024 * 1024

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
        self._queued_uploads: dict[str, UploadJob] = {}
        self._failed_uploads: dict[str, UploadJob] = {}
        self.config.maximum_upload_workers = normalize_maximum_upload_workers(
            self.config.maximum_upload_workers
        )
        self._auto_workers = (
            AutomaticUploadWorkers(
                maximum_upload_workers_limit(), initial=self.config.maximum_upload_workers
            )
            if self.config.automatic_upload_settings else None
        )
        self._effective_upload_workers = (
            self._auto_workers.current if self._auto_workers else self.config.maximum_upload_workers
        )
        self._single_chunk_mib = self.config.single_write_chunk_size_mib
        self._parallel_chunk_mib = self.config.parallel_write_chunk_size_mib
        self._effective_parallel_threads = self.config.parallel_upload_threads
        self._chunk_tuners = (
            {
                False: AutomaticChunkSize(self._single_chunk_mib),
                True: AutomaticChunkSize(
                    self._parallel_chunk_mib, parallel=True,
                    memory_cap_mib=self.config.memory_cap_mib,
                    parallel_threads=self._effective_parallel_threads,
                ),
            }
            if self.config.automatic_upload_settings else None
        )
        self._pending_parallel_chunk_mib: int | None = None
        self._configure_parallel_chunk_size(self._parallel_chunk_mib)
        self._enforce_chunk_memory_budget()
        self._recent_transfers: deque[TransferSample] = deque(maxlen=8)
        self._recent_throughput: float | None = None
        self._eta_smoother = EtaSmoother()
        self._throughput_history = ThroughputHistory()
        self._throughput_sample_timer = QTimer(self)
        self._throughput_sample_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._throughput_sample_timer.timeout.connect(self._sample_throughput)
        self._throughput_sample_timer.start(1000)
        self._last_transfer_activity_at: float | None = None
        self._throughput_idle_timer = QTimer(self)
        self._throughput_idle_timer.setSingleShot(True)
        self._throughput_idle_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._throughput_idle_timer.timeout.connect(self._handle_throughput_idle)
        self._upload_workers: list[UploadWorkerSlot] = []
        self._active_uploads: dict[str, UploadWorkerSlot] = {}
        self._pending_initial_scans: set[str] = {
            directory.source_directory for directory in self.config.monitored_directories
        }
        self._initial_scan_iterators: dict[str, Iterator[str]] = {}
        self._scheduled_initial_scans: set[str] = set()
        self._is_shutting_down = False
        self._login_dialog: LoginDialog | None = None
        self._show_window_after_login = False
        self._is_authenticated = False

        self._align_directory_targets_with_zone()

        self.sign_in_action = QAction("Main Window", self)
        self.sign_in_action.triggered.connect(
            lambda _checked=False: self.prompt_login(show_window_on_success=True)
        )
        self.sign_out_action = QAction("Logout", self)
        self.sign_out_action.triggered.connect(lambda _checked=False: self.sign_out())
        self.monitor_toggle_action = QAction("Toggle Monitoring", self)
        self.monitor_toggle_action.setCheckable(True)
        self.monitor_toggle_action.toggled.connect(self.set_monitoring_active)
        self.menu = QMenu()

        tray_icon = self._build_icon()
        self.tray_icon = QSystemTrayIcon(tray_icon, self)
        self.tray_icon.setToolTip("iRODS Ingest")
        if not self.app.windowIcon().isNull():
            self.window.setWindowIcon(self.app.windowIcon())

        self._build_menu()
        self.tray_icon.setContextMenu(self.menu)
        self._connect_signals()
        self._ensure_upload_workers()
        self.upload_worker = self._upload_workers[0].worker
        self.upload_thread = self._upload_workers[0].thread
        self.config.retry = normalize_retry_config(None)
        self.window.set_irods_environment(self.environment)
        self.window.set_retry_config(self.config.retry)
        self.window.set_maximum_upload_workers(self.config.maximum_upload_workers)
        self.window.set_automatic_upload_workers(
            self.config.automatic_upload_settings, self.config.memory_cap_mib
        )
        self.window.set_write_chunk_settings(
            self.config.single_write_chunk_size_mib, self.config.parallel_write_chunk_size_mib,
        )
        self.window.set_parallel_upload_threads(self.config.parallel_upload_threads)
        self._show_chunk_status()
        self._show_auto_worker_status()
        self._update_upload_summary()
        if self._is_authenticated:
            self._sync_from_config()
        else:
            self._apply_locked_state()
        self.tray_icon.show()
        self.app.aboutToQuit.connect(self.shutdown)

    def show_window(self) -> None:
        """Show and focus the settings window from the tray or startup path."""

        if not self._is_authenticated:
            self.prompt_login(show_window_on_success=True)
            return

        self.window.showNormal()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def toggle_window(self) -> None:
        """Hide the settings window if visible, otherwise show and focus it."""

        if not self._is_authenticated:
            self.prompt_login(show_window_on_success=True)
            return

        if self.window.isVisible():
            self.window.hide()
            return
        self.show_window()


    def prompt_login(self, *, show_window_on_success: bool = False) -> None:
        """Prompt for iRODS credentials while leaving the tray icon available."""

        self._show_window_after_login = self._show_window_after_login or show_window_on_success
        if self._login_dialog is not None:
            self._login_dialog.raise_()
            self._login_dialog.activateWindow()
            return

        login_dialog = LoginDialog(self.irods_environment_store.load())
        if not self.app.windowIcon().isNull():
            login_dialog.setWindowIcon(self.app.windowIcon())
        login_dialog.setModal(False)
        login_dialog.finished.connect(
            lambda result, dialog=login_dialog: self._handle_login_dialog_finished(dialog, result)
        )
        self._login_dialog = login_dialog
        login_dialog.show()
        login_dialog.raise_()
        login_dialog.activateWindow()

    def _handle_login_dialog_finished(
        self,
        login_dialog: LoginDialog,
        result: int,
    ) -> None:
        """Finish the asynchronous login flow after the dialog closes."""

        try:
            if result != LoginDialog.DialogCode.Accepted:
                self._show_window_after_login = False
                return
            if login_dialog.authenticated_environment is None:
                self._show_window_after_login = False
                return

            self._complete_login(login_dialog.authenticated_environment)
            if self._show_window_after_login:
                self.show_window()
        finally:
            self._show_window_after_login = False
            if self._login_dialog is login_dialog:
                self._login_dialog = None
            login_dialog.deleteLater()

    def add_directory(self, directory: object) -> None:
        """Normalize and persist a new monitored directory from the UI."""

        if not isinstance(directory, MonitoredDirectory):
            self.window.set_status_message("Failed to add folder: invalid folder configuration.", is_error=True)
            return

        if not directory.source_directory:
            self.window.set_status_message("Select a source directory to monitor.", is_error=True)
            return

        if (
            normalize_post_upload_action(directory.post_upload_action) == "move"
            and not normalize_file_path(directory.post_upload_destination)
        ):
            self.window.set_status_message(
                "Choose a destination for files moved after upload.",
                is_error=True,
            )
            return

        normalized_directories = normalize_monitored_directories([directory])
        if not normalized_directories:
            self.window.set_status_message("Select a source directory to monitor.", is_error=True)
            return

        normalized_directory = normalized_directories[0]
        normalized_source = normalized_directory.source_directory
        normalized_target = normalize_target_collection_for_zone(
            normalized_directory.target_collection,
            self.environment.irods_zone_name,
        )
        normalized_action = normalize_post_upload_action(normalized_directory.post_upload_action)
        normalized_destination = normalize_file_path(normalized_directory.post_upload_destination)
        if any(
            directory.source_directory == normalized_source
            for directory in self.config.monitored_directories
        ):
            self.window.set_status_message(f"Already monitoring {normalized_source}")
            return
        conflict_root = self._find_post_upload_destination_conflict(
            normalized_source,
            normalized_action,
            normalized_destination,
        )
        if conflict_root is not None:
            self.window.set_status_message(
                f"Move destination must be outside monitored folders. Conflicts with {conflict_root}.",
                is_error=True,
            )
            return

        overlapping_root = find_overlapping_monitored_directory(
            normalized_source,
            (directory.source_directory for directory in self.config.monitored_directories),
        )
        if overlapping_root is not None:
            self.window.set_status_message(
                f"Cannot monitor {normalized_source}: overlaps monitored folder {overlapping_root}.",
                is_error=True,
            )
            return

        self.config.monitored_directories.append(
            MonitoredDirectory(
                source_directory=normalized_source,
                target_collection=normalized_target,
                post_upload_action=normalized_action,
                post_upload_destination=normalized_destination,
                regex_filter=normalized_directory.regex_filter,
            )
        )
        self._persist_and_sync()
        self.window.set_status_message(f"Added {normalized_source}")
        self._pending_initial_scans.add(normalized_source)
        self._resume_initial_scans()

    def remove_directory(self, path: str) -> None:
        """Remove a monitored directory, then persist and resync background watches."""

        self.config.monitored_directories = [
            directory
            for directory in self.config.monitored_directories
            if directory.source_directory != path
        ]
        self._pending_initial_scans.discard(path)
        self._initial_scan_iterators.pop(path, None)
        self._persist_and_sync()
        self.window.set_status_message(f"Removed {path}")

    def set_monitoring_active(self, is_active: bool) -> None:
        """Apply the global monitoring toggle from either the tray or the window."""

        self.config.is_monitoring_active = is_active
        if not is_active:
            self._pause_pending_retries()
            if self._auto_workers is not None:
                self._auto_workers.reset_observations()
            if self._chunk_tuners is not None:
                for tuner in self._chunk_tuners.values():
                    tuner.reset()
        self._persist_and_sync()

    def exit_application(self) -> None:
        """Save state, stop background monitoring, and quit the Qt application cleanly."""

        self.shutdown()
        self.app.quit()

    def shutdown(self) -> None:
        """Stop background services once so any quit path uses the same cleanup."""

        if self._is_shutting_down:
            return

        self.window.flush_pending_settings_save()
        self._is_shutting_down = True
        self._throughput_sample_timer.stop()
        self._pending_initial_scans.clear()
        self._initial_scan_iterators.clear()
        self._scheduled_initial_scans.clear()
        self._snapshot_effective_upload_settings()
        self.config_store.save(self.config)
        self.monitor.shutdown()
        self._clear_upload_jobs()
        for slot in self._upload_workers:
            slot.thread.quit()
        for slot in self._upload_workers:
            slot.thread.wait(5000)
        if all(not slot.thread.isRunning() for slot in self._upload_workers):
            from irods import parallel

            parallel.COPY_BUF_SIZE = self._original_parallel_chunk_size
        self.window.hide()
        self.tray_icon.hide()

    def save_settings(self) -> None:
        """Persist retry settings and apply the maximum active upload workers."""

        automatic = self.window.get_automatic_upload_workers()
        memory_cap = normalize_memory_cap(self.window.get_memory_cap())
        cap_changed = memory_cap != self.config.memory_cap_mib
        self.config.memory_cap_mib = memory_cap
        if not automatic or not self.config.automatic_upload_settings:
            self.config.maximum_upload_workers = normalize_maximum_upload_workers(
                self.window.get_maximum_upload_workers()
            )
            single, parallel = self.window.get_write_chunk_settings()
            self.config.single_write_chunk_size_mib = normalize_write_chunk_size(
                single, DEFAULT_SINGLE_WRITE_CHUNK_MIB
            )
            self.config.parallel_write_chunk_size_mib = normalize_write_chunk_size(
                parallel, DEFAULT_PARALLEL_WRITE_CHUNK_MIB
            )
            self.config.parallel_upload_threads = normalize_parallel_upload_threads(
                self.window.get_parallel_upload_threads()
            )
        if automatic != self.config.automatic_upload_settings:
            self._auto_workers = (
                AutomaticUploadWorkers(maximum_upload_workers_limit()) if automatic else None
            )
            self._chunk_tuners = (
                {
                    False: AutomaticChunkSize(self.config.single_write_chunk_size_mib),
                    True: AutomaticChunkSize(
                        self.config.parallel_write_chunk_size_mib, parallel=True,
                        memory_cap_mib=self.config.memory_cap_mib,
                        parallel_threads=self.config.parallel_upload_threads,
                    ),
                }
                if automatic else None
            )
        self.config.automatic_upload_settings = automatic
        if not automatic:
            self._effective_parallel_threads = self.config.parallel_upload_threads
        if automatic and self._chunk_tuners is not None:
            self._chunk_tuners[True].memory_cap_mib = self.config.memory_cap_mib
            if cap_changed:
                self._chunk_tuners[True].reset()
                self._chunk_tuners[False].reset()
                self._auto_workers.reset_observations()
        self._single_chunk_mib = (
            self._chunk_tuners[False].current if automatic
            else self.config.single_write_chunk_size_mib
        )
        self._queue_parallel_chunk_change(
            self._chunk_tuners[True].current if automatic
            else self.config.parallel_write_chunk_size_mib
        )
        self._effective_upload_workers = (
            self._auto_workers.current if self._auto_workers else self.config.maximum_upload_workers
        )
        self._enforce_chunk_memory_budget()
        self._drain_upload_queue()
        self._snapshot_effective_upload_settings()
        self.config.retry = normalize_retry_config(self.window.get_retry_config())
        user_key = build_retry_settings_user_key(self.environment)
        self.config.retry_by_user[user_key] = self.config.retry
        self.config_store.save(self.config)
        self.window.set_retry_config(self.config.retry)
        self.window.set_maximum_upload_workers(self.config.maximum_upload_workers)
        self.window.set_automatic_upload_workers(automatic, self.config.memory_cap_mib)
        if not automatic:
            self.window.set_parallel_upload_threads(self.config.parallel_upload_threads)
            self.window.set_write_chunk_settings(
                self.config.single_write_chunk_size_mib, self.config.parallel_write_chunk_size_mib
            )
        self._show_chunk_status()
        self._show_auto_worker_status()
        self._ensure_upload_workers()
        self._drain_upload_queue()
        self.window.show_settings_saved_notice()
        self.window.append_activity(
            f"saved upload settings -> maximum workers={self._effective_upload_workers}, "
            f"retry attempts={self.config.retry.attempts}"
        )

    def _snapshot_effective_upload_settings(self) -> None:
        """Persist the sizes actually in use, not an uncommitted parallel trial."""

        if not self.config.automatic_upload_settings:
            return
        self.config.maximum_upload_workers = self._effective_upload_workers
        self.config.single_write_chunk_size_mib = self._single_chunk_mib
        self.config.parallel_write_chunk_size_mib = self._parallel_chunk_mib
        self.config.parallel_upload_threads = self._effective_parallel_threads

    def sign_out(self) -> None:
        """Lock the app and return control to the sign-in dialog."""

        if not self._is_authenticated:
            return

        self._is_authenticated = False
        self._clear_upload_jobs()
        self.upload_environment_cleared.emit()
        self.environment = self.irods_environment_store.load()
        self.window.set_irods_environment(self.environment)
        self.window.append_activity("logged out")
        self._apply_locked_state()
        self.prompt_login(show_window_on_success=True)

    def _build_icon(self) -> QIcon:
        """Use the same round wordmark in the tray as the app window and Dock."""

        if not self.app.windowIcon().isNull():
            return self.app.windowIcon()
        pixmap = QPixmap()
        if not pixmap.loadFromData(APP_ICON_RESOURCE.read_bytes(), "PNG"):
            raise RuntimeError("Could not load the bundled application icon.")
        return QIcon(pixmap)

    def _build_menu(self) -> None:
        """Create the tray context menu and wire actions to controller methods."""

        self.menu.addAction(self.sign_in_action)
        self.menu.addAction(self.sign_out_action)
        self.auth_separator = self.menu.addSeparator()
        open_action = self.menu.addAction("Main Window")
        open_action.triggered.connect(lambda _checked=False: self.show_window())
        self.open_settings_action = open_action
        self.menu.addAction(self.monitor_toggle_action)
        self.exit_separator = self.menu.addSeparator()
        exit_action = self.menu.addAction("Exit")
        exit_action.triggered.connect(lambda _checked=False: self.exit_application())
        self.exit_action = exit_action

    def _connect_signals(self) -> None:
        """Connect UI and monitor signals so changes flow through one controller."""

        self.window.add_folder_requested.connect(self.add_directory)
        self.window.remove_folder_requested.connect(self.remove_directory)
        self.window.retry_failed_upload_requested.connect(self.retry_failed_upload)
        self.window.save_settings_requested.connect(self.save_settings)
        self.window.monitoring_toggled.connect(self.set_monitoring_active)
        self.notification_open_requested.connect(self.show_window)
        self.monitor.file_event.connect(self._handle_file_event)
        self.monitor.ingest_requested.connect(self._queue_ingestion)
        self.monitor.monitored_directory_renamed.connect(self._handle_monitored_directory_renamed)
        self.monitor.monitored_directory_moved.connect(self._handle_monitored_directory_moved)
        self.monitor.monitored_directory_deleted.connect(self._handle_monitored_directory_deleted)
        self.monitor.monitor_error.connect(self._handle_monitor_error)

    def _ensure_upload_workers(self) -> None:
        """Create workers as needed; idle extras stay parked if the limit is reduced."""

        while len(self._upload_workers) < self._effective_upload_workers:
            thread = QThread(self)
            worker = IRODSUploadWorker(self.environment if self._is_authenticated else None)
            worker.moveToThread(thread)
            dispatcher = UploadDispatcher(self)
            dispatcher.upload.connect(worker.upload_file)
            self.upload_environment_updated.connect(worker.set_environment)
            self.upload_environment_cleared.connect(worker.clear_environment)
            worker.upload_started.connect(self._handle_upload_started)
            worker.upload_debug.connect(self.window.append_activity)
            worker.upload_cancelled.connect(self._handle_upload_cancelled)
            worker.upload_paths_resolved.connect(self._handle_upload_paths_resolved)
            worker.upload_progress.connect(self._handle_upload_progress)
            worker.upload_finished.connect(self._handle_upload_finished)
            worker.upload_warning.connect(self._handle_upload_warning)
            worker.upload_failed.connect(self._handle_upload_failed)
            self._upload_workers.append(UploadWorkerSlot(worker, thread, dispatcher))
            thread.start()

    def _configure_parallel_chunk_size(self, size_mib: int) -> None:
        """Change the client-wide value only when no transfer is using it."""

        from irods import parallel

        if not hasattr(self, "_original_parallel_chunk_size"):
            self._original_parallel_chunk_size = parallel.COPY_BUF_SIZE
        parallel.COPY_BUF_SIZE = size_mib * 1024 * 1024
        self._parallel_chunk_mib = size_mib

    def _queue_parallel_chunk_change(self, size_mib: int) -> None:
        self._pending_parallel_chunk_mib = (
            size_mib if size_mib != self._parallel_chunk_mib else None
        )

    def _enforce_chunk_memory_budget(self) -> None:
        if self._chunk_tuners is None:
            return
        parallel_tuner = self._chunk_tuners[True]
        self._effective_parallel_threads = min(
            self.config.parallel_upload_threads,
            max(1, self.config.memory_cap_mib // min(CHUNK_SIZES_MIB)),
        )
        if parallel_tuner.parallel_threads != self._effective_parallel_threads:
            parallel_tuner.parallel_threads = self._effective_parallel_threads
            parallel_tuner.reset()
            if self._auto_workers is not None:
                self._auto_workers.reset_observations()
        parallel_tuner.limit_for_workers(1)
        if self._auto_workers is not None:
            max_at_current_size = max(
                1, self.config.memory_cap_mib //
                (self._effective_parallel_threads * parallel_tuner.current)
            )
            self._effective_upload_workers = self._auto_workers.set_ceiling(
                min(maximum_upload_workers_limit(), max_at_current_size)
            )
        max_workers = max(
            1, self.config.memory_cap_mib //
            (self._effective_parallel_threads * min(CHUNK_SIZES_MIB))
        )
        self._effective_upload_workers = min(self._effective_upload_workers, max_workers)
        size = parallel_tuner.limit_for_workers(self._effective_upload_workers)
        self._queue_parallel_chunk_change(size)

    def _show_chunk_status(self) -> None:
        if self._chunk_tuners is not None:
            self.window.set_chunk_status(self._single_chunk_mib, self._parallel_chunk_mib)

    def _chunks_probing(self) -> bool:
        return self._chunk_tuners is not None and any(
            tuner.probing for tuner in self._chunk_tuners.values()
        )

    def _record_chunk_success(self, job: UploadJob, finished_at: float) -> None:
        if self._chunk_tuners is None or self._pending_parallel_chunk_mib is not None:
            return
        if self._auto_workers is not None and self._auto_workers.probing:
            for tuner in self._chunk_tuners.values():
                tuner.clear_window()
            return
        tuner = self._chunk_tuners[job.is_parallel]
        if job.chunk_size_mib != tuner.current:
            return
        if any(other.probing for other in self._chunk_tuners.values() if other is not tuner):
            tuner.clear_window()
            return
        was_probing = tuner.probing
        change = tuner.record_success(
            job.size_bytes, job.started_at, finished_at,
            backlogged=self._has_pending_uploads(), workers=self._effective_upload_workers,
        )
        if change is None:
            if was_probing and not tuner.probing and self._auto_workers is not None:
                self._auto_workers.reset_observations()
            return
        if self._auto_workers is not None:
            self._auto_workers.reset_observations()
        if job.is_parallel:
            self._queue_parallel_chunk_change(change)
        else:
            self._single_chunk_mib = change
        self.window.append_activity(
            f"automatic {'parallel' if job.is_parallel else 'single'} write size -> {change} MiB"
        )
        self._show_chunk_status()

    def _record_chunk_failure(self, job: UploadJob, message: str) -> None:
        if self._chunk_tuners is None or self._pending_parallel_chunk_mib is not None:
            return
        tuner = self._chunk_tuners[job.is_parallel]
        if job.chunk_size_mib != tuner.current:
            return
        change = tuner.record_failure(message, time.monotonic())
        if change is None:
            return
        if job.is_parallel:
            self._queue_parallel_chunk_change(change)
        else:
            self._single_chunk_mib = change
        self.window.append_activity(f"reverted write size after transfer failure -> {change} MiB")
        self._show_chunk_status()

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
            for slot in self._upload_workers:
                slot.worker.allow_directory_uploads(directory)

        self.window.set_monitoring_active(self.config.is_monitoring_active)
        self.sign_in_action.setVisible(False)
        self.sign_out_action.setVisible(True)
        self.sign_out_action.setEnabled(True)
        self.auth_separator.setVisible(True)
        self.open_settings_action.setVisible(True)
        self.open_settings_action.setEnabled(True)
        previous = self.monitor_toggle_action.blockSignals(True)
        self.monitor_toggle_action.setChecked(self.config.is_monitoring_active)
        self.monitor_toggle_action.blockSignals(previous)
        self.monitor_toggle_action.setVisible(True)
        self.monitor_toggle_action.setEnabled(True)
        self.exit_separator.setVisible(True)
        self.exit_action.setVisible(True)
        self.window.set_directories(self.config.monitored_directories, invalid_directories)
        self._refresh_failed_uploads()

        if show_status:
            if not self.config.is_monitoring_active:
                self.window.set_status_message("")
            elif directories_missing_targets:
                self.window.set_status_message(
                    f"{len(directories_missing_targets)} monitored folder(s) need a target collection before uploads can run.",
                    is_error=True,
                )
            elif invalid_directories:
                self.window.set_status_message(
                    f"{len(invalid_directories)} monitored folder(s) are missing.",
                    is_error=True,
                )
            else:
                self.window.set_status_message("")
        self._resume_initial_scans()
        self._drain_upload_queue()

    def _persist_and_sync(self) -> None:
        """Save the latest state and immediately refresh monitoring and UI widgets."""

        self.config.monitored_directories = normalize_monitored_directories(
            self.config.monitored_directories
        )
        self.config_store.save(self.config)
        self._sync_from_config()

    def _resume_initial_scans(self) -> None:
        """Scan saved and newly added folders once when monitoring is available."""

        if self._is_shutting_down or not self._is_authenticated or not self.config.is_monitoring_active:
            return

        configured_roots = {directory.source_directory for directory in self.config.monitored_directories}
        for root in tuple(self._pending_initial_scans):
            if root not in configured_roots:
                self._pending_initial_scans.discard(root)
                continue
            if not Path(root).is_dir():
                continue
            self._pending_initial_scans.remove(root)
            self._initial_scan_iterators[root] = self._iter_existing_files(root)

        for root in tuple(self._initial_scan_iterators):
            self._schedule_initial_scan(root)

    def _iter_existing_files(self, root: str) -> Iterator[str]:
        """Yield regular files recursively without following directory symlinks."""

        def report_error(error: OSError) -> None:
            self._handle_monitor_error(f"Failed to scan {error.filename}: {error}")

        for current_root, _directories, filenames in os.walk(root, onerror=report_error):
            for filename in filenames:
                path = Path(current_root) / filename
                if path.is_file():
                    yield str(path)

    def _schedule_initial_scan(self, root: str) -> None:
        if root in self._scheduled_initial_scans:
            return
        self._scheduled_initial_scans.add(root)
        QTimer.singleShot(0, lambda: self._scan_initial_batch(root))

    def _scan_initial_batch(self, root: str) -> None:
        """Queue a bounded batch, allowing the GUI to process events between batches."""

        self._scheduled_initial_scans.discard(root)
        iterator = self._initial_scan_iterators.get(root)
        if iterator is None:
            return
        if self._is_shutting_down or not any(
            directory.source_directory == root for directory in self.config.monitored_directories
        ):
            self._initial_scan_iterators.pop(root, None)
            return
        if not self._is_authenticated or not self.config.is_monitoring_active:
            return

        for _ in range(100):
            try:
                path = next(iterator)
            except StopIteration:
                self._initial_scan_iterators.pop(root, None)
                return
            self._queue_ingestion(path)
            if (
                self._is_shutting_down
                or not self._is_authenticated
                or not self.config.is_monitoring_active
                or self._initial_scan_iterators.get(root) is not iterator
            ):
                return
        self._schedule_initial_scan(root)

    def _apply_locked_state(self) -> None:
        """Keep the tray visible while preventing access to the main app before sign-in."""

        self.monitor.shutdown()
        self.window.hide()
        self.window.set_status_message("Sign in required before using the ingestion monitor.")
        self.sign_in_action.setVisible(True)
        self.sign_in_action.setEnabled(True)
        self.sign_out_action.setVisible(False)
        self.auth_separator.setVisible(False)
        self.open_settings_action.setVisible(False)
        self.monitor_toggle_action.setVisible(False)
        self.exit_separator.setVisible(False)
        self.exit_action.setVisible(True)

    def _complete_login(self, environment: IRODSEnvironment) -> None:
        """Persist the authenticated user and unlock the existing application UI."""

        old_zone_name = self.environment.irods_zone_name
        if normalize_irods_zone_name(old_zone_name) != normalize_irods_zone_name(
            environment.irods_zone_name
        ):
            self._rezone_directory_targets(old_zone_name, environment.irods_zone_name)
            self.config_store.save(self.config)

        self._is_authenticated = True
        if self._auto_workers is not None:
            self._auto_workers.reset_observations()
            self._enforce_chunk_memory_budget()
            self._show_auto_worker_status()
        self.environment = environment
        self.config.retry = self._retry_config_for_environment(environment)
        self.irods_environment_store.save(self._without_password(environment))
        self.upload_environment_updated.emit(environment)
        self.window.set_irods_environment(environment)
        self.window.set_retry_config(self.config.retry)
        self.window.append_activity(
            f"logged into iRODS as {environment.irods_user_name}#{environment.irods_zone_name} "
            f"to {environment.irods_host}:{environment.irods_port}"
        )
        self._sync_from_config()

    def _retry_config_for_environment(self, environment: IRODSEnvironment):
        """Return the saved retry settings for the current user, if any."""

        user_key = build_retry_settings_user_key(environment)
        if user_key in self.config.retry_by_user:
            return normalize_retry_config(self.config.retry_by_user[user_key])
        return normalize_retry_config(None)

    def _without_password(self, environment: IRODSEnvironment) -> IRODSEnvironment:
        """Return an environment snapshot safe to persist to disk."""

        return IRODSEnvironment(
            irods_host=environment.irods_host,
            irods_port=environment.irods_port,
            irods_user_name=environment.irods_user_name,
            irods_password="",
            irods_zone_name=environment.irods_zone_name,
        )

    def _handle_file_event(self, event_type: str, path: str, is_directory: bool) -> None:
        """Format background file events into readable activity log entries."""

        entry_type = "folder" if is_directory else "file"
        self.window.append_activity(f"{event_type}: {entry_type} -> {path}")
        if is_directory:
            return

        if event_type in {"deleted", "moved"}:
            self._remove_failed_upload_for_missing_path(path)

    def _queue_ingestion(self, path: str) -> None:
        """Forward created and moved files to the iRODS worker thread once per path."""

        if not self.config.is_monitoring_active:
            return
        if not self._is_authenticated:
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
        if not self._should_upload_file(normalized_path, monitored_directory):
            self.window.append_activity(f"regex filter skipped -> {normalized_path}")
            return
        if normalized_path in self._queued_uploads:
            return
        if normalized_path in self._failed_uploads:
            self._failed_uploads.pop(normalized_path, None)
            self._refresh_failed_uploads()

        job = UploadJob(
            local_path=normalized_path,
            monitored_root=monitored_directory.source_directory,
            target_collection=monitored_directory.target_collection,
            post_upload_action=monitored_directory.post_upload_action,
            post_upload_destination=monitored_directory.post_upload_destination,
            max_attempts=max(1, self.config.retry.attempts + 1),
        )
        self._queued_uploads[normalized_path] = job
        self.window.append_activity(f"queued upload -> {normalized_path}")
        self._dispatch_upload_job(job)

    def _dispatch_upload_job(self, job: UploadJob) -> None:
        """Queue one upload attempt until an active worker is available."""

        self._failed_uploads.pop(job.local_path, None)
        self._stop_retry_timer(job)
        job.status = "queued"
        job.started_at = 0.0
        job.bytes_sent = 0
        try:
            job.size_bytes = Path(job.local_path).stat().st_size
        except OSError:
            job.size_bytes = 0
        self._refresh_failed_uploads()
        self._drain_upload_queue()

    def _has_pending_uploads(self) -> bool:
        return any(job.status == "queued" for job in self._queued_uploads.values())

    def _update_upload_summary(self, *, sample_eta: bool = False) -> None:
        waiting = sum(job.status == "queued" for job in self._queued_uploads.values())
        throughput = self._current_throughput()
        remaining_bytes = self._remaining_upload_bytes()
        raw_eta = self._estimated_time_remaining_seconds(remaining_bytes, throughput)
        eta = self._eta_smoother.display(
            raw_eta, remaining_bytes, time.monotonic(), sample=sample_eta,
        )
        self.window.set_upload_summary(
            self._effective_upload_workers, len(self._active_uploads), waiting,
            throughput, remaining_bytes=remaining_bytes,
            eta_seconds=eta,
        )

    def _sample_throughput(self) -> None:
        """Refresh both graph and header from actual progress each second."""

        now = time.time()
        self._throughput_history.sample(now, time.monotonic())
        self.window.set_throughput_history(tuple(self._throughput_history.samples), now)
        self._update_upload_summary(sample_eta=True)

    def _current_throughput(self) -> float | None:
        if (
            self._last_transfer_activity_at is not None
            and (self._recent_throughput is not None or self._throughput_history.has_progress)
            and time.monotonic() - self._last_transfer_activity_at >= self.THROUGHPUT_IDLE_SECONDS
        ):
            return 0.0
        if self._throughput_history.has_progress and self._throughput_history.samples:
            return self._throughput_history.samples[-1].mib_per_second * 1024 * 1024
        return self._recent_throughput

    def _note_transfer_activity(self, when: float | None = None) -> None:
        self._last_transfer_activity_at = time.monotonic() if when is None else when
        if self._recent_throughput is not None or self._throughput_history.has_progress:
            self._throughput_idle_timer.start(int(self.THROUGHPUT_IDLE_SECONDS * 1000) + 50)

    def _handle_throughput_idle(self) -> None:
        self._update_upload_summary()
        self._show_auto_worker_status()

    def _remaining_upload_bytes(self) -> int | None:
        """Count unsent active bytes and file sizes waiting for an upload worker."""

        jobs = [
            job for job in self._queued_uploads.values()
            if job.status in {"queued", "dispatched", "uploading"}
        ]
        remaining_bytes = 0
        for job in jobs:
            if job.status == "queued" and job.size_bytes == 0:
                try:
                    job.size_bytes = Path(job.local_path).stat().st_size
                except OSError:
                    return None
            remaining_bytes += max(0, job.size_bytes - job.bytes_sent)
        return remaining_bytes

    def _estimated_time_remaining_seconds(
        self, remaining_bytes: int | None, throughput: float | None
    ) -> int | None:
        """Estimate time remaining at the last observed aggregate throughput."""

        if remaining_bytes == 0:
            return 0
        if (
            remaining_bytes is None or not self._is_authenticated
            or not self.config.is_monitoring_active
            or throughput is None or throughput <= 0
        ):
            return None
        return ceil(remaining_bytes / throughput)

    def _record_completed_transfer(self, job: UploadJob, finished_at: float) -> None:
        if job.size_bytes <= 0 or job.started_at <= 0:
            return
        if self._recent_transfers and job.started_at - max(
            sample.finished_at for sample in self._recent_transfers
        ) > 60:
            self._recent_transfers.clear()
        self._recent_transfers.append(
            TransferSample(job.size_bytes, job.started_at, finished_at)
        )
        elapsed = max(sample.finished_at for sample in self._recent_transfers) - min(
            sample.started_at for sample in self._recent_transfers
        )
        if elapsed > 0:
            self._recent_throughput = sum(
                sample.bytes_transferred for sample in self._recent_transfers
            ) / elapsed
            self._note_transfer_activity(finished_at)

    def _show_auto_worker_status(self) -> None:
        if self._auto_workers is not None:
            self.window.set_auto_worker_status(self._effective_upload_workers)
            self.window.set_parallel_upload_threads(self._effective_parallel_threads)

    def _apply_auto_worker_change(self, new_limit: int | None) -> None:
        if self._auto_workers is None:
            return
        if new_limit is not None and new_limit != self._effective_upload_workers:
            self._effective_upload_workers = new_limit
            if self._chunk_tuners is not None:
                for tuner in self._chunk_tuners.values():
                    tuner.reset()
                self._enforce_chunk_memory_budget()
            self._ensure_upload_workers()
            self.window.append_activity(
                f"automatic upload workers -> {self._effective_upload_workers}"
            )
        self._show_auto_worker_status()
        self._update_upload_summary()

    def _drain_upload_queue(self) -> None:
        """Dispatch pending files without exceeding the configured worker limit."""

        if self._pending_parallel_chunk_mib is not None:
            if any(slot.busy_path is not None for slot in self._upload_workers):
                self._update_upload_summary()
                return
            self._configure_parallel_chunk_size(self._pending_parallel_chunk_mib)
            self._pending_parallel_chunk_mib = None
            if self._chunk_tuners is not None:
                self._chunk_tuners[True].clear_window()
                self._enforce_chunk_memory_budget()
            self._show_chunk_status()

        if (
            self._is_shutting_down
            or not self._is_authenticated
            or not self.config.is_monitoring_active
            or sum(slot.busy_path is not None for slot in self._upload_workers)
            >= self._effective_upload_workers
        ):
            self._update_upload_summary()
            return

        workers = self._upload_workers[:self._effective_upload_workers]
        for job in self._queued_uploads.values():
            if sum(slot.busy_path is not None for slot in self._upload_workers) >= self._effective_upload_workers:
                break
            if job.status != "queued":
                continue
            slot = next((worker for worker in workers if worker.busy_path is None), None)
            if slot is None:
                break
            slot.busy_path = job.local_path
            self._active_uploads[job.local_path] = slot
            job.status = "dispatched"
            job.parallel_upload_threads = self._effective_parallel_threads
            job.is_parallel = (
                job.size_bytes > self.PARALLEL_TRANSFER_THRESHOLD
                and job.parallel_upload_threads > 1
            )
            job.chunk_size_mib = (
                self._parallel_chunk_mib if job.is_parallel else self._single_chunk_mib
            )
            slot.worker.write_buffer_size = job.chunk_size_mib * 1024 * 1024
            slot.worker.parallel_upload_threads = job.parallel_upload_threads
            args = (
                job.local_path,
                job.monitored_root,
                job.target_collection,
                job.post_upload_action,
                job.post_upload_destination,
            )
            self.queue_upload.emit(*args)
            slot.dispatcher.upload.emit(*args)
        self._update_upload_summary()

    def _release_upload_worker(self, local_path: str) -> None:
        slot = self._active_uploads.pop(local_path, None)
        if slot is None:
            slot = next(
                (worker for worker in self._upload_workers if worker.busy_path == local_path),
                None,
            )
        if slot is not None and slot.busy_path == local_path:
            slot.busy_path = None

    def retry_failed_upload(self, local_path: str) -> None:
        """Move a terminally failed upload back into the active queue and retry it now."""

        if not self.config.is_monitoring_active:
            self.window.set_status_message(
                "Enable monitoring before retrying uploads.",
                is_error=True,
            )
            return

        job = self._failed_uploads.pop(local_path, None)
        if job is None:
            self.window.set_status_message("Select a failed upload to retry.", is_error=True)
            self._refresh_failed_uploads()
            return

        job.attempt_number = 1
        job.max_attempts = max(1, self.config.retry.attempts + 1)
        job.logical_path = ""
        job.last_error = ""
        job.status = "queued"
        if not self._refresh_upload_job_destination(job):
            self._failed_uploads[local_path] = job
            self._refresh_failed_uploads()
            return
        self._queued_uploads[local_path] = job
        self.window.append_activity(f"manual retry -> {local_path}")
        self._dispatch_upload_job(job)

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
        self._remove_failed_uploads_for_directory(old_path)
        self.window.set_status_message(f"Updated monitored folder to {new_path}")
        self.window.append_activity(f"folder renamed -> {old_path} to {new_path}")

    def _handle_monitored_directory_moved(self, old_path: str, new_path: str) -> None:
        """Refresh the UI and notify the user when a watched folder leaves its parent."""

        if not any(
            directory.source_directory == old_path
            for directory in self.config.monitored_directories
        ):
            return

        self._cancel_uploads_for_directory(old_path)
        self._remove_failed_uploads_for_directory(old_path)
        self._sync_from_config(show_status=False)
        self.window.set_status_message(
            f"{old_path} was moved and is no longer being tracked.",
            is_error=True,
        )
        self.window.append_activity(f"folder moved -> {old_path} to {new_path}")
        self._show_moved_folder_notification(old_path)

    def _handle_monitored_directory_deleted(self, path: str) -> None:
        """Refresh the UI when a watched folder is deleted and can no longer be read."""

        if not any(
            directory.source_directory == path
            for directory in self.config.monitored_directories
        ):
            return

        self._cancel_uploads_for_directory(path)
        self._remove_failed_uploads_for_directory(path)
        self._sync_from_config(show_status=False)
        self.window.set_status_message(
            f"{path} is no longer available and can no longer be tracked.",
            is_error=True,
        )
        self.window.append_activity(f"folder deleted -> {path}")
        self._show_moved_folder_notification(path)

    def _handle_upload_started(self, local_path: str, logical_path: str) -> None:
        """Surface the start of an iRODS upload in the tray window."""

        job = self._queued_uploads.get(local_path)
        if job is not None:
            job.status = "uploading"
            job.logical_path = logical_path
            job.started_at = time.monotonic()
            try:
                job.size_bytes = Path(local_path).stat().st_size
            except OSError:
                pass  # The upload worker reports the missing file separately.

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

        job = self._queued_uploads.get(local_path)
        if job is not None:
            job.size_bytes = max(0, total_bytes)
            job.is_parallel = (
                job.size_bytes > self.PARALLEL_TRANSFER_THRESHOLD
                and job.parallel_upload_threads > 1
            )
            if job.is_parallel:
                job.chunk_size_mib = self._parallel_chunk_mib
            previous_bytes = job.bytes_sent
            job.bytes_sent = min(job.size_bytes, max(0, bytes_sent))
            if job.bytes_sent > previous_bytes:
                self._throughput_history.record_progress(job.bytes_sent - previous_bytes)
                self._note_transfer_activity()
        self._update_upload_summary()
        if total_bytes <= 0:
            self.window.set_status_message(f"Uploading {Path(local_path).name}...")
            return

        percent_complete = int((bytes_sent / total_bytes) * 100)
        self.window.set_status_message(
            f"Uploading {Path(local_path).name}: {percent_complete}%"
        )

    def _handle_upload_paths_resolved(self, local_path: str, logical_path: str) -> None:
        """Record the final paths used for the imminent iRODS put operation."""

        job = self._queued_uploads.get(local_path)
        if job is not None:
            job.logical_path = logical_path

        self.window.append_activity(
            f"iRODS put paths -> local={local_path} logical={logical_path}"
        )

    def _handle_upload_finished(self, local_path: str, logical_path: str) -> None:
        """Clear queue tracking and log successful background uploads."""

        job = self._queued_uploads.pop(local_path, None)
        if job is not None:
            self._stop_retry_timer(job)
            job.status = "finished"
            job.logical_path = logical_path

        self.window.set_status_message(f"Uploaded {Path(local_path).name} to iRODS.")
        self.window.append_activity(f"uploaded -> {local_path} to {logical_path}")
        self._release_upload_worker(local_path)
        finished_at = time.monotonic()
        if job is not None:
            self._record_completed_transfer(job, finished_at)
            chunk_probe_was_active = self._chunks_probing()
            self._record_chunk_success(job, finished_at)
        else:
            chunk_probe_was_active = False
        if (
            job is not None and self._auto_workers is not None
            and not chunk_probe_was_active and not self._chunks_probing()
            and self._pending_parallel_chunk_mib is None
        ):
            changed = self._auto_workers.record_success(
                job.size_bytes, job.started_at, finished_at,
                backlogged=self._has_pending_uploads(),
            )
            self._apply_auto_worker_change(changed)
        self._drain_upload_queue()

    def _handle_upload_warning(self, local_path: str, message: str) -> None:
        """Surface post-upload cleanup problems without marking the transfer failed."""

        self.window.set_status_message(message, is_error=True)
        self.window.append_activity(f"upload warning: {local_path} ({message})")

    def _handle_upload_failed(self, local_path: str, message: str) -> None:
        """Retry failed uploads later until the configured attempt limit is reached."""

        self._release_upload_worker(local_path)
        job = self._queued_uploads.get(local_path)
        if job is not None:
            chunk_probe_was_active = self._chunks_probing()
            self._record_chunk_failure(job, message)
        else:
            chunk_probe_was_active = False
        if (
            job is not None and self._auto_workers is not None
            and not chunk_probe_was_active and not self._chunks_probing()
            and self._pending_parallel_chunk_mib is None
        ):
            changed = self._auto_workers.record_failure(
                local_path, message, time.monotonic(), backlogged=self._has_pending_uploads()
            )
            self._apply_auto_worker_change(changed)
        if job is not None:
            job.last_error = message
            if not self.config.is_monitoring_active:
                self._queued_uploads.pop(local_path, None)
                self._stop_retry_timer(job)
                job.status = "failed"
                self._move_to_failed_uploads(job)
                self.window.set_status_message(
                    f"Monitoring paused before retrying {Path(local_path).name}.",
                    is_error=True,
                )
                self.window.append_activity(
                    f"upload failed while paused -> {local_path} ({message})"
                )
                self._drain_upload_queue()
                return
            if job.attempt_number < job.max_attempts:
                self.window.append_activity(f"upload failed: {local_path} ({message})")
                self._schedule_upload_retry(job)
                self._drain_upload_queue()
                return

            self._queued_uploads.pop(local_path, None)
            self._stop_retry_timer(job)
            job.status = "failed"
            self._move_to_failed_uploads(job)

        self.window.set_status_message(message, is_error=True)
        self.window.append_activity(f"upload failed: {local_path} ({message})")
        self._drain_upload_queue()

    def _handle_upload_cancelled(self, local_path: str, message: str) -> None:
        """Drop queued uploads cleanly once a monitored folder becomes unavailable."""

        job = self._queued_uploads.pop(local_path, None)
        if job is not None:
            self._stop_retry_timer(job)
            job.status = "cancelled"
            job.last_error = message

        self.window.append_activity(f"upload cancelled: {local_path} ({message})")
        self._release_upload_worker(local_path)
        self._drain_upload_queue()

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

        normalized_directory = str(Path(directory).expanduser().resolve(strict=False))
        for slot in self._upload_workers:
            slot.worker.cancel_directory_uploads(normalized_directory)

        for local_path, job in list(self._queued_uploads.items()):
            if job.monitored_root != normalized_directory or job.status in {"uploading", "dispatched"}:
                continue
            self._stop_retry_timer(job)
            self._queued_uploads.pop(local_path, None)

        self.window.append_activity(
            f"cancelling queued uploads for unavailable folder -> {normalized_directory}"
        )

    def _schedule_upload_retry(self, job: UploadJob) -> None:
        """Retry a failed upload later without blocking the shared upload thread."""

        retry_config = normalize_retry_config(self.config.retry)
        self._stop_retry_timer(job)
        delay_seconds = max(
            0.0,
            retry_config.first_delay_in_seconds
            * (retry_config.backoff_multiplier ** max(0, job.attempt_number - 1)),
        )
        next_attempt_number = job.attempt_number + 1
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda local_path=job.local_path: self._retry_upload_job(local_path))
        job.retry_timer = timer
        job.status = "retry_wait"

        delay_label = self._format_retry_delay(delay_seconds)
        self.window.set_status_message(
            f"Retrying {Path(job.local_path).name} in {delay_label}.",
            is_error=True,
        )
        self.window.append_activity(
            f"retry scheduled -> {job.local_path} (attempt {next_attempt_number}/{job.max_attempts} in {delay_label})"
        )
        timer.start(max(0, int(delay_seconds * 1000)))

    def _retry_upload_job(self, local_path: str) -> None:
        """Dispatch the next queued upload attempt once its retry timer fires."""

        job = self._queued_uploads.get(local_path)
        if job is None:
            return

        self._stop_retry_timer(job)
        if not self.config.is_monitoring_active:
            self._queued_uploads.pop(local_path, None)
            job.status = "failed"
            if not job.last_error:
                job.last_error = "Monitoring was paused before retry started."
            self._move_to_failed_uploads(job)
            self.window.set_status_message(
                f"Monitoring paused before retrying {Path(local_path).name}.",
                is_error=True,
            )
            self.window.append_activity(
                f"retry cancelled while paused -> {job.local_path}"
            )
            return
        if not self._is_authenticated:
            self._queued_uploads.pop(local_path, None)
            job.status = "cancelled"
            job.last_error = "User signed out before retry started."
            return

        if not self._refresh_upload_job_destination(job):
            self._queued_uploads.pop(local_path, None)
            job.status = "failed"
            self._move_to_failed_uploads(job)
            return

        job.attempt_number += 1
        self.window.append_activity(
            f"retrying upload -> {job.local_path} (attempt {job.attempt_number}/{job.max_attempts})"
        )
        self._dispatch_upload_job(job)

    def _refresh_upload_job_destination(self, job: UploadJob) -> bool:
        """Refresh a retrying upload job from the latest monitored-folder settings."""

        monitored_directory = self._match_monitored_directory(job.local_path)
        if monitored_directory is None:
            message = (
                f"{Path(job.local_path).name} is no longer inside a monitored folder."
            )
            job.last_error = message
            self.window.set_status_message(message, is_error=True)
            self.window.append_activity(f"warning: {message}")
            return False

        if not monitored_directory.target_collection:
            message = (
                f"No target collection configured for {monitored_directory.source_directory}."
            )
            job.last_error = message
            self.window.set_status_message(message, is_error=True)
            self.window.append_activity(f"warning: {message}")
            return False

        job.monitored_root = monitored_directory.source_directory
        job.target_collection = monitored_directory.target_collection
        return True

    def _move_to_failed_uploads(self, job: UploadJob) -> None:
        """Store a terminally failed upload separately from active queued jobs."""

        self._stop_retry_timer(job)
        self._failed_uploads[job.local_path] = job
        self._refresh_failed_uploads()

    def _remove_failed_upload_for_missing_path(self, path: str) -> None:
        """Drop a failed upload entry once its source file no longer exists there."""

        normalized_path = str(Path(path).expanduser().resolve(strict=False))
        removed_job = self._failed_uploads.pop(normalized_path, None)
        if removed_job is None:
            return

        self._refresh_failed_uploads()
        self.window.append_activity(
            f"failed upload removed -> {normalized_path} (source file no longer available)"
        )

    def _remove_failed_uploads_for_directory(self, directory: str) -> None:
        """Drop failed uploads that belong to a monitored folder no longer at that path."""

        normalized_directory = str(Path(directory).expanduser().resolve(strict=False))
        removed_paths = [
            local_path
            for local_path, job in self._failed_uploads.items()
            if job.monitored_root == normalized_directory
        ]
        if not removed_paths:
            return

        for local_path in removed_paths:
            self._failed_uploads.pop(local_path, None)

        self._refresh_failed_uploads()
        self.window.append_activity(
            f"cleared {len(removed_paths)} failed upload entr{'y' if len(removed_paths) == 1 else 'ies'} for unavailable folder -> {normalized_directory}"
        )

    def _pause_pending_retries(self) -> None:
        """Move retry-wait uploads back to the failed list when monitoring is paused."""

        paused_jobs = [
            job
            for job in list(self._queued_uploads.values())
            if job.status == "retry_wait"
        ]
        if not paused_jobs:
            return

        for job in paused_jobs:
            self._queued_uploads.pop(job.local_path, None)
            self._stop_retry_timer(job)
            job.status = "failed"
            if not job.last_error:
                job.last_error = "Monitoring was paused before retry started."
            self._failed_uploads[job.local_path] = job

        self._refresh_failed_uploads()
        self.window.append_activity(
            f"paused {len(paused_jobs)} pending upload retr{'y' if len(paused_jobs) == 1 else 'ies'}"
        )

    def _stop_retry_timer(self, job: UploadJob) -> None:
        """Dispose of any pending retry timer attached to an upload job."""

        if job.retry_timer is None:
            return
        job.retry_timer.stop()
        job.retry_timer.deleteLater()
        job.retry_timer = None

    def _clear_upload_jobs(self) -> None:
        """Drop controller-side upload jobs and stop any pending retry timers."""

        for job in self._queued_uploads.values():
            self._stop_retry_timer(job)
        for job in self._failed_uploads.values():
            self._stop_retry_timer(job)
        self._queued_uploads.clear()
        self._failed_uploads.clear()
        self._active_uploads.clear()
        self._recent_transfers.clear()
        self._recent_throughput = None
        self._last_transfer_activity_at = None
        self._throughput_idle_timer.stop()
        if self._auto_workers is not None:
            self._auto_workers.reset_observations()
        if self._chunk_tuners is not None:
            for tuner in self._chunk_tuners.values():
                tuner.reset()
        self._refresh_failed_uploads()
        self._update_upload_summary()

    def _refresh_failed_uploads(self) -> None:
        """Push the latest failed-upload snapshot into the Overview tab."""

        failed_upload_rows = [
            (job.local_path, job.last_error, job.attempt_number, job.max_attempts)
            for job in reversed(list(self._failed_uploads.values()))
        ]
        self.window.set_failed_uploads(failed_upload_rows)

    def _format_retry_delay(self, delay_seconds: float) -> str:
        """Return a compact delay label for retry-related UI messages."""

        if delay_seconds.is_integer():
            return f"{int(delay_seconds)}s"
        return f"{delay_seconds:.1f}s"
    
    def _should_upload_file(self, path: str, directory: MonitoredDirectory) -> bool:
        """Apply the optional per-folder regex filter and decide whether to upload."""

        regex_filter = directory.regex_filter
        if regex_filter.mode == "disabled":
            return True

        patterns = [re.compile(pattern) for pattern in regex_filter.patterns]

        candidate_path = Path(path).expanduser().resolve(strict=False)
        monitored_root = Path(directory.source_directory).expanduser().resolve(strict=False)
        match_candidates = {
            str(candidate_path),
            candidate_path.as_posix(),
            candidate_path.name,
        }
        try:
            relative_path = candidate_path.relative_to(monitored_root)
        except ValueError:
            relative_path = Path(candidate_path.name)
        match_candidates.add(str(relative_path))
        match_candidates.add(relative_path.as_posix())

        matched = any(
            pattern.search(candidate)
            for pattern in patterns
            for candidate in match_candidates
        )
        if regex_filter.mode == "allow":
            return matched
        return not matched

    def _find_post_upload_destination_conflict(
        self,
        source_directory: str,
        post_upload_action: str,
        post_upload_destination: str,
    ) -> str | None:
        """Return the monitored root that would re-trigger ingestion for moved files."""

        if post_upload_action != "move" or not post_upload_destination:
            return None

        destination_path = Path(post_upload_destination).expanduser().resolve(strict=False)
        monitored_roots = [
            directory.source_directory for directory in self.config.monitored_directories
        ]
        monitored_roots.append(source_directory)

        for monitored_root in monitored_roots:
            monitored_root_path = Path(monitored_root).expanduser().resolve(strict=False)
            try:
                destination_path.relative_to(monitored_root_path)
            except ValueError:
                continue
            return monitored_root

        return None

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
            self.window.append_activity(
                f"warning: failed to show folder notification ({exc})"
            )
