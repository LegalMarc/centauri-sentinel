# centauri-sentinel — Implementation Plan

Open-source, self-hosted failure detection and remote control for the Elegoo Centauri Carbon 2 FDM printer. Watches the printer's MJPEG camera with Obico's ML model, pauses the print on confirmed failure, and notifies via Telegram and/or ntfy.

---

## 1. Scope and goals

### v0.1 (this plan)
- Two-container Docker Compose stack deployable on Coolify with a single required env var (`PRINTER_IP`).
- Async watcher loop: pull frames from printer MJPEG, send to internal Obico ML API, count consecutive detections, pause print on confirmation.
- Notifications: Telegram (bot + inline keyboard) and ntfy — both optional, independent, gracefully skipped if not configured.
- Read-only status web page (FastAPI + server-rendered HTML, no Tailwind CDN, no SSE, no HTMX): current state, last detection, last snapshot, watcher heartbeat.
- SQLite persistence for detection events, pause history, and runtime settings overrides.
- Healthchecks on both containers; structured logging; documented threat model.

### Deferred to v0.2+
- Full HTMX dashboard with SSE live updates.
- Pre-built Tailwind CSS bundle (replace CDN if/when dashboard returns).
- Telegram bot rich TUI beyond the v0.1 commands.
- Multi-printer support.
- Cloud-hosted variant.

### Non-goals
- Public-internet exposure of the dashboard (assume trusted LAN; see threat model).
- Replacing slicer or print farm software.

---

## 2. Deployment assumption

**Coolify and the printer share a trusted LAN.** This is the design baseline. Implications:

- Dashboard auth defaults **off**. Documented as LAN-trust assumption.
- `/snapshot` and `/stream` are unauthenticated on the LAN.
- The app **refuses to start** if it detects it is binding to a non-loopback, non-RFC1918 interface without auth configured (footgun guard for users who try to expose it via Cloudflare Tunnel or port forwarding).
- Telegram bot is the one publicly reachable surface and is hardened accordingly (chat ID + user ID allowlist).
- ntfy: if using public ntfy.sh, the topic name is the only secret — documented; self-hosted ntfy on the LAN recommended.

---

## 3. Architecture

```
LAN
 ├── Printer (Centauri Carbon 2)
 │     ├── MJPEG @ :8080/mjpeg
 │     └── MQTT broker (port 1883, aiomqtt client)
 │
 └── Coolify host — docker compose stack
       ├── obico-ml   (Obico's ML API image, internal network only, no host ports)
       │     └── shared named volume: /shared/token  (read by sentinel)
       │
       └── sentinel   (this project, FastAPI)
             ├── Watcher loop  ── grab frame → POST to obico-ml → confirm → pause
             ├── Printer client (aiomqtt Carbon 2 client)
             ├── Notifier (Telegram + ntfy, both optional)
             ├── Status web (read-only HTML)
             ├── Telegram bot (long-polling)
             └── SQLite (named volume)
```

### Key revisions vs. the original draft

