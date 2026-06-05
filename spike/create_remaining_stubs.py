import os

backlog_dir = "/Users/mhm/Documents/Dev/centauri-sentinel/docs/backlog"

stubs = [
    {
        "filename": "1.1-notification-retry-leak.md",
        "title": "Infinite Retries on Notification Failures Cause Task and Memory Leaks",
        "id": "1.1",
        "severity": "High",
        "category": "Performance",
        "affected": "sentinel/notify/dispatcher.py",
        "description": "Infinite retries on notification failures cause task accumulation and memory leaks. The notification retry mechanism lacks limits or proper exception handling, causing tasks to pile up indefinitely when the notifier endpoint is unreachable.",
        "evidence": "In dispatcher.py, the notification retry loops indefinitely or has very long retry logic without upper limits or task limits.",
        "impact": "If notification channels are down or rate-limited, memory usage grows unboundedly, eventually crashing the application via OOM.",
        "criteria": [
            "Implement a maximum retry attempt limit or max timeout on notification tasks",
            "Ensure failed tasks are cleaned up and do not leak memory",
            "Write tests simulating notification down time and verify task/memory cleanup",
        ],
    },
    {
        "filename": "1.2-snapshot-cleanup-memory.md",
        "title": "Unbounded Memory Allocation in Database Snapshot Cleanup",
        "id": "1.2",
        "severity": "Medium",
        "category": "Performance",
        "affected": "sentinel/db/repo.py",
        "description": "Unbounded memory allocation during database snapshot cleanup. Querying all database snapshot paths into memory at once causes memory spikes when there are a large number of database rows.",
        "evidence": "Database query loads all snapshot records into memory in a single query instead of batching or querying only needed metadata.",
        "impact": "High memory consumption on large databases, leading to slowdowns or crashes on resource-constrained host devices.",
        "criteria": [
            "Implement batching or streaming for snapshot cleanup queries",
            "Verify memory usage stays low during cleanup execution",
            "Add tests with thousands of snapshot records",
        ],
    },
    {
        "filename": "1.3-mqtt-protocol-errors.md",
        "title": "Missing MQTT Protocol Errors Swallowed by Reconnection Loop Logic",
        "id": "1.3",
        "severity": "Medium",
        "category": "Stability",
        "affected": "sentinel/printer/client.py",
        "description": "MQTT protocol and parsing errors are swallowed by the reconnection loop logic. Instead of alerting or failing fast on parsing errors, the listener loops silently reconnecting, masking underlying issues.",
        "evidence": "Exception handlers in reconnection loop catch all exceptions (including parse/format errors) and trigger reconnect without logging detailed traceback.",
        "impact": "Difficulty debugging compatibility issues with printer firmware updates or payload format changes, leading to silent failures.",
        "criteria": [
            "Identify and propagate permanent/protocol errors while retrying only transient connection errors",
            "Log distinct warnings for message protocol mismatches",
            "Test reconnection behavior on malformed payloads vs connection loss",
        ],
    },
    {
        "filename": "1.4-mjpeg-connection-churn.md",
        "title": "TCP Connection Churn for Continuous MJPEG Frame Grabs",
        "id": "1.4",
        "severity": "Medium",
        "category": "Performance",
        "affected": "sentinel/camera/mjpeg.py",
        "description": "Continuous MJPEG frame grabbing opens and closes a new HTTP/TCP connection every 10 seconds. This results in heavy connection churn and high socket overhead.",
        "evidence": "mjpeg.py makes a new HTTP request for every frame grab instead of keeping the connection open or reusing a connection pool.",
        "impact": "Unnecessary TCP handshake overhead, file descriptor exhaustion, and high CPU usage on low-power devices.",
        "criteria": [
            "Reuse TCP connections or use a persistent client session for frame grabbing",
            "Verify socket reuse and reduced socket setup/teardown overhead",
            "Add performance tests verifying persistent connection usage",
        ],
    },
    {
        "filename": "1.5-ml-api-connection-churn.md",
        "title": "TCP Connection Churn for ML API Inferences",
        "id": "1.5",
        "severity": "Medium",
        "category": "Performance",
        "affected": "sentinel/ml/client.py",
        "description": "Inference client creates a new HTTP connection for each ML API request, causing TCP connection churn.",
        "evidence": "MlClient initializes an HTTP client per request rather than reusing a persistent client session.",
        "impact": "Increased inference latency and CPU overhead from repeatedly negotiating TCP and TLS handshakes.",
        "criteria": [
            "Use a persistent client session or connection pool for ML API calls",
            "Test that connections are pooled and reused across multiple inference requests",
        ],
    },
    {
        "filename": "1.6-db-write-lock-heartbeats.md",
        "title": "Database Write Lock Starvation Caused by High-Frequency Heartbeats",
        "id": "1.6",
        "severity": "Medium",
        "category": "Performance",
        "affected": "sentinel/db/repo.py",
        "description": "High-frequency watchdog updates / heartbeat writes to the SQLite database cause write lock contention and starve other database operations.",
        "evidence": "Heartbeat ticks perform synchronous write queries at short intervals, locking the single-writer database.",
        "impact": "Delay in recording critical detection events or dashboard load performance due to database lock starvation.",
        "criteria": [
            "Reduce heartbeat write frequency or optimize lock timeouts",
            "Verify concurrent read/write queries complete without timeout during active heartbeats",
        ],
    },
    {
        "filename": "1.7-analytics-unindexed-query.md",
        "title": "Full Table Scan on Unindexed Status Column for Analytics Query",
        "id": "1.7",
        "severity": "Medium",
        "category": "Performance",
        "affected": "sentinel/db/repo.py, schema.sql",
        "description": "Database queries filtering by state or status column execute full table scans due to lack of a database index.",
        "evidence": "No index defined on state column in schema.sql for detection or pause history tables.",
        "impact": "Severe query performance degradation as the database size increases over time.",
        "criteria": [
            "Add appropriate index to status/state column in database schema",
            "Verify EXPLAIN QUERY PLAN shows index utilization",
            "Ensure migration script safely creates index without breaking existing databases",
        ],
    },
    {
        "filename": "2.1-cli-overrides-ignored.md",
        "title": "CLI Host/Port Overrides Ignored by Downstream Components",
        "id": "2.1",
        "severity": "Medium",
        "category": "Bugs",
        "affected": "sentinel/__main__.py",
        "description": "Host and port values passed as command-line arguments are not forwarded to the ASGI server configuration and are ignored.",
        "evidence": "CommandLine options parsed in __main__.py are not passed down to settings overrides or uvicorn configuration.",
        "impact": "Inability for users to customize dashboard port or bind host via command-line flags.",
        "criteria": [
            "Pass CLI arguments to configuration setup and uvicorn runner",
            "Add unit tests verifying CLI overrides are respected",
        ],
    },
    {
        "filename": "2.2-ntfy-newline-crash.md",
        "title": "ntfy Notifier Crashes Due to Newline Characters in HTTP Headers",
        "id": "2.2",
        "severity": "Medium",
        "category": "Bugs",
        "affected": "sentinel/notify/ntfy.py",
        "description": "The ntfy notifier passes message titles or bodies in HTTP headers (e.g. X-Title). Newlines in these fields crash the HTTP client.",
        "evidence": "Raw multiline strings are formatted directly into ntfy headers without sanitization or header encoding.",
        "impact": "Crashes the notifier loop when sending alert notifications containing formatting or newlines.",
        "criteria": [
            "Sanitize or strip newline characters from all HTTP headers sent to ntfy",
            "Test notifier with multiline alert messages and verify successful delivery",
        ],
    },
    {
        "filename": "2.3-telegram-photo-keyboard-crash.md",
        "title": "Telegram Bot Inline Keyboard Crashes on Photo Messages",
        "id": "2.3",
        "severity": "Medium",
        "category": "Bugs",
        "affected": "sentinel/bot/commands.py",
        "description": "Telegram bot commands parsing incoming photo messages crash when trying to attach inline keyboards using methods meant for text messages.",
        "evidence": "Callback keyboard routing does not distinguish between photo message structures and text message structures in the Telegram library API.",
        "impact": "Uncaught exception crashes the bot loop when receiving certain user callbacks or photo updates.",
        "criteria": [
            "Differentiate between text and photo updates in bot inline keyboards",
            "Verify all callbacks execute without crash under mock photo messages",
        ],
    },
    {
        "filename": "2.4-watcher-race-condition.md",
        "title": "Watcher Loop Race Condition Causes Incorrect State and Notification Spam",
        "id": "2.4",
        "severity": "High",
        "category": "Bugs",
        "affected": "sentinel/watcher/loop.py",
        "description": "Race conditions in the main watcher task loop allow duplicate frames or overlapping ticks, leading to duplicate pause commands and notification spam.",
        "evidence": "The tick interval executes concurrently if a previous tick is still awaiting network operations, lacking a lock or busy flag.",
        "impact": "Repeated pause command dispatches and multiple duplicate Telegram alerts for a single failure event.",
        "criteria": [
            "Ensure watcher tick execution is fully sequential and protected against concurrency",
            "Add concurrency tests to verify ticks do not overlap",
        ],
    },
    {
        "filename": "2.5-snooze-task-corruption.md",
        "title": "Overlapping and Unbounded Snooze Tasks Cause State Corruption and DoS",
        "id": "2.5",
        "severity": "Medium",
        "category": "Bugs",
        "affected": "sentinel/bot/commands.py, sentinel/watcher/loop.py",
        "description": "Multiple snooze requests create overlapping background task timers, leading to state corruption and premature re-enabling of detection.",
        "evidence": "Snooze command schedules background task without cancelling previous active snooze tasks.",
        "impact": "Detection is re-enabled too early or state machine gets confused, triggering unexpected pauses.",
        "criteria": [
            "Cancel any existing snooze task before starting a new one",
            "Track active snooze tasks cleanly at class or module level",
        ],
    },
    {
        "filename": "2.6-print-duration-zero.md",
        "title": "Completed Print Duration is Always Recorded as 0 Seconds",
        "id": "2.6",
        "severity": "Medium",
        "category": "Bugs",
        "affected": "sentinel/watcher/loop.py",
        "description": "Completed print records in the database always show a print duration of 0 seconds due to incorrect calculation or lack of start time tracking.",
        "evidence": "Print duration logic computes elapsed time using unitialized fields or resets state too early on completion.",
        "impact": "Stale or useless print duration metrics in the analytics dashboard.",
        "criteria": [
            "Persist and compute duration correctly using printer status timeline",
            "Verify correct duration recorded in database on print completion",
        ],
    },
    {
        "filename": "2.7-auth-redirect-query-params.md",
        "title": "AuthMiddleware Drops URL Query Parameters on Redirect",
        "id": "2.7",
        "severity": "Low",
        "category": "Bugs",
        "affected": "sentinel/web/auth.py",
        "description": "Redirecting unauthorized requests to the login page drops the original URL query parameters.",
        "evidence": "Redirect URL string formatting does not append or preserve query parameters.",
        "impact": "Poor user experience as links with parameters (e.g. specific dashboard filters) are lost after logging in.",
        "criteria": [
            "Preserve original path and query parameters in redirect or next parameter",
            "Unit test redirection logic preserves query parameters",
        ],
    },
    {
        "filename": "2.8-cookie-secure-login-loop.md",
        "title": "Cookie Secure Flag Tied to EXTERNAL_BIND_ALLOWED Creates Login Loops",
        "id": "2.8",
        "severity": "Medium",
        "category": "Bugs",
        "affected": "sentinel/web/auth.py",
        "description": "Enabling the cookie Secure flag based strictly on EXTERNAL_BIND_ALLOWED causes login loops when deploying behind reverse proxies over HTTP.",
        "evidence": "Secure flag is set on cookies only if EXTERNAL_BIND_ALLOWED is set, but this does not detect proxy TLS status.",
        "impact": "Users are stuck in a login loop where cookies are rejected or ignored due to Secure flag mismatch behind HTTPS proxies.",
        "criteria": [
            "Make cookie security settings robust and configurable separate from network bind options",
            "Support X-Forwarded-Proto header for detecting TLS termination",
        ],
    },
    {
        "filename": "2.9-orphaned-snapshot-files.md",
        "title": "Orphaned Snapshot Files on Disk Deletion Failures",
        "id": "2.9",
        "severity": "Low",
        "category": "Bugs",
        "affected": "sentinel/watcher/loop.py, sentinel/db/repo.py",
        "description": "Failed disk deletions during database record purges leave orphaned snapshot image files behind.",
        "evidence": "File deletion failures (e.g. FileNotFoundError, PermissionError) are caught but no cleanup retry or logging is handled.",
        "impact": "Gradual buildup of orphaned files on disk, wasting storage space.",
        "criteria": [
            "Verify all files are deleted when DB rows are purged, with appropriate logging on failure",
            "Implement fallback disk-level directory cleanup",
        ],
    },
    {
        "filename": "3.1-ssrf-printer-ip.md",
        "title": "Server-Side Request Forgery via Printer IP Configuration",
        "id": "3.1",
        "severity": "High",
        "category": "Security",
        "affected": "sentinel/web/routes.py",
        "description": "Configuring printer IP allows Server-Side Request Forgery (SSRF). The printer IP setting accepts arbitrary IP ranges (including link-local or loopback addresses), allowing the application to connect to internal services.",
        "evidence": "No validation blocks setting printer IP to localhost (127.0.0.1) or internal address ranges like 169.254.169.254.",
        "impact": "An attacker with configuration access can scan local network endpoints or query cloud metadata services.",
        "criteria": [
            "Restrict printer IP field to valid external LAN IP or hostnames, blocking loopback and link-local ranges",
            "Add unit tests for SSRF address ranges",
        ],
    },
    {
        "filename": "3.2-bcrypt-dos.md",
        "title": "Basic Auth Login Route Vulnerable to CPU-Exhaustion DoS",
        "id": "3.2",
        "severity": "Medium",
        "category": "Security",
        "affected": "sentinel/web/auth.py",
        "description": "Basic authentication verification uses high-cost bcrypt checks on every request. High-frequency login attempts exhaust CPU resources.",
        "evidence": "Bcrypt verification is executed directly for invalid credentials without rate-limiting or caching results of failures.",
        "impact": "Denial of Service (DoS) against the dashboard due to high CPU load.",
        "criteria": [
            "Implement strict rate limiting on basic auth verification attempts",
            "Ensure failed logins have a small artificial delay that doesn't block the loop",
        ],
    },
    {
        "filename": "3.3-csrf-auth-bypass.md",
        "title": "Cross-Site Request Forgery on API Endpoints when Auth is Bypassed",
        "id": "3.3",
        "severity": "Medium",
        "category": "Security",
        "affected": "sentinel/web/auth.py",
        "description": "When dashboard authentication is bypassed or disabled, state-changing API endpoints remain vulnerable to Cross-Site Request Forgery (CSRF).",
        "evidence": "API routes lack CSRF protection (like Referer/Origin checks) when AUTH_USERNAME is unset.",
        "impact": "Malicious websites can issue state-changing requests to the local sentinel API (e.g. trigger pause or resume) via the user's browser.",
        "criteria": [
            "Enforce basic CSRF (Referer/Origin) checks on all post/put requests regardless of auth status",
            "Test that state-changing requests without proper headers are blocked",
        ],
    },
    {
        "filename": "3.4-ntfy-public-exposure.md",
        "title": "Privacy Risk: Unencrypted Exposure via Public Ntfy Topics",
        "id": "3.4",
        "severity": "Medium",
        "category": "Security",
        "affected": "sentinel/notify/ntfy.py",
        "description": "Sending detailed notifications (including camera snapshots) to public ntfy topics exposes private images of the user's room to anyone.",
        "evidence": "Default settings encourage public ntfy.sh servers and public topic names without warning users about visibility.",
        "impact": "Privacy leakage where third parties can monitor snapshots of the user's printer and surrounding environment.",
        "criteria": [
            "Enforce token auth for public ntfy domains or warn loudly in config",
            "Document privacy implications of using unauthenticated public topics",
        ],
    },
    {
        "filename": "3.5-json-request-size-limit.md",
        "title": "Lack of Request Size Limit on JSON Endpoints",
        "id": "3.5",
        "severity": "Medium",
        "category": "Security",
        "affected": "sentinel/web/routes.py",
        "description": "JSON request payloads have no size limits, exposing the server to memory exhaustion during parsing of extremely large payloads.",
        "evidence": "API endpoints accept arbitrary JSON bodies without validating content-length headers or stream size limits.",
        "impact": "Denial of Service (DoS) due to memory exhaustion.",
        "criteria": [
            "Limit maximum JSON request payload size to a small threshold (e.g. 1MB)",
            "Test that large payloads are rejected with HTTP 413",
        ],
    },
    {
        "filename": "3.6-auth-timing-oracle.md",
        "title": "User Enumeration via Basic Authentication Timing Oracle",
        "id": "3.6",
        "severity": "Low",
        "category": "Security",
        "affected": "sentinel/web/auth.py",
        "description": "Authentication check returns immediately when username is invalid but performs expensive bcrypt verification when username is valid, creating a timing oracle.",
        "evidence": "Bcrypt check is skipped entirely when the username does not exist in settings.",
        "impact": "Allows attackers to determine if a username exists based on response times.",
        "criteria": [
            "Perform a dummy bcrypt verification on invalid usernames to keep response timing uniform",
            "Verify timing is identical for valid vs invalid usernames",
        ],
    },
    {
        "filename": "4.1-ml-failure-resets-counter.md",
        "title": "ML API Network Failures Silently Reset Consecutive Detection Counter",
        "id": "4.1",
        "severity": "Medium",
        "category": "Maintainability",
        "affected": "sentinel/watcher/loop.py",
        "description": "Transient network failures to the ML API return score=0 and silently reset the consecutive detection counter, masking ongoing print failures.",
        "evidence": "Exception handler in watcher loop resets consecutive detection count on ML query errors.",
        "impact": "Failure to pause a print if network glitches occur during a spaghetti event.",
        "criteria": [
            "Retain consecutive confirmation count on transient ML failures",
            "Only reset count on a verified negative detection frame",
        ],
    },
    {
        "filename": "4.2-offline-masks-detection-halt.md",
        "title": "Printer Offline State Masks Complete Detection Halts",
        "id": "4.2",
        "severity": "Medium",
        "category": "Maintainability",
        "affected": "sentinel/watcher/loop.py",
        "description": "When the printer goes offline, the watcher transitions to OFFLINE, masking complete detection stalls or loop crashes.",
        "evidence": "Watcher state transitions hide internal watchdog status.",
        "impact": "Operators do not receive alerts if the watcher itself crashes vs when the printer is offline.",
        "criteria": [
            "Distinguish clearly between watcher loop stalls and printer connection drops",
            "Ensure liveness watchdog fires even in offline state",
        ],
    },
    {
        "filename": "4.3-pause-failure-resets-progress.md",
        "title": "Failed Pause Attempt During Detection Resets Confirmation Progress",
        "id": "4.3",
        "severity": "Medium",
        "category": "Maintainability",
        "affected": "sentinel/watcher/loop.py",
        "description": "If the MQTT pause command fails, the watcher resets confirmation progress, requiring another complete sequence to retry.",
        "evidence": "Failed pause calls reset confirm count to 0 in the exception block.",
        "impact": "Printer continues printing failed parts indefinitely if the first pause command fails.",
        "criteria": [
            "Keep confirm count or retry pause execution on pause failure",
            "Ensure notifier sends warning on failed pause",
        ],
    },
    {
        "filename": "4.4-stop-timeout-reset.md",
        "title": "Stop Command Timeout Resets Auto-Stop Timer",
        "id": "4.4",
        "severity": "Medium",
        "category": "Maintainability",
        "affected": "sentinel/watcher/loop.py",
        "description": "When a print stop command times out, the auto-stop timer resets, delaying cancellation.",
        "evidence": "Timeout handler resets timers instead of marking command failed.",
        "impact": "Print continues running despite cancellation requests.",
        "criteria": [
            "Log and retry/persist stop state on timeout",
            "Ensure cancel logic is robust against printer response timeouts",
        ],
    },
    {
        "filename": "4.5-unbounded-stream-proxy.md",
        "title": "Unbounded MJPEG Stream Proxying for Web Dashboard",
        "id": "4.5",
        "severity": "Medium",
        "category": "Performance",
        "affected": "sentinel/camera/mjpeg.py",
        "description": "The MJPEG camera stream proxy has no limits on duration or active stream count, consuming high bandwidth and CPU.",
        "evidence": "stream_proxy does not limit stream count or connection time.",
        "impact": "Low-power hosts run out of memory or CPU when multiple dashboard pages are open.",
        "criteria": [
            "Limit active stream proxies or add timeout/max connection limits",
            "Test proxy handles disconnection and resource cleanup correctly",
        ],
    },
    {
        "filename": "4.6-internal-snapshot-auth.md",
        "title": "External Bind Allows Unauthenticated Access to Internal Snapshot Endpoint",
        "id": "4.6",
        "severity": "Medium",
        "category": "Security",
        "affected": "sentinel/web/auth.py",
        "description": "When external bind is allowed, the unauthenticated internal snapshot endpoint can be accessed from the network.",
        "evidence": "The internal snapshot path has no client IP restrictions when running behind reverse proxy or external interfaces.",
        "impact": "Unauthorized users on the network can view snapshots using guessed or brute-forced tokens.",
        "criteria": [
            "Verify internal snapshot requests originate from localhost or match valid tokens strictly bound to localhost",
            "Add unit tests verifying external access is blocked",
        ],
    },
    {
        "filename": "4.7-stale-cached-status.md",
        "title": "Web UI and Telegram Cached Status Displays Stale Printer Progress",
        "id": "4.7",
        "severity": "Low",
        "category": "Maintainability",
        "affected": "sentinel/web/routes.py, sentinel/bot/commands.py",
        "description": "Dashboard and Telegram bot query cached printer status that can remain stale, causing confusing UI updates.",
        "evidence": "Status cache has a TTL or behavior that displays outdated print progress without check validation.",
        "impact": "Confusing user interface that displays incorrect progress percentages.",
        "criteria": [
            "Ensure status updates fetch fresh data or show clear cache timestamps",
            "Verify cache is invalidated on print state changes",
        ],
    },
]


def main() -> None:
    os.makedirs(backlog_dir, exist_ok=True)
    for stub in stubs:
        content = f"""# {stub["title"]}

**ID:** {stub["id"]}
**Severity:** {stub["severity"]}
**Category:** {stub["category"]}
**Status:** Open

## Affected Files
- `{stub["affected"]}`

## Description
{stub["description"]}

## Evidence
- {stub["evidence"]}

## Impact
- {stub["impact"]}

## Acceptance Criteria
"""
        for crit in stub["criteria"]:
            content += f"- [ ] {crit}\n"
        content += "- [ ] Tests pass\n- [ ] Coverage maintained ≥ 85%\n"

        filepath = os.path.join(backlog_dir, stub["filename"])
        with open(filepath, "w") as f:
            f.write(content)
        print(f"Created stub: {stub['filename']}")


if __name__ == "__main__":
    main()
