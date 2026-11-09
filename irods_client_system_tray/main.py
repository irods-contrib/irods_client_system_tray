"""Application entry point for the directory ingestion tray utility."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from importlib.resources import files
from string import Template

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFontInfo, QGuiApplication, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from .tray import TrayController

BACKGROUND_ENV_VAR = "IRODS_CLIENT_SYSTEM_TRAY_BACKGROUND"
FOREGROUND_ENV_VAR = "IRODS_CLIENT_SYSTEM_TRAY_FOREGROUND"
HELP_OPTIONS = ("-h", "--help")
THEME_TEMPLATE_PATH = files("irods_client_system_tray").joinpath("theme.qss.template")
APP_ICON_RESOURCE = files("irods_client_system_tray").joinpath("app_icon.png")
APP_NAME = "iRODS Ingest"

THEME_TOKENS = {
    "dark": {
        "window_bg": "#14171a",
        "card_bg": "#1c2024",
        "field_bg": "#15181b",
        "border": "#5b5c61",
        "text": "#e6e8eb",
        "muted": "#8b93a0",
        "accent": "#00BDAC",
        "accent_hover": "#17c8b7",
        "accent_pressed": "#029e90",
        "accent_text": "#062019",
        "error": "#f87171",
        "disabled_bg": "#3a3f46",
        "disabled_text": "#6b7280",
    },
    "light": {
        "window_bg": "#f8fafc",
        "card_bg": "#ffffff",
        "field_bg": "#ffffff",
        "border": "#d0d5dd",
        "text": "#101828",
        "muted": "#5b5c61",
        "accent": "#00BDAC",
        "accent_hover": "#17c8b7",
        "accent_pressed": "#029e90",
        "accent_text": "#062019",
        "error": "#b42318",
        "disabled_bg": "#98a2b3",
        "disabled_text": "#ffffff",
    },
}


# Stylesheet font sizes, keyed by the token name theme.qss.template refers to. Each
# value is a multiple of the font size the OS gives the application, so the whole scale
# moves together with the system text size.
FONT_SIZE_RATIOS = {
    "font_display": 1.71,
    "font_lg": 1.07,
    "font_md": 1.0,
    "font_sm": 0.93,
}


def _font_size_tokens_from_system_font(app: QApplication) -> dict[str, str]:
    """Derive the stylesheet's font sizes from the font the OS handed the application."""

    base_point_size = QFontInfo(app.font()).pointSize() or 13
    return {
        name: f"{max(round(base_point_size * ratio), 8)}pt"
        for name, ratio in FONT_SIZE_RATIOS.items()
    }


def _palette_from_tokens(tokens: dict[str, str]) -> QPalette:
    """Build a QPalette from the same token dict that fills theme.qss.template."""

    palette = QPalette()
    window_color = QColor(tokens["window_bg"])
    base_color = QColor(tokens["card_bg"])
    text_color = QColor(tokens["text"])
    muted_color = QColor(tokens["muted"])
    accent_color = QColor(tokens["accent"])
    accent_text_color = QColor(tokens["accent_text"])

    palette.setColor(QPalette.ColorRole.Window, window_color)
    palette.setColor(QPalette.ColorRole.WindowText, text_color)
    palette.setColor(QPalette.ColorRole.Base, base_color)
    palette.setColor(QPalette.ColorRole.AlternateBase, window_color)
    palette.setColor(QPalette.ColorRole.Text, text_color)
    palette.setColor(QPalette.ColorRole.Button, base_color)
    palette.setColor(QPalette.ColorRole.ButtonText, text_color)
    palette.setColor(QPalette.ColorRole.ToolTipBase, base_color)
    palette.setColor(QPalette.ColorRole.ToolTipText, text_color)
    palette.setColor(QPalette.ColorRole.PlaceholderText, muted_color)
    palette.setColor(QPalette.ColorRole.Highlight, accent_color)
    palette.setColor(QPalette.ColorRole.HighlightedText, accent_text_color)

    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, muted_color)

    return palette


def _apply_theme(app: QApplication) -> None:
    """
    Switch the app's palette and stylesheet to match the current OS color scheme.
    Uses QStyleHints instead of platform-specific registry/plist reads.
    """

    is_dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    tokens = THEME_TOKENS["dark" if is_dark else "light"]
    app.setPalette(_palette_from_tokens(tokens))
    substitutions = {**tokens, **_font_size_tokens_from_system_font(app)}
    app.setStyleSheet(Template(THEME_TEMPLATE_PATH.read_text()).substitute(substitutions))


