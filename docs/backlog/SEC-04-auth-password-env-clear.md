# AUTH_PASSWORD Remains in os.environ After Hashing

**ID:** SEC-04
**Severity:** Medium
**Category:** Security
**Status:** Closed

## Affected Files
- `sentinel/config.py` — clears `self.auth_password` but does not remove `AUTH_PASSWORD` from `os.environ`

## Description
At startup, `config.py` reads `AUTH_PASSWORD` from the environment and hashes it with bcrypt. The instance attribute `self.auth_password` is cleared afterwards, but the plaintext password remains accessible via `os.environ['AUTH_PASSWORD']` for the lifetime of the process.

## Evidence
- `config.py` sets `self.auth_password = None` after hashing but never calls `os.environ.pop('AUTH_PASSWORD', None)`.
- Any code or dependency with access to `os.environ` can read the plaintext password.

## Impact
- Plaintext password exposed in `/proc/<pid>/environ` on Linux hosts.
- Any debug endpoint, crash dump, or dependency that logs environment variables would leak the password.

## Acceptance Criteria
- [x] `os.environ.pop('AUTH_PASSWORD', None)` called immediately after bcrypt hashing
- [x] Unit test verifies `AUTH_PASSWORD` is absent from `os.environ` after config initialisation
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
