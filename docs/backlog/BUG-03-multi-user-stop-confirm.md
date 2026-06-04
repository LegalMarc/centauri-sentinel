# /stop Confirmation Has No Per-User Tracking

**ID:** BUG-03
**Severity:** Medium
**Category:** Bugs
**Status:** Closed

## Affected Files
- `sentinel/bot/commands.py` — `/confirm` handler does not validate initiating user

## Description
The `/stop` command requires a `/confirm` follow-up, but the confirmation is not scoped to the user who initiated `/stop`. In a multi-user Telegram group, User A can issue `/stop` and User B can `/confirm` it — either accidentally or maliciously.

## Evidence
- `/confirm` handler checks only that a pending stop exists, not who initiated it.
- No `user_id` stored alongside the pending stop state.

## Impact
- Unintended print cancellations in shared Telegram groups.
- Safety-critical action (/stop → cancel print) can be triggered by an unauthorized group member.

## Acceptance Criteria
- [x] Pending stop state stores the `user_id` of the initiator
- [x] `/confirm` only accepted from the user who initiated `/stop`
- [x] Other users receive a message indicating they cannot confirm another user's stop
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
