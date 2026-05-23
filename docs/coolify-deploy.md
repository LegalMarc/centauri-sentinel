# Coolify Deployment Guide

centauri-sentinel is deployed as a Docker Compose application on Coolify.

## Prerequisites

- Coolify ≥ 4.x running on your server
- The server must be on the same LAN as your Elegoo Centauri Carbon 2 printer
- A Git repository containing this codebase (e.g. GitHub)

## Quick deploy via Coolify UI

1. In Coolify → **Projects** → **New Resource** → **Docker Compose**.
2. Set **Source**: Git Repository → paste your repo URL.
3. Set **Branch**: `main`.
4. Set **Docker Compose file**: `./docker-compose.yml`.
5. Under **Environment Variables**, add:
   - `PRINTER_IP` → your printer's LAN IP (e.g. `192.168.1.50`). **Required.**
   - `PRINTER_ACCESS_CODE` → the access code from **Settings → Network** on the printer
     touchscreen. Default `123456` if you have never changed it.
   - Any optional vars from `.env.example` (Telegram, ntfy, auth, etc.).
6. Click **Save and Deploy**.
7. Wait for all three services to become healthy (typically < 90 s).

The dashboard will be available at the URL Coolify assigns (shown in the resource overview).

## Environment variables

See `.env.example` for the full reference. The minimum required set:

```
PRINTER_IP=192.168.x.x
PRINTER_ACCESS_CODE=123456   # change to match your printer's access code
```

Everything else has a safe default. See [README.md](../README.md#configuration-reference)
for the full variable reference.

## Re-deploying after a code change

Coolify watches the branch for new commits. Either:
- Push to `main` → Coolify auto-deploys (if webhook is configured).
- Or in the Coolify UI → resource → **Redeploy**.

## Updating the ML token

The ML token is generated once by the `token-init` container and stored in
the `ml-token` Docker volume.  Redeploying the stack does **not** regenerate
the token unless you delete the volume.

To rotate the token manually:

```sh
# On the Coolify host:
docker volume rm <stack_prefix>_ml-token
# Then redeploy the stack.
```

## Backup

The only stateful volume is `sentinel-data` (SQLite DB + snapshots).
Back it up by copying `/var/lib/docker/volumes/<stack_prefix>_sentinel-data`.

## Troubleshooting

See [docs/troubleshooting.md](troubleshooting.md) for common issues.
