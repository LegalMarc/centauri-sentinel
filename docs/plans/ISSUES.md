# centauri-sentinel — Issue Specifications (v0.1)

Each section is a fully-specified GitHub issue, coding-ready: scope, acceptance criteria, file layout, test plan, and docs deliverables. Issues are sequential — do not start N+1 until N's acceptance criteria are green.

Conventions:
- Language: Python 3.12.
- Package manager: `uv`.
- Lint: `ruff`. Types: `mypy --strict` on `sentinel/`.
- Tests: `pytest`, `pytest-asyncio`, `pytest-httpx`, `pytest-cov` ≥ 85% on `sentinel/`.
- All async I/O. No threads except where a library forces it.
- All external calls (printer, ML, Telegram, ntfy, MJPEG) wrapped in: timeout, retry-with-backoff, structured error, log line.
- Every state transition logs a single structured event.

---

## Issue #0 — Spike: verify external assumptions

**Goal:** resolve every unknown in `PLAN.md §6` before writing feature code.

**Deliverables:**
- `docs/verified-assumptions.md` with these sections, each citing source URL or repo commit:
  1. Obico ML container: image name, tag, registry, **ARM64 availability** (check manifest with `docker buildx imagetools inspect`). If no ARM64, document the chosen fallback (build from source in CI? scope to x86-only and warn in README?).
  2. Obico ML API surface: confirm whether `POST` with multipart file upload is supported, and the exact path/field name. Document the `GET /p/?img=<url>` fallback as well. Confirm auth header vs query param.
  3. Pycentauri: state strings for `printing`, `paused`, `idle`, `error`, `complete`. Confirm pause/resume/stop method signatures. Note any blocking calls that need `asyncio.to_thread`.
  4. MJPEG: real printer reconnect behavior. Capture 60 minutes of stream against a real Centauri, log frame intervals and any disconnect events. Document expected stall pattern.
  5. Coolify: current "deploy compose from Git" UX. Test on Coolify ≥ latest stable. Document the exact button URL format if one exists; otherwise document the manual flow.

**Acceptance:**
- Doc committed at `docs/verified-assumptions.md`.
- All four open unknowns from `PLAN.md` answered with citations.
- If any answer invalidates the plan (e.g. no ARM64 Obico image), open a PR amending `PLAN.md` in the same commit.

---

## Issue #1 — Project scaffolding

**Goal:** repo skeleton, tooling, CI.

**Files:**
```
pyproject.toml        # uv-managed, deps + dev-deps + tool config
uv.lock
Dockerfile            # multi-stage, slim, non-root user, healthcheck
.dockerignore
.env.example          # every var from PLAN.md §5, commented
.github/workflows/ci.yml   # ruff + mypy + pytest, matrix on x86_64 + arm64
.gitignore
README.md             # stub; filled in #13
LICENSE               # MIT
sentinel/
  __init__.py
  __main__.py         # `python -m sentinel` entrypoint
tests/
  __init__.py
  conftest.py
```

