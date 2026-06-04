# Bypassing Loopback Authorization & Rate Limiting via X-Forwarded-For Spoofing

**ID:** BUG-09
**Severity:** High
**Category:** Security
**Status:** Closed

## Affected Files
- `sentinel/web/auth.py` — `_resolve_client_ip` trusts `X-Forwarded-For` header unconditionally

## Description
The function `_resolve_client_ip` extracts the first IP address from the `X-Forwarded-For` header without verifying if the request came from a trusted reverse proxy. This spoofable client IP is used to bypass the loopback authorization check for `/__internal_snapshot/` and to track login attempts in basic auth rate limiting.

## Evidence
- `auth.py` lines 50-57:
  ```python
  def _resolve_client_ip(scope: Scope, headers: dict[bytes, bytes]) -> str:
      x_forwarded_for = headers.get(b"x-forwarded-for")
      if x_forwarded_for:
          return x_forwarded_for.decode().split(",")[0].strip()
      client = scope.get("client")
      return client[0] if client else "0.0.0.0"
  ```

## Impact
- External attackers can spoof their client IP to `127.0.0.1` and bypass loopback checks or rate-limiting restrictions.
- Attackers can easily bypass basic auth rate limits by rotating the IP address in the `X-Forwarded-For` header, allowing high-speed brute-force attacks.

## Acceptance Criteria
- [x] Implement a configuration option to enable or disable trusting proxy headers (e.g. `trust_proxies: bool = False`).
- [x] If proxy headers are not trusted (default), `_resolve_client_ip` must only use the immediate client IP from `scope["client"]`.
- [x] Add unit tests verifying both trusted and untrusted proxy scenarios.
- [x] Verify that `X-Forwarded-For` headers are ignored when proxy trust is disabled.
