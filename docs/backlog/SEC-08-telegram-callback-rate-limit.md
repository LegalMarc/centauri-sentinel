# Telegram Rate-Limiting is Bypassed by Callback Queries

**ID:** SEC-08
**Severity:** Medium
**Category:** Security
**Status:** Closed

## Affected Files
- `sentinel/bot/commands.py`

## Description
The Telegram bot rate limiter checks `if update.message is None: return True`. Since inline button clicks come in as callback queries (where `update.message` is `None` but `update.callback_query` is set), they bypass the rate limiter completely.

## Acceptance Criteria
- [x] Update `_check_rate_limit()` to apply rate limiting history check to callback queries as well, using `update.callback_query.from_user.id`.
- [x] Add unit test verifying that spammed callback queries are rejected with a warning or rate limit message.