**pyproject deps (initial):**
`fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `pydantic-settings`, `python-telegram-bot`, `aiosqlite`, `bcrypt`, `python-json-logger`, `jinja2`, `pycentauri`, `tenacity`.
Dev: `ruff`, `mypy`, `pytest`, `pytest-asyncio`, `pytest-httpx`, `pytest-cov`, `respx`.

**Dockerfile requirements:**
- Multi-stage: builder installs deps via `uv sync --frozen`, runtime stage copies venv and source.
- Runs as non-root UID 1000.
- `HEALTHCHECK` calls `/healthz` via `curl` or `python -c`.
- Final image ≤ 250 MB.
- Cross-platform build (`linux/amd64,linux/arm64`).

**CI:**
- Ruff check + format check.
- Mypy strict on `sentinel/`.
- Pytest with coverage gate ≥ 85% on `sentinel/`.
- Docker buildx for both arches; push to GHCR on tag.

**Acceptance:**
- `uv sync && uv run pytest` passes locally and in CI.
- `docker build` succeeds on both arches.
- `python -m sentinel --help` prints usage.

---

## Issue #2 — Config module

**Goal:** typed config from env, with validation and the external-bind safety guard.

**Files:**
- `sentinel/config.py` — `Settings` class (pydantic-settings, `BaseSettings`).
- `sentinel/safety.py` — `check_external_bind_safety(settings)` raises on unsafe config.
- `tests/test_config.py`, `tests/test_safety.py`.

**Settings behavior:**
- Loads from env first, then `.env`.
- Validates: if `TELEGRAM_BOT_TOKEN` set, `TELEGRAM_CHAT_ID` and `TELEGRAM_USER_IDS` required.
- Validates: if `AUTH_USERNAME` set, `AUTH_PASSWORD_BCRYPT` required and must be a valid bcrypt hash.
- Validates: `PRINTER_IP` must be a syntactically valid IP or hostname.
- Exposes computed properties: `telegram_enabled`, `ntfy_enabled`, `auth_enabled`.

**External-bind guard:**
- On startup, inspect `BIND_HOST`. If `0.0.0.0` and `EXTERNAL_BIND_ALLOWED=false` and `auth_enabled=false`, refuse to start with a clear error pointing at the docs section.
- Detect public-routable bind by enumerating interfaces; if any non-loopback non-RFC1918 IPv4 is bound and auth is off, refuse to start unless `EXTERNAL_BIND_ALLOWED=true`.
- The override `EXTERNAL_BIND_ALLOWED=true` is logged loudly at WARNING on every startup.

**Acceptance:**
- 100% branch coverage on `safety.py`.
- `.env.example` documents every var with a one-line comment and example value.
- Mypy strict clean.

---

## Issue #3 — Persistence

**Goal:** SQLite schema + repositories for events, pauses, runtime settings.

**Files:**
- `sentinel/db/__init__.py`
- `sentinel/db/schema.sql` — tables.
- `sentinel/db/migrate.py` — applies schema; idempotent; bumps `schema_version` row.
- `sentinel/db/repo.py` — async repository functions (aiosqlite).
- `tests/test_db.py`.

**Schema:**
```sql
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);

CREATE TABLE detection_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,            -- ISO 8601
  score REAL NOT NULL,
  consecutive INTEGER NOT NULL,
  confirmed INTEGER NOT NULL,      -- 0/1; 1 means this event triggered a pause
  snapshot_path TEXT               -- optional, on confirmed only
);

CREATE TABLE pause_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  source TEXT NOT NULL,            -- 'auto' | 'telegram' | 'web'
  result TEXT NOT NULL,            -- 'ok' | 'error'
  error_message TEXT
);

CREATE TABLE runtime_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL              -- JSON
);

