"""Tests for application startup helpers."""

from __future__ import annotations

import runpy
import sys
import types

import pytest

from irods_client_system_tray import main


def test_hide_from_macos_dock_noops_off_macos(monkeypatch):
    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "AppKit", None)

    main._hide_from_macos_dock()


def test_hide_from_macos_dock_sets_accessory_policy_on_macos(monkeypatch):
    policies = []

    class Application:
        def setActivationPolicy_(self, policy):
            policies.append(policy)

    class NSApplication:
        @staticmethod
        def sharedApplication():
            return Application()

    appkit = types.SimpleNamespace(
        NSApplication=NSApplication,
        NSApplicationActivationPolicyAccessory=1,
    )

    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    main._hide_from_macos_dock()

    assert policies == [1]


def test_main_hides_from_macos_dock_after_creating_qapplication(monkeypatch):
    events = []

    class QApplication:
        def __init__(self, _args):
            events.append("qapplication")

        def setApplicationName(self, _name):
            pass

        def setOrganizationName(self, _name):
            pass

        def setStyle(self, _style):
            pass

        def exec(self):
            return 0

        def quit(self):
            pass

    class StyleHints:
        colorSchemeChanged = types.SimpleNamespace(connect=lambda _callback: None)

    class QGuiApplication:
        @staticmethod
        def styleHints():
            return StyleHints()

    class QSystemTrayIcon:
        @staticmethod
        def isSystemTrayAvailable():
            return True

    class QTimer:
        def __init__(self):
            self.timeout = types.SimpleNamespace(connect=lambda _callback: None)

        @staticmethod
        def singleShot(_interval, _callback):
            pass

        def start(self, _interval):
            pass

    class TrayController:
        def __init__(self, _app):
            pass

        def prompt_login(self, *, show_window_on_success):
            pass

    def hide_from_macos_dock():
        events.append("hide_dock")

    monkeypatch.setattr(main, "QApplication", QApplication)
    monkeypatch.setattr(main, "QGuiApplication", QGuiApplication)
    monkeypatch.setattr(main, "QSystemTrayIcon", QSystemTrayIcon)
    monkeypatch.setattr(main, "QTimer", QTimer)
    monkeypatch.setattr(main, "TrayController", TrayController)
    monkeypatch.setattr(main, "_apply_theme", lambda _app: None)
    monkeypatch.setattr(main, "_hide_from_macos_dock", hide_from_macos_dock)
    monkeypatch.setattr(main, "_run_in_background_if_needed", lambda *_args: False)
    monkeypatch.setattr(main.signal, "signal", lambda *_args: None)

    assert main.main() == 0
    assert events == ["qapplication", "hide_dock"]


def test_parse_launcher_args_removes_foreground_flag():
    foreground, help_requested, qt_args = main._parse_launcher_args(
        ["irods-client-system-tray", "--foreground", "-platform", "offscreen"]
    )

    assert foreground is True
    assert help_requested is False
    assert qt_args == ["irods-client-system-tray", "-platform", "offscreen"]


def test_parse_launcher_args_accepts_short_foreground_flag():
    foreground, help_requested, qt_args = main._parse_launcher_args(
        ["irods-client-system-tray", "-f"]
    )

    assert foreground is True
    assert help_requested is False
    assert qt_args == ["irods-client-system-tray"]


def test_parse_launcher_args_removes_help_flag():
    foreground, help_requested, qt_args = main._parse_launcher_args(
        ["irods-client-system-tray", "--help", "-platform", "offscreen"]
    )

    assert foreground is False
    assert help_requested is True
    assert qt_args == ["irods-client-system-tray", "-platform", "offscreen"]


def test_parse_launcher_args_accepts_short_help_flag():
    foreground, help_requested, qt_args = main._parse_launcher_args(
        ["irods-client-system-tray", "-h"]
    )

    assert foreground is False
    assert help_requested is True
    assert qt_args == ["irods-client-system-tray"]


def test_main_prints_help_without_starting_app(monkeypatch, capsys):
    def run_in_background(_foreground, _qt_args):
        raise AssertionError("help should not start the app")

    monkeypatch.setattr(
        main.sys,
        "argv",
        [
            "/venv/bin/irods-client-system-tray",
            "--help",
        ],
    )
    monkeypatch.setattr(main, "_run_in_background_if_needed", run_in_background)

    assert main.main() == 0

    output = capsys.readouterr().out
    assert "Usage: irods-client-system-tray [OPTIONS] [QT_OPTIONS]" in output
    assert "-f, --foreground" in output
    assert "-h, --help" in output


