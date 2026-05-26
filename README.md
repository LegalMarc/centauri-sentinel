# centauri-sentinel

Self-hosted failure detection and remote control for the **Elegoo Centauri Carbon 2** FDM printer.
Watches the camera feed with the [Obico](https://github.com/TheSpaghettiDetective/obico-server) ML model,
pauses the printer on confirmed spaghetti, and alerts via Telegram and/or ntfy.

---

## What it does

centauri-sentinel connects to your printer over LAN, grabs frames from its MJPEG camera every
10 seconds, and scores them against the Obico failure-detection model. When a set number of
consecutive frames all exceed the confidence threshold, it pauses the print, sends you an alert
with a snapshot, and waits for your decision. A Telegram bot lets you resume or abort the print
from your phone. A local web dashboard shows live camera feed, watcher state, and detection
history.

---

## Hardware

- **Printer:** Elegoo Centauri Carbon 2 (Carbon 1 not supported — different MQTT API)
- **Host:** any Docker-capable Linux host on the same LAN as the printer (amd64 or arm64)
- **Tested firmware:** Centauri Carbon 2 ≥ 1.x (MQTT broker at port 1883)

---

## Deployment baseline — LAN trust

> **Security assumption:** the Coolify host and the printer share a trusted LAN.
>
> The status dashboard and camera stream are **not authenticated by default**.
> Do **not** expose port 8000 to the public internet without enabling auth (`AUTH_USERNAME` +
> `AUTH_PASSWORD_BCRYPT`) **and** placing the service behind a TLS-terminating reverse proxy.
>
> The startup guard (`EXTERNAL_BIND_ALLOWED=false`) refuses to start if the host is reachable
> externally and auth is not configured — but this guard is host-binding heuristic only.
> See [docs/threat-model.md](docs/threat-model.md) for the full analysis.

---

## Quick start (Coolify)

See **[docs/coolify-deploy.md](docs/coolify-deploy.md)** for the step-by-step guide.

Short version:

1. In Coolify → **New Resource** → **Docker Compose** → paste this repo URL.
2. Add env var `PRINTER_IP=<your printer's LAN IP>`.
3. Add env var `PRINTER_ACCESS_CODE=<access code from printer Settings → Network>`.
4. Click **Deploy**. All three services become healthy in under 90 s.

The dashboard is at the URL Coolify assigns (e.g. `https://<uuid>.your-domain.com`).

---

## Configuration reference

Only `PRINTER_IP` is required. Everything else has a sane default.

### Printer

| Variable | Default | Purpose |
|---|---|---|
| `PRINTER_IP` | **required** | LAN IP of the Centauri Carbon 2 |
| `PRINTER_ACCESS_CODE` | `123456` | MQTT broker password — find it in the printer's Settings → Network |
| `PRINTER_MQTT_PORT` | `1883` | MQTT broker port |
| `PRINTER_MJPEG_PORT` | `8080` | Camera stream port |
| `PRINTER_MJPEG_PATH` | `/mjpeg` | Camera stream path |

### ML detection

| Variable | Default | Purpose |
|---|---|---|
| `ML_API_URL` | `http://obico-ml:3333` | Internal URL of the Obico ML container |
| `ML_API_TOKEN_FILE` | `/shared/token` | Path to the shared auth token (written by `token-init`) |
| `ML_CONFIRM_COUNT` | `3` | Consecutive positive frames before triggering a pause |
| `ML_POLL_INTERVAL_SECONDS` | `10` | Seconds between frame grabs |
| `ML_SCORE_THRESHOLD` | `0.4` | Per-frame confidence threshold (0.0 – 1.0) |

### Detection behaviour

| Variable | Default | Purpose |
|---|---|---|
| `DETECTION_WARMUP_SECONDS` | `300` | Seconds after print start before arming (skips first-layer purge) |
| `DETECTION_ENABLED_DEFAULT` | `true` | Initial detection state — can be toggled at runtime via bot |
| `WATCHER_STALL_SECONDS` | `60` | Heartbeat age that triggers a stall alert |

### Telegram (optional)

Disabled if `TELEGRAM_BOT_TOKEN` is unset.

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Chat to send alerts to |
| `TELEGRAM_USER_IDS` | — | Comma-separated list of authorised user IDs |

See [Telegram setup](#telegram-setup) below.

### ntfy (optional)

Disabled if `NTFY_URL` is unset.

| Variable | Default | Purpose |
|---|---|---|
| `NTFY_URL` | — | Topic URL, e.g. `https://ntfy.sh/your-long-random-topic` |
| `NTFY_TOKEN` | — | Bearer token for self-hosted ntfy with auth |

See [ntfy setup](#ntfy-setup) below.

### Dashboard auth (optional)

Disabled if `AUTH_USERNAME` is unset.

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_USERNAME` | — | HTTP Basic Auth username |
| `AUTH_PASSWORD` | — | Plaintext password — hashed with bcrypt at startup (use instead of `AUTH_PASSWORD_BCRYPT` for convenience) |
| `AUTH_PASSWORD_BCRYPT` | — | Pre-computed bcrypt hash — takes precedence over `AUTH_PASSWORD` |

Generate a pre-computed hash:
```sh
python3 -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
```

### Web server

| Variable | Default | Purpose |
|---|---|---|
| `BIND_HOST` | `0.0.0.0` | Address to bind on |
| `BIND_PORT` | `8000` | Port to bind on (inside the container) |
| `SENTINEL_PORT` | `8000` | Host-side port published by Docker Compose |
| `EXTERNAL_BIND_ALLOWED` | `false` | Set `true` only with auth + TLS at reverse proxy |

### Misc

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `DB_PATH` | `/data/sentinel.db` | SQLite database path (should be on a named volume) |

---

## Telegram setup

1. Open [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`. Follow the prompts.
   Copy the **bot token** — this is `TELEGRAM_BOT_TOKEN`.

2. **Start a chat with your bot first.** Open Telegram, search for your bot by its username,
   and send `/start`. Without this step `getUpdates` will return an empty list and you cannot
   retrieve your chat ID.

3. Find your **chat ID**:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":...}` in the response. A personal chat gives a positive integer;
   a group gives a negative integer. This is `TELEGRAM_CHAT_ID`.

4. Find your **user ID**: look for `"from":{"id":...}` in the same response. This is your entry
   in `TELEGRAM_USER_IDS`. Add more users by comma-separating their IDs.

**Security notes:**
- centauri-sentinel enforces an allowlist on both chat ID and user ID. Messages from unknown
  chats or users are silently ignored.
- The `/stop` command requires a `/confirm` within 30 seconds to prevent accidental pauses.

**Available bot commands:**

| Command | Description |
|---|---|
| `/status` | Current watcher state and last heartbeat |
| `/snapshot` | Camera snapshot |
| `/pause` | Pause the print immediately |
| `/resume` | Resume after a pause |
| `/stop` | Initiate a stop (requires `/confirm` within 30 s) |
| `/confirm` | Confirm a pending `/stop` |
| `/enable` | Re-enable failure detection |
| `/disable` | Disable detection (or use the snooze button in alert messages) |
| `/help` | List available commands |

---

## ntfy setup

[ntfy](https://ntfy.sh) is an open-source push notification service.

**Using ntfy.sh (public):**
- Create a long, random topic name (treat it like a secret — anyone who knows it can read alerts):
  ```
  NTFY_URL=https://ntfy.sh/my-long-random-secret-topic-abc123
  ```
- Subscribe with the ntfy app on iOS or Android.
- Caveat: public ntfy.sh has rate limits and your topic is technically public knowledge if
  the URL leaks from your environment.

**Recommended: self-hosted ntfy on LAN:**
- Run ntfy alongside your Coolify stack. Set access control and a bearer token:
  ```
  NTFY_URL=https://ntfy.your-domain.com/centauri-alerts
  NTFY_TOKEN=your-secret-token
  ```

---

## Threat model summary

| Surface | Who can reach it | Mitigation |
|---|---|---|
| Status dashboard / snapshot / stream | LAN devices (default) | Optional Basic Auth + session cookie. Safety guard refuses external bind without auth. |
| Telegram bot | Anyone on Telegram | Allowlist by chat ID + user ID. `/stop` requires `/confirm`. |
| ntfy alerts | Anyone who knows the topic URL | Use a long random topic; self-hosted ntfy recommended. |
| Obico ML API | Internal Docker network only | Auth token in shared volume; no host ports; `internal: true` network. |
| Printer MQTT (port 1883) | LAN | Access code auth; every call has timeout + retry. |
| SQLite database | Host root | Self-hosted norm; back up the `sentinel-data` volume. |

See [docs/threat-model.md](docs/threat-model.md) for the full analysis.

---

## Updating

Coolify watches the `main` branch for new commits. Either push to `main` (Coolify auto-deploys
if webhook is configured) or click **Redeploy** in the Coolify UI.

The ML token is stored in the `ml-token` Docker volume and is **not** regenerated on redeploy.
To rotate it, delete the volume and redeploy:
```sh
docker volume rm <stack_prefix>_ml-token
```

---

## Backup

The only stateful volume is `sentinel-data` (SQLite DB + snapshots). Back it up by copying:
```
/var/lib/docker/volumes/<stack_prefix>_sentinel-data
```

---

## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Contributing

Pull requests welcome. Run the test suite before opening a PR:
```sh
uv run ruff check sentinel/ tests/
uv run mypy --strict sentinel/
uv run pytest --cov=sentinel --cov-fail-under=85
```

---

## License

MIT — see [LICENSE](LICENSE).
