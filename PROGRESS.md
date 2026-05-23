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
- Status: in_progress
- Started: 2026-05-23T19:10:00Z