CREATE TABLE watcher_heartbeat (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_tick_utc TEXT NOT NULL,
  state TEXT NOT NULL
);
```

**Repository API (async):**
- `record_detection(score, consecutive, confirmed, snapshot_path=None) -> int`
- `record_pause(source, result, error_message=None) -> int`
- `get_recent_detections(limit=20) -> list[DetectionEvent]`
- `get_recent_pauses(limit=20) -> list[PauseEvent]`
- `get_setting(key) -> Any | None` / `set_setting(key, value)`
- `update_heartbeat(state)` / `get_heartbeat() -> Heartbeat`

**Snapshot retention:**
- Confirmed-detection snapshots saved to `/data/snapshots/<event_id>.jpg`.
- Retention: keep last 50, delete older. Runs on each `record_detection(confirmed=True)`.

**Acceptance:**
- All repo functions unit-tested with an in-memory SQLite.
- Migration is idempotent (run twice → no errors, schema_version unchanged).
- Concurrent writes serialized via a single writer lock.

---

## Issue #4 — Printer client

**Goal:** pycentauri wrapper with timeouts, retries, structured errors.

**Files:**
- `sentinel/printer/__init__.py`
- `sentinel/printer/client.py` — `PrinterClient` async class.
- `sentinel/printer/types.py` — dataclasses: `PrinterStatus`, `PrinterState` enum.
- `sentinel/printer/errors.py` — `PrinterError`, `PrinterTimeout`, `PrinterProtocolError`.
- `tests/test_printer_client.py` (mock pycentauri).

**`PrinterClient` API:**
```python
async def status() -> PrinterStatus
async def pause() -> None
async def resume() -> None
async def stop() -> None
async def is_printing() -> bool
async def print_elapsed_seconds() -> int | None
```

**Robustness requirements:**
- All calls wrapped with `asyncio.wait_for(timeout=5s)`.
- Retry with `tenacity`: 3 attempts, exponential backoff 0.5s/1s/2s, only on `PrinterTimeout` and connection errors. Do NOT retry on protocol errors.
- Pycentauri's blocking calls executed via `asyncio.to_thread`.
- Protocol version check on first successful `status()` call; logged at INFO; mismatch logged at ERROR (does not crash, but heartbeat reports `protocol_mismatch`).
- `is_printing()` derived from the state string identified in spike #0.

**Acceptance:**
- All public methods have unit tests covering: success, timeout, retry-then-success, retry-then-fail, protocol error.
- No `time.sleep` anywhere; no `requests`; no synchronous I/O in async paths.

---

## Issue #5 — MJPEG frame grabber

**Goal:** robust single-frame grabber against a flaky MJPEG stream.

**Files:**
- `sentinel/camera/mjpeg.py` — `MjpegGrabber` class.
- `sentinel/camera/errors.py`.
- `tests/test_mjpeg.py` (uses a local aiohttp fake-mjpeg server fixture).

**API:**
```python
class MjpegGrabber:
    def __init__(self, url: str, *, connect_timeout=5, read_timeout=5): ...
    async def grab(self) -> bytes:  # one JPEG, raises on failure
    async def stream_proxy(self) -> AsyncIterator[bytes]:  # for web /stream
    @property
    def last_success_utc(self) -> datetime | None: ...
```

**Behavior:**
- `grab()` opens the stream, reads until it finds the first complete JPEG (SOI `\xff\xd8` … EOI `\xff\xd9`), returns its bytes, closes the connection.
- Reconnect with exponential backoff (0.5s → 30s cap) on any I/O error.
- After 3 consecutive failures, raise `CameraOfflineError`. Watcher distinguishes this from "no detection".
- `stream_proxy()` is for the web `/stream` endpoint: forwards the raw MJPEG multipart stream to the client, auto-reconnects on backend disconnect, closes when the client disconnects.

**Acceptance:**
- Tests cover: clean grab, mid-frame disconnect, slow stream, never-sends-EOI (read timeout fires), repeated failures → `CameraOfflineError`.
- No memory growth on `stream_proxy()` under a long run (verified by a 5-minute soak test in CI marked `@pytest.mark.slow`, optional gate).

---

## Issue #6 — ML client

**Goal:** call Obico ML API; primary path POST upload, fallback URL-fetch; fail-open.

**Files:**
- `sentinel/ml/client.py`
- `sentinel/ml/types.py` — `MlResult(score: float, raw: dict)`.
- `tests/test_ml_client.py` (respx).

**API:**
```python
class MlClient:
    async def detect(self, jpeg: bytes) -> MlResult
