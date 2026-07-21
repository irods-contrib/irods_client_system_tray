"""Settings window widgets and presentation for the tray-based monitor app."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import IRODSEnvironment, MonitoredDirectory, normalize_irods_zone_name


def _set_label_error_state(label: QLabel, is_error: bool) -> None:
    """Toggle QSS 'error' property so theme.qss.template can recolor the label."""

    label.setProperty("error", is_error)
    label.style().unpolish(label)
    label.style().polish(label)


class ZoneRootLineEdit(QLineEdit):
    """Keep an iRODS collection input anchored beneath an uneditable zone prefix."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prefix = "/"
        self.textEdited.connect(self._enforce_prefix)
        self.cursorPositionChanged.connect(self._enforce_cursor)

    def set_zone_name(self, zone_name: str) -> None:
        """Update the locked zone prefix while preserving the editable suffix."""

        normalized_zone = normalize_irods_zone_name(zone_name) or "tempZone"
        current_suffix = self._extract_suffix(self.text())
        self._prefix = f"/{normalized_zone}/"
        self.blockSignals(True)
        self.setText(self._prefix + current_suffix)
        self.blockSignals(False)
        self.setCursorPosition(len(self.text()))

    def collection_path(self) -> str:
        """Return the full target collection path including the locked prefix."""

        return self.text().strip()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Prevent destructive edits that would move or remove the zone prefix."""

        cursor_position = self.cursorPosition()
        prefix_length = len(self._prefix)
        if event.key() == Qt.Key.Key_Backspace and cursor_position <= prefix_length:
            return
        if event.key() == Qt.Key.Key_Delete and cursor_position < prefix_length:
            return
        if event.key() == Qt.Key.Key_Left and cursor_position <= prefix_length:
            return
        if event.key() == Qt.Key.Key_Home:
            self.setCursorPosition(prefix_length)
            return

        super().keyPressEvent(event)
        self._normalize_after_edit()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Keep the caret from landing inside the locked zone prefix."""

        super().mousePressEvent(event)
        self._enforce_cursor()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """Keep selection anchors from ending inside the locked zone prefix."""

        super().mouseReleaseEvent(event)
        self._enforce_cursor()

    def _extract_suffix(self, text: str) -> str:
        """Return the editable suffix portion of the current collection path."""

        if text.startswith(self._prefix):
            suffix = text[len(self._prefix) :]
        else:
            suffix = text.strip()
            if suffix.startswith("/"):
                suffix = suffix[1:]
        return suffix

    def _enforce_prefix(self, _text: str) -> None:
        """Restore the required zone prefix after direct text edits or paste actions."""

        self._normalize_after_edit()

    def _normalize_after_edit(self) -> None:
        """Rewrite the control value into prefix-plus-suffix form."""

        normalized_text = self._prefix + self._extract_suffix(self.text())
        if normalized_text == self.text():
            self._enforce_cursor()
            return

        self.blockSignals(True)
        self.setText(normalized_text)
        self.blockSignals(False)
        self._enforce_cursor()

    def _enforce_cursor(self, *_args) -> None:
        """Clamp the caret to the first editable character after the prefix."""

        prefix_length = len(self._prefix)
        if self.cursorPosition() < prefix_length:
            self.setCursorPosition(prefix_length)


class AddDirectoryDialog(QDialog):
    """Collect the local folder path and destination collection for a new watch."""

    def __init__(self, zone_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Monitored Folder")
        self.resize(560, 220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        form_layout = QFormLayout()
        form_layout.setSpacing(10)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.source_directory_input = QLineEdit()
        self.source_directory_input.setPlaceholderText("Choose a folder to monitor")
        browse_button = QPushButton("Browse")
        browse_button.setProperty("variant", "ghost")
        browse_button.clicked.connect(self._choose_source_directory)

        source_layout = QHBoxLayout()
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.addWidget(self.source_directory_input, 1)
        source_layout.addWidget(browse_button)
        source_widget = QWidget()
        source_widget.setLayout(source_layout)

        self.target_collection_input = ZoneRootLineEdit()
        self.target_collection_input.set_zone_name(zone_name)
        self.target_collection_input.setPlaceholderText("home/alice/collection")
        self.recursive_checkbox = QCheckBox("Monitor subfolders recursively")
        self.recursive_checkbox.setChecked(True)

        form_layout.addRow("Source directory", source_widget)
        form_layout.addRow("Target collection", self.target_collection_input)
        form_layout.addRow("Recursive", self.recursive_checkbox)

        self.validation_label = QLabel()
        self.validation_label.setObjectName("validationLabel")
        self.validation_label.setWordWrap(True)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setProperty(
            "variant", "pill"
        )
        self.button_box.accepted.connect(self._accept_if_valid)
        self.button_box.rejected.connect(self.reject)

        layout.addLayout(form_layout)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.button_box)

    def get_directory(self) -> MonitoredDirectory:
        """Return the user-entered folder mapping from the dialog form."""

        return MonitoredDirectory(
            source_directory=self.source_directory_input.text().strip(),
            target_collection=self.target_collection_input.collection_path(),
            recursive=self.recursive_checkbox.isChecked(),
        )

    def _choose_source_directory(self) -> None:
        """Open a native picker and populate the source directory field."""

        starting_directory = self.source_directory_input.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select folder to monitor",
            starting_directory,
        )
        if selected:
            self.source_directory_input.setText(selected)
            self.validation_label.clear()

    def _accept_if_valid(self) -> None:
        """Require both folder attributes before closing with acceptance."""

        if not self.source_directory_input.text().strip():
            self.validation_label.setText("Select a source directory to monitor.")
            return
        self.validation_label.clear()
        self.accept()


