# iRODS Client - System Tray

This system tray application monitors a set of configured local directories and uploads files to the configured iRODS Collections.

The primary use case is the automatic, hands-free ingest of incoming instrument data (e.g. images) into an iRODS Zone without filling up available storage on the client machine attached to the instrument.

This Python application is designed for use on Windows and MacOS. Linux desktop
installs are supported when the operating system provides Qt's native graphics and
system tray integration libraries.

## Install

Install dependencies:

```bash
pip install irods-client-system-tray
```
or

```bash
pip install ./
```

On Ubuntu, pip installs the Python packages, but Qt still needs native graphics and
desktop integration libraries from apt. On a minimal Ubuntu install or CI image, install:

```bash
sudo apt update
sudo apt install libdbus-1-3 libegl1 libgl1 libglib2.0-0 libxkbcommon0 \
    libxcb-cursor0 libxkbcommon-x11-0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-xinerama0
```

GNOME sessions also need system tray/AppIndicator support. If the tray icon is not
available, install or enable the distribution's AppIndicator/KStatusNotifierItem
extension, for example `gnome-shell-extension-appindicator` on Ubuntu GNOME.

## Run

Run the application:

```bash
irods-client-system-tray
```

For a source checkout, this also works:

```bash
python -m irods_client_system_tray
```

The application stores user settings outside the installed package:
`%APPDATA%/irods-client-system-tray` on Windows,
`~/Library/Application Support/irods-client-system-tray` on macOS, and
`$XDG_CONFIG_HOME/irods-client-system-tray` or
`~/.config/irods-client-system-tray` on Linux.

## Testing

Run the tests:

```bash
pytest tests/
```

The tests stub the iRODS session and need no server. Tests that upload to a live zone
are skipped unless `IRODS_TEST_LIVE_SERVER` is set; export that along with the other
`IRODS_TEST_*` variables listed in `tests/conftest.py` to run them against a real
deployment.

Run the tests with coverage:

```bash
pytest --cov=irods_client_system_tray
```