```

**Behavior:**
- Reads token from `ML_API_TOKEN_FILE`; reloads if file mtime changes.
- POST multipart upload as primary (assuming spike #0 confirms support); else falls back to a configurable `MODE=url` that serves the frame via an internal-only `sentinel:8000/__internal_snapshot/<nonce>` endpoint, where the nonce is a single-use 32-byte token. The internal endpoint is unauthenticated but the nonce is single-use and expires in 10s.
- Timeout 10s. On any error: log WARNING and return `MlResult(score=0.0, raw={"error": "..."})` — **fail open** so a sick ML service does not pause prints.

**Acceptance:**
- Both modes covered by tests (mocked HTTP).
- Token reload tested.
- Fail-open behavior tested for: HTTP 5xx, timeout, malformed JSON.

---

## Issue #7 — Detection watcher

**Goal:** the brain — state machine that turns frames + ML scores into a confirmed pause.

**Files:**
- `sentinel/watcher/loop.py` — `Watcher` class with `run()` async method.
- `sentinel/watcher/state.py` — explicit state machine.
- `tests/test_watcher.py`.

**States:**
`IDLE` (printer not printing) → `WARMUP` (printing but within `DETECTION_WARMUP_SECONDS`) → `ARMED` (detection live) → `PAUSED` (printer paused, detection suspended) → back to `IDLE` when print ends or `ARMED` when resumed.
Cross-cutting: `CAMERA_OFFLINE`, `PROTOCOL_MISMATCH`, `STALLED` (set by external heartbeat watcher).

**Loop (every `ML_POLL_INTERVAL_SECONDS`):**
1. Update heartbeat row with current state.
2. Get printer status. Transition state if needed.
3. If state is `ARMED` and `detection_enabled` runtime setting is true:
   a. Grab one frame (or set `CAMERA_OFFLINE` on 3 failures).
   b. Call ML.
   c. If `score >= ML_SCORE_THRESHOLD`, increment counter and record event.
   d. If counter ≥ `ML_CONFIRM_COUNT`: save snapshot, `record_detection(confirmed=True)`, transition to `PAUSED`, call `printer.pause()`, fire notifier. If pause fails, **still fire notifier** with an error-tagged message.
   e. If score below threshold, reset counter to 0 (consecutive detections only).
4. Sleep.

**Heartbeat watchdog:** a separate task checks `watcher_heartbeat.last_tick_utc` every 15s. If older than `WATCHER_STALL_SECONDS`, fire one notifier alert ("watcher stalled") and set state to `STALLED`. Re-alert only on transition into stall, not every check.

**Acceptance:**
- State machine unit-tested with all transitions; no untested transition.
- Counter-resets-on-below-threshold tested.
- Pause-fails-but-notifier-still-fires tested.
- Heartbeat-stall path tested with a fake clock.

---

## Issue #8 — Notifier: Telegram

**Goal:** send rich Telegram alerts; reject all unauthorized senders.

**Files:**
- `sentinel/notify/telegram.py`
- `sentinel/notify/types.py` — `Alert` dataclass.
- `tests/test_telegram_notify.py`.

**API:**
```python
class TelegramNotifier:
    async def send_alert(self, alert: Alert) -> None
    async def send_text(self, text: str) -> None
