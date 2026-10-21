# Troubleshooting

---

## Stack does not become healthy within 90 seconds

Check container logs in Coolify (Resources → your app → Logs) or run:

```sh
docker compose logs --tail=50
```

The most common causes are listed below.

---

## obico-ml fails to start or stays unhealthy

**Symptom:** `obico-ml` never passes its healthcheck. Logs show a Python import error or
`start.sh: not found`.

**Cause:** The Dockerfile performs a sparse clone of the obico-server repo at build time.
If the build ran without internet access, the `ml_api/` directory is empty.

**Fix:** Ensure the Coolify host has internet access during the initial image build.
If you have a pre-built image, set `image:` directly in `docker-compose.yml` instead of
using `build:`.

---

## ML token mismatch (`401 Unauthorized` from obico-ml)

**Symptom:** `sentinel` logs show `MLClientError: 401` repeatedly.

**Cause:** The `ml-token` volume was partially initialised or contains a stale token.

**Fix:**
```sh
# On the Coolify host
docker volume rm <stack_prefix>_ml-token
# Then redeploy the stack in Coolify UI (or via API)
```

The `token-init` container will regenerate the token on the next deploy.

---

## MJPEG camera unreachable

**Symptom:** Watcher state shows `CAMERA_OFFLINE`. Logs: `CameraOfflineError` or
`Connection refused` connecting to `<PRINTER_IP>:8080`.

**Causes and fixes:**

1. **Wrong IP.** Verify `PRINTER_IP` matches the printer's current LAN IP. DHCP leases can
   change. Set a static IP on the printer or create a DHCP reservation on your router.

2. **Wrong port/path.** Defaults are port `8080`, path `/mjpeg`. If your firmware uses a
   different path, set `PRINTER_MJPEG_PORT` and `PRINTER_MJPEG_PATH`.

3. **Printer is off or idle.** The MJPEG camera is only active when the printer's screen is
   on. Wake the printer and try again.

4. **Firewall.** Check that port 8080 TCP is allowed from the Coolify host to the printer.

---

## Printer MQTT connection refused

**Symptom:** Logs show `MQTTConnectionError` or `Connection refused` on port 1883.

**Causes and fixes:**

1. **Wrong access code.** The log shows CRITICAL `MQTT permanent authentication failure` and
   the listener stops retrying. Check `PRINTER_ACCESS_CODE` matches the code in
   **Settings → Network** on the printer touchscreen.

2. **Wrong IP.** Same as above.

3. **Printer MQTT broker not running.** This should not happen under normal operation.
   Power-cycle the printer.

---

## Pause/resume/stop do nothing (status + detection still work)

**Symptom:** Detection fires and notifications are sent, but the printer never
actually pauses — neither the UI **Pause** button nor auto-pause on detection.
Logs show a tight loop of `CONFIRMED DETECTION … pausing printer` immediately
followed by `Printer resumed externally — transitioning ARMED`, with the printer
never reaching a `paused` state.

**Cause:** On **firmware 02.x**, the printer only honours `api_request` commands
from a client that has completed the `api_register` handshake; unregistered
commands are silently dropped. Status pushes (`api_status`) are broadcast and
need no registration, so detection keeps working while every pause is a no-op.
(A second, older bug used the wrong command codes entirely — `1001/1002/1003`
are read queries, not pause/resume/stop.)

**Fix:** Handled in code as of 2026-07-18 — the command path now registers,
sends the correct codes (pause `1021`, stop `1022`, resume `1023`) with the
`{"id","method","params"}` envelope, and confirms the printer's ack. A command
the printer does not acknowledge now raises (surfaced as a UI 500 / a
"pause failed" notification) instead of silently succeeding. See
`docs/verified-assumptions.md` and `sentinel/printer/client.py`.

**Verify on the hardware** (from any host on the printer's LAN, with the
same `.env`) while a print is running:

```bash
python -m sentinel printer-cmd pause
```

