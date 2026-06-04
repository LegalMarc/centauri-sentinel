# ML API Token File Written with Default Permissions

**ID:** SEC-03
**Severity:** Medium
**Category:** Security
**Status:** Closed

## Affected Files
- `docker/token-init/` — token initialisation script writes API token file with default 644 permissions

## Description
The ML API token file is written to a Docker volume with default file permissions (644), making it world-readable inside the container and on the host filesystem. Any process running in sibling containers sharing the volume, or any user on the host, can read the token.

## Evidence
- Token init script in `docker/token-init/` does not explicitly set file mode on the generated token file.
- Default `umask` in the container yields mode 644.

## Impact
- Leaked ML API token could allow unauthorised inference requests or quota exhaustion.
- Violates principle of least privilege for secrets at rest.

## Acceptance Criteria
- [x] Token file created with mode `600` (owner read/write only)
- [x] Dockerfile or init script explicitly sets `chmod 600` or uses `os.open` with restricted mode
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
