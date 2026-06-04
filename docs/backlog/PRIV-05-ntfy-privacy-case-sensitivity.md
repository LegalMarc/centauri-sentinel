# Case-Sensitive Host Matching in ntfy Privacy Block Allows Public Feed Exposure

**ID:** PRIV-05
**Severity:** Medium
**Category:** Privacy
**Status:** Closed

## Affected Files
- `sentinel/notify/ntfy.py`

## Description
The block `if self._enabled and "ntfy.sh" in self._url and not self._token:` does not lowercase the URL before checking, which allows the privacy validation check to be bypassed by using uppercase or mixed-case domains (e.g. `https://NTFY.SH/topic`).

## Acceptance Criteria
- [x] Lowercase the URL before checking for `"ntfy.sh"`: `"ntfy.sh" in self._url.lower()`.
- [x] Add unit test verifying that configurations with mixed/uppercase `ntfy.sh` domain and no token still raise a privacy validation error on initialization.
