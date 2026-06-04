# Synchronous Disk Read of ML Token Blocks the FastAPI ASGI Request Loop

**ID:** PERF-06
**Severity:** Medium
**Category:** Performance
**Status:** Closed

## Affected Files
- `sentinel/web/auth.py`

## Description
`AuthMiddleware._load_token` performs synchronous file system calls directly on the main event loop thread without wrapping in `asyncio.to_thread`. Under load, slow disk access will block the FastAPI request processing loop.

## Acceptance Criteria
- [x] Wrap the file system calls inside `AuthMiddleware._load_token` with `asyncio.to_thread` or perform the file reading asynchronously.
- [x] Verify that request flow remains functional and tests pass.
