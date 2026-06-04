# No Content-Security-Policy Header on Dashboard

**ID:** SEC-05
**Severity:** Medium
**Category:** Security
**Status:** Closed

## Affected Files
- `sentinel/web/app.py` — no CSP middleware or header injection
- `templates/` — inline scripts lack nonce attributes

## Description
The web dashboard does not set a `Content-Security-Policy` header. Without CSP, the application relies solely on output encoding to prevent cross-site scripting (XSS). A single encoding miss would allow full script injection.

## Evidence
- No `Content-Security-Policy` header in response middleware or route handlers.
- Templates contain inline `<script>` blocks without nonce attributes.

## Impact
- Increased XSS attack surface — no defence-in-depth beyond output encoding.
- Fails common security-header audits (e.g., Mozilla Observatory).

## Acceptance Criteria
- [x] CSP header set on all HTML responses with nonce-based script directives
- [x] Inline scripts updated to include `nonce` attribute matching the CSP nonce
- [x] `style-src` and `img-src` directives appropriately scoped
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
