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

1. **Wrong access code.** Check `PRINTER_ACCESS_CODE` matches the code in
   **Settings → Network** on the printer touchscreen.

2. **Wrong IP.** Same as above.

3. **Printer MQTT broker not running.** This should not happen under normal operation.
   Power-cycle the printer.

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
