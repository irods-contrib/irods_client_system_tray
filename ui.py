"""Settings window widgets and presentation for the tray-based monitor app."""

from __future__ import annotations

import logging
import re
import socket
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config import (
    DEFAULT_POST_UPLOAD_ACTION,
    IRODSEnvironment,
    MonitoredDirectory,
    RetryConfig,
    RegexFilterConfig,
    normalize_directory,
    normalize_irods_zone_name,
)


POST_UPLOAD_ACTION_OPTIONS = (
    ("Recycle local files after upload", "recycle"),
    ("Keep local files", "keep"),
    ("Move local files after upload", "move"),
    ("Delete local files permanently after upload", "delete"),
)

logger = logging.getLogger(__name__)

def _set_label_error_state(label: QLabel, is_error: bool) -> None:
    """Toggle QSS 'error' property so theme.qss.template can recolor the label."""

    label.setProperty("error", is_error)
    label.style().unpolish(label)
    label.style().polish(label)
    

class LoginWorker(QObject):
    """Authenticate against iRODS on a background thread."""

    authentication_finished = Signal(bool, object)
    finished = Signal()
    CONNECTION_PRECHECK_TIMEOUT_SECONDS = 5.0
    HEARTBEAT_MESSAGE = (
        b"\x00\x00\x00\x33<MsgHeader_PI><type>HEARTBEAT</type></MsgHeader_PI>"
    )
    HEARTBEAT_RESPONSE = b"HEARTBEAT"
    HEARTBEAT_RESPONSE_BYTES = 256

    def __init__(self, environment: IRODSEnvironment) -> None:
        super().__init__()
        self._environment = environment

    @Slot()
    def authenticate(self) -> None:
        """Attempt a real iRODS login without blocking the Qt UI thread."""

        try:
            from irods.session import iRODSSession
        except ImportError as exc:  # pragma: no cover - environment dependent
            self.authentication_finished.emit(
                False,
                RuntimeError(
                    "python-irodsclient is not installed. Install it to enable iRODS login."
                ),
            )
            self.finished.emit()
            return

        try:
            self._probe_server_heartbeat()
        except (OSError, RuntimeError) as exc:
            self.authentication_finished.emit(False, exc)
            self.finished.emit()
            return

        try:
            with iRODSSession(
                host=self._environment.irods_host,
                port=self._environment.irods_port,
                user=self._environment.irods_user_name,
                password=self._environment.irods_password,
                zone=self._environment.irods_zone_name,
            ) as session:
                session.users.get(
                    self._environment.irods_user_name,
                    self._environment.irods_zone_name,
                )
        except Exception as exc:  # noqa: BLE001
            self.authentication_finished.emit(False, exc)
        else:
            self.authentication_finished.emit(True, None)
        finally:
            self.finished.emit()

    def _probe_server_heartbeat(self) -> None:
        """Fail fast unless the configured endpoint responds like an iRODS server."""

        address = (self._environment.irods_host, self._environment.irods_port)
        try:
            with socket.create_connection(
                address,
                timeout=self.CONNECTION_PRECHECK_TIMEOUT_SECONDS,
            ) as connection:
                connection.settimeout(self.CONNECTION_PRECHECK_TIMEOUT_SECONDS)
                connection.sendall(self.HEARTBEAT_MESSAGE)
                response = connection.recv(self.HEARTBEAT_RESPONSE_BYTES)
        except OSError as exc:
            raise OSError(
                "Could not reach iRODS server at "
                f"{self._environment.irods_host}:{self._environment.irods_port}"
            ) from exc

        if response != self.HEARTBEAT_RESPONSE:
            raise RuntimeError(
                "Heartbeat probe failed for iRODS server at "
                f"{self._environment.irods_host}:{self._environment.irods_port}"
            )


