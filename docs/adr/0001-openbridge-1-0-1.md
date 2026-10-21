# ADR 0001: Stay on OpenBridge web components 1.0.1

## Status

Accepted

## Context

The dashboard is built on `@oicl/openbridge-webcomponents`. Version 1.0.1 is licensed
Apache-2.0. Version 2.0.0 (released 2026-09-29) is AGPL-3.0-only, with each 2.x release
becoming Apache-2.0 180 days after it ships. AGPL would add source-offer obligations for a
network-served app.

A direct comparison of the two packages (2026-10-20) found no visible gain for this dashboard:

- Palette tokens moved by 3 to 5 RGB units, except a deeper caution amber, a darker night
  alarm red and slightly brighter dusk text.
- The button, top bar, toggle and icon button changed only in build tooling.
- The 221 new tokens and 9 new components cover nautical charts, ship automation diagrams
  and alert counters, none of which the dashboard uses.

## Decision

Pin 1.0.1 in `web/package.json`. Do not upgrade to any 2.x release while it is AGPL-only.

## Consequences

- No license obligations beyond the Apache, BSD and OFL notices already shipped in
  `web/THIRD_PARTY_LICENSES.txt`.
- On or after 2027-03-28, 2.0.0 becomes available under Apache-2.0. Upgrading then means
  checking `LICENSE.txt` in the new package, bumping the pin, rebuilding, and a visual check
  with `tools/dev_preview.py` in all four palettes.
- If the deeper 2.0 caution amber is wanted sooner, set that one token in
  `sentinel/web/static/dashboard.css` instead of upgrading.

## Date

2026-10-20
