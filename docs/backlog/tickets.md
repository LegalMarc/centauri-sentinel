# Backlog: Pre-Public Beta Audit Findings

## Critical Issues
1. [ ] **Unauthenticated Web UI / API and Camera Feed by default**
   *Description:* `auth_username` defaults to `None`, disabling `AuthMiddleware`. `docker-compose.yml` exposes port 8000. `safety.py` permits binding to `0.0.0.0` without authentication if a container environment is detected. `/stream` and `/snapshot` are exposed by default.
2. [ ] **Blocking Event Loop via Synchronous DNS Resolution**
   *Description:* `socket.gethostbyname()` is called synchronously in `sentinel/network.py` (`resolve_and_validate_printer_ip`), freezing the event loop during slow DNS resolution.

## High Issues
3. [ ] **Cleartext HTTP transmission of Bearer Tokens over internal networks**
   *Description:* `validate_https` in `sentinel/network.py` permits HTTP to `.lan`/`.local`, leaking ML and Ntfy bearer tokens over the local network.
4. [ ] **`ntfy` Notifier ignores `ntfy_send_snapshots` configuration**
   *Description:* `sentinel/notify/ntfy.py` unconditionally sends snapshots without checking `ntfy_send_snapshots`.
5. [ ] **Database migration logic fails to update v1 schemas to v6**
   *Description:* `sentinel/db/migrate.py` skips dropping v1 tables but uses `CREATE TABLE IF NOT EXISTS`, failing to add new columns while bumping `schema_version` to `6`.
6. [ ] **Resource Leak in Telegram Bot Supervisor Loop**
   *Description:* In `sentinel/bot/runner.py`, partial startup exceptions orphan background tasks, and teardown exceptions skip `app.stop()` and `app.shutdown()`.

## Medium Issues
7. [ ] **Missing CSRF protection on Web UI routes when auth is disabled**
   *Description:* CSRF checks in `AuthMiddleware` are skipped when auth is disabled.
8. [ ] **Hardcoded Default Printer Password**
   *Description:* `printer_access_code` defaults to `123456` in `sentinel/config.py`.
9. [ ] **Spurious "Printer paused externally" alerts upon manual resume**
   *Description:* Resuming forces state to `ARMED` instantly, but a stale `"paused"` status from the printer causes an immediate spurious pause alert.
10. [ ] **Infinite log spam due to Camera Offline / Armed state flap**
    *Description:* `watcher/loop.py` flaps between `CAMERA_OFFLINE` and `ARMED` when retrying camera grabs, spamming logs.

## Low Issues
11. [ ] **Secrets loaded from environment variables remain visible**
    *Description:* `os.environ.pop` in `config.py` is ineffective at hiding secrets from `docker inspect` or `/proc/<pid>/environ`.
12. [ ] **Single-use nonces for snapshots can be read multiple times before expiry**
    *Description:* `/__internal_snapshot/{nonce}` uses `.get()` instead of `.pop()`.
13. [ ] **`LimitUploadSizeMiddleware` payload limit returns 500 instead of 413**
    *Description:* `RuntimeError("Payload Too Large")` is converted to HTTP 500 by Starlette's exception handler.
14. [ ] **Unbounded Memory Growth in PrinterClient Layers Cache**
    *Description:* `_file_layers_cache` grows indefinitely.
15. [ ] **ML fail-open behavior allows unmonitored prints silently**
    *Description:* ML client returns `0.0` after 10 consecutive failures instead of halting.

