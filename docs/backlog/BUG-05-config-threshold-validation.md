# Missing Field Validators for ML Config Thresholds

**ID:** BUG-05
**Severity:** Medium
**Category:** Bugs
**Status:** Closed

## Affected Files
- `sentinel/config.py` — `ml_score_threshold` and `ml_confirm_count` lack `@field_validator` constraints

## Description
`ml_score_threshold` has no `@field_validator` ensuring it falls within the valid range of 0.0–1.0. `ml_confirm_count` is not validated to be ≥ 1. Invalid values are silently accepted, leading to nonsensical watcher behaviour at runtime.

## Evidence
- `config.py` defines `ml_score_threshold: float` and `ml_confirm_count: int` without range validators.
- Setting `ml_score_threshold=5.0` or `ml_confirm_count=0` would be accepted without error.

## Impact
- `ml_score_threshold > 1.0` means detections never trigger (silent failure).
- `ml_score_threshold < 0.0` means every frame triggers (false positive storm).
- `ml_confirm_count = 0` could cause immediate detection without confirmation.

## Acceptance Criteria
- [x] `@field_validator` added for `ml_score_threshold` enforcing `0.0 <= value <= 1.0`
- [x] `@field_validator` added for `ml_confirm_count` enforcing `value >= 1`
- [x] Invalid values raise clear `ValidationError` at startup
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
