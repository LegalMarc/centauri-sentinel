# 2026-10-09 code review and persistent login sessions

Branch: `fix/persistent-sessions-and-review`.

## Requested

1. Code-quality review of the whole codebase, fixing anything meaningful.
2. Stay logged in to the dashboard until someone presses a logout button.

## Done

- `0ce10c4` Persistent server-side sessions (`sentinel/web/sessions.py`, `auth_sessions`
  table, schema v8). No expiry until logout. Cookie re-issued with a 400-day Max-Age at most
  daily. Logout revokes server-side. Dashboard Log out button. Basic auth no longer creates
  sessions. See `docs/threat-model.md#attack-surface`.
- `e2d21fa` Shared `effective_stall_seconds` helper in `sentinel/watcher/state.py`.
- `68dd451` All review findings below fixed with regression tests.
- `87a444a`, `fa0cee3`, `e1ee155` Three deep-tier review passes over the branch and their
  fixes: late-landing pause adoption (60 s window, unacknowledged only), operator pause and
  resume hooks, graceful shutdown (uvicorn 5 s timeout, no SIGTERM re-raise,
  `stop_grace_period: 30s`), config bounds clamp instead of reject, login `next` hardening,
  Basic-auth digest cache. The third pass found no critical or high issues. 760 tests pass,
  93.7% coverage.

## Review findings fixed

Three reviewers (watcher, printer/camera/ML, bot/notify/db/web) reported:

- Printer: ack timeout re-publishes commands; MQTT auth-failure rc check never matches
  (ReasonCode vs int); AttributeError when the listener is closed mid-wait; DNS failure
  not retried.
- Watcher: pause-failure alert flag outlives its episode (silent second failure); pause
  shield does not survive shutdown; ML fail-closed pause is unshielded and unaudited;
  `_ml_error_count` not reset on disable; PAUSED exit via OFFLINE skips the exit hook;
  snooze re-enables detection that was disabled.
- Web/config: every settings save reconnects printer and camera; no upper bounds on poll
  interval / confirm count / warmup; settings update not atomic, list body is a 500;
  `/readyz` and dashboard use raw stall threshold; Telegram photo fallback duplicates alerts.

## Deferred (need a decision or hardware)

- Per-command MQTT registration ids may exhaust printer client slots. Needs hardware check.
- Whether an ML-outage fail-closed pause should count toward auto-stop.
- MJPEG `grab()` can return the same stale frame for up to 10 s (only matters for poll < 4 s).
- `healthcheck.py` always probes localhost, so a specific non-loopback `BIND_HOST` is unhealthy.

## Next step

Merged to local `main`. Not pushed: pushing may trigger a Coolify redeploy to the live
printer host, so that is the owner's call. After deploy, sign in once; the session then
persists until Log out.

## Phase 2 (2026-10-11): UI redesign and auth follow-ups

Branch `feat/ui-redesign-and-auth-followups`. Owner decisions: hold the push until more
changes land (Q1); add a settings toggle for ML-outage auto-stop, default off (Q2); use the
real printer to settle the MQTT slot question (Q3); add "log out all devices" (Q4); redesign
the dashboard with OpenBridge.

- `f1105cc` Browser requests never get the Basic popup (JSON 401, page goes to /login);
  `ml_failure_auto_stop`; POST /logout-all; `python -m sentinel.diagnostics mqtt-slots`.
- `45683f4`, `3ac23fe` OpenBridge dashboard (vendored 1.0.1, Apache-2.0; 2.x is AGPL),
  styled login, static versioned assets, Dockerfile web stage, `tools/dev_preview.py`.
  See `docs/ui.md`.

Q3 status: the printer is on 192.168.40.x, unreachable from the dev Mac (192.168.10.x), and
the `servany` SSH key is restricted to an inventory command. After deploy, run
`python -m sentinel.diagnostics mqtt-slots` in the Coolify terminal of the sentinel
service while the printer is idle, then decide whether to reuse one registration id.

- `82fe1e3`, `b9cdca6`, `f299087` Three deep-tier review passes and fixes: nested logout
  forms (Log out was submitting settings), fail-closed mqtt-slots guard (allowlisted idle
  states only), Stop double-click guard, Enter saves settings, full third-party licenses.
  868 tests pass. Merged to local `main`; not pushed.

Next: owner decides when to push (Q1). After deploy, run the mqtt-slots diagnostic (Q3).

## Phase 3 (2026-10-20/21): QA, push and deploy

QA on local `main` before pushing (all passed):

- `ruff check`, `ruff format --check`, `mypy sentinel/ tests/`: clean. pytest: 870 passed,
  93.69% coverage.
- `docs_sync.py audit`: clean.
- `docker build`: succeeded. The image contains `ob.js`, `openbridge.css`, `NotoSans.ttf` and
  `LICENSE.txt` under `/app/sentinel/web/static/vendor/openbridge`.
