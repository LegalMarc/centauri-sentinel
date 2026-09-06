# Architecture — how a frame becomes a paused print

Sentinel watches the printer's camera, scores each frame with Obico's
spaghetti-detection model, and pauses the print when enough consecutive frames
score above the threshold. This document covers the moving parts and the rules
that govern them. Wire-level protocol details live in
[verified-assumptions.md](verified-assumptions.md); symptoms and fixes live in
[troubleshooting.md](troubleshooting.md).

## Components

| Component | Module | Responsibility |
|---|---|---|
| Watcher | `sentinel/watcher/loop.py` | The state machine and detection loop; the only component that decides to pause |
| Printer client | `sentinel/printer/client.py` | MQTT status subscription and control commands (pause/resume/stop) |
| Camera | `sentinel/camera/mjpeg.py` | One persistent MJPEG connection, fanned out to grabbers and stream viewers |
| ML client | `sentinel/ml/client.py` | Scores a JPEG via the Obico API, which fetches the image back through a nonce URL |
| Dispatcher | `sentinel/notify/dispatcher.py` | Fire-and-forget fan-out to Telegram and ntfy |
| Web | `sentinel/web/` | Dashboard, JSON API, camera proxy, auth middleware |
| Bot | `sentinel/bot/` | Telegram commands and inline keyboards |
| Database | `sentinel/db/` | SQLite: detections, pauses, print jobs, runtime settings, heartbeat |

The watcher owns the printer, camera, ML client, database, and dispatcher. The
web and bot layers reach the printer through the watcher so that state
transitions stay in one place.

## Watcher state machine

`run_forever()` starts three tasks in a `TaskGroup`: the detection loop, a
heartbeat watchdog, and a periodic cleanup job.

| State | Meaning | Detection |
|---|---|---|
| `IDLE` | Printer is not printing | off |
| `WARMUP` | Printing, but still inside `DETECTION_WARMUP_SECONDS` | off |
| `ARMED` | Printing past warmup | **on** |
| `PAUSED` | Paused, whether by Sentinel or externally | off |
| `CAMERA_OFFLINE` | Camera unreachable | off, retried each tick |
| `OFFLINE` | Printer status is stale | off |
| `STALLED` | Watchdog saw a stale heartbeat | off |

Warmup exists because the first layers look nothing like a healthy print, and
scoring them produces false positives.

Transitions out of `PAUSED` are deliberately narrow. An external resume (from
the printer's own screen) is recognised only after the printer has reported
`printing` and at least five seconds have passed since the pause, which
prevents a stale status push from bouncing the watcher back to `ARMED`.
Setting state away from `PAUSED` also clears the printer's pause debounce, so a
re-detection inside the 30-second window still publishes a real pause.

The watchdog compares the heartbeat age against
`max(WATCHER_STALL_SECONDS, 2 × poll interval)`. Deriving the threshold from
the poll interval stops a large interval from producing false stall alerts.

## Detection path

Each tick, while `ARMED` and detection is enabled:

1. Skip if inside the post-resume cooldown, so a frame from before the resume
   cannot immediately re-pause the print.
2. Grab a frame. A `CameraOfflineError` moves to `CAMERA_OFFLINE` and alerts once.
3. Score it. The JPEG goes into an in-memory nonce store and the ML API fetches
   it back from `/__internal_snapshot/<nonce>`, because the API accepts a URL
   rather than an upload.
4. Compare against `ML_SCORE_THRESHOLD`. Above it, increment the confirm
   counter; below it, reset to zero.
5. At `ML_CONFIRM_COUNT` consecutive hits, pause.

Requiring consecutive hits is what makes the system usable. A single frame with
a bad shadow scores high; three in a row usually means the print is failing.
The counter resets whenever the score drops, detection is toggled off, the
camera fails, or a new job starts, so hits never carry across contexts.

## Safety rules

These are the invariants. Changing any of them changes what happens to a real
printer with hot plastic in it.

- **Pause only through `_on_confirmed_detection()`.** The watcher never calls
  `pause()` or `stop()` from anywhere else, and never during `IDLE` or `WARMUP`.
- **State follows the printer, not the intent.** The watcher moves to `PAUSED`
  only after the pause command is acknowledged, or after live status confirms
  the printer really is paused. A failed pause leaves it `ARMED` so the next
  tick retries.
- **The pause publish is shielded from cancellation.** A cancellation arriving
  mid-publish still lets the MQTT command complete.
- **Fail closed on ML failure.** After `ML_CONSECUTIVE_FAILURE_THRESHOLD`
  consecutive ML errors, the printer is paused rather than left unwatched.
- **Retries are silent, alerts are not.** When a pause keeps failing, the
  command is retried every tick but the operator is alerted once per episode,
  with one snapshot and one detection row. Every attempt is still written to
  `pause_history`.
- **Stop is never automatic without a timeout.** `AUTO_STOP_TIMEOUT_SECONDS`
  defaults to `0`, meaning disabled. When set, a Sentinel-initiated pause that
  outlives it escalates to a stop, and the escalation stays eligible to retry
  until a stop actually succeeds.
- **Commands must be acknowledged.** An unacknowledged pause raises rather than
  reporting success, so a silently-dropped command surfaces as a real failure.

## Notification fan-out

`NotificationDispatcher` is fire-and-forget by design: the detection loop must
never block on a slow Telegram API. Each alert becomes a background task with a
strong reference, a 90-second ceiling, and a retry policy that covers transient
network errors only. Permanent failures (revoked token, 4xx) are not retried,
because holding JPEG bytes in memory for minutes helps nobody. Concurrent tasks
are capped, and non-critical alerts are dropped first when the cap is hit.

A channel that fails completely is recorded in `failed_channels` and surfaced on
the dashboard, so a silently broken notifier is visible rather than assumed
working.

## Telegram bot

Every command checks two things before doing anything: the chat id matches and
the user id is in the allowlist. Unauthorised messages are logged and dropped
without a reply. Commands are rate limited per user.

Stopping a print takes two steps, `/stop` then `/confirm` within 30 seconds,
because cancelling a print is irreversible. Resuming transitions the watcher out
of `PAUSED` atomically, which keeps detection armed for the rest of the print
rather than leaving it silently disabled.