def _show_in_macos_dock(icon_data: bytes) -> None:
    """Give the regular macOS application a Dock and app-switcher icon."""

    if sys.platform != "darwin":
        return

    from AppKit import NSApplication, NSApplicationActivationPolicyRegular, NSImage
    from Foundation import NSData

    ns_image = NSImage.alloc().initWithData_(
        NSData.dataWithBytes_length_(icon_data, len(icon_data))
    )
    application = NSApplication.sharedApplication()
    application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    application.setApplicationIconImage_(ns_image)


def _set_macos_process_name() -> None:
    """Override the Python interpreter name before Qt creates its native menus."""

    if sys.platform != "darwin":
        return

    from Foundation import NSProcessInfo

    NSProcessInfo.processInfo().setProcessName_(APP_NAME)


def _set_macos_menu_bar_name() -> None:
    """Name the native application menu once the first window has been shown."""

    if sys.platform != "darwin":
        return

    from AppKit import NSApplication

    menu = NSApplication.sharedApplication().mainMenu()
    if menu is not None and menu.numberOfItems():
        menu.itemAtIndex_(0).setTitle_(APP_NAME)


def _set_application_icon(app: QApplication) -> None:
    """Use the bundled circular wordmark for app windows and the macOS Dock."""

    icon_data = APP_ICON_RESOURCE.read_bytes()
    pixmap = QPixmap()
    if not pixmap.loadFromData(icon_data, "PNG"):
        raise RuntimeError("Could not load the bundled application icon.")
    app.setWindowIcon(QIcon(pixmap))
    _show_in_macos_dock(icon_data)


def _usage(program_name: str) -> str:
    program_name = os.path.basename(program_name)

    return f"""Usage: {program_name} [OPTIONS] [QT_OPTIONS]

Start the iRODS Client System Tray application.

By default, the command starts the tray app in the background and returns the
shell prompt. Any unrecognized options are passed through to Qt.

Options:
  -f, --foreground  Keep the app attached to the terminal.
  -h, --help        Show this help message and exit.

Environment:
  {FOREGROUND_ENV_VAR}=1  Keep the app attached to the terminal.
"""


def _parse_launcher_args(argv: list[str]) -> tuple[bool, bool, list[str]]:
    """Separate launcher options from arguments that should be passed to Qt."""

    foreground = False
    help_requested = False
    qt_args = [argv[0]]
    for arg in argv[1:]:
        if arg in ("-f", "--foreground"):
            foreground = True
            continue
        if arg in HELP_OPTIONS:
            help_requested = True
            continue
        qt_args.append(arg)

    return foreground, help_requested, qt_args


def _run_in_background_if_needed(foreground: bool, qt_args: list[str]) -> bool:
    """Start the tray app detached and let the command-line launcher exit."""

    if (
        foreground
        or os.environ.get(BACKGROUND_ENV_VAR)
        or os.environ.get(FOREGROUND_ENV_VAR)
    ):
        return False

    env = os.environ.copy()
    env[BACKGROUND_ENV_VAR] = "1"
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }

    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True

    subprocess.Popen(
        [sys.executable, "-m", "irods_client_system_tray", *qt_args[1:]],
        **popen_kwargs,
    )
    return True


def main() -> int:
    """Create the Qt application, validate tray support, and start the event loop.

    The tray controller owns the persistent background services and the settings
    window, so the entry point only needs to bootstrap Qt and hand off control.
    """

    foreground, help_requested, qt_args = _parse_launcher_args(sys.argv)

    if help_requested:
        print(_usage(qt_args[0]))
        return 0

    if _run_in_background_if_needed(foreground, qt_args):
        return 0

    _set_macos_process_name()
    app = QApplication(qt_args)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("iRODS")
    app.setStyle("Fusion")
    _set_application_icon(app)
    _apply_theme(app)
    QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _scheme: _apply_theme(app))

    if not QSystemTrayIcon.isSystemTrayAvailable():
        raise SystemExit("System tray is not available in this environment.")

    controller = TrayController(app)
    QTimer.singleShot(0, lambda: controller.prompt_login(show_window_on_success=True))
    if sys.platform == "darwin":
        QTimer.singleShot(0, _set_macos_menu_bar_name)

    signal.signal(signal.SIGINT, lambda *_args: app.quit())

    interrupt_timer = QTimer()
    interrupt_timer.timeout.connect(lambda: None)
    interrupt_timer.start(200)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