- Manual UI pass in the built-in browser against both preview servers. Checked: Monitor fold
  at 1440×900 and 1280×720; Pause, Resume and Stop (a second Stop click under 600 ms is
  ignored); snooze chip; local times in History; Save and Enter; ML-outage toggle round trip;
  all four palettes with tracks visible; one bottom row at 375×812; styled login; user menu;
  both Log out paths (API returns 401 with no `WWW-Authenticate`); a session ended elsewhere
  sends the open page to /login in about 1 s.

Found and fixed in `899cc7f`, with regression tests:

- At 1280×720 the rail (recent activity) scrolled the page by 120 px. Side by side, the rail
  is now capped at the height left under the bars and scrolls on its own.
- The preview's fake watcher ignored Snooze, so the "Snoozed to HH:MM" chip could not be
  checked. It now writes the same settings as `WatcherLoop.snooze`.

Pushed `44077a5..899cc7f` (19 commits). That range includes `96e1c9d` (ADR 0001, docs only),
which another session committed to `main` during QA. CI run 39541708354: the first attempt
failed because the test runner could not resolve github.com during checkout. The rerun passed
(869 passed, 1 skipped by design because the runner is root, 93.66% coverage), and the Docker
build passed.

Deploy: no webhook is configured, so the push did not deploy. Deployed through the Coolify
API (`POST /api/v1/deploy`, application `orlnd6yvyq885pegzrl4omvt`, deployment
`cpce5pry75eljezmrjkmzcuc`, commit `899cc7f`). `/healthz` returns 200. `/readyz` reports db,
watcher, mqtt and camera all OK. `/login` serves asset version `7e774e30cfe2`.

Owner feedback after signing in, fixed in `30f4e25` and deployed through the API
(deployment `o7shu0vzghfbvm5namtpmhml`, asset version `2e7aba2b228f`, `/readyz` ready):

- Snooze length is now a menu on the Snooze button. It opens upward on phones.
- The camera chip showed "LIVE" with no resolution because the `load` event is unreliable for
  MJPEG. The size is now also read on every poll.
- The score age read "896678s ago". It now reads "10d ago".
- With the printer idle, the phone bar shows only Snooze, as designed.

Still open:

- **F1 (critical, needs a decision and hardware).** The status listener only subscribes to
  pushes, and the printer pushes deltas. After every listener (re)connect the merged state
  is cleared and holds only the blocks that changed since. Production logs show
  `print_status`, `machine_status` and `heater_bed` missing, so the bed reads "—".
  `_parse_status` defaults a missing `print_status.state` to "idle", so a reconnect,
  restart or redeploy mid-print makes the watcher record the job as ended and go IDLE. It
  then stays IDLE until the state changes, so detection is off for the rest of the print.
  An idle printer also hits the 15 s read timeout and reconnects about every 30 s.
  Proposed fix: request a full snapshot (`1002` GET_STATUS) after each status connect;
  treat a merged state without `print_status.state` as unknown (stale), never idle; keep
  the last state across reconnects; and stop treating 15 s of silence while idle as a
  failure. GET_STATUS needs a registered command session, so settle Q3 (slot capacity)
  first.
- Read the camera resolution from the chip now that it renders.
- Q3: run `python -m sentinel.diagnostics mqtt-slots` in the Coolify terminal while the
  printer is idle, then act on its last JSON line (see `docs/troubleshooting.md`).
- Coolify has no push webhook for this app. Either add one or deploy through the API or UI
  after each push.

## Phase 4 (2026-10-21): F1 status fix and the slot diagnostic

- Coolify web terminal fixed in homelab-iac (`8660d71`): the admin-allowlist router
  (priority 200) captured `/terminal/ws` and `/app`; two priority-210 routers now send them to
  the terminal server and reverb.
- `97fbd7a` (on main, not deployed): the diagnostic uses the printer address saved in
  Settings, as the service does. Production saves 192.168.40.18; the `PRINTER_IP` env value is
  stale (update it in Coolify at the next deploy).
- Live probes (recorded in `docs/verified-assumptions.md` §3): pushes are deltas; GET_STATUS
  returns the full state; idle is `state ""`, `enable true`, `machine_status.status 1`.
- Branch `fix/status-snapshot-and-print-state` (pushed, not merged): GET_STATUS snapshot after
  a reconnect, unknown state never idle, `""` idle only with status 1, `enable` ignored, 45 s
  silence tolerance; the diagnostic learns the serial over HTTP and the state from GET_STATUS,
  and watches for a print start with one open subscription. 906 tests pass, 93.59%.
  A deep-tier review is running.

Next: fix all review findings, re-review until clean, merge, deploy (printer idle; check the
logs show "Printer status snapshot loaded" and the bed temperature in the dashboard), then
have the owner run `python -m sentinel.diagnostics mqtt-slots` and capture GET_STATUS during a
print (`docs/verified-assumptions.md` §3 live probes) before trusting the `""` rule mid-print.
