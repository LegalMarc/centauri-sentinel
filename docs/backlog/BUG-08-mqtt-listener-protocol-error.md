# MQTT Listener Re-Raises on Malformed Messages

**ID:** BUG-08
**Severity:** Low
**Category:** Bugs
**Status:** Closed

## Affected Files
- `sentinel/printer/client.py` (lines 302-303) — `_listen_loop` re-raises `PrinterProtocolError`

## Description
When `_listen_loop` encounters a malformed MQTT message, it raises `PrinterProtocolError` instead of logging the error and continuing to process subsequent messages. A single bad message tears down the entire listener.

## Evidence
- `client.py` lines 302-303 re-raise `PrinterProtocolError` from `_parse_status`.
- No try/except around individual message processing within the loop body.

## Impact
- A single malformed MQTT message from the printer firmware causes the listener to crash.
- Reconnection logic restarts the listener, but any buffered messages are lost.
- Printer status goes stale until reconnection completes.

## Acceptance Criteria
- [x] Single malformed message logged at WARNING level and skipped
- [x] Listener loop continues processing subsequent messages
- [x] Counter or metric tracks malformed message frequency
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
