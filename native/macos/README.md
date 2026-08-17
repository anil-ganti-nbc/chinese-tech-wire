# Chinese Tech Wire macOS field test

The Finder-launchable app reuses CTW's canonical newsroom dashboard. Read-only
configuration, templates, and static assets are bundled. Mutable local state is
isolated beneath `~/Library/Application Support/Chinese Tech Wire/`:

- `ctw.db` contains newsroom data, feedback, outcomes, and translation cache;
- `data/dashboard_runtime.json` tracks the live local dashboard;
- `logs/dashboard-launcher.log` records launcher lifecycle events;
- `field-test.env` is the optional local-only environment file.

The app binds only to loopback, waits for `/healthz` before opening a browser,
does not start collectors automatically, and disables the dashboard's manual
collector subprocess in the packaged field-test environment. No secrets or
database files are bundled.

Build from the repository root:

```bash
PYTHON="$(pwd)/.venv/bin/python" native/macos/build.sh
```