Exit 0 means register → command → ack → printer reports `paused` all worked;
run `printer-cmd resume` afterwards. Exit 1 with `FAIL: PrinterTimeoutError`
means no registration/ack (wrong access code, wrong IP, or the printer's
client slots are full). Exit 1 with "accepted but not honoured" means the
firmware acked but ignored the command.

If it recurs after a future firmware update, capture the real exchange by
subscribing to `elegoo/#` on the printer's LAN while pausing from the official
Elegoo app, and compare the command envelope / method codes.

Note that a failed auto-pause now alerts once per detection episode and then
retries the pause silently on every poll tick (the `pause_history` table
records every attempt); alerts resume when the score drops and a new
detection is confirmed.

---

## MQTT client slots exhausted (`too many clients`)

**Symptom:** Pause, resume or stop fail with `registration rejected: too many clients
connected to the printer`, while status and detection still work. The printer
touchscreen or the official app may also refuse new connections.

**Cause (suspected, not yet measured):** every command opens a short MQTT session and
registers with a fresh `client_id`/`request_id` (`PrinterClient._send_command`). If the
firmware keeps each registration for minutes after the session closes, a burst of commands
fills its client table and later registrations are refused until the old ones expire.

**Measure it on the real printer.** Open the Coolify terminal on the `sentinel` service
(the container has the printer's `PRINTER_IP` and `PRINTER_ACCESS_CODE`) and run, **while
the printer is idle**. Like the service, it uses the address saved in Settings > Printer
over `PRINTER_IP`, and prints the address it dials first:

```bash
python -m sentinel.diagnostics mqtt-slots
python -m sentinel.diagnostics mqtt-slots --max 20 --expiry-wait 900
```

The tool registers only. It never sends pause, resume, stop or any print command, but it
does occupy client slots until they expire, which makes the printer refuse Sentinel's
pause/stop commands in the meantime. It therefore **runs only when the printer reports a
known idle state** (`standby`, `complete`, `cancelled` or `error`). Printing, paused, an empty
or unrecognised state, or no state at all counts as busy: it prints `REFUSED: ...`
and a JSON line with `"refused": true` and the `print_state`, registers nothing, and exits 2.
Pass `--force` to run anyway (for example to reproduce the refusal during a print), accepting
that Sentinel cannot pause that print until the slots expire. It prints a warning, a human
summary and a final JSON line. In order, it:

1. learns the printer serial and print state from the status topic, as the client does, and
   refuses here if a print is active (unless `--force`);
2. registers one id pair twice on two separate sessions and reports whether the repeat is
   accepted (if yes, the client could reuse one id instead of consuming a slot per command);
3. registers up to `--max` (default 12) fresh id pairs, each on its own short session that
   disconnects after the response, and stops at the first refusal;
4. with `--expiry-wait SECONDS` and a refusal, retries a fresh registration every 30 s
   until one succeeds or the wait ends, and reports how long that took (resolution 30 s).
   Before each retry it re-reads the print state and stops early, reporting why in the
   summary and in `expiry.stopped_reason`, if a print has started or the state cannot be
   re-read (`--force` skips the re-check).

Each registration is classified as `ok`, `too many clients`, `fail` (any other `error`
value, raw value in `detail`), `timeout` (no `register_response`), `connect_error` (MQTT
connect or socket failure) or `protocol_error` (the `register_response` was malformed, not
a JSON object, or the connection closed before it arrived).

Exit 0 means the measurement ran. Exit 1 means it could not (printer unreachable, wrong
access code, no status push); the JSON line then holds an `error` field. Exit 2 means it
refused because a print is active (see above). Settings come from
the environment, so a printer IP changed from the dashboard (stored in the database) is not
picked up: set `PRINTER_IP` to match.

---

## Watcher appears stalled (`WATCHER_STALL_SECONDS` alert fires)

**Symptom:** You receive a "watcher stalled" notification. Watcher state is not updating.

**Cause:** The watcher loop crashed or deadlocked. This is a bug — please open an issue.

**Immediate fix:** Redeploy or restart the `sentinel` container:
```sh
docker compose restart sentinel
```

The watcher resumes from the last known state in the database.

---

## Telegram auth refused (bot ignores commands)

**Symptom:** You send `/status` to the bot and nothing happens.

**Causes:**

1. **User ID not in allowlist.** Verify your Telegram user ID is in `TELEGRAM_USER_IDS`.
   Find it by calling `https://api.telegram.org/bot<TOKEN>/getUpdates` and looking for
   `"from":{"id":...}`.

2. **Chat ID mismatch.** `TELEGRAM_CHAT_ID` must match the chat where you are sending
   commands. Group chats have negative IDs; personal chats have positive IDs.

3. **Bot not started.** Send `/start` to the bot first to register the chat.

---

## Dashboard shows blank camera image

**Symptom:** The status page loads but the `<img>` shows a broken image.

**Cause:** The `/snapshot` endpoint returned an error because the camera is offline.

**Fix:** See [MJPEG camera unreachable](#mjpeg-camera-unreachable) above.

**Note:** The "Camera Connected/Offline" badge reflects Sentinel's own MJPEG probe
(the same check `/readyz` uses), not the printer's self-reported MQTT
`external_device.camera` flag. MQTT status and the MJPEG HTTP stream are separate
connections, so it's possible for one to work while the other doesn't — e.g. a
third-party viewer (like the Elegoo slicer) can show a live feed even while the
printer hasn't (yet) reported a camera client to Sentinel over MQTT, or vice versa.

---

## `EXTERNAL_BIND_ALLOWED` safety guard fires

**Symptom:** `sentinel` exits at startup with a message like:
```
ConfigurationError: EXTERNAL_BIND_ALLOWED is false but BIND_HOST is not 127.0.0.1 ...
```

**Cause:** You set `BIND_HOST=0.0.0.0` (or any non-loopback address) without configuring
auth, and `EXTERNAL_BIND_ALLOWED` is `false` (the default).

**Fix (preferred):** Set `AUTH_USERNAME` and `AUTH_PASSWORD_BCRYPT`, then place the service
behind a TLS reverse proxy. Then set `EXTERNAL_BIND_ALLOWED=true`.

**Fix (LAN-only, no auth):** If the host is truly LAN-only and you accept the risk, set
`EXTERNAL_BIND_ALLOWED=true` without auth. Read [docs/threat-model.md](threat-model.md) first.

---

## Database is missing detections after redeploy

**Symptom:** The detection history and pause history are empty after redeploying the stack.

**Cause:** The `sentinel-data` volume was deleted, or the `DB_PATH` env var changed.

**Fix:** Do not delete the `sentinel-data` volume unless you intend to start fresh.
The volume persists across redeployments automatically.

To recover a backup:
```sh
docker volume create <stack_prefix>_sentinel-data
# Copy your backup into the volume mountpoint on the host
```

---

## ntfy notifications fail to display snapshots due to NAT / firewall

**Symptom:** Notifications are received on the ntfy client, but they do not contain the camera snapshot images.

**Details:** When `NTFY_SEND_SNAPSHOTS` is enabled, the notifier does not send an image URL to the ntfy server (which would require the ntfy server to make an inbound request back to the printer/sentinel, failing due to NAT, firewall, or lack of external traversal). Instead, `centauri-sentinel` performs a direct POST body upload where the raw JPEG snapshot bytes are sent directly in the HTTP request body, and the textual message is sent in the `X-Message` header.

**Resolution / Verification:**
- Ensure that `NTFY_SEND_SNAPSHOTS` is set to `true` (or `true` equivalent).
- Ensure your ntfy server version supports attachments and HTTP headers (standard in all recent versions).
- Check that the client has a working internet connection to the ntfy server to upload the image directly.
