"""MJPEG soak logger — characterizes the printer's camera stream over a long run.

Run for at least 60 minutes against the real printer. Outputs:
  - frame intervals (ms) histogram
  - disconnect events with timestamps
  - any read stalls (no bytes for > stall_seconds)

Usage:
    python 04_mjpeg_soak.py --url http://192.168.1.50:8080/mjpeg --minutes 60

Findings feed issue #5 (MJPEG grabber) backoff tuning and the
CAMERA_OFFLINE thresholds.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime

import httpx

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def stream_once(url: str, stall_seconds: float, log) -> tuple[list[float], int]:
    """Consume MJPEG frames until disconnect or stall. Returns (intervals, frame_count)."""
    intervals: list[float] = []
    last_frame_t: float | None = None
    last_byte_t = time.perf_counter()
    frames = 0
    buf = bytearray()

    timeout = httpx.Timeout(connect=5.0, read=stall_seconds, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("GET", url) as r:
            log(f"connected status={r.status_code} ct={r.headers.get('content-type')}")
            async for chunk in r.aiter_bytes():
                now = time.perf_counter()
                if now - last_byte_t > stall_seconds:
                    log(f"stall detected: {now - last_byte_t:.1f}s without bytes")
                last_byte_t = now
                buf += chunk
                # Extract complete JPEGs from buf.
                while True:
                    soi = buf.find(SOI)
                    if soi < 0:
                        buf.clear()
                        break
                    eoi = buf.find(EOI, soi + 2)
                    if eoi < 0:
                        if soi > 0:
                            del buf[:soi]
                        break
                    frames += 1
                    t = time.perf_counter()
                    if last_frame_t is not None:
                        intervals.append((t - last_frame_t) * 1000)
                    last_frame_t = t
                    del buf[: eoi + 2]
    return intervals, frames


async def main(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.minutes * 60
    log_path = f"mjpeg_soak_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.log"
    log_f = open(log_path, "w")

    def log(msg: str) -> None:
        line = f"{now_iso()} {msg}"
        print(line)
        log_f.write(line + "\n")
        log_f.flush()

    log(f"soak start url={args.url} minutes={args.minutes} stall={args.stall_seconds}s")

    all_intervals: list[float] = []
    total_frames = 0
    disconnects = 0
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        try:
            intervals, frames = await stream_once(args.url, args.stall_seconds, log)
            all_intervals.extend(intervals)
            total_frames += frames
            log(f"stream ended cleanly: frames={frames} attempt={attempt}")
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            disconnects += 1
            log(f"disconnect: {e!r} attempt={attempt}")
        # Backoff before reconnect.
        await asyncio.sleep(min(0.5 * (2 ** min(attempt, 6)), 30))

    # Summary.
    log("=" * 60)
    log(f"total frames: {total_frames}")
    log(f"disconnects:  {disconnects}")
    if all_intervals:
        log(f"interval ms — min={min(all_intervals):.1f} "
            f"median={statistics.median(all_intervals):.1f} "
            f"p95={statistics.quantiles(all_intervals, n=20)[18]:.1f} "
            f"max={max(all_intervals):.1f}")
        buckets = Counter()
        for v in all_intervals:
            if v < 50: buckets["<50ms"] += 1
            elif v < 100: buckets["50-100ms"] += 1
            elif v < 250: buckets["100-250ms"] += 1
            elif v < 500: buckets["250-500ms"] += 1
            elif v < 1000: buckets["500-1000ms"] += 1
            elif v < 5000: buckets["1-5s"] += 1
            else: buckets[">=5s"] += 1
        for b, c in buckets.most_common():
            log(f"  {b}: {c}")

    log("=== VERIFIED ASSUMPTION: MJPEG ===")
    log(f"  url:                {args.url}")
    log(f"  duration_minutes:   {args.minutes}")
    log(f"  total_frames:       {total_frames}")
    log(f"  disconnects:        {disconnects}")
    log("  recommended_backoff: <fill in: starting delay, cap, retries before CAMERA_OFFLINE>")
    log("  recommended_stall:  <fill in WATCHER stall threshold based on observed gaps>")
    log("===")
    log_f.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="Printer MJPEG URL")
    p.add_argument("--minutes", type=int, default=60)
    p.add_argument("--stall-seconds", type=float, default=10.0,
                   help="No-bytes window that counts as a stall")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        sys.exit(130)
