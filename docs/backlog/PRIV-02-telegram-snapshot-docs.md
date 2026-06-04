# Document Telegram Snapshot Data Flow and Add Opt-Out

**ID:** PRIV-02
**Severity:** Medium
**Category:** Privacy
**Status:** Closed

## Affected Files
- `README.md` — no mention of snapshot uploads to Telegram servers
- `docs/threat-model.md` — data flow not documented

## Description
Telegram notifications upload detection snapshots to Telegram's servers. Users may not realise that images of their printing environment are stored on third-party infrastructure. This data flow is not documented, and there is no option to disable snapshot uploads while keeping text notifications.

## Evidence
- Telegram notifier sends photos via `sendPhoto` API.
- README and threat model do not mention this data flow.
- No `TELEGRAM_SEND_SNAPSHOTS` configuration toggle exists.

## Impact
- Users unknowingly share potentially sensitive images with Telegram's infrastructure.
- No way to receive text-only Telegram alerts without snapshots.

## Acceptance Criteria
- [x] Data flow documented in README privacy section and `docs/threat-model.md`
- [x] `TELEGRAM_SEND_SNAPSHOTS` configuration option added (default: `true` for backward compatibility)
- [x] When disabled, Telegram notifications send text-only messages
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
