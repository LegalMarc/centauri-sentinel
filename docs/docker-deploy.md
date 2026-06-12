# Docker Deployment Guide

centauri-sentinel ships as a three-service Docker Compose stack (`token-init`,
`obico-ml`, `sentinel`). This guide covers running it directly with Docker and
the Compose plugin — no Coolify or other PaaS required. If you deploy via
Coolify instead, see [coolify-deploy.md](coolify-deploy.md).

## Prerequisites

- A Linux host on the **same LAN** as your Elegoo Centauri Carbon 2 printer
  (amd64 or arm64).
- **Docker Engine ≥ 24** with the **Compose v2 plugin** (`docker compose version`
  should print v2.x). Install via the [official Docker docs](https://docs.docker.com/engine/install/).
- `git` to clone this repository.

## 1. Clone the repository

```sh
git clone https://github.com/LegalMarc/centauri-sentinel.git
cd centauri-sentinel
```

## 2. Create your `.env` file

Copy the reference and edit it:

```sh
cp .env.example .env
```

At minimum, set your printer's LAN IP and access code:

```sh
# .env
PRINTER_IP=192.168.1.50
PRINTER_ACCESS_CODE=123456   # from the printer's Settings → Network screen
```

Everything else has a safe default — see
[README.md](../README.md#configuration-reference) for the full reference.

### Enabling dashboard auth (recommended if reachable beyond localhost)

The dashboard binds on `0.0.0.0` by default, so the startup guard refuses to
start when the host is externally reachable unless auth is configured. To enable
the login form, set a username and a **bcrypt hash** of your password.

Generate the hash:

```sh
python3 -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
# -> $2b$12$X5qE.czdChxBlEzJHYwZPe9jUkcv6uN9OVnwuJWxwz0xZwr91oN.2
```

> **⚠️ Critical `.env` gotcha — escape every `$` as `$$`.**
> Docker Compose performs variable interpolation on values in the `.env` file,
> so a literal `$` is treated as the start of a variable reference. A bcrypt
> hash is full of `$` separators (`$2b$12$…`) and **will be silently corrupted**
> — the container receives a broken hash and every login fails with
> "Invalid username or password."
>
> Double every `$` to `$$` when writing the hash into `.env`:
>
> ```sh
> # WRONG — interpolated and corrupted:
> AUTH_USERNAME=admin
> AUTH_PASSWORD_BCRYPT=$2b$12$X5qE.czdChxBlEzJHYwZPe9jUkcv6uN9OVnwuJWxwz0xZwr91oN.2
>
> # RIGHT — each $ doubled to $$:
> AUTH_USERNAME=admin
> AUTH_PASSWORD_BCRYPT=$$2b$$12$$X5qE.czdChxBlEzJHYwZPe9jUkcv6uN9OVnwuJWxwz0xZwr91oN.2
> ```
>
> Produce the escaped form in one step:
>
> ```sh
> python3 -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode().replace('\$','\$\$'))"
> ```

## 3. Start the stack

```sh
docker compose up -d --build
```

This builds the `sentinel` and `token-init` images from source, pulls the
`obico-ml` image, and starts all three services. First build takes a few
minutes; subsequent starts are fast.

Check status until all services are healthy (typically < 90 s):

```sh
docker compose ps
```

You want `sentinel` showing `running (healthy)`. The `token-init` service is a
one-shot — it exits `0` after writing the shared ML token, which is expected.

## 4. Access the dashboard

```
http://<host-ip>:8000
```

`8000` is the host port published by Compose (`SENTINEL_PORT`, defaults to
`8000`). If you enabled auth, you'll get the login form; otherwise the dashboard
loads directly.

> **Exposing beyond the LAN:** do not publish port 8000 to the public internet
> directly. Put it behind a TLS-terminating reverse proxy (Caddy, nginx,
> Traefik), enable auth as above, and set `EXTERNAL_BIND_ALLOWED=true` plus
> `TRUST_PROXIES=true`. See [threat-model.md](threat-model.md).

## Viewing logs

```sh
docker compose logs -f sentinel     # follow the sentinel service
docker compose logs obico-ml        # ML API
```

## Updating to a new version

```sh
git pull
docker compose up -d --build
```

The `ml-token` and `sentinel-data` volumes persist across updates, so your token
and database are retained.

## Rotating the ML token

The ML token is generated once by `token-init` and stored in the `ml-token`
volume. It is **not** regenerated on update. To rotate it:

```sh
docker compose down
docker volume rm centauri-sentinel_ml-token
docker compose up -d --build
```

(The volume name is prefixed with the Compose project name, which defaults to
the directory name `centauri-sentinel`. Confirm with `docker volume ls`.)

## Backup

The only stateful volume is `sentinel-data` (SQLite database + camera
snapshots). Back it up with a throwaway container:

```sh
docker run --rm \
  -v centauri-sentinel_sentinel-data:/data:ro \
  -v "$(pwd)":/backup \
  busybox tar czf /backup/sentinel-data-backup.tar.gz -C /data .
```

Restore by extracting the tarball back into the volume.

## Stopping / removing

```sh
docker compose down            # stop, keep volumes (data preserved)
docker compose down -v         # stop AND delete volumes (wipes DB + token)
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Every login fails with "Invalid username or password" | `$` in the bcrypt hash was interpolated by Compose | Escape each `$` as `$$` in `.env` (see step 2) |
| Container refuses to start, logs mention external bind | Reachable externally with auth unset | Set `AUTH_USERNAME` + `AUTH_PASSWORD_BCRYPT`, or restrict the host |
| `sentinel` unhealthy, ML errors in logs | `obico-ml` not healthy yet | Wait for `obico-ml` healthcheck; check `docker compose logs obico-ml` |
| Can't reach the printer | Wrong `PRINTER_IP` / not on same LAN | Verify the IP and that the host shares the printer's network |

See [troubleshooting.md](troubleshooting.md) for more.
