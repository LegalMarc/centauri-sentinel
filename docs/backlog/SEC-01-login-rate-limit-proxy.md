# Login Rate Limiter Uses Proxy IP Instead of Real Client IP

**ID:** SEC-01
**Severity:** High
**Category:** Security
**Status:** Closed

## Affected Files
- `sentinel/web/auth.py` (lines 135-149) — rate limiter reads `scope['client']` IP which resolves to the reverse proxy address, not the actual client

## Description
The login rate limiter uses `scope['client']` to identify the client IP. Behind a reverse proxy this always resolves to the proxy's IP address, not the real client. `X-Forwarded-For` is already parsed for the `__internal_snapshot` endpoint but is **not** consulted by the login rate limiter, meaning all login attempts share a single bucket when proxied.

## Evidence
- `auth.py` lines 135-149 extract IP via `scope['client'][0]`.
- `__internal_snapshot` handler separately parses `X-Forwarded-For`.
- No shared utility for resolving the real client IP.

## Impact
- Rate limiting is ineffective behind a reverse proxy: a single attacker can be rate-limited while simultaneously locking out every other user.
- Brute-force protection is effectively disabled in typical Docker / Nginx deployments.

## Acceptance Criteria
- [x] Rate limiter uses resolved client IP from `X-Forwarded-For` header when present
- [x] Shared helper function for real-IP resolution used by both login and snapshot endpoints
- [x] Unit test confirms rate limiting keys on forwarded IP behind proxy
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
