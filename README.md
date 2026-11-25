# Receipt Ledger

A single-user receipt uploader for a private home network. Images stay on this machine after saving. When you ask for extraction, the uploaded image is sent to the configured OpenAI-compatible vision endpoint; check and correct the result before saving.

## Start

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set a strong `APP_PASSWORD` and `LLM_API_KEY`. Then launch the app with:

```sh
./run.sh
```

The script uses the project's `.venv` and listens on `0.0.0.0:8000` by default. Override the address or port with `APP_HOST` and `APP_PORT`.

Open `http://<this-computer>:8000` from a device on your home network. Do not forward the port to the public internet. For use over HTTPS, set `COOKIE_SECURE=true` when your trusted TLS proxy terminates HTTPS.

## Service management (systemd)

The app runs as a systemd user service named `receipts`. Its unit file is at `~/.config/systemd/user/receipts.service`:

```
[Unit]
Description=Receipt Ledger web app

[Service]
Type=simple
WorkingDirectory=/opt/apps/receipts
EnvironmentFile=-/opt/apps/receipts/.env
ExecStart=/opt/apps/receipts/.venv/bin/uvicorn main:app --app-dir /opt/apps/receipts --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`EnvironmentFile=-` points at the app's `.env`; the leading `-` means a missing file is ignored. `ExecStart` is equivalent to running `./run.sh`. The unit is enabled at login via `~/.config/systemd/user/default.target.wants/receipts.service`.

Useful commands (user units):

```sh
systemctl --user start receipts
systemctl --user stop receipts
systemctl --user restart receipts
systemctl --user status receipts
systemctl --user daemon-reload    # after editing the unit file

journalctl --user -u receipts           # logs
journalctl --user -u receipts --follow  # live logs
```

Enable or disable automatic start:

```sh
systemctl --user enable receipts
systemctl --user disable receipts
```

Summary of the app's environment file (`/opt/apps/receipts/.env`), with sensitive values obfuscated:

- `APP_PASSWORD=<redacted>` — required by the app.
- `LLM_BASE_URL=https://opencode.ai/inference/openai/v1/chat/completions`
- `LLM_MODEL=gpt-6-luna`
- `LLM_API_KEY=<redacted>` — the `.env` comment notes it expires on 11/25/2026.
- `DATA_DIR=./data`
- `COOKIE_SECURE=false`

## Settings

Process environment variables take precedence over `.env` values. `APP_PASSWORD` is required. `LLM_BASE_URL` defaults to `https://api.openai.com/v1`, `LLM_MODEL` defaults to `gpt-5.4-mini`, and `LLM_API_KEY` is optional for compatible local endpoints that do not require a key. The default `DATA_DIR` is `./data`; its database, image originals, and temporary uploads are excluded from version control.

Images must be JPEG (including MPO multi-picture JPEG), PNG, or WebP and at most 15 MB. MPO uploads are kept unchanged; extraction uses a standard JPEG copy of the first image. Abandoned uploads are removed after 24 hours. Saved images are retained byte-for-byte and named from the receipt's printed date and time, followed by the uploaded filename stem. Receipt time uses 24-hour `HH:MM` or `HH:MM:SS`; missing date or time values are left out rather than replaced with the upload time. A numeric suffix is added if a name already exists. An uploaded filename stem of `image` is archived as `receipt`. Receipt summary fields include subtotal, tax, total, payment, and change. The change field also holds an amount labeled cash back on the receipt, so the amount is stored once. Explicit cash-back lines become a `Cash back` marker item with no amount in its description. Item descriptions and categories are reviewed before saving. CSV exports contain receipt summary fields plus `item_description`, `item_category`, `archived_filename`, and the uploaded `original_filename`, with one row per item and receipt-level fields repeated; receipts without items get one row with blank item columns.

## Mobile and installable app

The page offers separate camera and photo-library actions on mobile browsers. The app shell has a web manifest and service worker. Browsers generally require HTTPS to install a PWA from a LAN address; `localhost` is also treated as secure. Uploaded receipt images and API responses are never cached by the service worker.

## Data and privacy

SQLite is the source of truth. Keep the `DATA_DIR` private and include it in your own encrypted backups if desired. The app has no multi-user support. The password protects the UI, API, and downloads. During extraction the configured LLM endpoint receives the image and the prompt; choose an endpoint you trust. The app does not log receipt contents.

## Tests

Install development dependencies and run the mocked test suite with:

```sh
pip install -r requirements-dev.txt
python -m pytest
```

The tests replace the vision endpoint with local mocks; they do not send receipt images to a provider.

## Reset application storage

Stop the running app and run:

```sh
./.venv/bin/python manage.py initialize
```

The command removes everything under `DATA_DIR`, including the database, saved originals, staged uploads, exports, and other files, then creates a fresh database and empty storage folders. It asks you to type `INITIALIZE` before proceeding. The `.env` file and the app lock are kept. A storage lock prevents running this command while the web app is active. The web app has no reset control or endpoint.

## License

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
