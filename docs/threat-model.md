# Threat model

centauri-sentinel is designed for deployment on a **trusted LAN** alongside the printer.
This document describes the attack surface, the mitigations in place, and the steps required
to raise the security bar when exposing the service externally.

---

## Design baseline

The Coolify host and the printer share a trusted LAN. Threat actors on the LAN are not in scope
for v0.1. This is stated explicitly so operators who deviate from the baseline know they are
operating outside the design envelope.

---

## Attack surface

### Status dashboard (`/`, `/snapshot`, `/stream`)

**Reachable by:** Any device that can reach the host on port 8000.

**Default configuration:** No authentication. Any LAN device can view the status page, camera
snapshot, and camera stream.

**Mitigation (optional):** Set `AUTH_USERNAME` and `AUTH_PASSWORD_BCRYPT` to enable HTTP Basic
Auth. The service issues an HMAC-SHA256 signed session cookie (1-hour TTL) on successful login,
so the password is sent only once per session. The cookie carries `SameSite=Strict; HttpOnly`.
When `EXTERNAL_BIND_ALLOWED=true` the `Secure` flag is added automatically so the cookie is
only transmitted over HTTPS.

**Safety guard:** If `EXTERNAL_BIND_ALLOWED` is `false` (default) and the service detects it
is reachable on a non-loopback interface without auth configured, it refuses to start. This
guard is a heuristic based on the bind address — it is not a substitute for correct network
configuration.

### Telegram bot

**Reachable by:** Anyone on Telegram.

**Mitigations:**
- Allowlist enforced on **both** `chat_id` and `user_id`. Messages from unknown chats or users
  are silently ignored.
- Destructive commands (`/stop`) require a `/confirm` within a 30-second window, preventing
  accidental or replayed triggers.
- The bot token is a secret — treat it like a password. Rotate via @BotFather if compromised.

### ntfy alerts

**Reachable by:** Anyone who knows the topic URL.

**Mitigations:**
- Treat the topic URL as a secret. A long, random topic name provides obscurity.
- For stronger guarantees, run a self-hosted ntfy instance on the LAN and set `NTFY_TOKEN`.

### Obico ML API (`obico-ml` container)

**Reachable by:** Internal Docker network only.

**Mitigations:**
- The `internal: true` Docker network prevents egress from `obico-ml`. It has no host ports.
- An auth token is generated once by `token-init` and stored in the `ml-token` shared volume.
  `sentinel` sends the token in the `Authorization` header on every request.
- Even with `ML_API_TOKEN=""` (current default, since auth is enforced by the network boundary),
  an attacker who somehow reaches the container cannot reach the wider internet from it.

### Printer MQTT (port 1883)

**Reachable by:** LAN devices.

**Mitigations:**
- The MQTT broker on the Carbon 2 requires the access code (`PRINTER_ACCESS_CODE`) as the
  broker password.
- Every MQTT call is wrapped with a timeout and retry. The client does not expose raw MQTT
  to the application — it wraps commands in typed methods.

### SQLite database

**Reachable by:** Host root (via the `sentinel-data` Docker volume).

**Note:** This is the normal threat model for self-hosted services. The database stores
detection events, pause records, and settings — no credentials or secrets.

---

## Exposing externally — checklist

If you want to make the status dashboard reachable from outside your LAN, complete **all**
of the following before doing so:

- [ ] Enable Basic Auth: set `AUTH_USERNAME` and a strong `AUTH_PASSWORD_BCRYPT`.
- [ ] Set `EXTERNAL_BIND_ALLOWED=true` (the safety guard will block startup otherwise).
- [ ] Put the service behind a TLS-terminating reverse proxy (Coolify / Traefik / nginx).
      The session cookie uses `Secure; HttpOnly; SameSite=Strict` — it is transmitted only
      over HTTPS.
- [ ] Use a strong password (≥ 16 random characters). The bcrypt hash makes offline attacks
      expensive, but a weak password is still a weak password.
- [ ] Restrict Coolify network exposure to 443 only. Do not expose port 8000 directly.
- [ ] Consider IP-allowlisting at the reverse proxy if your client IP is stable.

The camera stream (`/stream`) proxies MJPEG from the printer. If the service is exposed
externally, anyone with valid credentials can watch the live camera feed. This is intentional
but worth noting explicitly.

---

## Non-threats (accepted risks)

| Item | Rationale |
|---|---|
| MQTT traffic is not encrypted | MQTT 3.1.1 on a trusted LAN; Carbon 2 does not support TLS on MQTT in v0.1. Accepted for v0.1; TLS upgrade path exists in MQTT 5. |
| ntfy alert contains a camera snapshot | Snapshot is sent intentionally; use self-hosted ntfy if the image must not leave the LAN. |
| Telegram snapshot in alerts | Same as above; accept or disable Telegram notifications. |
| ML token is a hex string, not a JWT | ML container is on an internal network; the token provides defence-in-depth only. |

---

## v0.2 candidates

- Add ML-reachability ping to `/readyz`.
- Replace session cookie HMAC with proper JWT (rotate on password change).
- Audit log: record all bot commands and who sent them to the database.
- Evaluate MQTT 5 TLS for Carbon 2 when firmware support lands.
