# Dashboard UI

The dashboard is one server-rendered page (`sentinel/web/templates/dashboard.html`) styled
with the OpenBridge design system, plus the login page rendered by the auth middleware.
This document covers its layout, how the OpenBridge components are vendored, and how to
work on it without a printer.

## Layout

Two bars stay on top: the OpenBridge top bar (name, current view, watcher state, palette
and account buttons) and a command bar (views on the left, printer commands on the right).

- **Monitor** fills the rest of the screen without scrolling. The camera is as wide as its
  column allows unless that would push the job strip below the fold; its box takes the
  stream's real aspect ratio (read from the first frame), so there is no letterboxing. The
  native resolution shows in the camera's corner. A rail on the right holds the failure
  watch (score against the pause threshold, confirm streak, heartbeat), temperatures,
  recent activity and the slicer thumbnail. When the rail is taller than the screen, the
  rail scrolls on its own and the page does not.
- **History** lists jobs, detections and pauses in local time, with the clear-history
  action at the bottom (two clicks, no browser dialog).
- **Settings** holds the detection settings, the printer address, and the account actions
  (log out, log out all devices).

State is shown in form as well as text: a caution frame and banner around the camera when
the print is paused, status chips coloured from the OpenBridge alert palette, and the
gauge turning red when the score crosses the threshold.

Snooze opens a menu of lengths (5 min, 10 min, 30 min, 1 hour); choosing one snoozes
detection and the Failure watch chip shows "Snoozed to HH:MM". The camera chip shows the
stream's native resolution, read from the frame on every poll because browsers do not
reliably fire `load` for an MJPEG image. The score age reads in the largest whole unit
("42s ago", "7m ago", "10d ago").

On phones (≤ 640 px) the commands move to a bottom bar that shows only the commands that
apply right now. With the printer idle that is Snooze alone. The snooze menu opens upward.

## OpenBridge components

OpenBridge is a maritime bridge design system. Sentinel uses
`@oicl/openbridge-webcomponents` **1.0.1**, the last release licensed Apache-2.0. Version
2.0.0 is AGPL-3.0-only (each 2.x release becomes Apache-2.0 180 days after it ships), so do
not upgrade without checking the license of the target version (see [ADR 0001](adr/0001-openbridge-1-0-1.md)). The full license texts for
everything in the bundle (OpenBridge, Lit, Noto Sans) are in `web/THIRD_PARTY_LICENSES.txt`,
copied next to the bundle as `LICENSE.txt`. The OpenBridge name and logo are not used in the UI.

- `web/entry.js` imports only the components and icons the page uses. Icons are
  `obi-*` elements; components are `obc-*`.
- `web/build.mjs` bundles them with esbuild into
  `sentinel/web/static/vendor/openbridge/` (`ob.js`, `openbridge.css`, `NotoSans.ttf`,
  `LICENSE.txt`). That directory is build output and is not committed.
- The Dockerfile's `web` stage runs the build, so the runtime image never fetches anything
  and the strict CSP (`script-src 'self'`) holds.
- Palettes are `day`, `dusk`, `night` and `bright`, set as `data-obc-theme` on `<html>` by
  `sentinel/web/static/palette.js` before first paint. The top bar's dimming button cycles
  them and the choice is remembered per browser.
- OpenBridge has no danger button. Stop is the normal button retinted through its own CSS
  custom properties (`obc-button.danger` in `dashboard.css`).
- `obc-button` does not reflect `disabled` to the attribute, so the page toggles the
  attribute (which sets the property) to let CSS react to it.

Page styles and script live in `sentinel/web/static/dashboard.css` and `dashboard.js`.
Static URLs carry `?v=<content hash>` (`asset_version` in `sentinel/web/app.py`), so a deploy
is never served from a stale browser cache. `/static/` is public: it holds no private data
and the login page needs it before sign-in.

## Building and previewing

```bash
npm --prefix web ci
```

```bash
npm --prefix web run build
```

```bash
uv run python tools/dev_preview.py --scenario printing
```

The preview runs the real app, routes, templates, auth and SQLite repository against a fake
printer, camera and watcher, seeded with sample history. The camera streams
`tools/preview_assets/sample_frame.jpg`. Scenarios are `printing`, `paused`, `idle` and
`offline`; Pause, Resume and Stop change the fake's state, and Snooze records the same
settings the real watcher writes (it does not expire; restart the preview). `--auth`
enables login with the preview-only credentials defined in `tools/dev_preview.py`.
`.claude/launch.json` defines both as `dashboard-preview` (port 8765) and
`dashboard-preview-auth` (port 8766).
