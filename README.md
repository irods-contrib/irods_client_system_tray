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

The command starts the tray app in the background and returns the shell prompt.
Pass `--foreground` or `-f`, or set `IRODS_CLIENT_SYSTEM_TRAY_FOREGROUND=1`,
to keep it attached to the terminal.
On macOS the running app also appears in the Dock and app switcher, even when
started from the terminal in the background.
The same round iRODS wordmark appears in the app window, Dock, and tray/menu bar.

For a source checkout, this also works:

```bash
python -m irods_client_system_tray
```

The application stores user settings outside the installed package:
`%APPDATA%/irods-client-system-tray` on Windows,
`~/Library/Application Support/irods-client-system-tray` on macOS, and
`$XDG_CONFIG_HOME/irods-client-system-tray` or
`~/.config/irods-client-system-tray` on Linux.

After upload, files can be sent to trash, deleted, or moved outside monitored
folders. Saved folders using the old "recycle" setting are migrated to "trash".

Adding a monitored folder queues its existing files, including files in subfolders,
for upload according to its regex filter. If monitoring is paused, the scan waits
until monitoring resumes.
At startup, saved monitored folders are also scanned once after login so files
added while the app was closed are picked up.

The Settings tab controls retry behavior and the maximum number of files uploaded
at once. Upload workers default to two, capped at the machine's logical CPU count.
This limit is separate from iRODS's parallel threads for a single large file.
On first launch, Automatic mode starts at two workers by default. It cautiously
adjusts the limit from recent transfer throughput when files are waiting, up to
the machine's CPU count and a configurable memory cap. The single checkbox
controls both worker and write sizes; their disabled settings show the current
values. Turning Automatic off retains those values as the editable Manual
settings. The memory cap is editable only in Automatic mode.
The main window header shows the active worker limit, waiting queue, and recent
aggregate throughput. The effective worker count and write sizes are saved on exit
and restored on restart, whether Automatic or Manual is selected; tuning measurements
themselves are not saved.
Write sizes can also be set manually or tuned automatically for single-threaded
and parallel PUTs separately (4, 8, or 16 MiB). Automatic worker and write-size
trials do not run together. Parallel writes use a client-wide setting, so new
uploads briefly wait while active transfers finish before it changes. The tuner
uses a configurable budget (default 256 MiB) for workers × parallel threads × write size.
The Queue line shows remaining bytes and estimates time from active and waiting
files at the recent throughput. It shows `ETA --` until a usable speed is available.
Throughput drops to zero after five seconds without transfer activity.

The About tab summarizes the app and shows the installed package version, publisher,
license, and project links. It checks PyPI for newer compatible stable releases shortly
after launch and daily thereafter without blocking uploads. When one is available, it
shows the version and a link to PyPI; it never upgrades the app automatically.
Set `IRODS_CLIENT_SYSTEM_TRAY_DISABLE_UPDATE_CHECK=1` to disable network checks.

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