class LoginDialog(QDialog):
    """Gate access to the application until live iRODS authentication succeeds."""

    def __init__(self, environment: IRODSEnvironment) -> None:
        super().__init__()
        self._environment = environment
        self.authenticated_environment: IRODSEnvironment | None = None
        self._auth_thread: QThread | None = None
        self._auth_worker: LoginWorker | None = None
        self._pending_login: tuple[str, int, str, str, str] | None = None
        self._auth_result: tuple[bool, object] | None = None
        self._last_login_error_details: str | None = None
        self._default_dialog_width = 420
        self._default_dialog_height = 320

        self.setWindowTitle("iRODS Login")
        self.setModal(True)
        self.resize(self._default_dialog_width, self._default_dialog_height)

        title_label = QLabel("Sign in to iRODS")
        title_label.setStyleSheet("font-size: 22px; font-weight: 600;")

        subtitle_label = QLabel(
            "Enter your iRODS connection details and credentials before accessing "
            "the ingestion monitor."
        )
        subtitle_label.setWordWrap(True)
        subtitle_label.setStyleSheet("color: #667085;")

        form_layout = QFormLayout()
        form_layout.setSpacing(10)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("Enter iRODS host")
        self.host_input.setText(environment.irods_host)
        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText("Enter iRODS port")
        self.port_input.setText(str(environment.irods_port))
        self.zone_name_input = QLineEdit()
        self.zone_name_input.setPlaceholderText("Enter iRODS zone")
        self.zone_name_input.setText(environment.irods_zone_name)
        self.user_name_input = QLineEdit()
        self.user_name_input.setPlaceholderText("Enter iRODS username")
        self.user_name_input.setText(environment.irods_user_name)
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Enter iRODS password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self._attempt_login)

        form_layout.addRow("Host", self.host_input)
        form_layout.addRow("Port", self.port_input)
        form_layout.addRow("Zone", self.zone_name_input)
        form_layout.addRow("Username", self.user_name_input)
        form_layout.addRow("Password", self.password_input)

        self.status_label = QLabel("Enter your iRODS connection details to continue.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #344054;")

        self.error_details_link = QLabel(
            '<a href="toggle" style="color: #667085; text-decoration: underline;">Show details</a>'
        )
        self.error_details_link.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        self.error_details_link.setOpenExternalLinks(False)
        self.error_details_link.linkActivated.connect(self._toggle_login_error_details)
        self.error_details_link.hide()

        self.error_details_view = QPlainTextEdit()
        self.error_details_view.setReadOnly(True)
        self.error_details_view.setMaximumHeight(85)
        self.error_details_view.hide()

        button_row = QHBoxLayout()
        button_row.addStretch(1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.sign_in_button = QPushButton("Sign In")
        self.sign_in_button.clicked.connect(self._attempt_login)

        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.sign_in_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        layout.addLayout(form_layout)
        layout.addWidget(self.status_label)
        layout.addWidget(self.error_details_link)
        layout.addWidget(self.error_details_view)
        layout.addLayout(button_row)

        self.setStyleSheet(
            "QWidget { background: #f8fafc; color: #101828; }"
            "QLineEdit { border: 1px solid #d0d5dd; border-radius: 10px; padding: 10px; background: white; }"
            "QPlainTextEdit { border: 1px solid #d0d5dd; border-radius: 10px; padding: 10px; background: white; }"
            "QLabel { color: #101828; }"
            "QLabel[role='details-link'] { color: #667085; }"
            "QPushButton { background: #101828; color: white; border-radius: 10px; padding: 10px 14px; }"
        )
        self.error_details_link.setProperty("role", "details-link")
        self.style().unpolish(self.error_details_link)
        self.style().polish(self.error_details_link)

    def _attempt_login(self) -> None:
        """Allow access only when the entered credentials authenticate with iRODS."""

        self._set_login_error_details(None)
        entered_host = self.host_input.text().strip()
        entered_port_text = self.port_input.text().strip()
        entered_zone = self.zone_name_input.text().strip()
        entered_user = self.user_name_input.text().strip()
        entered_password = self.password_input.text()

        if (
            not entered_host
            or not entered_port_text
            or not entered_zone
            or not entered_user
            or not entered_password
        ):
            self._set_status_message(
                "Enter host, port, zone, username, and password.",
                is_error=True,
            )
            return

        try:
            entered_port = int(entered_port_text)
        except ValueError:
            self._set_status_message("Enter a valid numeric port.", is_error=True)
            return

        if entered_port < 1 or entered_port > 65535:
            self._set_status_message("Port must be between 1 and 65535.", is_error=True)
            return

        self.sign_in_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self._set_status_message("Signing in to iRODS...")
        self._pending_login = (
            entered_host,
            entered_port,
            entered_zone,
            entered_user,
            entered_password,
        )
        self._start_authentication(
            IRODSEnvironment(
                irods_host=entered_host,
                irods_port=entered_port,
                irods_user_name=entered_user,
                irods_password=entered_password,
                irods_zone_name=entered_zone,
            )
        )

    def _format_login_error(self, exc: Exception) -> str:
        """Convert low-level iRODS errors into stable user-facing feedback."""

        detail_text = self._extract_login_error_details(exc)
        lowered_details = detail_text.lower()
        lowered_type = exc.__class__.__name__.lower()

        if any(
            token in lowered_details or token in lowered_type
            for token in (
                "invalid user",
                "unknown user",
                "user does not exist",
                "cat_invalid_user",
                "invalid authentication",
                "authentication error",
                "password",
                "pam_auth_password",
                "cat_invalid_authentication",
                "auth",
            )
        ):
            return "Sign-in failed. Check the username and password."

        if any(
            token in lowered_details or token in lowered_type
            for token in (
                "could not reach irods server",
                "heartbeat",
                "connection refused",
                "timed out",
                "timeout",
                "temporary failure in name resolution",
                "name or service not known",
                "nodename nor servname provided",
                "failed to resolve",
                "network",
                "ssl",
                "tls",
                "certificate",
            )
        ):
            return "Could not connect. Check the host, port, and zone."

        return f"Sign-in failed: {detail_text}"

    def _extract_login_error_details(self, exc: Exception) -> str:
        """Return raw login error text for logs and on-demand display."""

        rendered = str(exc).strip()
        if rendered:
            if rendered == "None":
                return f"{exc.__class__.__name__}: None"
            return rendered

        details = [str(part).strip() for part in getattr(exc, "args", ()) if str(part).strip()]
        if details:
            detail_text = ": ".join(details)
            if detail_text == "None":
                return f"{exc.__class__.__name__}: None"
            return detail_text
        return exc.__class__.__name__

    def _set_login_error_details(self, details: str | None) -> None:
        """Show or clear the low-level login error affordance."""

        self._last_login_error_details = details.strip() if details and details.strip() else None
        if self._last_login_error_details is None:
            self.error_details_link.hide()
            self.error_details_link.setText(
                '<a href="toggle" style="color: #667085; text-decoration: underline;">Show details</a>'
            )
            self.error_details_view.clear()
            self.error_details_view.hide()
            self._resize_for_login_error_details()
            return

        self.error_details_link.setText(
            '<a href="toggle" style="color: #667085; text-decoration: underline;">Show details</a>'
        )
        self.error_details_view.setPlainText(self._last_login_error_details)
        self.error_details_view.hide()
        self.error_details_link.show()
        self._resize_for_login_error_details()

    def _toggle_login_error_details(self, _link: str) -> None:
        """Expand or collapse the raw login error details panel."""

        is_visible = self.error_details_view.isVisible()
        self.error_details_view.setVisible(not is_visible)
        link_label = "Hide details" if not is_visible else "Show details"
        self.error_details_link.setText(
            f'<a href="toggle" style="color: #667085; text-decoration: underline;">{link_label}</a>'
        )
        self._resize_for_login_error_details()

    def _resize_for_login_error_details(self) -> None:
        """Resize the dialog to fit the current details visibility without crowding the form."""

        layout = self.layout()
        if layout is None:
            return

        layout.activate()
        target_size = self.sizeHint()
        target_width = max(self.width(), self._default_dialog_width, target_size.width())
        if self.error_details_view.isVisible():
            target_height = max(self.height(), target_size.height())
        else:
            target_height = max(self._default_dialog_height, target_size.height())
        self.resize(target_width, target_height)

    def _set_status_message(self, message: str, *, is_error: bool = False) -> None:
        """Render feedback inside the login dialog."""

        color = "#b42318" if is_error else "#344054"
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {color};")

    def reject(self) -> None:
        """Keep the dialog open while a background sign-in attempt is still running."""

        if self._auth_thread is not None:
            self._set_status_message(
                "Wait for the current sign-in attempt to finish.",
                is_error=True,
            )
            return
        super().reject()

    def _start_authentication(self, environment: IRODSEnvironment) -> None:
        """Run iRODS authentication on a worker thread and report the result later."""

        self._set_authentication_in_progress(True)
        self._auth_result = None

        thread = QThread(self)
        worker = LoginWorker(environment)
        worker.moveToThread(thread)

        thread.started.connect(worker.authenticate)
        worker.authentication_finished.connect(self._store_authentication_result)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._finalize_authentication_attempt)

        self._auth_thread = thread
        self._auth_worker = worker
        thread.start()

    def _set_authentication_in_progress(self, is_authenticating: bool) -> None:
        """Disable editing while the dialog is waiting on a live sign-in attempt."""

        self.host_input.setEnabled(not is_authenticating)
        self.port_input.setEnabled(not is_authenticating)
        self.zone_name_input.setEnabled(not is_authenticating)
        self.user_name_input.setEnabled(not is_authenticating)
        self.password_input.setEnabled(not is_authenticating)
        self.sign_in_button.setEnabled(not is_authenticating)
        self.cancel_button.setEnabled(not is_authenticating)

    def _store_authentication_result(self, succeeded: bool, result: object) -> None:
        """Capture the worker outcome until the thread has fully stopped."""

        self._auth_result = (succeeded, result)

    def _finalize_authentication_attempt(self) -> None:
        """Handle the last auth result only after the worker thread has exited cleanly."""

        self._auth_thread = None
        self._auth_worker = None
        self._set_authentication_in_progress(False)

        if self._auth_result is None:
            self._set_login_error_details(None)
            self._set_status_message(
                "Sign-in failed: Authentication thread ended unexpectedly.",
                is_error=True,
            )
            return

        succeeded, result = self._auth_result
        self._auth_result = None

        if not succeeded:
            self._pending_login = None
            self.password_input.clear()
            error = result if isinstance(result, Exception) else RuntimeError("Unknown login failure")
            logger.error(
                "iRODS login failed: %s",
                self._extract_login_error_details(error),
                exc_info=(type(error), error, error.__traceback__),
            )
            self._set_login_error_details(self._extract_login_error_details(error))
            self._set_status_message(self._format_login_error(error), is_error=True)
            return

        if self._pending_login is None:
            self._set_login_error_details(None)
            self._set_status_message("Sign-in failed: Missing login state.", is_error=True)
            return

        host, port, zone_name, user_name, password = self._pending_login
        self._pending_login = None
        self._set_login_error_details(None)
        self.authenticated_environment = IRODSEnvironment(
            irods_host=host,
            irods_port=port,
            irods_user_name=user_name,
            irods_password=password,
            irods_zone_name=zone_name,
        )
        self.accept()


