# Connection Pool Churn in NtfyNotifier Due to Per-Request Client Instantiation

**ID:** PERF-07
**Severity:** Medium
**Category:** Performance
**Status:** Closed

## Affected Files
- `sentinel/notify/ntfy.py`
- `sentinel/__main__.py`

## Description
A new `httpx.AsyncClient` is created for every HTTP POST attempt inside `NtfyNotifier._post`'s retry loop. This prevents connection reuse and keep-alive, introducing TCP/SSL handshake overhead on every alert.

## Acceptance Criteria
- [x] Initialize a single `httpx.AsyncClient` in `NtfyNotifier.__init__` and reuse it for all HTTP requests.
- [x] Add an async `close()` method to `NtfyNotifier` to close the client cleanly on shutdown.
- [x] Update `__main__.py` shutdown to call `notifier.close()` if the notifier has one.
- [x] Verify that tests pass and coverage is maintained.
