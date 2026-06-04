# No End-to-End Integration Test for Detection Pipeline

**ID:** MAINT-02
**Severity:** Medium
**Category:** Maintainability
**Status:** Completed

## Affected Files
- `tests/` — no integration test covering the full camera→ML→pause→notify pipeline

## Description
There is no end-to-end integration test that exercises the full detection pipeline: camera frame grab → ML inference → confirmed detection → printer pause → notification dispatch. Each component has unit tests, but the integrated flow is untested.

## Evidence
- Test directory contains unit tests for individual modules.
- No test file exercises the watcher loop with mocked camera, ML, printer, and notification subsystems together.

## Impact
- Integration bugs (e.g., incorrect state transitions, race conditions between subsystems) are only caught in production.
- Refactoring any component risks breaking the pipeline without test coverage to catch regressions.

## Acceptance Criteria
- [x] At least one integration test exercises the full detection→pause→notify path
- [x] Test uses mocked camera, ML API, MQTT client, and notification channels
- [x] Test verifies correct state transitions and notification delivery
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
