"""Live probe against a Centauri Carbon 2 via pycentauri.

Goal: capture the exact state strings, response shapes, and timing of
status / pause / resume / stop calls — so issue #4 (printer client) can
be written against real data, not guesses.

Usage:
    python 03_pycentauri_probe.py --ip 192.168.1.50

What it does (read-only by default):
  1. Connects, calls status() N times, prints raw response + identified state.
  2. If --interactive is passed, prompts before each control call (pause/resume/stop).
  3. Times every call and logs blocking behavior (so we know what needs asyncio.to_thread).

NOTE: pycentauri's exact API is unknown until installed. This script uses a
best-effort introspection pattern — adjust attribute/method names once the
library is installed and `dir(client)` reveals the real surface.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

try:
    import pycentauri  # type: ignore
except ImportError:
    print("pycentauri not installed. Run: uv pip install pycentauri", file=sys.stderr)
    sys.exit(1)


def timed(label: str, fn, *a, **kw) -> Any:
    t0 = time.perf_counter()
    try:
        out = fn(*a, **kw)
        ok = True
        err = None
    except Exception as e:
        out = None
        ok = False
        err = repr(e)
    dt_ms = (time.perf_counter() - t0) * 1000
    print(f"  [{dt_ms:7.1f} ms] {label}  ok={ok}  err={err}")
    return out


def dump(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str, indent=2)[:1000]
    except Exception:
        return repr(obj)[:1000]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ip", required=True, help="Printer IP on LAN")
    p.add_argument("--port", type=int, default=None, help="Optional override port")
    p.add_argument("--samples", type=int, default=5, help="status() calls to make")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between status calls")
    p.add_argument("--interactive", action="store_true", help="Prompt before pause/resume/stop")
    args = p.parse_args()

    print(f"pycentauri module: {pycentauri.__file__}")
    print(f"pycentauri exports: {[n for n in dir(pycentauri) if not n.startswith('_')]}")
    print()

    # Best-effort: try a few likely constructor names.
    client = None
    for attr in ("Printer", "Client", "PrinterClient", "Centauri"):
        if hasattr(pycentauri, attr):
            print(f"Trying constructor: pycentauri.{attr}(ip={args.ip!r})")
            try:
                client = getattr(pycentauri, attr)(args.ip) if args.port is None \
                    else getattr(pycentauri, attr)(args.ip, args.port)
                break
            except Exception as e:
                print(f"  failed: {e!r}")
    if client is None:
        print("Could not construct a client. Inspect dir(pycentauri) above and patch this script.")
        sys.exit(2)

    print(f"\nclient type: {type(client).__name__}")
    print(f"client methods: {[m for m in dir(client) if not m.startswith('_')]}")
    print()

    seen_states: set[str] = set()
    for i in range(args.samples):
        print(f"-- status call {i+1}/{args.samples} --")
        resp = timed("status()", getattr(client, "status", lambda: None))
        if resp is not None:
            print(dump(resp))
            # Try common shapes to identify the state field.
            for key in ("state", "status", "printer_state", "printState"):
                if isinstance(resp, dict) and key in resp:
                    seen_states.add(str(resp[key]))
        time.sleep(args.interval)

    print(f"\nObserved state values: {sorted(seen_states)}")

    if args.interactive:
        for cmd in ("pause", "resume", "stop"):
            ans = input(f"Call {cmd}()? [y/N] ").strip().lower()
            if ans == "y" and hasattr(client, cmd):
                timed(f"{cmd}()", getattr(client, cmd))
                time.sleep(2)
                print("Post-call status:")
                print(dump(timed("status()", getattr(client, "status", lambda: None))))

    print("\n=== VERIFIED ASSUMPTION: pycentauri ===")
    print("Fill in for docs/verified-assumptions.md:")
    print(f"  library_version:   <pip show pycentauri>")
    print(f"  client_class:      {type(client).__name__}")
    print(f"  status_method:     status()  # confirm name")
    print(f"  state_field:       <key in response, e.g. 'state'>")
    print(f"  state_strings:     {sorted(seen_states)}")
    print(f"  blocking_io:       <yes | no — based on timings above>")
    print(f"  pause/resume/stop: <method names + return shapes>")
    print(f"  notes:             <protocol version field if exposed>")
    print("===")


if __name__ == "__main__":
    main()
