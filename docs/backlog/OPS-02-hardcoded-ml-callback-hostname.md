# Hardcoded Hostname "sentinel" for ML Callbacks Prevents Deployment Flexibility

**ID:** OPS-02
**Severity:** Medium
**Category:** Maintainability
**Status:** Closed

## Affected Files
- `sentinel/ml/client.py`
- `sentinel/config.py`

## Description
The ML callback URL generation logic hardcodes the hostname `"sentinel"` if running inside a Docker container. If the user deploys the service with a customized name in Docker Compose, the callback fails.

## Acceptance Criteria
- [x] Add a new configuration parameter, e.g. `SENTINEL_CALLBACK_HOST` (or `callback_host`), allowing users to configure the callback host/URL.
- [x] If the parameter is set, use it; otherwise, fall back to the existing auto-detection logic.
- [x] Verify that tests still pass and configuration is correctly validated.
