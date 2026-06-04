# Plain-Text AUTH_PASSWORD Remains Visible in Environment

**ID:** SEC-09
**Severity:** Medium
**Category:** Security
**Status:** Closed

## Affected Files
- `sentinel/config.py`

## Description
The application attempts to pop `AUTH_PASSWORD` from the process environment to hide it. However, `/proc/<pid>/environ` and `docker inspect` show the exec-time environment variables, which cannot be modified after the process has started.

## Acceptance Criteria
- [x] Update documentation and code warnings to explicitly note that using plain-text `AUTH_PASSWORD` is insecure and visible via container metadata and system memory.
- [x] Encourage users to use `AUTH_PASSWORD_BCRYPT` instead.
