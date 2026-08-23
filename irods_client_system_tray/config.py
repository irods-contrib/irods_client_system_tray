"""Configuration persistence helpers for the tray application's saved state."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable


def _default_config_dir() -> Path:
    """Return the per-user config directory for persisted application state."""

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base_dir = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base_dir / "irods-client-system-tray"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "irods-client-system-tray"

    base_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base_dir / "irods-client-system-tray"


APP_CONFIG_DIR = _default_config_dir()
CONFIG_PATH = APP_CONFIG_DIR / "app_state.json"
IRODS_ENVIRONMENT_PATH = APP_CONFIG_DIR / "irods_environment.json"
ACTIVITY_LOG_PATH = APP_CONFIG_DIR / "activity.log"
DEFAULT_POST_UPLOAD_ACTION = "delete"
POST_UPLOAD_ACTIONS = frozenset({"keep", "recycle", "delete", "move"})
DEFAULT_REGEX_FILTER_MODE = "disabled"
REGEX_FILTER_MODES = frozenset({"allow", "deny", "disabled"})


@dataclass(slots=True)
class AppConfig:
    """Store the persisted monitoring toggle and normalized directory list."""

    is_monitoring_active: bool = True
    monitored_directories: list[MonitoredDirectory] = field(default_factory=list)
    retry: RetryConfig = field(default_factory=lambda: RetryConfig())
    retry_by_user: dict[str, RetryConfig] = field(default_factory=dict)


@dataclass(slots=True)
class RetryConfig:
    """Store retry behavior for failed uploads."""

    attempts: int = 1
    first_delay_in_seconds: int = 1
    backoff_multiplier: float = 1.0


@dataclass(slots=True)
class RegexFilterConfig:
    """Store the optional regex-based allow/deny file filter for a folder."""

    mode: str = DEFAULT_REGEX_FILTER_MODE
    patterns: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MonitoredDirectory:
    """Describe one monitored local folder and its destination iRODS collection."""

    source_directory: str
    target_collection: str = ""
    recursive: bool = True
    post_upload_action: str = DEFAULT_POST_UPLOAD_ACTION
    post_upload_destination: str = ""
    regex_filter: RegexFilterConfig = field(default_factory=RegexFilterConfig)


@dataclass(slots=True)
class IRODSEnvironment:
    """Store the persisted iRODS session details used for background uploads."""

    irods_host: str = ""
    irods_port: int = 1247
    irods_user_name: str = ""
    irods_password: str = ""
    irods_zone_name: str = "tempZone"


def build_retry_settings_user_key(environment: IRODSEnvironment) -> str:
    """Return a stable per-user key for retry settings persistence."""

    host = environment.irods_host.strip()
    user_name = environment.irods_user_name.strip()
    zone_name = normalize_irods_zone_name(environment.irods_zone_name)
    return f"{user_name}@{host}:{int(environment.irods_port)}/{zone_name}"


def normalize_directory(path: str) -> str:
    """Convert a user-provided path into one canonical absolute directory string.

    This keeps saved paths consistent so duplicate entries caused by relative paths,
    home-directory shortcuts, or mixed path styles collapse to a single value.
    """

    return str(Path(path).expanduser().resolve(strict=False))


def normalize_post_upload_action(action: str) -> str:
    """Return a supported post-upload action or fall back to the safe default."""

    normalized_action = str(action).strip().lower()
    if normalized_action not in POST_UPLOAD_ACTIONS:
        return DEFAULT_POST_UPLOAD_ACTION
    return normalized_action


def normalize_file_path(path: str) -> str:
    """Return a canonical absolute filesystem path string when one is provided."""

    strpath = str(path)
    if not strpath.strip():
        return ""
    return str(Path(strpath).expanduser().resolve(strict=False))


def normalize_regex_filter_mode(mode: str) -> str:
    """Return a supported regex filter mode or fall back to disabled mode."""

    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in REGEX_FILTER_MODES:
        return DEFAULT_REGEX_FILTER_MODE
    return normalized_mode


def normalize_regex_filter(
    regex_filter: RegexFilterConfig | dict[str, object] | None,
) -> RegexFilterConfig:
    """Return a normalized regex filter configuration from supported input shapes."""

    if isinstance(regex_filter, RegexFilterConfig):
        mode = regex_filter.mode
        patterns = regex_filter.patterns
    elif isinstance(regex_filter, dict):
        mode = str(regex_filter.get("mode", DEFAULT_REGEX_FILTER_MODE))
        patterns = regex_filter.get("patterns", [])
    else:
        mode = DEFAULT_REGEX_FILTER_MODE
        patterns = []

    raw_mode = str(mode).strip().lower()
    normalized_mode = normalize_regex_filter_mode(mode)
    normalized_patterns = _normalize_regex_patterns(patterns)
    if normalized_mode != raw_mode or normalized_patterns is None:
        return RegexFilterConfig()
    if normalized_mode != "disabled" and not normalized_patterns:
        return RegexFilterConfig()

    return RegexFilterConfig(mode=normalized_mode, patterns=normalized_patterns)


def _normalize_regex_patterns(patterns: object) -> list[str] | None:
    """Return cleaned regex patterns or ``None`` when the input is malformed."""

    if not isinstance(patterns, list):
        return None

    normalized_patterns: list[str] = []
    for raw_pattern in patterns:
        if not isinstance(raw_pattern, str):
            return None

        pattern = raw_pattern.strip()
        if not pattern:
            continue

        try:
            re.compile(pattern)
        except re.error:
            return None
        normalized_patterns.append(pattern)

    return normalized_patterns


def normalize_monitored_directories(
    directories: Iterable[MonitoredDirectory | dict[str, object]]
) -> list[MonitoredDirectory]:
    """Normalize, de-duplicate, and preserve monitored directory order.
    """

    unique_directories: list[MonitoredDirectory] = []
    seen: set[str] = set()
    for raw_directory in directories:
        normalized = _normalize_monitored_directory(raw_directory)
        if normalized is None:
            continue
        if normalized.source_directory in seen:
            continue
        seen.add(normalized.source_directory)
        unique_directories.append(normalized)
    return unique_directories


def normalize_retry_config(retry: RetryConfig | dict[str, object] | None) -> RetryConfig:
    """Return a bounded retry config from supported persisted shapes."""

    defaults = RetryConfig()
    if isinstance(retry, RetryConfig):
        attempts = retry.attempts
        first_delay_in_seconds = retry.first_delay_in_seconds
        backoff_multiplier = retry.backoff_multiplier
    elif isinstance(retry, dict):
        attempts = retry.get("attempts", defaults.attempts)
        first_delay_in_seconds = retry.get(
            "first_delay_in_seconds", defaults.first_delay_in_seconds
        )
        backoff_multiplier = retry.get("backoff_multiplier", defaults.backoff_multiplier)
    else:
        return defaults

    try:
        normalized_attempts = max(0, int(attempts))
    except (TypeError, ValueError):
        normalized_attempts = defaults.attempts

    try:
        normalized_first_delay = max(0, int(first_delay_in_seconds))
    except (TypeError, ValueError):
        normalized_first_delay = defaults.first_delay_in_seconds

    try:
        normalized_backoff = max(1.0, float(backoff_multiplier))
    except (TypeError, ValueError):
        normalized_backoff = defaults.backoff_multiplier

    return RetryConfig(
        attempts=normalized_attempts,
        first_delay_in_seconds=normalized_first_delay,
        backoff_multiplier=normalized_backoff,
    )


def normalize_retry_map(retry_by_user: object) -> dict[str, RetryConfig]:
    """Normalize persisted per-user retry settings and discard malformed entries."""

    if not isinstance(retry_by_user, dict):
        return {}

    normalized: dict[str, RetryConfig] = {}
    for raw_user_key, raw_retry in retry_by_user.items():
        user_key = str(raw_user_key).strip()
        if not user_key:
            continue
        normalized[user_key] = normalize_retry_config(raw_retry)
    return normalized


def _normalize_monitored_directory(
    directory: MonitoredDirectory | dict[str, object],
) -> MonitoredDirectory | None:
    """Return a normalized monitored directory entry from supported input shapes."""

    if isinstance(directory, MonitoredDirectory):
        source_directory = directory.source_directory
        target_collection = directory.target_collection
        recursive = directory.recursive
        post_upload_action = directory.post_upload_action
        post_upload_destination = directory.post_upload_destination
        regex_filter = directory.regex_filter
    elif isinstance(directory, dict):
        source_directory = str(directory.get("source_directory", "")).strip()
        target_collection = str(directory.get("target_collection", "")).strip()
        recursive = bool(directory.get("recursive", True))
        post_upload_action = str(
            directory.get("post_upload_action", DEFAULT_POST_UPLOAD_ACTION)
        )
        post_upload_destination = str(directory.get("post_upload_destination", ""))
        regex_filter = directory.get("regex_filter")
    else:
        return None

    if not source_directory:
        return None

    normalized_target = ""
    if target_collection:
        normalized_target = normalize_irods_collection(target_collection)

    normalized_action = normalize_post_upload_action(post_upload_action)
    normalized_destination = normalize_file_path(post_upload_destination)
    if normalized_action != "move":
        normalized_destination = ""
    elif not normalized_destination:
        normalized_action = DEFAULT_POST_UPLOAD_ACTION

    return MonitoredDirectory(
        source_directory=normalize_directory(source_directory),
        target_collection=normalized_target,
        recursive=recursive,
        post_upload_action=normalized_action,
        post_upload_destination=normalized_destination,
        regex_filter=normalize_regex_filter(regex_filter),
    )
def normalize_irods_collection(path: str) -> str:
    """Return a stable absolute iRODS collection path suitable for uploads."""

    normalized = PurePosixPath(path.strip() or "/").as_posix()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/") or "/"


def normalize_irods_zone_name(zone_name: str) -> str:
    """Return a stable zone name without surrounding whitespace or slashes."""

    return zone_name.strip().strip("/")


def normalize_target_collection_for_zone(path: str, zone_name: str) -> str:
    """Force a target collection to live beneath the configured iRODS zone root."""

    normalized_zone = normalize_irods_zone_name(zone_name)
    normalized_path = normalize_irods_collection(path)
    if not normalized_zone:
        return normalized_path

    path_parts = [part for part in PurePosixPath(normalized_path).parts if part != "/"]
    if path_parts and path_parts[0] == normalized_zone:
        suffix_parts = path_parts[1:]
    else:
        suffix_parts = path_parts

    return str(PurePosixPath("/").joinpath(normalized_zone, *suffix_parts))


def rezone_target_collection(path: str, old_zone_name: str, new_zone_name: str) -> str:
    """Move a target collection from one zone root to another, preserving its suffix."""

    normalized_new_zone = normalize_irods_zone_name(new_zone_name)
    normalized_path = normalize_irods_collection(path)
    if not normalized_new_zone:
        return normalized_path

    normalized_old_zone = normalize_irods_zone_name(old_zone_name)
    path_parts = [part for part in PurePosixPath(normalized_path).parts if part != "/"]
    if path_parts and path_parts[0] in {normalized_old_zone, normalized_new_zone}:
        suffix_parts = path_parts[1:]
    else:
        suffix_parts = path_parts

    return str(PurePosixPath("/").joinpath(normalized_new_zone, *suffix_parts))


class ConfigStore:
    """Load and save the application state as JSON on disk.

    This class isolates file I/O from the tray controller so the rest of the app can
    work with a simple ``AppConfig`` object instead of raw JSON data.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Allow tests or callers to override the default config file location."""

        self.path = path or CONFIG_PATH

    def load(self) -> AppConfig:
        """Read configuration from disk and fall back to defaults on any error.

        Invalid JSON, missing files, or malformed directory lists should not prevent
        the tray application from starting, so this method always returns a usable
        ``AppConfig`` instance.
        """

        if not self.path.exists():
            return AppConfig()

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AppConfig()

        directories = payload.get("monitored_directories", [])
        if not isinstance(directories, list):
            directories = []

        return AppConfig(
            is_monitoring_active=bool(payload.get("is_monitoring_active", True)),
            monitored_directories=normalize_monitored_directories(directories),
            retry=normalize_retry_config(payload.get("retry")),
            retry_by_user=normalize_retry_map(payload.get("retry_by_user")),
        )

    def save(self, config: AppConfig) -> None:
        """Persist the current configuration atomically to reduce corruption risk.

        The file is written to a temporary path first and then replaced in one step so
        an interrupted write is less likely to leave behind a partially written config.
        """

        normalized_retry = normalize_retry_config(config.retry)
        normalized_retry_by_user = normalize_retry_map(config.retry_by_user)
        payload = {
            "is_monitoring_active": bool(config.is_monitoring_active),
            "monitored_directories": [
                {
                    "source_directory": directory.source_directory,
                    "target_collection": directory.target_collection,
                    "recursive": directory.recursive,
                    "post_upload_action": directory.post_upload_action,
                    "post_upload_destination": directory.post_upload_destination,
                    "regex_filter": {
                        "mode": directory.regex_filter.mode,
                        "patterns": directory.regex_filter.patterns,
                    },
                }
                for directory in normalize_monitored_directories(config.monitored_directories)
            ],
            "retry": {
                "attempts": normalized_retry.attempts,
                "first_delay_in_seconds": normalized_retry.first_delay_in_seconds,
                "backoff_multiplier": normalized_retry.backoff_multiplier,
            },
            "retry_by_user": {
                user_key: {
                    "attempts": retry.attempts,
                    "first_delay_in_seconds": retry.first_delay_in_seconds,
                    "backoff_multiplier": retry.backoff_multiplier,
                }
                for user_key, retry in normalized_retry_by_user.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self.path)


class IRODSEnvironmentStore:
    """Load and save the iRODS client environment in a dedicated JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or IRODS_ENVIRONMENT_PATH

    def ensure_exists(self) -> None:
        """Create the environment file with defaults when it is missing."""

        if self.path.exists():
            return
        self.save(IRODSEnvironment())

    def load(self) -> IRODSEnvironment:
        """Read iRODS settings from disk and fall back to sane defaults on errors."""

        if not self.path.exists():
            return IRODSEnvironment()

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return IRODSEnvironment()

        default_environment = IRODSEnvironment()
        port = payload.get("irods_port", default_environment.irods_port)
        try:
            parsed_port = int(port)
        except (TypeError, ValueError):
            parsed_port = default_environment.irods_port

        return IRODSEnvironment(
            irods_host=str(payload.get("irods_host", default_environment.irods_host)).strip(),
            irods_port=parsed_port,
            irods_user_name=str(
                payload.get("irods_user_name", default_environment.irods_user_name)
            ).strip(),
            irods_password="",
            irods_zone_name=normalize_irods_zone_name(
                str(payload.get("irods_zone_name", default_environment.irods_zone_name))
            )
            or default_environment.irods_zone_name,
        )

    def save(self, environment: IRODSEnvironment) -> None:
        """Persist iRODS settings in the standard client JSON shape."""

        payload = {
            "irods_host": environment.irods_host.strip(),
            "irods_port": int(environment.irods_port),
            "irods_user_name": environment.irods_user_name.strip(),
            "irods_zone_name": normalize_irods_zone_name(environment.irods_zone_name)
            or IRODSEnvironment().irods_zone_name,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=4), encoding="utf-8")
        temp_path.replace(self.path)
