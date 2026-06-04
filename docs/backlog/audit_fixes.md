# Pre-Public Beta Audit Fixes Backlog

## Security
- [x] **SEC-1**: Authentication Bypassed / API Exposed Externally by Default (Update `EXTERNAL_BIND_ALLOWED` in `docker-compose.yml`)
- [x] **SEC-2**: Open Redirect Vulnerability in Authentication Middleware (Validate `path` in `sentinel/web/auth.py`)

## Bugs & Correctness
- [x] **BUG-1**: Obico ML API Response Parsing Flaw (Always Fails Open) (Update `_parse` in `sentinel/ml/client.py`)
- [x] **BUG-2**: Printer Pause Failure Causes Infinite Spam Loop (Handle pause exception in `sentinel/watcher/loop.py`)
- [x] **BUG-3**: MJPEG Camera Stream Indefinite Hang (Add read timeout in `sentinel/camera/mjpeg.py`)
- [x] **BUG-4**: Auto-Stop Timeout Acts as a Reminder Only (Call `self._printer.stop()` in `sentinel/watcher/loop.py`)

## Performance & Stability
- [x] **PERF-1**: `ML_API_URL` HTTP validation fails with default config (Update `validate_https` in `sentinel/network.py`)
- [x] **PERF-2**: MQTT Status Listener Reconnection Deadlock (Clear `_accumulated_data` in `sentinel/printer/client.py`)
- [x] **PERF-3**: DB Analytics Cache Race Condition (Clear cache post-commit in `sentinel/db/repo.py`)
- [x] **PERF-4**: No Fallback Retries for ML API Network Timeouts (Use `tenacity` in `sentinel/ml/client.py`)
- [x] **PERF-5**: Resource Leak on Fast Shutdown (Make teardown async in `sentinel/camera/mjpeg.py` & `sentinel/__main__.py`)
- [x] **PERF-6**: In-Memory NonceStore Bound May Allow Substantial Memory Usage (Reduce `_MAX_SIZE` in `sentinel/ml/nonce.py`)

## Maintainability
- [x] **MAINT-1**: `sentinel` container fails to read root-owned ML token (Fix permissions in `docker/token-init/entrypoint.sh` or `entrypoint.sh`)
- [x] **MAINT-2**: CI matrix overwrites multi-arch Docker manifests (Update `.github/workflows/ci.yml`)
- [x] **MAINT-3**: `obico-ml` only built for `amd64` (Update `.github/workflows/build-obico-ml.yml`)
- [x] **MAINT-4**: Build-time dependency on upstream GitHub repository (Pin commits in `docker/obico-ml/Dockerfile`)
- [x] **MAINT-5**: JSON logging is missing despite being planned (Configure `python-json-logger` in `sentinel/__main__.py`)
- [x] **MAINT-6**: Unclear Obico ML authentication state and dead code (Remove unused token auth logic)