class SettingsWindow(QWidget):
    """Provide the configuration window for monitored folders and recent activity.

    The window emits high-level signals instead of directly changing application state,
    which keeps the UI focused on presentation while the tray controller performs the
    actual persistence and monitor updates.
    """

    monitoring_toggled = Signal(bool)
    add_folder_requested = Signal(str, str, bool)
    remove_folder_requested = Signal(str)
    save_irods_requested = Signal()

    def __init__(self) -> None:
        """Construct the minimalist settings UI used by the tray application."""

        super().__init__()
        self.setObjectName("settingsWindow")
        self.setWindowTitle("Ingestion Monitor")
        self.resize(640, 460)
        self._irods_zone_for_new_folders = "tempZone"

        self.title_label = QLabel("Directory Ingestion")
        self.title_label.setObjectName("settingsTitleLabel")

        self.subtitle_label = QLabel("Monitor folders in the background from the system tray.")
        self.subtitle_label.setObjectName("settingsSubtitleLabel")

        self.monitor_toggle = QCheckBox("Background monitoring enabled")
        self.monitor_toggle.toggled.connect(self.monitoring_toggled)
        self.monitor_toggle.setObjectName("monitorToggleCheckbox")

        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("settingsStatusLabel")

        irods_card = QFrame()
        irods_card.setFrameShape(QFrame.Shape.StyledPanel)
        irods_card.setObjectName("irodsCard")

        irods_layout = QVBoxLayout(irods_card)
        irods_layout.setContentsMargins(16, 16, 16, 16)
        irods_layout.setSpacing(12)

        irods_title = QLabel("iRODS session")
        irods_title.setObjectName("irodsCardTitle")

        form_layout = QFormLayout()
        form_layout.setSpacing(10)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.irods_host_input = QLineEdit()
        self.irods_port_input = QSpinBox()
        self.irods_port_input.setRange(1, 65535)
        self.irods_user_name_input = QLineEdit()
        self.irods_password_input = QLineEdit()
        self.irods_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.irods_zone_name_input = QLineEdit()
        form_layout.addRow("Host", self.irods_host_input)
        form_layout.addRow("Port", self.irods_port_input)
        form_layout.addRow("User", self.irods_user_name_input)
        form_layout.addRow("Password", self.irods_password_input)
        form_layout.addRow("Zone", self.irods_zone_name_input)

        irods_button_row = QHBoxLayout()
        irods_button_row.setSpacing(10)
        self.save_irods_button = QPushButton("Save iRODS Settings")
        self.save_irods_button.clicked.connect(self._emit_save_irods_requested)
        irods_button_row.addWidget(self.save_irods_button)
        irods_button_row.addStretch(1)

        irods_layout.addWidget(irods_title)
        irods_layout.addLayout(form_layout)
        irods_layout.addLayout(irods_button_row)

        directory_card = QFrame()
        directory_card.setFrameShape(QFrame.Shape.StyledPanel)
        directory_card.setObjectName("directoryCard")

        directory_layout = QVBoxLayout(directory_card)
        directory_layout.setContentsMargins(16, 16, 16, 16)
        directory_layout.setSpacing(12)

        directory_title = QLabel("Monitored folders")
        directory_title.setObjectName("directoryCardTitle")

        self.directory_list = QListWidget()
        self.directory_list.currentItemChanged.connect(self._update_remove_button_state)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)

        self.add_button = QPushButton("Add Folder")
        self.add_button.clicked.connect(self._emit_add_requested)
        self.remove_button = QPushButton("Remove Folder")
        self.remove_button.setProperty("variant", "pill")
        self.remove_button.clicked.connect(self._emit_remove_selected)
        self.remove_button.setEnabled(False)

        button_row.addWidget(self.add_button)
        button_row.addWidget(self.remove_button)
        button_row.addStretch(1)

        directory_layout.addWidget(directory_title)
        directory_layout.addWidget(self.directory_list)
        directory_layout.addLayout(button_row)

        activity_title = QLabel("Recent activity")
        activity_title.setObjectName("activityTitle")

        self.activity_list = QListWidget()
        self.activity_list.setMaximumHeight(140)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)
        layout.addWidget(self.monitor_toggle)
        layout.addWidget(self.status_label)
        layout.addWidget(irods_card)
        layout.addWidget(directory_card, 1)
        layout.addWidget(activity_title)
        layout.addWidget(self.activity_list)

    def set_monitoring_active(self, is_active: bool) -> None:
        """Update the checkbox state without re-emitting the user-facing toggle signal."""

        previous = self.monitor_toggle.blockSignals(True)
        self.monitor_toggle.setChecked(is_active)
        self.monitor_toggle.blockSignals(previous)

    def set_directories(
        self,
        directories: list[MonitoredDirectory],
        invalid_directories: set[str],
    ) -> None:
        """Refresh the folder list and visually flag directories that no longer exist."""

        self.directory_list.clear()
        for directory in directories:
            target_label = directory.target_collection or "(target collection required)"
            recursive_label = "recursive" if directory.recursive else "top-level only"
            label = f"{directory.source_directory} -> {target_label} ({recursive_label})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, directory.source_directory)
            item.setToolTip(
                f"Source directory: {directory.source_directory}\n"
                f"Target collection: {target_label}\n"
                f"Recursive monitoring: {'On' if directory.recursive else 'Off'}"
            )
            if directory.source_directory in invalid_directories:
                item.setForeground(QColor("#b42318"))
                item.setToolTip(
                    "Directory does not currently exist and is not being watched.\n"
                    f"Target collection: {target_label}\n"
                    f"Recursive monitoring: {'On' if directory.recursive else 'Off'}"
                )
            self.directory_list.addItem(item)
        self._update_remove_button_state()

    def set_irods_environment(self, environment: IRODSEnvironment) -> None:
        """Populate the iRODS settings form from persisted configuration."""

        self.irods_host_input.setText(environment.irods_host)
        self.irods_port_input.setValue(environment.irods_port)
        self.irods_user_name_input.setText(environment.irods_user_name)
        self.irods_password_input.setText(environment.irods_password)
        self.irods_zone_name_input.setText(environment.irods_zone_name)
        self._irods_zone_for_new_folders = environment.irods_zone_name

    def get_irods_environment(self) -> IRODSEnvironment:
        """Collect the current form values into the config dataclass."""

        return IRODSEnvironment(
            irods_host=self.irods_host_input.text().strip(),
            irods_port=self.irods_port_input.value(),
            irods_user_name=self.irods_user_name_input.text().strip(),
            irods_password=self.irods_password_input.text(),
            irods_zone_name=self.irods_zone_name_input.text().strip(),
        )

    def set_status_message(self, message: str, *, is_error: bool = False) -> None:
        """Show a normal or error status message near the top of the window."""

        self.status_label.setText(message)
        _set_label_error_state(self.status_label, is_error)

    def append_activity(self, message: str) -> None:
        """Prepend a new activity message and keep only a short rolling history."""

        self.activity_list.insertItem(0, message)
        while self.activity_list.count() > 50:
            self.activity_list.takeItem(self.activity_list.count() - 1)

    def closeEvent(self, event) -> None:  # noqa: N802
        """Hide the window instead of quitting so tray monitoring keeps running."""

        event.ignore()
        self.hide()

    def _emit_add_requested(self, _checked: bool = False) -> None:
        """Translate the add button click into a controller-facing signal."""

        dialog = AddDirectoryDialog(self._irods_zone_for_new_folders, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        directory = dialog.get_directory()
        self.add_folder_requested.emit(
            directory.source_directory,
            directory.target_collection,
            directory.recursive,
        )

    def _emit_remove_selected(self, _checked: bool = False) -> None:
        """Emit the currently selected directory so the controller can remove it."""

        item = self.directory_list.currentItem()
        if item is None:
            return
        directory = item.data(Qt.ItemDataRole.UserRole)
        self.remove_folder_requested.emit(directory)

    def _emit_save_irods_requested(self, _checked: bool = False) -> None:
        """Notify the controller that the user wants to persist iRODS settings."""

        self.save_irods_requested.emit()

    def _update_remove_button_state(self, *_args) -> None:
        """Enable removal only when the user has a directory selected in the list."""

        self.remove_button.setEnabled(self.directory_list.currentItem() is not None)
