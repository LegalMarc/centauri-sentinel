# centauri-sentinel Build Progress

## Ticket #0 — Spike: verify external assumptions
- Status: done
- PR: (see below)
- Started: 2026-05-23T18:35:00Z
- Finished: 2026-05-23T19:10:00Z
- Notes: All five assumptions resolved. Two significant plan amendments:
  (A) ML API is URL-fetch only (`GET /p/?img=<url>`) — no POST multipart. The single-use nonce endpoint in issue #6 becomes the primary ML integration path.
  (B) pycentauri does not support the Centauri Carbon 2. The Carbon 2 uses MQTT (port 1883) for control. Replaced pycentauri with `aiomqtt` + custom Carbon 2 MQTT client. New required env var: `PRINTER_ACCESS_CODE`.
  Camera confirmed at port 8080/mjpeg (matches PLAN.md defaults). Coolify v4.1.0 API flow verified.
  PLAN.md amended in this commit. Full details in `docs/verified-assumptions.md`.
  v0.2 candidates: full 60-min MJPEG soak (deferred to #14); Carbon 2 MQTT method code table (need one print cycle to observe all state transitions).

## Ticket #1 — Project scaffolding
- Status: done
- PR: #32
- Started: 2026-05-23T19:10:00Z
- Finished: 2026-05-23T19:30:00Z

## Ticket #2 — Config + safety guard
- Status: done
- PR: #33
- Finished: 2026-05-23T20:00:00Z

## Ticket #3 — Persistence layer
- Status: done
- PR: #33
- Finished: 2026-05-23T20:30:00Z

## Ticket #4 — Printer client
- Status: done
- PR: #34
- Finished: 2026-05-23T21:00:00Z

## Ticket #5 — Camera MJPEG grabber
- Status: done
- PR: #35
- Finished: 2026-05-23T21:30:00Z

## Ticket #6 — ML client
- Status: done
- PR: #36
- Finished: 2026-05-23T22:00:00Z

## Ticket #7 — Watcher loop
- Status: done
- PR: #37
- Finished: 2026-05-23T22:30:00Z

## Ticket #8 — Telegram notifier
- Status: done
- PR: #38
- Finished: 2026-05-23T23:00:00Z
- Notes: TelegramNotifier with chat+user allowlist, tenacity retry, 17 tests.

## Ticket #9 — ntfy notifier
- Status: done
- PR: #38
- Finished: 2026-05-23T23:00:00Z
- Notes: NtfyNotifier with Bearer auth, Priority/Tags headers, tenacity retry, 14 tests.

## Ticket #12 — Docker Compose stack
- Status: done
- PRs: #41 (stack), #43 (obico-ml CUDA fix), #44 (obico-ml CPU-only rewrite)
- Started: 2026-05-24T01:00:00Z
- Finished: 2026-05-24T16:10:00Z
- Notes: Three-service Docker Compose stack (token-init → obico-ml → sentinel). GHCR CI workflow for obico-ml (build-obico-ml.yml). Deployment via homelab Coolify (coolify.example.com, 192.168.1.50) using the /api/v1/applications/public endpoint with build_pack=dockercompose (the /dockercompose endpoint is absent in this Coolify build). App UUID: app-uuid-redacted. Three fixes required during deployment: (1) obico-ml source copy path and CMD fixed in PR #43; (2) 5 GB CUDA base image replaced with python:3.10-slim + onnxruntime CPU in PR #44 — the homelab VM has no NVIDIA GPU and only 1.9 GB free disk; (3) Coolify env vars set: PRINTER_IP, PRINTER_ACCESS_CODE, EXTERNAL_BIND_ALLOWED=true, SENTINEL_PORT=18000. Sentinel live with SSL via Traefik at https://sentinel.example.com/healthz → {"status":"ok"}. Both sentinel and obico-ml containers running:healthy.

## Ticket #13 — Documentation
- Status: done
- PR: #42
- Finished: 2026-05-24T03:30:00Z
- Notes: README full rewrite (quick start, config reference table, Telegram/ntfy setup, threat model summary). New docs: threat-model.md, printer-setup.md, troubleshooting.md. Updated coolify-deploy.md with PRINTER_ACCESS_CODE step.

## Ticket #11 — Telegram bot commands
- Status: done
- PR: #40
- Started: 2026-05-24T00:00:00Z
- Finished: 2026-05-24T00:45:00Z
- Notes: BotCommandHandler with all 9 commands + inline keyboard callbacks. Auth guard uses TelegramNotifier.is_authorized (chat+user allowlist). /stop+/confirm with 30s window tracked per user_id via time.monotonic(). Snooze callback disables detection and schedules asyncio re-enable task (task reference kept in module-level set to prevent GC). BotRunner does lazy PTB import and clean lifecycle (initialize/start/polling/stop/shutdown). 21 tests; full suite 201 tests, 88% coverage.

## Ticket #10 — Status web UI
- Status: done
- PR: #39
- Started: 2026-05-23T23:05:00Z
- Finished: 2026-05-23T23:55:00Z
- Notes: Full FastAPI app factory with optional DB/watcher/camera injection. AuthMiddleware (Basic auth + HMAC-SHA256 session cookie, 1h TTL) gates all routes except /healthz and /__internal_snapshot. Routes: / (Jinja2 status page, 10s meta-refresh), /snapshot, /stream, /readyz (heartbeat-age + DB-write check), /__internal_snapshot/{nonce} (single-use). Template: plain HTML, embedded CSS under 300 bytes, no CDN. 21 web tests; full suite 180 tests, 93% coverage. Assumption: printer state/elapsed not shown on status page since WatcherLoop does not expose last PrinterStatus as a public property — watcher state (IDLE/WARMUP/ARMED/PAUSED) conveys the same information for the operator. v0.2 candidate: add ML-reachability ping to /readyz.