```

**Behavior:**
- Sends a photo (the confirmed snapshot) with caption and inline keyboard: `Resume`, `Stop`, `Snooze 10m`.
- Disabled gracefully if `telegram_enabled` is false (`send_*` is a no-op + DEBUG log).
- All sends wrapped with 10s timeout + 3 retries (tenacity) on network errors only.

**Security:**
- Bot handler (in #11) calls `is_authorized(update)` which checks `chat.id in {TELEGRAM_CHAT_ID}` **and** `user.id in TELEGRAM_USER_IDS`.
- Unauthorized updates: log at WARNING with masked IDs, no reply.

**Acceptance:**
- Disabled-mode no-op tested.
- Auth check unit tested with allowlist, wrong chat, wrong user, missing user.
- Retry on transient failure tested.

---

## Issue #9 — Notifier: ntfy

**Goal:** ntfy POST with optional bearer auth and image attachment.

**Files:**
- `sentinel/notify/ntfy.py`
- `tests/test_ntfy.py`.

**Behavior:**
- POSTs to `NTFY_URL` with title, body, priority `high`, tag `warning`.
- If snapshot available, attaches via `X-Attach` header pointing at a one-time signed URL served by sentinel (10-minute single-use nonce, same pattern as #6 internal endpoint). Falls back to text-only if URL not reachable.
- Optional `Authorization: Bearer NTFY_TOKEN`.
- Disabled gracefully if `ntfy_enabled` is false.
- 10s timeout + 3 retries on network errors.

**Acceptance:**
- Enabled and disabled paths tested.
- Auth header presence tested.
- Attachment URL generation tested (and nonce single-use enforced).

---

## Issue #10 — Status web

**Goal:** read-only HTML status page + camera proxies + healthchecks.

**Files:**
- `sentinel/web/app.py` — FastAPI app factory.
- `sentinel/web/routes.py`
- `sentinel/web/auth.py` — optional HTTP Basic, bcrypt verify with session cookie cache (1h).
- `sentinel/web/templates/status.html` — Jinja2, plain HTML, **no Tailwind, no CDN**. Minimal embedded CSS.
- `tests/test_web.py`.

**Routes:**
| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | optional | Server-rendered status page; `<meta http-equiv="refresh" content="10">`. |
| GET | `/snapshot` | optional | Returns latest grabbed JPEG (in-memory cached, max 2s old). |
| GET | `/stream` | optional | MJPEG proxy via `MjpegGrabber.stream_proxy()`. |
| GET | `/healthz` | none | `{"status":"ok"}` always if process alive. |
| GET | `/readyz` | none | 200 if heartbeat fresh + DB writable + ML reachable; else 503 with reasons. |
| GET | `/__internal_snapshot/{nonce}` | nonce | Single-use, 10s expiry, used only by ML URL-fallback and ntfy attachment. |

**Status page content:**
- Printer state, print elapsed, watcher state, last tick age.
- Live camera (plain `<img src="/snapshot">` with JS-free meta-refresh).
- Last 10 detections (table).
- Last 10 pauses (table).
- Footer: link to README threat model section.

**Auth:**
- If `auth_enabled` is false, all routes (except internal/health) are open.
- If true, basic auth verified once, session cookie issued, cookie cache avoids per-request bcrypt cost.
- Auth applies to `/`, `/snapshot`, `/stream`. Health endpoints always open.

**Startup:**
- Runs the external-bind safety check from #2 before binding.

**Acceptance:**
- Page renders with sample data in tests (snapshot HTML diffed against fixture).
- Auth on/off both tested.
- `/readyz` returns 503 with reasons when watcher stalled.
- No Tailwind CDN reference in any template; CSS is embedded and < 2 KB.

---

## Issue #11 — Telegram bot commands

**Goal:** interactive control surface via Telegram.

**Files:**
- `sentinel/bot/commands.py`
- `sentinel/bot/runner.py` — long-polling task wired into app lifecycle.
- `tests/test_bot.py`.

**Commands** (all require `is_authorized` from #8):
| Command | Action |
|---|---|
| `/status` | Print state + watcher state + last detection. |
| `/snapshot` | Send current frame. |
| `/pause` | Pause print. No confirmation needed (reversible). |
| `/resume` | Resume print. |
| `/stop` | Replies "Reply `/confirm` within 30s to cancel the print." |
| `/confirm` | Only valid within 30s of a `/stop` from the same user; cancels print. |
| `/enable` | Sets runtime `detection_enabled=true`. |
| `/disable` | Sets runtime `detection_enabled=false`. |
| `/help` | Lists commands. |

**Inline keyboard callbacks** (from #8 alerts): `Resume`, `Stop` (with same confirm flow), `Snooze 10m` (sets `detection_enabled=false` and schedules a re-enable).

**Acceptance:**
- Stop/confirm window tested (within/expired/wrong user).
- Unauthorized user: no reply, WARNING logged.
- Snooze re-enable scheduled and fires.

---

## Issue #12 — Docker Compose stack

**Goal:** one-shot deployable stack on Coolify (LAN-trust assumption).

**Files:**
- `docker-compose.yml`
- `docker/token-init/Dockerfile` + `docker/token-init/entrypoint.sh` — generates `/shared/token` on first run; idempotent.
- `docker/sentinel.Dockerfile` (from #1).
- `docs/coolify-deploy.md` (linked from README).

**Compose layout:**
```yaml
services:
  token-init:
    # one-shot: generates /shared/token if absent
    restart: "no"
  obico-ml:
    image: <verified in #0>
    depends_on:
      token-init:
        condition: service_completed_successfully
    volumes:
      - ml-token:/shared:ro
    networks: [internal]
    healthcheck: { ... }
  sentinel:
    image: ghcr.io/<owner>/centauri-sentinel:latest
    depends_on:
      obico-ml:
        condition: service_healthy
    environment:
      PRINTER_IP: ${PRINTER_IP:?required}
      # ... all optional vars passed through
    volumes:
      - ml-token:/shared:ro
      - sentinel-data:/data
    ports:
      - "${SENTINEL_PORT:-8000}:8000"
    networks: [internal, default]
    healthcheck:
      test: ["CMD", "python", "-m", "sentinel.healthcheck"]
