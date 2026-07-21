"""Configuration persistence helpers for the tray application's saved state."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable


CONFIG_PATH = Path(__file__).resolve().with_name("app_state.json")
IRODS_ENVIRONMENT_PATH = Path(__file__).resolve().with_name("irods_environment.json")


@dataclass(slots=True)
class AppConfig:
    """Store the persisted monitoring toggle and normalized directory list."""

    is_monitoring_active: bool = True
    monitored_directories: list[MonitoredDirectory] = field(default_factory=list)


@dataclass(slots=True)
class MonitoredDirectory:
    """Describe one monitored local folder and its destination iRODS collection."""

    source_directory: str
    target_collection: str = ""
    recursive: bool = True


@dataclass(slots=True)
class IRODSEnvironment:
    """Store the persisted iRODS session details used for background uploads."""

    irods_host: str = "127.0.0.1"
    irods_port: int = 1247
    irods_user_name: str = "alice"
    irods_password: str = "alicepass"
    irods_zone_name: str = "tempZone"


def normalize_directory(path: str) -> str:
    """Convert a user-provided path into one canonical absolute directory string.

    This keeps saved paths consistent so duplicate entries caused by relative paths,
    home-directory shortcuts, or mixed path styles collapse to a single value.
    """

    return str(Path(path).expanduser().resolve(strict=False))


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


def _normalize_monitored_directory(
    directory: MonitoredDirectory | dict[str, object],
) -> MonitoredDirectory | None:
    """Return a normalized monitored directory entry from supported input shapes."""

    if isinstance(directory, MonitoredDirectory):
        source_directory = directory.source_directory
        target_collection = directory.target_collection
        recursive = directory.recursive
    elif isinstance(directory, dict):
        source_directory = str(directory.get("source_directory", "")).strip()
        target_collection = str(directory.get("target_collection", "")).strip()
        recursive = bool(directory.get("recursive", True))
    else:
        return None

    if not source_directory:
        return None

    normalized_target = ""
    if target_collection:
        normalized_target = normalize_irods_collection(target_collection)

    return MonitoredDirectory(
        source_directory=normalize_directory(source_directory),
        target_collection=normalized_target,
        recursive=recursive,
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
        )

    def save(self, config: AppConfig) -> None:
        """Persist the current configuration atomically to reduce corruption risk.

        The file is written to a temporary path first and then replaced in one step so
        an interrupted write is less likely to leave behind a partially written config.
        """

        payload = {
            "is_monitoring_active": bool(config.is_monitoring_active),
            "monitored_directories": [
                {
                    "source_directory": directory.source_directory,
                    "target_collection": directory.target_collection,
                    "recursive": directory.recursive,
                }
                for directory in normalize_monitored_directories(config.monitored_directories)
            ],
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
            irods_password=str(
                payload.get("irods_password", default_environment.irods_password)
            ),
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
            "irods_password": environment.irods_password,
            "irods_zone_name": normalize_irods_zone_name(environment.irods_zone_name)
            or IRODSEnvironment().irods_zone_name,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=4), encoding="utf-8")
        temp_path.replace(self.path)
