# No Per-User Rate Limiting on Telegram Bot Commands

**ID:** SEC-07
**Severity:** Low
**Category:** Security
**Status:** Closed

## Affected Files
- `sentinel/bot/commands.py` — command handlers have no per-user throttle

## Description
Telegram bot command handlers execute without any per-user rate limiting. A malicious or compromised Telegram user could spam commands (`/status`, `/stop`, `/snooze`, etc.) causing excessive resource consumption on the sentinel process.

## Evidence
- `commands.py` processes every incoming command immediately with no throttle or cooldown logic.
- No decorator or middleware checks command frequency per `chat_id` / `user_id`.

## Impact
- Resource exhaustion (CPU, network) from rapid command spam.
- Potential interference with time-sensitive operations (e.g., flooding `/stop` during a detection).

## Acceptance Criteria
- [x] Per-user rate limit of max 5 commands per minute enforced
- [x] Rate-limited commands return a friendly "slow down" message
- [x] Rate limit state keyed on Telegram `user_id`
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
