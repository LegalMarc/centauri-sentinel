# Container Health Check Uses /readyz Instead of /healthz

**ID:** STAB-07
**Severity:** High
**Category:** Stability
**Status:** Closed

## Affected Files
- `sentinel/healthcheck.py`

## Description
The Docker healthcheck queries `/readyz`, which returns HTTP 503 if the printer is turned off or the camera is offline. This causes orchestrators to mark the container unhealthy and restart it repeatedly in a crash/restart loop.

## Acceptance Criteria
- [x] Modify `sentinel/healthcheck.py` to query `/healthz` instead of `/readyz`.
- [x] Verify that when the printer is offline (so `/readyz` is 503 but `/healthz` is 200), the healthcheck script exits with status 0.
