# MQTT Payload Silently Defaults on Missing Fields

**ID:** BUG-04
**Severity:** Medium
**Category:** Bugs
**Status:** Completed

## Affected Files
- `sentinel/printer/client.py` — `_parse_status` defaults to `0`/`None` for missing fields without logging

## Description
The `_parse_status` method silently defaults to `0` or `None` when expected fields are missing from the MQTT payload. This masks firmware changes, protocol mismatches, or corrupted messages that would otherwise be caught early.

## Evidence
- `_parse_status` uses `.get()` with silent defaults for critical fields like print progress, temperatures, and state.
- No warning logged when expected keys are absent.

## Impact
- Incorrect printer status displayed on dashboard (e.g., 0% progress when field is simply missing).
- Silent data corruption makes debugging firmware/protocol issues difficult.
- Watcher logic may make incorrect decisions based on defaulted values.

## Acceptance Criteria
- [x] Log a warning when key fields are missing from the MQTT payload
- [x] Distinguish between "field is 0" and "field is missing" in parsed status
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