def test_run_in_background_starts_detached_child(monkeypatch):
    launched = []

    def popen(command, **kwargs):
        launched.append((command, kwargs))

    monkeypatch.delenv(main.BACKGROUND_ENV_VAR, raising=False)
    monkeypatch.delenv(main.FOREGROUND_ENV_VAR, raising=False)
    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.setattr(main.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(main.subprocess, "Popen", popen)

    assert main._run_in_background_if_needed(
        False,
        ["irods-client-system-tray", "-platform", "offscreen"],
    )

    command, kwargs = launched[0]
    assert command == [
        "/usr/bin/python3",
        "-m",
        "irods_client_system_tray",
        "-platform",
        "offscreen",
    ]
    assert kwargs["env"][main.BACKGROUND_ENV_VAR] == "1"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == main.subprocess.DEVNULL
    assert kwargs["stdout"] == main.subprocess.DEVNULL
    assert kwargs["stderr"] == main.subprocess.DEVNULL


def test_run_in_background_skips_inside_detached_child(monkeypatch):
    def popen(_command, **_kwargs):
        raise AssertionError("child process should not be relaunched")

    monkeypatch.setenv(main.BACKGROUND_ENV_VAR, "1")
    monkeypatch.delenv(main.FOREGROUND_ENV_VAR, raising=False)
    monkeypatch.setattr(main.subprocess, "Popen", popen)

    assert not main._run_in_background_if_needed(False, ["irods-client-system-tray"])


def test_run_in_background_skips_when_foreground_is_requested(monkeypatch):
    def popen(_command, **_kwargs):
        raise AssertionError("foreground process should not be relaunched")

    monkeypatch.delenv(main.BACKGROUND_ENV_VAR, raising=False)
    monkeypatch.setenv(main.FOREGROUND_ENV_VAR, "1")
    monkeypatch.setattr(main.subprocess, "Popen", popen)

    assert not main._run_in_background_if_needed(False, ["irods-client-system-tray"])


def test_run_in_background_skips_when_foreground_flag_is_used(monkeypatch):
    def popen(_command, **_kwargs):
        raise AssertionError("foreground process should not be relaunched")

    monkeypatch.delenv(main.BACKGROUND_ENV_VAR, raising=False)
    monkeypatch.delenv(main.FOREGROUND_ENV_VAR, raising=False)
    monkeypatch.setattr(main.subprocess, "Popen", popen)

    assert not main._run_in_background_if_needed(True, ["irods-client-system-tray"])


def test_palette_uses_theme_tokens_for_active_and_disabled_text():
    from PySide6.QtGui import QColor, QPalette

    tokens = main.THEME_TOKENS["dark"]
    palette = main._palette_from_tokens(tokens)

    assert palette.color(QPalette.ColorRole.Window) == QColor(tokens["window_bg"])
    assert palette.color(QPalette.ColorRole.Highlight) == QColor(tokens["accent"])
    assert palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text) == QColor(
        tokens["muted"]
    )


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_apply_theme_sets_palette_and_stylesheet(monkeypatch, scheme):
    color_scheme = getattr(main.Qt.ColorScheme, scheme.capitalize())
    monkeypatch.setattr(
        main, "QGuiApplication",
        types.SimpleNamespace(styleHints=lambda: types.SimpleNamespace(colorScheme=lambda: color_scheme)),
    )
    monkeypatch.setattr(
        main,
        "_font_size_tokens_from_system_font",
        lambda _app: {name: "12pt" for name in main.FONT_SIZE_RATIOS},
    )
    applied = {}
    app = types.SimpleNamespace(
        setPalette=lambda palette: applied.setdefault("palette", palette),
        setStyleSheet=lambda stylesheet: applied.setdefault("stylesheet", stylesheet),
    )

    main._apply_theme(app)

    assert main.THEME_TOKENS[scheme]["window_bg"] in applied["stylesheet"]
    assert applied["palette"].color(main.QPalette.ColorRole.Window).name() == (
        main.THEME_TOKENS[scheme]["window_bg"]
    )


def test_run_in_background_uses_windows_process_flags(monkeypatch):
    launched = []
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(main.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(main.subprocess, "Popen", lambda command, **kwargs: launched.append(kwargs))
    monkeypatch.delenv(main.BACKGROUND_ENV_VAR, raising=False)
    monkeypatch.delenv(main.FOREGROUND_ENV_VAR, raising=False)

    assert main._run_in_background_if_needed(False, ["app"])
    assert launched[0]["creationflags"] == 0x208
    assert "start_new_session" not in launched[0]


def test_main_returns_without_creating_qt_when_launching_detached(monkeypatch):
    monkeypatch.setattr(main.sys, "argv", ["app"])
    monkeypatch.setattr(main, "_run_in_background_if_needed", lambda *_args: True)
    monkeypatch.setattr(main, "QApplication", lambda *_args: pytest.fail("Qt should not start"))

    assert main.main() == 0


def test_main_explains_when_no_system_tray_is_available(monkeypatch):
    class App:
        def __init__(self, _args):
            pass

        def setApplicationName(self, _name):
            pass

        def setOrganizationName(self, _name):
            pass

        def setStyle(self, _name):
            pass

    monkeypatch.setattr(main.sys, "argv", ["app", "--foreground"])
    monkeypatch.setattr(main, "QApplication", App)
    monkeypatch.setattr(main, "_hide_from_macos_dock", lambda: None)
    monkeypatch.setattr(main, "_apply_theme", lambda _app: None)
    monkeypatch.setattr(
        main, "QGuiApplication",
        types.SimpleNamespace(styleHints=lambda: types.SimpleNamespace(
            colorSchemeChanged=types.SimpleNamespace(connect=lambda _callback: None)
        )),
    )
    monkeypatch.setattr(main, "QSystemTrayIcon", types.SimpleNamespace(isSystemTrayAvailable=lambda: False))

    with pytest.raises(SystemExit, match="System tray is not available"):
        main.main()


def test_module_entrypoint_exits_with_main_result(monkeypatch):
    monkeypatch.setattr(main, "main", lambda: 23)

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("irods_client_system_tray.__main__", run_name="__main__")

    assert excinfo.value.code == 23
