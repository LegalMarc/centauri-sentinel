# Docs index

<!-- docs-sync
roots: docs/ README.md
exclude: vendor/ node_modules/ .venv*/ .claude/ .git/ .next/ coverage/ .terraform/ .pytest_cache/
code: sentinel/
-->

Read this file first. Each line: `path` — one-sentence scope. anchors: heading-slugs. covers: code globs.

## (root)
- `README.md` — Front door: what Sentinel does, supported hardware, the LAN-trust deployment baseline, and the full environment-variable reference. anchors: what-it-does, hardware, deployment-baseline--lan-trust, deployment, configuration-reference, telegram-setup, ntfy-setup, threat-model-summary covers: sentinel/__init__.py, sentinel/__main__.py, sentinel/config.py

## docs/
- `docs/CONTEXT.md` — Glossary of terms specific to this repo's domain (printer states, detection vocabulary), grown one entry at a time as terms get resolved.
- `docs/architecture.md` — How a camera frame becomes a paused print: the watcher state machine, the confirm-count gate, and the safety rules governing every pause and stop. anchors: components, watcher-state-machine, detection-path, safety-rules, notification-fan-out, telegram-bot covers: sentinel/watcher/*.py, sentinel/bot/*.py, sentinel/notify/*.py
- `docs/coolify-deploy.md` — Deploying and re-deploying the Compose stack on Coolify, including env vars, ML token rotation, and backups. anchors: prerequisites, quick-deploy-via-coolify-ui, environment-variables, re-deploying-after-a-code-change, updating-the-ml-token, backup, troubleshooting
- `docs/docker-deploy.md` — Running the three-service Compose stack (token-init, obico-ml, sentinel) directly with Docker, from clone to dashboard. anchors: prerequisites, 1-clone-the-repository, 2-create-your-env-file, 3-start-the-stack, 4-access-the-dashboard, viewing-logs, updating-to-a-new-version, rotating-the-ml-token covers: sentinel/healthcheck.py
- `docs/printer-setup.md` — One-time setup on the Centauri Carbon 2 itself: finding the access code, verifying LAN reachability, and where to place the printer on the network. anchors: supported-hardware, prerequisites, step-1--find-the-access-code, step-2--verify-lan-connectivity, step-3--network-placement, troubleshooting-connectivity
- `docs/threat-model.md` — The LAN-trust security baseline, the attack surface it accepts, and the checklist required before exposing the dashboard externally. anchors: design-baseline, attack-surface, exposing-externally--checklist, non-threats-accepted-risks, data-retention-policy, v02-candidates covers: sentinel/web/*.py, sentinel/network.py, sentinel/safety.py
- `docs/troubleshooting.md` — Symptom-first runbook for the failures seen in production: unhealthy stack, ML token mismatch, unreachable camera, MQTT refusal, ignored pause commands, stalled watcher, and Telegram auth. anchors: stack-does-not-become-healthy-within-90-seconds, obico-ml-fails-to-start-or-stays-unhealthy, ml-token-mismatch-401-unauthorized-from-obico-ml, mjpeg-camera-unreachable, printer-mqtt-connection-refused, pauseresumestop-do-nothing-status--detection-still-work, watcher-appears-stalled-watcher_stall_seconds-alert-fires, telegram-auth-refused-bot-ignores-commands covers: sentinel/camera/*.py, sentinel/ml/*.py, sentinel/db/*.py
- `docs/verified-assumptions.md` — Ground truth for every external interface, established by live probing: the Obico ML API, the Carbon 2 MQTT control protocol (register handshake and command codes), the MJPEG stream, and the Coolify API. anchors: 1-obico-ml-container, 2-obico-ml-api-surface, 3-printer-control-centauri-carbon-2-vs-pycentauri, 4-mjpeg-camera-stream, 5-coolify-deployment, summary-of-plan-amendments covers: sentinel/printer/*.py

## docs/adr/
- `docs/adr/0000-template.md` — Copy-me template for a new architecture decision record. anchors: status, context, decision, consequences, date

## docs/backlog/
- `docs/backlog/audit-fixes.md` — Checklist of coded fixes (B1..Bn) from the pre-beta audit, grouped by defect class. anchors: bugs--correctness, security--privacy, performance--scalability, stability--reliability, maintainability--operational-readiness
- `docs/backlog/pre_beta_audit_tasks.md` — Narrative findings behind the pre-public-beta audit checklist, with impact and resolution notes per item. anchors: bugs-and-correctness-defects, security-and-privacy-risks, performance-and-scalability-risks, stability-reliability-and-operational-readiness-risks
- `docs/backlog/tickets.md` — Audit tickets bucketed by severity, critical through low. anchors: critical-issues, high-issues, medium-issues, low-issues

## docs/plans/
- `docs/plans/ISSUES.md` — The v0.1 build broken into sequential, fully-specified issues, each with scope, acceptance criteria, file layout, and test plan. anchors: issue-0--spike-verify-external-assumptions, issue-1--project-scaffolding, issue-2--config-module, issue-3--persistence, issue-4--printer-client, issue-5--mjpeg-frame-grabber, issue-6--ml-client, issue-7--detection-watcher
- `docs/plans/PLAN.md` — The original v0.1 implementation plan: scope, deployment assumption, architecture, threat model, configuration, and quality bar. anchors: 1-scope-and-goals, 2-deployment-assumption, 3-architecture, 4-threat-model-documented-in-readme, 5-configuration, 6-open-unknowns--resolved-by-spike-0-see-docsverified-assumptionsmd, 7-issue-list-v01, 8-quality-bar

## docs/reports/
- `docs/reports/PROGRESS.md` — Per-ticket build log for the v0.1 sequence, recording status, timing, and notes for each issue as it landed. anchors: ticket-0--spike-verify-external-assumptions, ticket-1--project-scaffolding, ticket-2--config--safety-guard, ticket-3--persistence-layer, ticket-4--printer-client, ticket-5--camera-mjpeg-grabber, ticket-6--ml-client, ticket-7--watcher-loop
- `docs/reports/review_findings.md` — The eight-pass pre-publication review: all 54 findings with severity, impact, fix, and resolution status. anchors: summary, pass-1--correctness--business-logic, pass-2--security--threat-model, pass-3--async--concurrency, pass-4--resilience--error-handling, pass-5--test-quality--coverage, pass-6--docker--deployment, pass-7--documentation--ux
