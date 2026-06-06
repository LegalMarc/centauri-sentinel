# Pre-Beta Audit Resolutions

This backlog documents the findings from the pre-public-beta audit and serves as a trackable checklist for resolution.

## Bugs and Correctness Defects
- [x] Watcher loop permanently stalls if MQTT listener goes stale
- [x] Snapshot cleanup crashes permanently on `None` paths
- [x] Print stop command defeats retry logic by clearing pending flag immediately
- [x] Debounced printer pause incorrectly handled as critical failure
- [x] Printer resume ignores warmup window debounce, causing false pause loops
- [x] Failing closed preserves `_confirm_count`, causing immediate subsequent pauses
- [x] Flaky connections spam external pause notifications
- [x] Back-to-back print jobs suppress start notifications
- [x] Camera offline warning is unreachable during print start

## Security and Privacy Risks
- [x] Unauthenticated Web Dashboard Exposure via Misleading Startup Suggestion
- [x] DNS Rebinding Protection Misconfiguration Forces Insecure Workarounds

## Performance and Scalability Risks
- [x] MJPEG Stream Drip-Feed Stalls Frame Updates Silently
- [x] Unbounded Memory Growth in MQTT Client Dictionary
- [x] Unindexed Database Query for Pause History Cleanup

## Stability, Reliability, and Operational-Readiness Risks
- [x] Watcher background task exceptions are swallowed, causing silent detection failure
- [x] MQTT transient connection errors cause critical commands to fail without retry
- [x] Obico ML client fails without retry if API returns malformed JSON
- [x] Hardcoded ML consecutive failure threshold prevents tuning for flaky networks
- [x] Hardcoded config directory makes testing and systemd deployments fragile
- [x] SQLite busy errors lock up the database on concurrent webhook requests