def _describe_post_upload_policy(directory: MonitoredDirectory) -> str:
    """Return a short user-facing summary of the folder cleanup policy."""

    if directory.post_upload_action == "recycle":
        return "send to recycling bin after upload"
    if directory.post_upload_action == "delete":
        return "delete permanently after upload"
    if directory.post_upload_action == "move":
        destination = directory.post_upload_destination or "(destination required)"
        return f"move to {destination}"
    return "keep local files"


def _describe_regex_filter(directory: MonitoredDirectory) -> str:
    """Return a short user-facing summary of the regex filter configuration."""

    if directory.regex_filter.mode == "disabled":
        return "regex filtering off"

    mode_label = "allow" if directory.regex_filter.mode == "allow" else "deny"
    pattern_count = len(directory.regex_filter.patterns)
    pattern_label = "pattern" if pattern_count == 1 else "patterns"
    return f"regex {mode_label}: {pattern_count} {pattern_label}"


def _is_path_within_directory(path: str, directory: str) -> bool:
    """Return whether the candidate path lands inside or equals a watched root."""

    candidate = Path(path).expanduser().resolve(strict=False)
    root = Path(directory).expanduser().resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


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


class RegexEditorDialog(QDialog):
    """Collect and validate one regex pattern per line before saving."""

    def __init__(
        self,
        patterns: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Regex Patterns")
        self.resize(520, 360)
        self.patterns: list[str] = list(patterns or [])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        helper_label = QLabel("One regex pattern per line")
        helper_label.setWordWrap(True)

        self.pattern_input = QPlainTextEdit()
        self.pattern_input.setPlainText("\n".join(self.patterns))

        self.validation_label = QLabel()
        self.validation_label.setObjectName("validationLabel")
        self.validation_label.setWordWrap(True)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setProperty(
            "variant", "pill"
        )
        self.button_box.accepted.connect(self._accept_if_valid)
        self.button_box.rejected.connect(self.reject)

        layout.addWidget(helper_label)
        layout.addWidget(self.pattern_input, 1)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.button_box)

    def _accept_if_valid(self) -> None:
        """Validate each non-empty line as a standalone regex before saving."""

        cleaned_patterns: list[str] = []
        for line_number, raw_line in enumerate(self.pattern_input.toPlainText().splitlines(), start=1):
            pattern = raw_line.strip()
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                self.validation_label.setText(
                    f"Invalid regex on line {line_number}: {exc}"
                )
                return
            cleaned_patterns.append(pattern)

        self.validation_label.clear()
        self.patterns = cleaned_patterns
        self.accept()


