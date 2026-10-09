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
re-detection inside the 30-second window still publishes a real pause. The same
exit handling runs when a paused printer drops to `OFFLINE` or `STALLED` and
then comes back printing.

Heartbeats are written at most every `max(10, WATCHER_STALL_SECONDS / 2)`
seconds. The watchdog, `/readyz` and the dashboard all judge staleness with
`effective_stall_seconds()` (`sentinel/watcher/state.py`):
`max(WATCHER_STALL_SECONDS, heartbeat interval + 2 × poll interval)`. Deriving
it from both intervals stops a large poll interval or heartbeat throttling from
producing false stall alerts.

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
camera fails, the printer is paused or resumed externally, or a new job starts,
so hits never carry across contexts. The same reset ends a pause-failure
episode, so the next failure alerts again instead of being treated as a retry.

Snoozing detection remembers whether detection was on. When the snooze ends,
detection is re-enabled only if it was enabled when the snooze began.

## Safety rules

These are the invariants. Changing any of them changes what happens to a real
printer with hot plastic in it.

- **Pause only through `_publish_pause()`.** Confirmed detection and the ML
  fail-closed path are its only callers. The watcher never pauses during `IDLE`
  or `WARMUP`, and calls `stop()` only for the auto-stop escalation.
- **State follows the printer, not the intent.** The watcher moves to `PAUSED`
  only after the pause command is acknowledged, or after live status confirms
  the printer really is paused. A failed pause leaves it `ARMED` so the next
  tick retries.
- **Only an unknown outcome can be adopted.** A pause is "unknown" when the
  command was published but never acknowledged, or the call was cancelled
  mid-flight, so the printer may have acted on it. If live status shows `paused`
  within 60 seconds of that, it is Sentinel's own pause landing late: the watcher
  takes ownership (so `AUTO_STOP_TIMEOUT_SECONDS` still applies), records an `ok`
  `pause_history` row and sends no "paused externally" alert. A `failed` pause
  (rejected by the printer or never published) cannot land, so it never arms
  adoption and a later pause is external. The marker is cleared by a successful
  pause, the job ending or changing, a resume, and the adoption itself. Camera
  errors and disabled detection leave it alone. An operator pause from the web
  API or Telegram `/pause` clears it first, and any pause seen outside the window
  is the operator's. Neither is ever adopted or auto-stopped.
- **The pause publish is shielded from cancellation.** A cancellation arriving
  mid-publish waits up to 15 seconds for the MQTT command to complete, and
  shutdown awaits the watcher before closing the printer client. The compose
  file sets `stop_grace_period: 30s` on the `sentinel` service so Docker's 10
  second default does not SIGKILL the process during that wait.
- **Shutdown always runs to the end.** uvicorn runs with
  `timeout_graceful_shutdown=5` (`sentinel/server.py`), because an open dashboard
  `/stream` connection never closes on its own and would otherwise stall shutdown
  until Docker's SIGKILL, skipping the watcher unwind and `db.checkpoint()`.
  5 + 15 seconds fits inside the 30-second grace period. `SentinelServer` also
  stops uvicorn from re-raising SIGTERM after `serve()` returns, which would kill
  the process (exit 143) before that sequence ran. A second signal sets uvicorn's
  `force_exit`, which cancels remaining connections and tasks; it does not
  shorten the wait on an open `/stream`, which the 5-second timeout bounds.
  Outside Docker a second Ctrl+C during the watcher's 15-second unwind reaches
  asyncio's own SIGINT handler, which cancels main and skips `bot.stop()` and the
  client closes (`db.checkpoint()` still runs).
- **Fail closed on ML failure.** After `ML_CONSECUTIVE_FAILURE_THRESHOLD`
  consecutive ML errors, the printer is paused rather than left unwatched. The
  attempt is recorded in `pause_history` with source `ml_failure`.
- **Retries are silent, alerts are not.** When a pause keeps failing, the
  command is retried every tick but the operator is alerted once per episode,
  with one snapshot and one detection row. Every attempt is still written to
  `pause_history`.
- **Stop is never automatic without a timeout.** `AUTO_STOP_TIMEOUT_SECONDS`
  defaults to `0`, meaning disabled. When set, a Sentinel-initiated pause that
  outlives it escalates to a stop, and the escalation stays eligible to retry
  until a stop actually succeeds.
- **Commands must be acknowledged, and are published once.** An unacknowledged
  command raises `PrinterCommandUnacknowledgedError` rather than reporting
  success. It is never re-published by the client's retry loop, because the
  printer may already have acted on it. That includes a publish that timed out
  waiting for the broker's confirmation: the packet was already handed to the
  socket. Only a publish paho refused synchronously (`MqttCodeError`: no socket,
  queue full) counts as not sent and stays retryable. Retries otherwise cover
  only connecting and registering. An unacknowledged pause keeps the 30-second debounce, so the
  next tick checks live status before sending anything again.

## Notification fan-out

`NotificationDispatcher` is fire-and-forget by design: the detection loop must
never block on a slow Telegram API. Each alert becomes a background task with a
strong reference, a 90-second ceiling, and a retry policy that covers transient
network errors only. Permanent failures (revoked token, 4xx, Telegram
`BadRequest`) are not retried,
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
