# Spike scripts (issue #0)

Four probes to resolve the open assumptions before feature work. Each script writes
a numbered fragment into `docs/verified-assumptions.md` (or stdout you paste in).

Run order doesn't matter; #3 and #4 need physical access to the printer on LAN.

```
spike/
  01_obico_image_inspect.sh    # ARM64 manifest check + Obico repo metadata
  02_obico_api_probe.py        # Hit a running obico-ml container, map the API
  03_pycentauri_probe.py       # Live status() against the printer, dump state strings
  04_mjpeg_soak.py             # 60-minute MJPEG logger: frame intervals + disconnects
```

## Prereqs

```sh
uv venv .venv && source .venv/bin/activate
uv pip install httpx pycentauri rich
# Docker required for #1 and #2.
```

## Expected outputs

Each script prints a clearly-delimited block like:

```
=== VERIFIED ASSUMPTION: obico-ml ARM64 ===
image: ghcr.io/...:tag
arm64: yes|no
source: <command/url cited>
===
```

Copy these blocks into `docs/verified-assumptions.md` under the matching section.
