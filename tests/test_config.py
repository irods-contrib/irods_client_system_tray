"""Tests for the pure config-normalization logic and the JSON-backed stores."""

from __future__ import annotations

import pytest

import irods_client_system_tray.config as config_module
from irods_client_system_tray.config import (
    DEFAULT_POST_UPLOAD_ACTION,
    AppConfig,
    ConfigStore,
    IRODSEnvironment,
    IRODSEnvironmentStore,
    MonitoredDirectory,
    normalize_irods_collection,
    normalize_irods_zone_name,
    normalize_monitored_directories,
    normalize_post_upload_action,
    normalize_target_collection_for_zone,
    rezone_target_collection,
)


def test_default_config_dir_uses_consistent_app_name(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    xdg_config = tmp_path / "xdg"
    home = tmp_path / "home"

    monkeypatch.setattr(config_module.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    assert config_module._default_config_dir() == appdata / "irods-client-system-tray"

    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    monkeypatch.setattr(config_module.Path, "home", lambda: home)
    assert (
        config_module._default_config_dir()
        == home / "Library" / "Application Support" / "irods-client-system-tray"
    )

    monkeypatch.setattr(config_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    assert config_module._default_config_dir() == xdg_config / "irods-client-system-tray"


def test_typed_paths_are_normalized_before_use():
    # Checks that what the user types in the settings form is tidied before it becomes
    # part of an upload path. Stray spaces and slashes here would otherwise end up
    # inside real iRODS collection paths and send files somewhere nobody expects.
    assert normalize_irods_zone_name("  /tempZone/  ") == "tempZone"
    assert normalize_irods_collection("tempZone/home/") == "/tempZone/home"

    # An empty box falls back to the zone root rather than an unusable empty path.
    assert normalize_irods_collection("") == "/"
    assert normalize_irods_collection("   ") == "/"


@pytest.mark.parametrize(
    "path",
    [
        "/home/alice",
        # Already zoned: a naive prepend would give /tempZone/tempZone/home/alice.
        "/tempZone/home/alice",
    ],
)
def test_folder_targets_are_forced_under_the_configured_zone(path):
    # Checks every upload target ends up inside the user's own zone, whether they
    # typed the zone themselves or not. Getting this wrong sends files outside the
    # zone the user is actually logged into.
    assert normalize_target_collection_for_zone(path, "tempZone") == "/tempZone/home/alice"


def test_switching_zones_updates_existing_targets():
    # Checks that changing zones in settings repoints saved folders instead of
    # leaving them aimed at the old zone, which would silently break every upload.
    assert (
        rezone_target_collection("/tempZone/home/alice", "tempZone", "otherZone")
        == "/otherZone/home/alice"
    )


def test_duplicate_folders_are_stored_once(tmp_path):
    # Checks a folder cannot end up in the list twice and get uploaded twice. The
    # saved file stores folders as plain data while the running app uses objects, so
    # both forms have to count as the same folder. A blank path is dropped entirely.
    source = str(tmp_path)
    directories = [
        MonitoredDirectory(source_directory=source, target_collection="/tempZone/a"),
        {"source_directory": source, "target_collection": "/tempZone/b"},
        {"source_directory": "", "target_collection": "/tempZone/c"},
    ]

    result = normalize_monitored_directories(directories)

    assert len(result) == 1
    assert result[0].source_directory == source


def test_settings_survive_an_app_restart(tmp_path):
    # Checks the monitoring toggle and folder list come back exactly as saved.
    store = ConfigStore(path=tmp_path / "app_state.json")
    source = str(tmp_path)
    config = AppConfig(
        is_monitoring_active=False,
        monitored_directories=[MonitoredDirectory(source_directory=source)],
    )

    store.save(config)
    loaded = store.load()

    assert loaded.is_monitoring_active is False
    assert loaded.monitored_directories[0].source_directory == source


def test_corrupt_settings_file_falls_back_to_defaults(tmp_path):
    # Checks the app still opens if its settings file is missing or damaged. It
    # falls back to defaults rather than refusing to launch.
    missing = ConfigStore(path=tmp_path / "missing.json")
    assert missing.load() == AppConfig()

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not json", encoding="utf-8")
    assert ConfigStore(path=bad_json).load() == AppConfig()


def test_irods_connection_details_survive_a_restart(tmp_path):
    # Checks the saved host and port come back on the next launch.
    store = IRODSEnvironmentStore(path=tmp_path / "irods_environment.json")
    environment = IRODSEnvironment(irods_host="host.example.org", irods_port=1247)

    store.save(environment)
    loaded = store.load()

    assert loaded.irods_host == "host.example.org"
    assert loaded.irods_port == 1247


def test_invalid_port_falls_back_to_the_default(tmp_path):
    # Checks a bad port does not crash the app. irods_environment.json is meant to
    # be hand-edited, so a non-numeric port is a realistic typo to be handled.
    path = tmp_path / "irods_environment.json"
    path.write_text('{"irods_port": "not-a-number"}', encoding="utf-8")

    loaded = IRODSEnvironmentStore(path=path).load()

    assert loaded.irods_port == IRODSEnvironment().irods_port


def test_irods_password_is_never_written_to_disk(tmp_path):
    # Checks the password stays in memory only. It must not be written when saving,
    # and if someone pastes one into the file by hand it must not be read back.
    path = tmp_path / "irods_environment.json"
    store = IRODSEnvironmentStore(path=path)

    store.save(IRODSEnvironment(irods_user_name="alice", irods_password="alicepass"))

    written = path.read_text(encoding="utf-8")
    assert "irods_password" not in written
    assert "alicepass" not in written
    assert store.load().irods_password == ""

    path.write_text('{"irods_password": "alicepass"}', encoding="utf-8")
    assert store.load().irods_password == ""


def test_recursive_setting_survives_a_restart(tmp_path):
    # Checks a user who unticks "monitor subfolders" still has it unticked after a
    # restart. An older saved file with no such setting defaults to watching
    # subfolders, which matches what the app did before the option existed.
    source = str(tmp_path)
    store = ConfigStore(path=tmp_path / "app_state.json")
    store.save(
        AppConfig(
            monitored_directories=[
                MonitoredDirectory(source_directory=source, recursive=False)
            ]
        )
    )

    assert store.load().monitored_directories[0].recursive is False
    assert normalize_monitored_directories([{"source_directory": source}])[0].recursive is True


@pytest.mark.parametrize(
    ("stored_action", "expected"),
    [
        ("recycle", "recycle"),
        ("  ReCyCle ", "recycle"),
        # Anything the app does not recognize becomes the default, which is "delete".
        ("banana", DEFAULT_POST_UPLOAD_ACTION),
        ("", DEFAULT_POST_UPLOAD_ACTION),
    ],
)
def test_unrecognized_cleanup_action_falls_back_to_delete(stored_action, expected):
    # The saved file is hand-editable, so it can hold anything. A value the app does
    # not know falls back to DEFAULT_POST_UPLOAD_ACTION, which permanently deletes the
    # local file after upload — the fallback is the destructive one.
    assert normalize_post_upload_action(stored_action) == expected


def test_folder_without_a_cleanup_action_defaults_to_delete(tmp_path):
    # An app_state.json written by an older build has no post_upload_action at all.
    # It loads as "delete", so upgrading turns every existing watched folder into one
    # that removes local files once they reach iRODS.
    loaded = normalize_monitored_directories([{"source_directory": str(tmp_path)}])[0]

    assert loaded.post_upload_action == DEFAULT_POST_UPLOAD_ACTION == "delete"


def test_move_without_a_destination_falls_back_to_delete(tmp_path):
    # "move" with no destination cannot be carried out, and rather than being rejected
    # it collapses to the default action. The result is that a folder configured to
    # move files ends up deleting them.
    loaded = normalize_monitored_directories(
        [{"source_directory": str(tmp_path), "post_upload_action": "move"}]
    )[0]

    assert loaded.post_upload_action == "delete"
    assert loaded.post_upload_destination == ""


def test_destination_is_kept_only_for_the_move_action(tmp_path):
    # Every other action leaves the file where it is or removes it, so a leftover
    # destination from an earlier edit is discarded rather than being acted on.
    loaded = normalize_monitored_directories(
        [
            {
                "source_directory": str(tmp_path),
                "post_upload_action": "keep",
                "post_upload_destination": str(tmp_path / "archive"),
            }
        ]
    )[0]

    assert loaded.post_upload_action == "keep"
    assert loaded.post_upload_destination == ""


def test_cleanup_settings_survive_a_restart(tmp_path):
    # Both cleanup fields are written to app_state.json and read back, so a folder set
    # to keep its files does not silently revert to the deleting default on relaunch.
    source = str(tmp_path)
    destination = str(tmp_path / "archive")
    store = ConfigStore(path=tmp_path / "app_state.json")
    store.save(
        AppConfig(
            monitored_directories=[
                MonitoredDirectory(
                    source_directory=source,
                    post_upload_action="move",
                    post_upload_destination=destination,
                )
            ]
        )
    )

    reloaded = store.load().monitored_directories[0]

    assert reloaded.post_upload_action == "move"
    assert reloaded.post_upload_destination == destination
