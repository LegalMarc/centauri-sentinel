# Printer setup

This page covers the one-time configuration on the Elegoo Centauri Carbon 2 needed before
centauri-sentinel can connect.

---

## Supported hardware

| Model | Status |
|---|---|
| Elegoo Centauri Carbon 2 | Supported |
| Elegoo Centauri Carbon 1 | **Not supported** — different MQTT API |
| Other Elegoo models | Unknown — not tested |

---

## Prerequisites

- The printer is powered on and connected to your LAN via Wi-Fi or Ethernet.
- You know the printer's LAN IP address (check your router's DHCP table, or look in
  **Settings → Network** on the printer's touchscreen).

---

## Step 1 — Find the access code

1. On the printer touchscreen, go to **Settings → Network** (or **Settings → Wi-Fi**,
   depending on firmware version).
2. Look for **Access Code** (sometimes labelled **Printer Code**). It is a short numeric
   or alphanumeric string (default: `123456`).
3. This value is `PRINTER_ACCESS_CODE` in your centauri-sentinel configuration.

> **Note:** The access code is the MQTT broker password. If you change it on the printer,
> update `PRINTER_ACCESS_CODE` and redeploy centauri-sentinel.

---

## Step 2 — Verify LAN connectivity

From the machine that will run Coolify, verify the MQTT broker is reachable:

```sh
# Requires mosquitto-clients or any MQTT client
mosquitto_sub -h <PRINTER_IP> -p 1883 -u bblp -P <ACCESS_CODE> -t '#' -v
```

If the printer is reachable you will see a stream of MQTT messages. Press Ctrl-C.

Alternatively, verify the camera:

```sh
curl -s "http://<PRINTER_IP>:8080/mjpeg" --max-time 3 -o /dev/null -w "%{http_code}"
# Should return 200
```

---

## Step 3 — Network placement

centauri-sentinel uses two protocols to talk to the printer:

| Protocol | Port | Direction |
|---|---|---|
| MQTT | 1883 | centauri-sentinel → printer |
| MJPEG HTTP | 8080 | centauri-sentinel → printer |

Both connections are **outbound from the Coolify host to the printer**. No inbound firewall
rules are needed on the printer.

The Coolify host and the printer must be on the same L3 segment (or the firewall must allow
these ports between them). The printer does not need internet access for centauri-sentinel to
work.

---

## Troubleshooting connectivity

See [docs/troubleshooting.md](troubleshooting.md) for common issues.
