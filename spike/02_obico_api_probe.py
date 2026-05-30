# ruff: noqa
"""Probe a running Obico ML container to map its API surface.

Start the container first, e.g.:
    docker run --rm -p 3333:3333 -e ML_API_TOKEN=test <image>

Then:
    python 02_obico_api_probe.py --base http://localhost:3333 --token test --image sample.jpg

What it checks:
  1. Auth modes: query param (?token=) vs header (Authorization: Bearer).
  2. Endpoints: GET /p/?img=<url> (URL-fetch) and POST /p/ with multipart (upload).
  3. Response shape — score field name, range, extra metadata.

Output is a citable block for docs/verified-assumptions.md.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx


async def try_request(client: httpx.AsyncClient, label: str, method: str, url: str, **kw) -> dict:
    print(f"\n--- {label} ---")
    print(f"  {method} {url}")
    try:
        r = await client.request(method, url, timeout=15, **kw)
    except httpx.HTTPError as e:
        print(f"  ERROR: {e!r}")
        return {"label": label, "ok": False, "error": repr(e)}
    print(f"  status: {r.status_code}")
    body_preview = r.text[:400].replace("\n", " ")
    print(f"  body[:400]: {body_preview}")
    return {
        "label": label,
        "ok": r.is_success,
        "status": r.status_code,
        "content_type": r.headers.get("content-type"),
        "body_preview": body_preview,
    }


async def main(args: argparse.Namespace) -> None:
    image_bytes = Path(args.image).read_bytes()
    async with httpx.AsyncClient() as client:
        results = []

        # 1. URL-fetch mode with token as query param.
        results.append(await try_request(
            client, "GET /p/ ?img=<url>&token=<t>", "GET",
            f"{args.base}/p/", params={"img": args.public_image_url, "token": args.token},
        ))

        # 2. URL-fetch mode with bearer header.
        results.append(await try_request(
            client, "GET /p/ ?img=<url>  (bearer header)", "GET",
            f"{args.base}/p/", params={"img": args.public_image_url},
            headers={"Authorization": f"Bearer {args.token}"},
        ))

        # 3. POST multipart upload with bearer header.
        results.append(await try_request(
            client, "POST /p/  (multipart, bearer)", "POST",
            f"{args.base}/p/",
            files={"img": ("frame.jpg", image_bytes, "image/jpeg")},
            headers={"Authorization": f"Bearer {args.token}"},
        ))

        # 4. POST multipart with token as query param.
        results.append(await try_request(
            client, "POST /p/  (multipart, ?token=)", "POST",
            f"{args.base}/p/",
            files={"img": ("frame.jpg", image_bytes, "image/jpeg")},
            params={"token": args.token},
        ))

        # 5. No-auth control (expect 401/403).
        results.append(await try_request(
            client, "GET /p/  (no auth, control)", "GET",
            f"{args.base}/p/", params={"img": args.public_image_url},
        ))

    print("\n=== VERIFIED ASSUMPTION: obico-ml API ===")
    print("Fill in for docs/verified-assumptions.md, based on which probes returned 2xx:")
    print("  upload_supported:  <yes | no>          # probe 3 or 4 succeeded")
    print("  url_fetch_supported: <yes | no>        # probe 1 or 2 succeeded")
    print("  auth_mode:         <query | header | both>")
    print("  endpoint_path:     <e.g. /p/>")
    print("  score_field:       <e.g. detections[0].confidence>  # from successful body")
    print("  notes:             <any quirks>")
    print("===")
    print("\nRaw results:")
    for r in results:
        print(f"  {r}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:3333", help="Base URL of running obico-ml")
    p.add_argument("--token", required=True, help="ML API token the container was started with")
    p.add_argument("--image", required=True, help="Path to a JPEG file for upload probes")
    p.add_argument(
        "--public-image-url",
        default="https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/320px-Cat03.jpg",
        help="A publicly-fetchable image URL for URL-fetch probes",
    )
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        sys.exit(130)