class AddDirectoryDialog(QDialog):
    """Collect the local folder path and destination collection for a new watch."""

    def __init__(
        self,
        zone_name: str,
        monitored_directories: list[MonitoredDirectory],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._monitored_roots = [
            normalize_directory(directory.source_directory)
            for directory in monitored_directories
        ]
        self._regex_patterns: list[str] = []
        self.setWindowTitle("Add Monitored Folder")
        self.resize(560, 380)

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

        self.post_upload_action_input = QComboBox()
        for label, value in POST_UPLOAD_ACTION_OPTIONS:
            self.post_upload_action_input.addItem(label, value)
        default_action_index = self.post_upload_action_input.findData(DEFAULT_POST_UPLOAD_ACTION)
        if default_action_index >= 0:
            self.post_upload_action_input.setCurrentIndex(default_action_index)
        self.post_upload_action_input.currentIndexChanged.connect(
            self._update_post_upload_action_state
        )

        self.post_upload_destination_input = QLineEdit()
        self.post_upload_destination_input.setPlaceholderText("Choose a destination folder")
        destination_browse_button = QPushButton("Browse")
        destination_browse_button.setProperty("variant", "ghost")
        destination_browse_button.clicked.connect(self._choose_post_upload_destination)

        destination_layout = QHBoxLayout()
        destination_layout.setContentsMargins(0, 0, 0, 0)
        destination_layout.addWidget(self.post_upload_destination_input, 1)
        destination_layout.addWidget(destination_browse_button)
        self.post_upload_destination_widget = QWidget()
        self.post_upload_destination_widget.setLayout(destination_layout)

        form_layout.addRow("Source directory", source_widget)
        form_layout.addRow("Target collection", self.target_collection_input)
        form_layout.addRow("Recursive", self.recursive_checkbox)
        form_layout.addRow("After upload", self.post_upload_action_input)
        self.post_upload_destination_label = QLabel("Move destination")
        form_layout.addRow(
            self.post_upload_destination_label,
            self.post_upload_destination_widget,
        )

        self.regex_filter_toggle = QCheckBox("Enable Regex Filtering")
        self.regex_filter_toggle.toggled.connect(self._update_regex_filter_state)

        self.regex_deny_radio = QRadioButton("Deny")
        self.regex_allow_radio = QRadioButton("Allow")
        self.regex_deny_radio.setChecked(True)

        regex_mode_layout = QHBoxLayout()
        regex_mode_layout.setContentsMargins(0, 0, 0, 0)
        regex_mode_layout.addWidget(self.regex_deny_radio)
        regex_mode_layout.addWidget(self.regex_allow_radio)
        regex_mode_layout.addStretch(1)
        regex_mode_widget = QWidget()
        regex_mode_widget.setLayout(regex_mode_layout)

        self.regex_summary_label = QLabel()
        self.regex_summary_label.setWordWrap(True)

        self.regex_edit_button = QPushButton("Edit Regex...")
        self.regex_edit_button.setProperty("variant", "ghost")
        self.regex_edit_button.clicked.connect(self._edit_regex_patterns)

        regex_pattern_layout = QHBoxLayout()
        regex_pattern_layout.setContentsMargins(0, 0, 0, 0)
        regex_pattern_layout.addWidget(self.regex_summary_label, 1)
        regex_pattern_layout.addWidget(self.regex_edit_button)
        regex_pattern_widget = QWidget()
        regex_pattern_widget.setLayout(regex_pattern_layout)

        self.regex_filter_details = QWidget()
        regex_layout = QFormLayout(self.regex_filter_details)
        regex_layout.setSpacing(10)
        regex_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        regex_layout.addRow("Mode", regex_mode_widget)
        regex_layout.addRow("Patterns", regex_pattern_widget)

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
        layout.addWidget(self.regex_filter_toggle)
        layout.addWidget(self.regex_filter_details)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.button_box)
        self._update_post_upload_action_state()
        self._update_regex_summary()
        self._update_regex_filter_state()

    def get_directory(self) -> MonitoredDirectory:
        """Return the user-entered folder mapping from the dialog form."""

        return MonitoredDirectory(
            source_directory=self.source_directory_input.text().strip(),
            target_collection=self.target_collection_input.collection_path(),
            recursive=self.recursive_checkbox.isChecked(),
            post_upload_action=self.post_upload_action_input.currentData(),
            post_upload_destination=self.post_upload_destination_input.text().strip(),
            regex_filter=RegexFilterConfig(
                mode=(
                    "disabled"
                    if not self.regex_filter_toggle.isChecked()
                    else "allow" if self.regex_allow_radio.isChecked() else "deny"
                ),
                patterns=list(self._regex_patterns),
            ),
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

    def _choose_post_upload_destination(self) -> None:
        """Open a native picker and populate the move destination field."""

        starting_directory = self.post_upload_destination_input.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select destination for moved files",
            starting_directory,
        )
        if selected:
            self.post_upload_destination_input.setText(selected)
            self.validation_label.clear()

    def _edit_regex_patterns(self) -> None:
        """Open the regex editor dialog and keep only validated pattern lines."""

        dialog = RegexEditorDialog(self._regex_patterns, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self._regex_patterns = list(dialog.patterns)
        self._update_regex_summary()
        self.validation_label.clear()

    def _accept_if_valid(self) -> None:
        """Require both folder attributes before closing with acceptance."""

        source_directory = self.source_directory_input.text().strip()
        if not source_directory:
            self.validation_label.setText("Select a source directory to monitor.")
            return

        if self.post_upload_action_input.currentData() == "move":
            destination = self.post_upload_destination_input.text().strip()
            if not destination:
                self.validation_label.setText(
                    "Select a destination for files moved after upload."
                )
                return

            conflict_root = self._find_conflicting_monitored_root(source_directory, destination)
            if conflict_root is not None:
                self.validation_label.setText(
                    f"Move destination must be outside monitored folders. Conflicts with {conflict_root}."
                )
                return

        if self.regex_filter_toggle.isChecked() and not self._regex_patterns:
            self.validation_label.setText(
                "Add at least one regex pattern or disable regex filtering."
            )
            return

        self.validation_label.clear()
        self.accept()

    def _find_conflicting_monitored_root(
        self,
        source_directory: str,
        destination: str,
    ) -> str | None:
        """Return the monitored root that would re-ingest moved files, if any."""

        normalized_source = normalize_directory(source_directory)
        normalized_destination = normalize_directory(destination)
        for monitored_root in [*self._monitored_roots, normalized_source]:
            if _is_path_within_directory(normalized_destination, monitored_root):
                return monitored_root
        return None

    def _update_post_upload_action_state(self) -> None:
        """Only show the destination picker when the move action is selected."""

        requires_destination = self.post_upload_action_input.currentData() == "move"
        self.post_upload_destination_widget.setEnabled(requires_destination)
        self.post_upload_destination_widget.setVisible(requires_destination)
        self.post_upload_destination_label.setVisible(requires_destination)
        self.validation_label.clear()

    def _update_regex_filter_state(self, *_args) -> None:
        """Show regex controls only while regex filtering is enabled."""

        self.regex_filter_details.setVisible(self.regex_filter_toggle.isChecked())
        self.validation_label.clear()

    def _update_regex_summary(self) -> None:
        """Refresh the small pattern-count summary shown beside the editor button."""

        pattern_count = len(self._regex_patterns)
        if pattern_count == 0:
            self.regex_summary_label.setText("No patterns configured")
            return

        pattern_label = "pattern" if pattern_count == 1 else "patterns"
        self.regex_summary_label.setText(f"{pattern_count} {pattern_label} configured")


class SettingsWindow(QWidget):
    """Provide the configuration window for monitored folders and recent activity.

    The window emits high-level signals instead of directly changing application state,
    which keeps the UI focused on presentation while the tray controller performs the
    actual persistence and monitor updates.
    """

    monitoring_toggled = Signal(bool)
    add_folder_requested = Signal(object)
    remove_folder_requested = Signal(str)
    retry_failed_upload_requested = Signal(str)
    save_settings_requested = Signal()

    def __init__(self) -> None:
        """Construct the minimalist settings UI used by the tray application."""

        super().__init__()
        self.setObjectName("settingsWindow")
        self.setWindowTitle("Ingestion Monitor")
        self.resize(640, 460)
        self._irods_zone_for_new_folders = "tempZone"
        self._monitored_directories: list[MonitoredDirectory] = []

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

        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")

        overview_tab = QWidget()
        overview_layout = QVBoxLayout(overview_tab)
        overview_layout.setContentsMargins(0, 0, 0, 0)
        overview_layout.setSpacing(14)

        session_card = QFrame()
        session_card.setFrameShape(QFrame.Shape.StyledPanel)
        session_card.setObjectName("sessionSummaryCard")

        session_layout = QVBoxLayout(session_card)
        session_layout.setContentsMargins(16, 16, 16, 16)
        session_layout.setSpacing(8)

        session_title = QLabel("iRODS session")
        session_title.setObjectName("irodsCardTitle")

        self.session_summary_label = QLabel("No iRODS session is configured")
        self.session_summary_label.setWordWrap(True)
        self.session_zone_label = QLabel("Zone: not configured")
        self.session_zone_label.setWordWrap(True)

        session_layout.addWidget(session_title)
        session_layout.addWidget(self.session_summary_label)
        session_layout.addWidget(self.session_zone_label)

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

        irods_layout.addWidget(irods_title)
        irods_layout.addLayout(form_layout)

        retry_card = QFrame()
        retry_card.setFrameShape(QFrame.Shape.StyledPanel)
        retry_card.setObjectName("retryCard")

        retry_layout = QVBoxLayout(retry_card)
        retry_layout.setContentsMargins(16, 16, 16, 16)
        retry_layout.setSpacing(12)

        retry_title = QLabel("Upload retry")
        retry_title.setObjectName("directoryCardTitle")

        retry_form_layout = QFormLayout()
        retry_form_layout.setSpacing(10)
        retry_form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.retry_attempts_input = QSpinBox()
        self.retry_attempts_input.setRange(0, 100)
        self.retry_first_delay_input = QSpinBox()
        self.retry_first_delay_input.setRange(0, 3600)
        self.retry_backoff_multiplier_input = QDoubleSpinBox()
        self.retry_backoff_multiplier_input.setRange(1.0, 100.0)
        self.retry_backoff_multiplier_input.setDecimals(2)
        self.retry_backoff_multiplier_input.setSingleStep(0.1)

        retry_form_layout.addRow("Retry attempts", self.retry_attempts_input)
        retry_form_layout.addRow("Initial delay (s)", self.retry_first_delay_input)
        retry_form_layout.addRow("Backoff multiplier", self.retry_backoff_multiplier_input)

        retry_layout.addWidget(retry_title)
        retry_layout.addLayout(retry_form_layout)

        settings_button_row = QHBoxLayout()
        settings_button_row.setSpacing(10)
        self.save_settings_button = QPushButton("Save Settings")
        self.save_settings_button.clicked.connect(self._emit_save_settings_requested)
        settings_button_row.addWidget(self.save_settings_button)
        settings_button_row.addStretch(1)

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

        failed_uploads_card = QFrame()
        failed_uploads_card.setFrameShape(QFrame.Shape.StyledPanel)
        failed_uploads_card.setObjectName("failedUploadsCard")

        failed_uploads_layout = QVBoxLayout(failed_uploads_card)
        failed_uploads_layout.setContentsMargins(16, 16, 16, 16)
        failed_uploads_layout.setSpacing(12)

        failed_uploads_title = QLabel("Failed uploads")
        failed_uploads_title.setObjectName("directoryCardTitle")

        self.failed_uploads_list = QListWidget()
        self.failed_uploads_list.setMaximumHeight(120)
        self.failed_uploads_list.currentItemChanged.connect(self._update_retry_failed_button_state)

        failed_uploads_button_row = QHBoxLayout()
        failed_uploads_button_row.setSpacing(10)
        self.retry_failed_upload_button = QPushButton("Retry Now")
        self.retry_failed_upload_button.clicked.connect(self._emit_retry_failed_upload_requested)
        self.retry_failed_upload_button.setEnabled(False)
        failed_uploads_button_row.addWidget(self.retry_failed_upload_button)
        failed_uploads_button_row.addStretch(1)

        failed_uploads_layout.addWidget(failed_uploads_title)
        failed_uploads_layout.addWidget(self.failed_uploads_list)
        failed_uploads_layout.addLayout(failed_uploads_button_row)

        activity_title = QLabel("Recent activity")
        activity_title.setObjectName("activityTitle")

        self.activity_list = QListWidget()
        self.activity_list.setMaximumHeight(140)

        overview_layout.addWidget(session_card)
        overview_layout.addWidget(directory_card, 1)
        overview_layout.addWidget(failed_uploads_card)
        overview_layout.addWidget(activity_title)
        overview_layout.addWidget(self.activity_list)

        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(14)
        settings_layout.addWidget(irods_card)
        settings_layout.addWidget(retry_card)
        settings_layout.addLayout(settings_button_row)
        settings_layout.addStretch(1)

        self.tabs.addTab(overview_tab, "Overview")
        self.tabs.addTab(settings_tab, "Settings")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)
        layout.addWidget(self.monitor_toggle)
        layout.addWidget(self.status_label)
        layout.addWidget(self.tabs, 1)

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
        self._monitored_directories = list(directories)
        for directory in directories:
            target_label = directory.target_collection or "(target collection required)"
            recursive_label = "recursive" if directory.recursive else "top-level only"
            cleanup_label = _describe_post_upload_policy(directory)
            regex_label = _describe_regex_filter(directory)
            label = (
                f"{directory.source_directory} -> {target_label} "
                f"({recursive_label}, {cleanup_label}, {regex_label})"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, directory.source_directory)
            item.setToolTip(
                f"Source directory: {directory.source_directory}\n"
                f"Target collection: {target_label}\n"
                f"Recursive monitoring: {'On' if directory.recursive else 'Off'}\n"
                f"Post-upload action: {cleanup_label}\n"
                f"Regex filter: {regex_label}"
            )
            if directory.source_directory in invalid_directories:
                item.setForeground(QColor("#b42318"))
                item.setToolTip(
                    "Directory does not currently exist and is not being watched.\n"
                    f"Target collection: {target_label}\n"
                    f"Recursive monitoring: {'On' if directory.recursive else 'Off'}\n"
                    f"Post-upload action: {cleanup_label}\n"
                    f"Regex filter: {regex_label}"
                )
            self.directory_list.addItem(item)
        self._update_remove_button_state()

    def set_irods_environment(self, environment: IRODSEnvironment) -> None:
        """Populate the iRODS settings form from persisted configuration."""

        self.irods_host_input.setText(environment.irods_host)
        self.irods_port_input.setValue(environment.irods_port)
        self.irods_user_name_input.setText(environment.irods_user_name)
        self.irods_password_input.clear()
        self.irods_zone_name_input.setText(environment.irods_zone_name)
        self._irods_zone_for_new_folders = environment.irods_zone_name
        if environment.irods_user_name and environment.irods_host:
            self.session_summary_label.setText(
                f"Signed in as {environment.irods_user_name}@{environment.irods_host}:{environment.irods_port}"
            )
        else:
            self.session_summary_label.setText("No iRODS session is configured")
        if environment.irods_zone_name:
            self.session_zone_label.setText(f"Zone: {environment.irods_zone_name}")
        else:
            self.session_zone_label.setText("Zone: not configured")

    def set_failed_uploads(self, failed_uploads: list[tuple[str, str, int, int]]) -> None:
        """Refresh the failed upload list and selection-dependent retry action."""

        self.failed_uploads_list.clear()
        for local_path, last_error, attempt_number, max_attempts in failed_uploads:
            item = QListWidgetItem(
                f"{Path(local_path).name} ({attempt_number}/{max_attempts})"
            )
            item.setData(Qt.ItemDataRole.UserRole, local_path)
            item.setToolTip(
                f"File: {local_path}\n"
                f"Last error: {last_error or 'Unknown error'}\n"
                f"Attempts used: {attempt_number}/{max_attempts}"
            )
            self.failed_uploads_list.addItem(item)
        self._update_retry_failed_button_state()

    def get_irods_environment(self) -> IRODSEnvironment:
        """Collect the current form values into the config dataclass."""

        return IRODSEnvironment(
            irods_host=self.irods_host_input.text().strip(),
            irods_port=self.irods_port_input.value(),
            irods_user_name=self.irods_user_name_input.text().strip(),
            irods_password=self.irods_password_input.text(),
            irods_zone_name=self.irods_zone_name_input.text().strip(),
        )

    def set_retry_config(self, retry: RetryConfig) -> None:
        """Populate the retry controls from the persisted app configuration."""

        self.retry_attempts_input.setValue(retry.attempts)
        self.retry_first_delay_input.setValue(retry.first_delay_in_seconds)
        self.retry_backoff_multiplier_input.setValue(retry.backoff_multiplier)

    def get_retry_config(self) -> RetryConfig:
        """Collect the current retry controls into the config dataclass."""

        return RetryConfig(
            attempts=self.retry_attempts_input.value(),
            first_delay_in_seconds=self.retry_first_delay_input.value(),
            backoff_multiplier=self.retry_backoff_multiplier_input.value(),
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

        dialog = AddDirectoryDialog(
            self._irods_zone_for_new_folders,
            self._monitored_directories,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        directory = dialog.get_directory()
        self.add_folder_requested.emit(directory)

    def _emit_remove_selected(self, _checked: bool = False) -> None:
        """Emit the currently selected directory so the controller can remove it."""

        item = self.directory_list.currentItem()
        if item is None:
            return
        directory = item.data(Qt.ItemDataRole.UserRole)
        self.remove_folder_requested.emit(directory)

    def _emit_retry_failed_upload_requested(self, _checked: bool = False) -> None:
        """Emit the selected failed upload so the controller can retry it now."""

        item = self.failed_uploads_list.currentItem()
        if item is None:
            return
        local_path = item.data(Qt.ItemDataRole.UserRole)
        self.retry_failed_upload_requested.emit(local_path)

    def _emit_save_settings_requested(self, _checked: bool = False) -> None:
        """Notify the controller that the user wants to persist all settings."""

        self.save_settings_requested.emit()

    def _update_remove_button_state(self, *_args) -> None:
        """Enable removal only when the user has a directory selected in the list."""

        self.remove_button.setEnabled(self.directory_list.currentItem() is not None)

    def _update_retry_failed_button_state(self, *_args) -> None:
        """Enable retry only when the user has a failed upload selected in the list."""

        self.retry_failed_upload_button.setEnabled(self.failed_uploads_list.currentItem() is not None)
