# Audit Fixes Backlog

## Group 1: Performance, Scalability, Stability, and Reliability
- [x] 1.1: Infinite Retries on Notification Failures Resulting in Task and Memory Leaks
- [x] 1.2: Unbounded Memory Allocation in Database Snapshot Cleanup
- [x] 1.3: Missing MQTT Protocol Errors Swallowed by Reconnection Loop Logic
- [x] 1.4: TCP Connection Churn for Continuous MJPEG Frame Grabs
- [x] 1.5: TCP Connection Churn for ML API Inferences
- [x] 1.6: Database Write Lock Starvation Caused by High-Frequency Heartbeats
- [x] 1.7: Full Table Scan on Unindexed Status Column for Analytics Query
- [x] 1.8: Watcher Loop Permanent Crash via Stream Proxy Cancellation (BUG-11)

## Group 2: Bugs and Correctness Defects
- [x] 2.1: CLI host/port overrides are ignored by downstream components
- [x] 2.2: ntfy notifier crashes due to newline characters in HTTP headers
- [x] 2.3: Telegram bot inline keyboard crashes on photo messages
- [x] 2.4: Watcher loop race condition causes incorrect state and notification spam
- [x] 2.5: Overlapping and Unbounded Snooze Tasks Cause State Corruption and DoS
- [x] 2.6: Completed print duration is always recorded as 0 seconds
- [x] 2.7: AuthMiddleware drops URL query parameters on redirect
- [x] 2.8: Cookie Secure flag tied to EXTERNAL_BIND_ALLOWED creates login loops
- [x] 2.9: Orphaned snapshot files on disk deletion failures
- [x] 2.10: Incompatible Exception Type in Camera Close Queue (BUG-10)

## Group 3: Security and Privacy Risks
- [x] 3.1: Server-Side Request Forgery (SSRF) via Printer IP configuration
- [x] 3.2: Basic Auth Login Route Vulnerable to CPU-Exhaustion DoS
- [x] 3.3: Cross-Site Request Forgery (CSRF) on API endpoints when Auth is bypassed
- [x] 3.4: Privacy Risk: Unencrypted Exposure via Public Ntfy Topics
- [x] 3.5: Lack of Request Size Limit on JSON Endpoints
- [x] 3.6: User Enumeration via Basic Authentication Timing Oracle
- [x] 3.7: Bypassing Loopback Authorization & Rate Limiting via X-Forwarded-For Spoofing (BUG-09)

## Group 4: Maintainability and Operational-Readiness
- [x] 4.1: ML API Network Failures Silently Reset Consecutive Detection Counter
- [x] 4.2: Printer Offline State Masks Complete Detection Halts
- [x] 4.3: Failed Pause Attempt During Detection Resets Confirmation Progress
- [x] 4.4: Stop Command Timeout Resets Auto-Stop Timer
- [x] 4.5: Unbounded MJPEG Stream Proxying for Web Dashboard
- [x] 4.6: External Bind Allows Unauthenticated Access to Internal Snapshot Endpoint
- [x] 4.7: Web UI and Telegram Cached Status Displays Stale Printer Progress

