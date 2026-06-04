# Audit Remediation Backlog

This document tracks the remediation tickets from the pre-public-beta audit.
These tickets will be implemented using a stacked branch strategy.

## Bugs and Correctness

### Ticket 1.1: `obico-ml` image fetch blocked by AuthMiddleware
- **Location:** `sentinel/web/auth.py`
- **Issue:** Strict loopback IP check blocks `obico-ml` Docker container from fetching snapshots.
- **Fix:** Update `AuthMiddleware` to allow requests from Docker internal subnets or rely exclusively on the unguessable nonce for `/__internal_snapshot/`.

### Ticket 1.2: Negative snooze duration causes permanent detection disablement
- **Location:** `sentinel/web/routes.py`, `sentinel/watcher/loop.py`
- **Issue:** `control_snooze` accepts negative values, crashing `asyncio.sleep()`.
- **Fix:** Add `seconds >= 0` validation in `control_snooze` and wrap `asyncio.sleep()` in a `try...except` block.

### Ticket 1.3: Dead code and impossible condition in back-to-back print job tracking
- **Location:** `sentinel/watcher/loop.py`
- **Issue:** Impossible condition logic inside an `if` block.
- **Fix:** Remove the impossible condition check and simplify the elapsed duration calculation.

## Security

### Ticket 2.1: SSRF Protection Bypass via Hostname DNS Rebinding and Unspecified/Mapped IPs
- **Location:** `sentinel/network.py`
- **Issue:** `validate_printer_ip` allows hostnames without resolution, accepts `0.0.0.0` and IPv4-mapped IPv6 loopbacks.
- **Fix:** Resolve hostnames to IPs before validation, strictly enforce private/safe IP ranges, block `0.0.0.0`, and unmap IPv4-mapped IPv6 addresses.

### Ticket 2.2: Authentication Loopback Bypass via X-Forwarded-For Spoofing
- **Location:** `sentinel/web/auth.py`
- **Issue:** `_resolve_client_ip` extracts the leftmost IP, which can be spoofed by the attacker.
- **Fix:** Retrieve the real client IP by evaluating the rightmost IP added by the trusted proxy, or validate against known proxy networks.

## Privacy

### Ticket 3.1: Ntfy Token and Privacy Exposure over cleartext HTTP
- **Location:** `sentinel/notify/ntfy.py`
- **Issue:** `NtfyNotifier._post` allows cleartext HTTP, exposing tokens and snapshots.
- **Fix:** Throw a validation error if `ntfy_url` begins with `http://` (except for localhost).

### Ticket 3.2: Obico ML Bearer Token Exposure over cleartext HTTP
- **Location:** `sentinel/ml/client.py`
- **Issue:** `ml_api_url` defaults to `http://obico-ml:3333`, transmitting tokens in cleartext.
- **Fix:** Enforce TLS (`https://`) for external ML service connections in settings validation, and explicitly document risks.

## Performance

### Ticket 4.1: Infinite loop and CPU spin on snapshot deletion failure
- **Location:** `sentinel/watcher/loop.py`
- **Issue:** `cleanup_old_snapshots` loops infinitely if file deletion fails because the offset doesn't advance.
- **Fix:** Ensure the loop breaks or advances its offset if a batch fails, or drop the DB path reference even if disk cleanup fails.

### Ticket 4.2: Unbounded database scans on dashboard load
- **Location:** `sentinel/db/repo.py`
- **Issue:** `/` dashboard endpoint computes full table aggregates every load.
- **Fix:** Introduce application-level caching or store running totals in `runtime_settings`.

## Stability

### Ticket 5.1: Camera HTTP Stream Timeout Cuts Connection Every 10 Seconds
- **Location:** `sentinel/camera/mjpeg.py`
- **Issue:** `asyncio.timeout(10.0)` wraps the entire MJPEG stream loop.
- **Fix:** Remove the explicit `asyncio.timeout` wrapper.

### Ticket 5.2: Printer Client Hangs Indefinitely on Silent Network Drops
- **Location:** `sentinel/printer/client.py`
- **Issue:** `_fetch_status` raises timeouts but doesn't cancel the stuck `_listener_task`.
- **Fix:** Explicitly call `cancel()` on `_listener_task` when the staleness timeout is hit.

### Ticket 5.3: Stream disconnect resets detection confirmations
- **Location:** `sentinel/camera/mjpeg.py`
- **Issue:** `MjpegGrabber.stream_proxy` eagerly cancels `self._broadcaster_task`.
- **Fix:** Remove the explicit `_broadcaster_task.cancel()` and rely on the idle timeout.

### Ticket 5.4: Notification Task Eviction Drops Arbitrary Tasks
- **Location:** `sentinel/notify/dispatcher.py`
- **Issue:** Evicting from `set` drops an arbitrary task.
- **Fix:** Use an insertion-ordered collection (`collections.OrderedDict` or `list`).

### Ticket 5.5: Snooze State Becomes Permanent on Process Restart
- **Location:** `sentinel/watcher/loop.py`
- **Issue:** Process restart drops the ephemeral `asyncio.sleep` task, leaving detection disabled.
- **Fix:** Persist a `snooze_until` timestamp in the database and evaluate dynamically.

## Maintainability

### Ticket 6.1: Default docker-compose configuration crashloops on startup
- **Location:** `docker-compose.yml`, `sentinel/safety.py`, `sentinel/config.py`
- **Issue:** `BIND_HOST=0.0.0.0` with `EXTERNAL_BIND_ALLOWED=false` causes crashloop.
- **Fix:** Change `EXTERNAL_BIND_ALLOWED` default to `true` in `docker-compose.yml`.

### Ticket 6.2: Missing standard application logging configuration
- **Location:** `sentinel/__main__.py`
- **Issue:** Entry point doesn't call `logging.basicConfig()`, swallowing operational logs.
- **Fix:** Add `logging.basicConfig(level=settings.log_level)` early in `_run()`.
