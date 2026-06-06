# Pre-Public Beta Audit Fixes Backlog

## Bugs & Correctness
- [ ] B1: Handle `False` return from `PrinterClient.pause()` in `WatcherLoop._do_pause()`
- [ ] B2: Set `self._paused_since = datetime.now(tz=UTC)` immediately when transitioning to `PAUSED`
- [ ] B3: Enforce `printing = False` if `print_state == "completed"` or `"idle"` regardless of `enable` key
- [ ] B4: Set `self._latest_frame = None` inside the exception block of `_stream_proxy_internal`

## Security & Privacy
- [ ] S1: Catch `ValueError` in `_listen_loop` and `_stream_proxy_internal` to prevent SSRF crashes
- [ ] S2: Change sensitive fields in `Settings` to `SecretStr`
- [ ] S3: Implement `TrustedHostMiddleware` or allowed hosts check for CSRF DNS Rebinding
- [ ] S4: Require CSRF token for `/logout` route

## Performance & Scalability
- [ ] P1: Add indexes on `ts_utc` and `started_at` in schema migration
- [ ] P2: Add `httpx.HTTPStatusError` to retry tuple in `ntfy._post`
- [ ] P3: Wrap `clear_all_data` directory deletion loop in `asyncio.to_thread()`
- [ ] P4: Consolidate sequential `db.get_setting()` calls in `status_page` route using `asyncio.gather()`

## Stability & Reliability
- [ ] R1: Remove `self._confirm_count = 0` on pause command exception
- [ ] R2: Implement non-destructive `ALTER TABLE` schema migration from v1
- [ ] R3: Persist `snooze_until_utc` timestamp in the database instead of in-memory sleep
- [ ] R4: Track consecutive ML API failures and bubble up critical failure state
- [ ] R5: Prevent resetting `_paused_since` on transient network failure during auto-stop
- [ ] R6: Isolate Telegram fallback logic so retries only hit text endpoint
- [ ] R7: Implement absolute timeout across the frame extraction loop in MJPEG proxy
- [ ] R8: Prioritize critical alerts over informational in Notification Dispatcher
- [ ] R9: Cache total layers or index list in `_parse_status`
- [ ] R10: Use persistent client ID with `clean_session=True` for AIOMQTT

## Maintainability & Operational-Readiness
- [ ] M1: Deprecate `AUTH_PASSWORD` plaintext and mandate `AUTH_PASSWORD_BCRYPT`
- [ ] M2: Switch healthcheck to `/readyz` or add watcher heartbeats
- [ ] M3: Add `USER sentinel` directive to Dockerfile
- [ ] M4: Add dynamic SHA-based tagging to GitHub Actions workflow
- [ ] M5: Remove `structlog` from `pyproject.toml`
