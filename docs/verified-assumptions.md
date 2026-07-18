# Verified Assumptions — centauri-sentinel spike #0

Conducted: 2026-05-23
Printer under test: Elegoo Centauri Carbon 2 @ 192.168.40.23
Spike scripts: `spike/01_obico_image_inspect.sh`, `spike/02_obico_api_probe.py`,
`spike/03_pycentauri_probe.py` (adapted to async), `spike/04_mjpeg_soak.py` (adapted for Python 3.10)

---

## 1. Obico ML Container

**Finding: No pre-built image — must build from source. ARM64 supported via base image.**

| Field | Value |
|---|---|
| Base image | `thespaghettidetective/ml_api_base:1.4` |
| amd64 | yes (confirmed via Docker Hub manifest API) |
| arm64 | yes (confirmed: `ml_api_base:1.4` manifest lists `arm64`) |
| Production image | Must be built from [obico-server](https://github.com/TheSpaghettiDetective/obico-server) `ml_api/` directory |
| Dockerfile | `ml_api/Dockerfile` (`FROM thespaghettidetective/ml_api_base:1.4${IMAGE_TAG_SUFFIX}`) |
| Port | 3333 |
| Healthcheck | `GET /hc/` → returns `"ok"` |
| Source URL | `https://github.com/TheSpaghettiDetective/obico-server/blob/master/ml_api/Dockerfile` |

**Notes:** The `thespaghettidetective/ml_api:latest` image on Docker Hub dates from 2019 and should not be used. The base image `1.4` supports both architectures. The `IMAGE_TAG_SUFFIX` build arg selects the arch-specific base:
- `linux/amd64`: no suffix (default)
- `linux/arm64`: set `IMAGE_TAG_SUFFIX=-linux-arm64` in CI buildx

**Plan amendment:** `docker-compose.yml` must build `obico-ml` from a Git submodule or embed the Dockerfile. A pre-built GHCR image should be created in CI (`ticket/01` CI workflow) to avoid pulling the full obico-server repo at deploy time.

---

## 2. Obico ML API Surface

**Finding: URL-fetch only (GET). No POST multipart upload. This invalidates the PLAN.md §3 "POST-from-sentinel primary" assumption.**

| Field | Value |
|---|---|
| Endpoint | `GET /p/` |
| URL-fetch supported | **yes** — via `?img=<url>` query param |
| POST multipart supported | **no** — the route only handles GET |
| Auth mode | `Authorization: Bearer <token>` header |
| Token source | `ML_API_TOKEN` env var (if unset, auth is disabled) |
| Score field | `detections[i][1]` (confidence float 0.0–1.0) |
| Full response shape | `{"detections": [["label", confidence, [x, y, w, h]], ...]}` |
| Source URL | `https://github.com/TheSpaghettiDetective/obico-server/blob/master/ml_api/server.py` |
| Auth source | `https://github.com/TheSpaghettiDetective/obico-server/blob/master/ml_api/auth.py` |

**Plan amendment (significant):** PLAN.md §3 Key Revision #1 ("ML input is POST-from-sentinel, not URL-fetch") is inverted. URL-fetch is the **only** mode the ML API supports. The sentinel must serve an ephemeral internal endpoint for ML to fetch, or we implement a thin POST proxy. The recommended approach for v0.1: sentinel serves a single-use nonce endpoint (`/__internal_snapshot/<nonce>`) as already specified in issue #6 fallback — this is now the primary (and only) mode.

**Score interpretation:** Any value ≥ `ML_SCORE_THRESHOLD` (default 0.4) in `detections[i][1]` counts as a positive. Multiple detections can appear per frame; use the maximum score.

---

## 3. Printer Control (Centauri Carbon 2 vs pycentauri)

**Finding: CRITICAL PLAN INVALIDATION — The Centauri Carbon 2 uses MQTT (port 1883), not SDCP WebSocket (port 3030). pycentauri 0.4.2 does NOT support the Carbon 2.**

### Confirmed port map (live scan 2026-05-23)

| Port | Status | Service |
|---|---|---|
| 80 | OPEN | libhv/1.3.4 HTTP server + WebSocket (read-only, no SDCP response) |
| 1883 | OPEN | MQTT broker (requires auth) |
| 3030 | CLOSED | — (original Carbon 1 SDCP; not present on Carbon 2) |
| 3031 | CLOSED | — (pycentauri camera; not present on Carbon 2) |
| 8080 | OPEN | MJPEG camera stream |

### Carbon 2 control protocol (source: [ELEGOO-3D/elegoo-link](https://github.com/ELEGOO-3D/elegoo-link))

| Field | Value |
|---|---|
| Protocol | MQTT v3/v5 |
| Broker | `mqtt://192.168.x.x:1883` |
| MQTT username | `elegoo` |
| MQTT password | Printer access code (set in printer network settings; default `123456` but usually changed) |
| Command topic | `elegoo/<sn>/<clientId>/api_request` |
| Response topic | `elegoo/<sn>/<clientId>/api_response` |
| Status push topic | `elegoo/<sn>/api_status` |
| Registration topic | `elegoo/<sn>/api_register` |
| Discovery probe | UDP port 3000, payload `{"id": 0, "method": 7000}` |
| HTTP info endpoint | `GET http://<ip>/system/info?X-Token=<accessCode>` |

### Message format

Request:
```json
{"id": 1234, "method": <int>, "params": {}}
```

**Live-observed message format (read-only, from active print 2026-05-23):**

Status push on `elegoo/<sn>/api_status`:
```json
{
  "id": 531900,
  "method": 6000,
  "result": {
    "gcode_move": {"extruder": 43.1, "speed": 2400, "x": 200.0, "y": 140.4},
    "print_status": {"print_duration": 5790, "total_duration": 5872}
  }
}
```
- Method `6000` = status push
- `print_status.print_duration` = seconds elapsed in current print job
- Printer serial number observed: `F0113YK6ZM8FV2F` (format: `F01<model><sn>`)

Heartbeat: the broker sends PING, client responds PONG on `elegoo/<sn>/<clientId>/api_request` and `api_response`.

Known method codes (from elegoo-link source + live observation):
- `6000`: status push (server → client)
- Pause: maps to `MethodType::PAUSE_PRINT`
- Resume: maps to `MethodType::RESUME_PRINT`
- Stop: maps to `MethodType::STOP_PRINT`

### ⚠️ CORRECTION (2026-07-18): control-command codes + firmware-02.x registration

The numeric command codes were previously **wrong** in the client (pause=1001,
resume=1002, stop=1003). Those are actually *read* queries — `1001` =
GET_ATTRIBUTES, `1002` = GET_STATUS — so "pause" silently queried attributes and
the printer never paused. Verified correct CC2 codes (source:
[danielcherubini/elegoo-homeassistant `cc2/const.py`](https://github.com/danielcherubini/elegoo-homeassistant),
cross-checked against the community `CC2_PROTOCOL.md`):

| Operation | Method code |
|---|---|
| Start print | 1020 |
| **Pause** | **1021** |
| **Stop** | **1022** |
| **Resume** | **1023** |

Command envelope: `{"id": <int>, "method": <code>, "params": {}}` (all three
fields; `id` is echoed in the ack for matching).

**Firmware 02.x requires a registration handshake before any `api_request`
command is honoured.** Status pushes on `elegoo/<sn>/api_status` are broadcast
and need no registration (which is why detection kept working while pausing was
silently dropped). The command path must, on the same MQTT session:

1. Subscribe to `elegoo/<sn>/<request_id>/register_response` and
   `elegoo/<sn>/<client_id>/api_response`.
2. Publish `{"client_id", "request_id"}` to `elegoo/<sn>/api_register`.
3. Wait for `{"error": "ok"}` on the register_response topic (3 s timeout;
   `"too many clients"` / `"fail"` are rejections).
4. Only then publish the command to `elegoo/<sn>/<client_id>/api_request` and
   confirm the matching-id ack on `api_response`.

`client_id` (`0cli<ts><rand>`, 10 chars) and `request_id` (16-hex + ts) formats
mirror the Elegoo web interface; the firmware is picky about their shape.
See `sentinel/printer/client.py::_send_command` and `_generate_cc2_ids`.

### pycentauri status (0.4.2)

pycentauri explicitly notes: *"The newer Centauri Carbon 2 uses a different JSON-RPC probe and is not supported here."* (discovery.py docstring). The `Printer.connect()` hardcodes port 3030 which is closed on the Carbon 2.

**pycentauri IS removed from v0.1 deps. Replace with `aiomqtt` + custom Carbon 2 client.**

### Plan amendment (significant)

Issue #4 (printer client) is redesigned:
- Remove `pycentauri` dependency
- Add `aiomqtt>=2.0` to deps
- `PRINTER_ACCESS_CODE` is a new **required** env var when the printer has auth enabled
- `PrinterClient` uses MQTT for status, pause, resume, stop
- Status polling: subscribe to `elegoo/<sn>/api_status` for push updates; or send a get-status request
- No `asyncio.to_thread` needed (aiomqtt is fully async)

New env var added to PLAN.md §5:

| Var | Default | Purpose |
|---|---|---|
| `PRINTER_ACCESS_CODE` | `123456` | MQTT broker password for Carbon 2. Must match printer's access code. |
| `PRINTER_MQTT_PORT` | `1883` | |

---

## 4. MJPEG Camera Stream

**Finding: Camera confirmed at port 8080 (not 3031). Paths `/`, `/mjpeg`, and `/video` all serve MJPEG.**

| Field | Value |
|---|---|
| URL | `http://<ip>:8080/mjpeg` (also `/` and `/video` work identically) |
| Content-Type | `multipart/x-mixed-replace; boundary=frame` |
| Frame size (observed) | ~34 KB (at printer default resolution) |
| Auth | None |
| Soak test duration | 2 minutes (short; production should use longer) |

### Soak test results (2026-05-23, 2-minute run)

- Stream connected immediately (HTTP 200)
- No disconnects observed during the 2-minute window
- Full 60-minute soak deferred to ticket #14 (hardware E2E); 2-minute run sufficient to verify no systematic reconnect pattern at this time window

**Plan amendment (minor):** Default `PRINTER_MJPEG_PORT=8080` is already correct in PLAN.md §5. Default `PRINTER_MJPEG_PATH=/mjpeg` is also correct. No change needed. The pycentauri `camera.py` hard-codes port 3031 which is incorrect for Carbon 2; confirmed `/mjpeg` on 8080 works directly.

**Recommended backoff** (from 2-minute soak — stream was stable so no disconnects to tune from):
- Start delay: 0.5s
- Cap: 30s
- Retries before `CameraOfflineError`: 3

---

## 5. Coolify Deployment

**Finding: Coolify 4.1.0. Docker Compose stacks deployed as "services" via API. Confirmed working.**

| Field | Value |
|---|---|
| Coolify version | 4.1.0 |
| API base | `https://<your-coolify-host>/api/v1` |
| Auth | `Authorization: Bearer <api-key>` |
| Create service | `POST /api/v1/services` with `docker_compose_raw` (base64) |
| Start | `POST /api/v1/services/<uuid>/start` |
| Status | `GET /api/v1/services/<uuid>` → `.applications[].status` |
| Delete | `DELETE /api/v1/services/<uuid>` |

### Tested flow (2026-05-23)

1. Created project `centauri-sentinel` → UUID `x8aw7bw1uohncnfgmklt02yk`
2. Environment `production` auto-created → UUID `qvsjovwtjn8t08tq1xv5r3j7`
3. Server `localhost` UUID `uc4w00gwssko08gsok8kog4s`
4. Created test service with base64 compose → received UUID `rctykydq8ulx5wb8s7ulchnq`
5. Start request returned `"Service starting request queued."`
6. Status polling confirmed: `.applications[].status` = `"exited"` (nginx had no config to serve)
7. Delete confirmed working

**Note:** `docker_compose_raw` must be base64-encoded. The service creation endpoint returns `{"uuid": "..."}` on success.

**Coolify deploy URL:** No deep-link button for automatic deploy exists in v4.1.0 UI. The manual flow is: Dashboard → New Resource → Service → provide compose YAML. Via API: the `POST /api/v1/services` flow documented above. For v0.1 ticket #12, the Coolify deploy script will use this API flow.

---

## Summary of Plan Amendments

The following require a `PLAN.md` amendment in this commit:

| # | Finding | Amendment |
|---|---|---|
| A | ML API is URL-fetch only (no POST) | Issue #6: URL-fetch is primary mode. Remove POST multipart path. |
| B | pycentauri does not support Carbon 2 | Issue #4: Replace pycentauri with aiomqtt + custom MQTT client. Add `PRINTER_ACCESS_CODE` env var. |
| C | Camera is at port 8080 (confirmed) | PLAN.md §5: No change (already 8080). pycentauri camera code is irrelevant. |
| D | Obico ML must be built from source | Issue #12: Add token-init + ml-api build stage to docker-compose.yml. |

See `PLAN.md` for the amended text.
