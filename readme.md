## System Tray Ingestion App

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the application:

```bash
python main.py
```

The app persists monitored folders and the global monitoring toggle in `app_state.json`.
When background monitoring is enabled it also watches each monitored folder's parent
directory so it can detect folder renames and moved-away folders.

The iRODS client session is stored separately in `irods_environment.json` and can be
edited from the settings window. Install `python-irodsclient` from `requirements.txt`
to enable background uploads. Install `win11toast` from `requirements.txt` to receive
Windows notifications when a monitored folder is moved and can no longer be tracked.
Install `send2trash` from `requirements.txt` to enable delete-after-upload cleanup.
