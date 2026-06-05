# Initiation prompt — centauri-sentinel autonomous build

Paste the block below into a fresh Claude Code (Sonnet) session opened in
`/Users/mhm/Documents/Dev/centauri-sentinel`. It is self-contained: Sonnet
will not have seen the planning conversation, so the prompt carries every
piece of context it needs.

---

You are building **centauri-sentinel**, an open-source self-hosted failure detector for the Elegoo Centauri Carbon 2 3D printer. The full plan is already written in this repo:

- `PLAN.md` — architecture, scope, threat model, config reference
- `ISSUES.md` — fully specified tickets #0 through #14
- `spike/` — four ready-to-run probe scripts for ticket #0

**Read both files completely before doing anything else.** They define the work. Do not deviate from the scope in `PLAN.md §1` without surfacing it as a blocker.

## Your job

Execute tickets **#0 through #13 in order**, autonomously, in a loop. **Stop before #14** (hardware E2E) and hand off — I will run that manually.

## Operating rules

1. **One ticket per iteration.** After finishing a ticket: commit, push, comment on the GitHub issue, update `PROGRESS.md`, then `/clear` and start the next ticket fresh. Clearing context between tickets is mandatory — each ticket must be self-contained from `ISSUES.md` + the current repo state.

2. **Acceptance criteria are the gate.** A ticket is done only when every box in its "Acceptance" section is green. Run the tests. Run the linters. Run the type checker. If anything fails, fix it before moving on.

3. **Commit hygiene.** One feature branch per ticket: `ticket/NN-short-name`. Open a PR against `main`, merge it yourself (squash) once CI is green, then move on. Conventional Commits style. Every commit signed with the Co-Authored-By trailer.

4. **Stop only on real blockers.** A blocker is:
   - An acceptance criterion that cannot be met without a decision I have to make (scope change, security tradeoff, missing external info that the spike could not resolve).
   - A persistent CI failure you cannot diagnose after two honest attempts.
   - Anything that would cost money, send messages externally, or affect systems beyond this repo and the local Coolify instance.

   Minor ambiguities: pick the most practical interpretation, document it in the PR description under "Assumptions", continue. Do **not** stop for these.

5. **No scope creep.** v0.1 scope is locked: Telegram + ntfy + read-only status page. No Tailwind, no HTMX, no SSE. If you find yourself wanting to add something not in `PLAN.md` or `ISSUES.md`, that desire is wrong — note it as a v0.2 candidate in `PROGRESS.md` and move on.

6. **Quality bar from `ISSUES.md` header is non-negotiable:** Python 3.12, `uv`, `ruff`, `mypy --strict` on `sentinel/`, pytest ≥ 85% coverage. Structured JSON logs. All external I/O wrapped with timeout + retry + structured error.

## Environment and secrets

- **Repo:** `https://github.com/LegalMarc/centauri-sentinel` (already created, empty). The `gh` CLI is authenticated as `LegalMarc`. Push directly.
- **Coolify instance:** `https://coolify.example.com`. The API key lives in the macOS keychain. Retrieve it with:
  ```sh
  security find-generic-password -s "coolify-api-key" -a "$USER" -w
  ```
  If that fails, the alternate service name to try is `centauri-sentinel-coolify`. If neither works, that is a blocker — ask me.
- **Printer IP:** ask me interactively the first time you need it (ticket #0, script #3 or #4). Cache it in `.env.local` (gitignored) for subsequent iterations.
- **GHCR push:** use the `gh auth token` to log Docker into ghcr.io. Tagged builds push automatically via the CI workflow you write in #1.

## Coolify deployment

After ticket #12 (Docker Compose stack), deploy the stack to `coolify.example.com` using the API.

- Coolify API docs: `https://coolify.io/docs/api-reference/api/authorization`. Use Bearer auth with the keychain token.
- Create the application as a "Docker Compose" resource pointing at this Git repo, `main` branch, compose file at `./docker-compose.yml`.
- Set `PRINTER_IP` as an environment variable (value: the printer IP I gave you in #0).
- Trigger a deploy. Wait for healthy status (poll the API; do not sleep-loop — use the same backoff pattern you build into the app).
- If the deploy succeeds, record the application UUID and the deployed URL in `PROGRESS.md`. If it fails, capture the Coolify logs via the API, paste them into the ticket #12 issue, and treat it as a blocker.

The Coolify deploy is part of #12's acceptance, not a separate ticket.

## Progress reporting

After **every** ticket (success or blocker):

1. **Append to `PROGRESS.md`** at repo root, with this format:
   ```
   ## Ticket #NN — <title>
   - Status: done | blocked
   - PR: <url>
   - Started: <ISO timestamp>
   - Finished: <ISO timestamp>
   - Notes: <one paragraph: what shipped, any assumptions made, any v0.2 candidates discovered>
   - Blocker (if status=blocked): <what you need from me to proceed>
   ```
2. **Comment on the GitHub issue** for that ticket with the same content. If no issue exists yet, create one from the spec in `ISSUES.md` first.

Before starting each ticket, also append a `Started:` line so I can see live progress.

## Issue setup (do this before ticket #0)

Create GitHub issues #0 through #14 on the repo, one per section of `ISSUES.md`. Use the section heading as the issue title; the section body as the issue body. Add labels: `v0.1`, plus one of `spike|scaffolding|feature|infra|docs|hardware`. Pin the v0.1 milestone to all of them.

## The loop

Pseudocode for what you are doing:

```
read PLAN.md, ISSUES.md
create GitHub issues #0..#14
for ticket in [0..13]:
    branch = "ticket/NN-slug"
    git checkout -b $branch
    append "Started" line to PROGRESS.md + issue comment
    do the work per ISSUES.md acceptance criteria
    run: uv run ruff check && uv run mypy sentinel/ && uv run pytest --cov
    if anything fails after 2 honest attempts:
        report blocker, stop
    open PR, wait for CI, squash-merge
    append "done" entry to PROGRESS.md + issue comment
    if ticket == 12: deploy to coolify.example.com
    /clear
post final report: "v0.1 ready for hardware E2E (#14)"
```

## What "honest attempts" means

A real attempt reads the actual error, forms a hypothesis, tests it, observes the result. Two of those before declaring a blocker. **Not** two random tweaks. If you find yourself patching the same file with no understanding of why the test fails, stop and report.

## Final deliverable

When you stop (either at #13 done or at a real blocker):

1. `PROGRESS.md` reflects everything.
2. `main` is green in CI.
3. The Coolify deploy is live and healthy at whatever URL Coolify assigned.
4. A final summary comment on the repo's `README.md` PR (or as an issue if no PR) listing: tickets completed, total commits, total PRs, deploy URL, anything I need to do for #14.

Begin by reading `PLAN.md` and `ISSUES.md`. Then create the GitHub issues. Then start ticket #0.
