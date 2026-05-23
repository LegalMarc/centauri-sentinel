# centauri-sentinel

> Self-hosted failure detection and remote control for the **Elegoo Centauri Carbon 2** FDM printer.
> Watches the printer's MJPEG camera with the Obico ML model, pauses on confirmed spaghetti,
> and alerts via Telegram and/or ntfy.

**Status:** v0.1 in progress — see [PROGRESS.md](PROGRESS.md).

## Quick start

See [docs/coolify-deploy.md](docs/coolify-deploy.md) for the full deployment guide.

## Configuration

All configuration is via environment variables. See [.env.example](.env.example) for the full
reference.

**Required:** `PRINTER_IP` (printer's IP on the LAN), `PRINTER_ACCESS_CODE` (from printer's
Settings → Network).

## Security

This service is designed for a trusted LAN. Read [docs/threat-model.md](docs/threat-model.md)
before exposing it externally.

## License

MIT — see [LICENSE](LICENSE).
