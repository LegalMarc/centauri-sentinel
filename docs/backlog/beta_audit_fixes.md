# Beta Audit Fixes Backlog

## Phase 2: Security & Privacy Mitigations
- [x] **SEC-01: Fix SSRF vulnerabilities across API and load states**
  - [x] Extract `is_link_local` and multicast checks from `config.py` to a utility.
  - [x] Apply checks in `sentinel/web/routes.py` (`update_settings`).
  - [x] Apply checks in `sentinel/__main__.py` when loading from DB.
- [x] **SEC-02: Hard fail for unauthenticated public `ntfy.sh` configurations**
  - [x] Update `sentinel/notify/ntfy.py` to raise an error at startup if `https://ntfy.sh` is used without an auth token.
- [x] **SEC-03: Fix CSRF header validation logic**
  - [x] Update `AuthMiddleware` in `sentinel/web/auth.py` to use `urllib.parse` for strict `Referer` matching.
  - [x] Block requests if both `Origin` and `Referer` are completely absent on state-changing methods.
- [x] **SEC-04: Address the `__internal_snapshot` IP spoofing**
  - [x] Replace IP-based exemption for `/__internal_snapshot` with a secure internal token or strictly bind the check to `127.0.0.1` taking proxies into account.

## Phase 3: Stability & Correctness Fixes
- [x] **STAB-01: Centralize snooze task logic and respect manual disable**
  - [x] Cancel existing snooze tasks if a new snooze is issued.
  - [x] Cancel snooze task if `/disable` is called via API or Telegram.
- [x] **STAB-02: Fix auto-stop violation**
  - [x] Remove `await self._printer.stop()` from `watcher/loop.py`.
  - [x] Dispatch a push notification instead asking the user to manually stop or resume.
- [x] **STAB-03: Handle MQTT silent failures via `stale` flag**
  - [x] In `WatcherLoop._update_state`, check `status.stale` and set `self._state = STALLED` or `OFFLINE` so it stops grabbing frames.
- [x] **STAB-04: Correct analytics data corruption logic**
  - [x] Ensure back-to-back prints don't auto-complete the previous print unless it actually finished.
- [x] **STAB-05: Fix live stream broadcaster task cancellation on settings change**
  - [x] Add `close()` to `MjpegGrabber` that cancels `_broadcaster_task`. Call it when `printer_ip` is updated.

## Phase 4: Performance & Resource Constraints
- [x] **PERF-01: Resolve memory leak in auth middleware**
  - [x] `sentinel/web/auth.py` binds auth secrets in a closure or uses unbounded LRU caches for session verification that are never invalidated. Replace with standard expiring session tokens (or bound the dict).
- [x] **PERF-02: Catch chunked transfer OOM conditions**
  - [x] Enforce max body size actively while streaming instead of just checking `Content-Length`.
- [x] **PERF-03: Cache DB settings to remove per-tick watcher query load**
  - [x] Read settings from a memory cache in the watcher loop.
  - [x] Invalidate/update cache when API changes settings.
- [x] **PERF-04: Optimize analytics aggregations and batch DB operations**
  - [x] Chunk the `IN (...)` parameter list in `delete_old_snapshots`.