volumes:
  ml-token:
  sentinel-data:
networks:
  internal:
    internal: true
```

**Requirements:**
- `obico-ml` has no host ports.
- `internal` network is `internal: true` (no egress).
- ARM64 verified to work in #0; if not, document amd64-only and add `platform: linux/amd64` with a warning.
- Both images pinned to specific tags in compose; `:latest` only in CI for testing.

**Acceptance:**
- `PRINTER_IP=x.x.x.x docker compose up` brings stack up cleanly on a fresh host.
- Restarting the stack does not regenerate the ML token.
- Both healthchecks report healthy within 60s.

---

## Issue #13 — Documentation

**Goal:** README + deploy guide + threat model + troubleshooting that an unfamiliar user can follow.

**Files:**
- `README.md` — top of repo.
- `docs/coolify-deploy.md`
- `docs/printer-setup.md`
- `docs/threat-model.md` (linked from dashboard footer and README).
- `docs/troubleshooting.md`
- `docs/verified-assumptions.md` (from #0, kept current).

**README sections (in order):**
1. What it does (3 sentences + screenshot of status page).
2. Hardware: tested printer firmware versions.
3. Deployment baseline: **LAN-trust assumption stated prominently**, with the "do not expose to the public internet without auth + TLS" warning in a callout.
4. Quick start: Coolify one-click section.
5. Configuration reference: every env var, default, purpose (mirrors `PLAN.md §5`).
6. Telegram setup: BotFather flow, finding chat ID, finding user ID, security notes.
7. ntfy setup: public ntfy.sh caveats, self-hosted recommendation.
8. Threat model summary + link to `docs/threat-model.md`.
9. Updating + backup (volumes).
10. Troubleshooting link.
11. Contributing + license.

**Threat-model doc:** mirrors `PLAN.md §4` with more detail, including "if you want to expose externally" checklist.

**Troubleshooting:** common failures: ML token mismatch, MJPEG unreachable, pycentauri protocol mismatch, watcher stalled, Telegram auth refused.

**Acceptance:**
- A reviewer unfamiliar with the project can deploy on Coolify following only the README.
- Every env var in `.env.example` is documented in the README.
- No broken internal links (CI link-check).

---

## Issue #14 — End-to-end verification on real hardware

**Goal:** a checklist proving v0.1 actually works against a real Centauri Carbon 2.

**Deliverable:** `docs/e2e-checklist.md` with each item ticked + log excerpts. PR description includes a short demo video or GIF.

**Checklist:**
- [ ] Fresh Coolify deploy from Git with only `PRINTER_IP` set. Stack healthy in <90s.
- [ ] Status page reachable on LAN; camera image visible.
- [ ] Telegram bot responds to `/status` and `/snapshot`.
- [ ] Start a print; warmup state visible; transitions to armed after `DETECTION_WARMUP_SECONDS`.
- [ ] Force a failure (deliberate spaghetti) → detection counter climbs → pause fires → Telegram + ntfy alerts arrive with snapshot.
- [ ] Pause failure simulation (block pycentauri socket): alert still fires, marked `pause_failed`.
- [ ] Watcher-stall simulation (kill watcher task): stall alert fires once.
- [ ] Camera-offline simulation (block MJPEG): state shows `CAMERA_OFFLINE`; no false pauses.
- [ ] Stop/confirm flow in Telegram works; `/stop` without `/confirm` does nothing after 30s.
- [ ] Restart stack: detection history and pause history persist.
- [ ] `EXTERNAL_BIND_ALLOWED=false` + bind on public interface: app refuses to start with the documented error.
- [ ] All healthchecks green throughout.

**Acceptance:**
- Checklist committed with evidence (timestamps, log lines, screenshots).
- Any failures filed as v0.2 issues, not blockers, unless they violate the v0.1 scope.
