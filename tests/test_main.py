"""Tests for application startup helpers."""

from __future__ import annotations

import sys
import types

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
    monkeypatch.setattr(main.signal, "signal", lambda *_args: None)

    assert main.main() == 0
    assert events == ["qapplication", "hide_dock"]
