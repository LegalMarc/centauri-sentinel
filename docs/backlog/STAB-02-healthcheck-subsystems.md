# Healthcheck Missing MQTT and Camera Subsystem Checks

**ID:** STAB-02
**Severity:** Medium
**Category:** Stability
**Status:** Closed

## Affected Files
- `sentinel/web/routes.py` — `/readyz` endpoint only checks DB and heartbeat
- `sentinel/healthcheck.py` — no MQTT or camera status aggregation

## Description
The `/readyz` readiness endpoint checks database connectivity and heartbeat freshness but does not report on MQTT connection status or camera availability. The system can report "ready" while critical subsystems are down.

## Evidence
- `/readyz` handler queries DB and checks last heartbeat timestamp.
- No check for MQTT client connection state.
- No check for camera reachability or last successful frame grab.

## Impact
- Load balancers and orchestrators route traffic to an instance with degraded subsystems.
- Operators get a false sense of system health.
- Silent failures in MQTT or camera go undetected until a print failure occurs.

## Acceptance Criteria
- [x] `/readyz` reports MQTT connection status (connected/disconnected)
- [x] `/readyz` reports camera subsystem status (reachable/unreachable)
- [x] Response includes per-subsystem breakdown in JSON body
- [x] Overall readiness is `false` if any critical subsystem is down
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