1. **ML input is URL-fetch only (spike #0 confirmed).** The Obico ML API (`GET /p/?img=<url>`) does not support POST multipart upload. Sentinel serves a single-use nonce endpoint (`/__internal_snapshot/<nonce>`) so the ML container can fetch each frame. The nonce is 32-byte random, single-use, expires in 10s.
2. **No Tailwind, no HTMX, no SSE in v0.1.** Status page is server-rendered HTML with a meta-refresh and a plain `<img>` tag pointing at `/snapshot`. Smaller surface, no CDN runtime dependency.
3. **SQLite persistence** from day one. Without it, the status page is useless across restarts.
4. **Watcher heartbeat** is a first-class signal. Notifier alerts on watcher-stalled, not just print-failed.
5. **Detection auto-suspends on pause/idle** so the watcher doesn't keep alerting on the frozen failed frame.
6. **Warmup gating**: configurable seconds-from-print-start before detection arms (covers purge/prime).
7. **Security hardening folded into feature issues**; no standalone hardening issue.
8. **Centauri Carbon 2 uses MQTT, not SDCP WebSocket (spike #0 confirmed).** pycentauri only supports the original Carbon (port 3030). The Carbon 2 uses MQTT (port 1883). The printer client is implemented directly with `aiomqtt`; pycentauri is not a dependency.

---

## 4. Threat model (documented in README)

| Surface | Reachable by | Mitigation |
|---|---|---|
| Status page, `/snapshot`, `/stream` | LAN devices | None by default. Optional basic auth. Refuse to start if bound externally without auth. |
| Telegram bot | Public internet | Allowlist chat ID **and** user ID; `/stop` requires `/confirm`. |
| ntfy alerts | Anyone who knows the topic | Recommend long random topic; recommend self-hosted ntfy on LAN. |
| Obico ML API | Internal docker network only | Token in shared volume; no host ports. |
| aiomqtt → printer MQTT (port 1883) | LAN | MQTT auth with access code; treat as untrusted, wrap every call with timeout + retry. |
| SQLite DB | Host root | Self-hosted norm; documented. |

Users who expose the dashboard externally are explicitly steered to: enable auth, use a strong password, put behind TLS at the reverse proxy. The "external bind without auth" startup guard makes the unsafe path require deliberate effort.

---

## 5. Configuration

Single required env var: `PRINTER_IP`. Everything else optional with sane defaults.

| Var | Default | Purpose |
|---|---|---|
| `PRINTER_IP` | — (required) | Centauri IP on LAN. |
| `PRINTER_ACCESS_CODE` | `123456` | MQTT broker password for Carbon 2. Set to match the access code in the printer's network settings. |
| `PRINTER_MQTT_PORT` | `1883` | |
| `PRINTER_MJPEG_PORT` | `8080` | Confirmed via spike: camera at port 8080. |
| `PRINTER_MJPEG_PATH` | `/mjpeg` | |
| `ML_API_URL` | `http://obico-ml:3333` | Internal docker DNS name. |
| `ML_API_TOKEN_FILE` | `/shared/token` | Shared volume. |
| `ML_CONFIRM_COUNT` | `3` | Consecutive detections before pause. |
| `ML_POLL_INTERVAL_SECONDS` | `10` | |
| `ML_SCORE_THRESHOLD` | `0.4` | Obico-style score. |
| `DETECTION_WARMUP_SECONDS` | `300` | Skip detection for first N seconds of a print. |
| `DETECTION_ENABLED_DEFAULT` | `true` | Initial state; overridable at runtime. |
| `WATCHER_STALL_SECONDS` | `60` | Heartbeat threshold for stall alert. |
| `TELEGRAM_BOT_TOKEN` | — | If unset, Telegram disabled. |
| `TELEGRAM_CHAT_ID` | — | Required if bot token set. |
| `TELEGRAM_USER_IDS` | — | Comma-separated allowlist; required if bot token set. |
| `NTFY_URL` | — | e.g. `https://ntfy.sh/your-topic`. If unset, ntfy disabled. |
| `NTFY_TOKEN` | — | Optional bearer for self-hosted. |
| `AUTH_USERNAME` | — | If unset, dashboard auth disabled. |
| `AUTH_PASSWORD_BCRYPT` | — | Bcrypt hash. |
| `BIND_HOST` | `0.0.0.0` | |
| `BIND_PORT` | `8000` | |
| `EXTERNAL_BIND_ALLOWED` | `false` | Set true to override the external-bind safety guard. |
| `LOG_LEVEL` | `INFO` | |
| `DB_PATH` | `/data/sentinel.db` | Named volume. |

---

## 6. Open unknowns — **RESOLVED by spike #0** (see `docs/verified-assumptions.md`)

1. ✅ **Obico ML image + ARM64**: Must build from source; `ml_api_base:1.4` supports both amd64 and arm64.
2. ✅ **Obico ML API**: URL-fetch only (`GET /p/?img=<url>`); no POST. Auth: `Authorization: Bearer <token>`. Score in `detections[i][1]`.
3. ✅ **Printer control**: pycentauri does NOT support Carbon 2. Carbon 2 uses MQTT port 1883. See amendment in §3 revision #8.
4. ✅ **MJPEG**: Port 8080, path `/mjpeg`. Stream stable in 2-min soak; full 60-min soak deferred to #14.
5. ✅ **Coolify**: v4.1.0. Services API: `POST /api/v1/services` with base64 compose. No deep-link button in UI.

Spike outputs a one-page `docs/verified-assumptions.md` that all subsequent issues reference.

---

## 7. Issue list (v0.1)

Each issue is fully specified in `ISSUES.md`. Order is sequential; later issues depend on earlier ones.

0. Spike: verify external assumptions.
1. Project scaffolding (uv, pyproject, ruff, mypy, pytest, Dockerfile).
2. Config module (pydantic-settings, validation, external-bind guard).
3. Persistence (SQLite schema, migrations, repositories).
4. Printer client (aiomqtt Carbon 2 MQTT client with timeouts, retries, structured errors).
5. MJPEG frame grabber (reconnect, backoff, frame-age tracking).
6. ML client (POST upload primary, URL-fetch fallback; fail-open).
7. Detection watcher (state machine: idle/warmup/armed/paused/stalled; heartbeat).
8. Notifier — Telegram (allowlist, photo+keyboard, /confirm gating).
9. Notifier — ntfy (POST with optional auth, image attachment).
10. Status web (read-only HTML, optional basic auth, snapshot/stream proxies, external-bind guard).
11. Telegram bot commands (/status /snapshot /pause /resume /stop /confirm /enable /disable).
12. Docker Compose stack (token init container, healthchecks, ARM64 verified).
13. Documentation (README with threat model, Coolify guide, printer setup, troubleshooting).
14. End-to-end verification checklist on real hardware.

---

## 8. Quality bar

- **Tests**: unit tests for config, persistence, printer client (mocked socket), ML client, watcher state machine, notifier formatters. Integration test that runs the full stack against a fake printer + fake ML API in CI.
- **Lint/type**: ruff + mypy in strict mode for the `sentinel/` package; CI gate.
- **Logging**: structured JSON logs (stdlib `logging` + `python-json-logger`); one log line per state transition.
- **Healthchecks**: `/healthz` (process alive) and `/readyz` (watcher heartbeat fresh, DB writable, ML reachable).
- **Docs**: every env var documented in `.env.example`; every command documented in `--help`; README threat-model section is mandatory reading and linked from the dashboard footer.
