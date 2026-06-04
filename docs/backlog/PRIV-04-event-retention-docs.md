# Event Retention Policy Not Documented

**ID:** PRIV-04
**Severity:** Low
**Category:** Privacy
**Status:** Closed

## Affected Files
- `README.md` — no documentation of data retention policy or snapshot limits

## Description
The event and snapshot retention policy is not documented for end users. The snapshot limit is hardcoded to 50 in the codebase with no explanation in user-facing documentation. Users have no way to know how long their data is retained or how to configure retention.

## Evidence
- Snapshot limit hardcoded to 50 in cleanup logic.
- README does not mention retention limits or data lifecycle.
- No configuration option to adjust retention.

## Impact
- Users cannot make informed decisions about data stored by the system.
- Compliance concerns for users in jurisdictions with data-retention regulations.

## Acceptance Criteria
- [x] Retention policy documented in README (snapshot count limit, event history duration)
- [x] Snapshot limit made configurable via environment variable
- [x] Default value and behaviour clearly stated
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
