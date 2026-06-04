# Bot Runner Crash Stays Down Until Container Restart

**ID:** STAB-03
**Severity:** Medium
**Category:** Stability
**Status:** Closed

## Affected Files
- `sentinel/__main__.py` — bot runner task has no supervisor/restart logic

## Description
If the Telegram bot runner task crashes (e.g., due to an unhandled exception or network error), it remains down for the lifetime of the container. The only recovery is a full container restart. No alert is sent when the bot goes down.

## Evidence
- `__main__.py` launches the bot runner as an asyncio task with no exception handler or restart wrapper.
- No exponential backoff or retry logic around the bot lifecycle.

## Impact
- Bot becomes unresponsive — users cannot issue commands or receive alerts via Telegram.
- Silent failure: no notification that the bot is down.
- Requires manual intervention or external health-check to recover.

## Acceptance Criteria
- [x] Supervisor loop restarts bot runner with exponential backoff on crash
- [x] Maximum backoff capped (e.g., 5 minutes)
- [x] ntfy alert sent when bot crashes and when it successfully recovers
- [x] Crash count exposed via healthcheck endpoint
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
