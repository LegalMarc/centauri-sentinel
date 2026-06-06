#!/usr/bin/env bash
#
# build-local-runner.sh — provision a fresh repo-scoped GitHub Actions self-hosted
# runner on the local Coolify instance (coolify.example.com) for any repo.
#
# Credentials are NEVER passed on the command line or written to disk. They are read
# from the macOS Keychain at runtime:
#   - Coolify API token       -> keychain service "coolify-api"
#   - GitHub runner PAT        -> keychain service "github-runner-pat"
#                                 (fine-grained PAT, repo Administration: Read & write)
# GitHub-side reads/polling use the ambient `gh` CLI auth.
#
# Usage:
#   build-local-runner.sh create   OWNER/REPO [options]
#   build-local-runner.sh verify   OWNER/REPO
#   build-local-runner.sh teardown OWNER/REPO
#
# create options:
#   --labels   <csv>     Extra runner labels (default: "<repo-slug>,coolify,local")
#   --prefix   <name>    Runner name prefix   (default: "github-runner-<repo-slug>")
#   --docker             Mount the host docker.sock into the runner (needed only for
#                        CI jobs that build/run containers, e.g. testcontainers).
#                        OFF by default — mounting the socket is host-root-equivalent.
#   --ephemeral          One job per runner then re-register clean (EPHEMERAL=true).
#
set -euo pipefail

# ---- defaults (override via env) --------------------------------------------
COOLIFY_URL="${COOLIFY_URL:-https://coolify.example.com}"
COOLIFY_KEYCHAIN_SERVICE="${COOLIFY_KEYCHAIN_SERVICE:-coolify-api}"
RUNNER_PAT_KEYCHAIN_SERVICE="${RUNNER_PAT_KEYCHAIN_SERVICE:-github-runner-pat}"
COOLIFY_PROJECT_NAME="${COOLIFY_PROJECT_NAME:-Github Actions}"
COOLIFY_ENV_NAME="${COOLIFY_ENV_NAME:-production}"
COOLIFY_SERVER_NAME="${COOLIFY_SERVER_NAME:-localhost}"
RUNNER_IMAGE="${RUNNER_IMAGE:-myoung34/github-runner:latest}"
POLL_TIMEOUT_SECS="${POLL_TIMEOUT_SECS:-240}"

# ---- pretty output ----------------------------------------------------------
c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
say()  { printf '%s\n' "$*" >&2; }
ok()   { printf '%s✓%s %s\n' "$c_grn" "$c_rst" "$*" >&2; }
warn() { printf '%s!%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit 1; }
step() { printf '\n%s==>%s %s\n' "$c_dim" "$c_rst" "$*" >&2; }

# ---- preflight: required tooling --------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
need gh; need jq; need curl; need security; need base64

# ---- credential readers (Keychain) ------------------------------------------
read_keychain() { # $1 = service name
  security find-generic-password -s "$1" -w 2>/dev/null || return 1
}

load_coolify_token() {
  COOLIFY_TOKEN="$(read_keychain "$COOLIFY_KEYCHAIN_SERVICE")" || die \
"No Coolify API token in Keychain (service '$COOLIFY_KEYCHAIN_SERVICE').
  Mint a token at ${COOLIFY_URL}/security/api-tokens (read+write on the
  '$COOLIFY_PROJECT_NAME' project), then store it once:
    security add-generic-password -U -a \"\$USER\" -s $COOLIFY_KEYCHAIN_SERVICE -w
  (you'll be prompted for the token; it won't appear in shell history)"
  [ -n "$COOLIFY_TOKEN" ] || die "Coolify token in Keychain is empty"
}

load_runner_pat() {
  RUNNER_PAT="$(read_keychain "$RUNNER_PAT_KEYCHAIN_SERVICE")" || die \
"No GitHub runner PAT in Keychain (service '$RUNNER_PAT_KEYCHAIN_SERVICE').
  Create a fine-grained PAT (github.com/settings/personal-access-tokens) with
  repository permission 'Administration: Read and write' on the target repo,
  then store it once:
    security add-generic-password -U -a \"\$USER\" -s $RUNNER_PAT_KEYCHAIN_SERVICE -w"
  [ -n "$RUNNER_PAT" ] || die "Runner PAT in Keychain is empty"
}

# ---- Coolify API helper -----------------------------------------------------
cf() { # $1=METHOD $2=path ; optional JSON body on stdin
  local method="$1" path="$2"
  if [ "$method" = GET ]; then
    curl -fsS -H "Authorization: Bearer $COOLIFY_TOKEN" -H "Accept: application/json" \
      "${COOLIFY_URL}/api/v1${path}"
  else
    curl -fsS -X "$method" -H "Authorization: Bearer $COOLIFY_TOKEN" \
      -H "Accept: application/json" -H "Content-Type: application/json" \
      --data @- "${COOLIFY_URL}/api/v1${path}"
  fi
}

# ---- GitHub helpers ---------------------------------------------------------
gh_repo_json() { gh api "repos/$1" 2>/dev/null; }

pat_can_register() { # $1 = OWNER/REPO ; uses RUNNER_PAT, prints http code
  curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $RUNNER_PAT" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$1/actions/runners/registration-token"
}

list_repo_runners() { # $1 = OWNER/REPO
  gh api "repos/$1/actions/runners" \
    --jq '.runners[]? | {id,name,status,busy,labels:[.labels[].name]}' 2>/dev/null
}

# ---- compose builder --------------------------------------------------------
build_compose() { # args: repo_url labels prefix ephemeral docker  -> prints YAML
  local repo_url="$1" labels="$2" prefix="$3" ephemeral="$4" docker="$5"
  local volumes="      - runner-data:/runner" secopt=""
  if [ "$docker" = yes ]; then
    volumes=$'      - /var/run/docker.sock:/var/run/docker.sock\n      - runner-data:/runner'
    secopt=$'\n    security_opt:\n      - label:disable'
  fi
  cat <<YAML
services:
  runner:
    image: ${RUNNER_IMAGE}
    restart: always
    environment:
      REPO_URL: ${repo_url}
      RUNNER_SCOPE: repo
      LABELS: ${labels}
      RUNNER_NAME_PREFIX: ${prefix}
      RUNNER_NAME_SUFFIX: "true"
      EPHEMERAL: "${ephemeral}"
      DISABLE_AUTOMATIC_DEREGISTRATION: "false"
      ACCESS_TOKEN: \${ACCESS_TOKEN}
    volumes:
${volumes}${secopt}
volumes:
  runner-data:
YAML
}

b64() { base64 | tr -d '\n'; }   # macOS base64 -> single line

# ---- discovery --------------------------------------------------------------
discover_targets() {
  step "Discovering Coolify project / environment / server"
  local projects
  projects="$(cf GET /projects)"
  PROJECT_UUID="$(jq -r --arg n "$COOLIFY_PROJECT_NAME" \
    '.[] | select(.name==$n) | .uuid' <<<"$projects" | head -1)"
  [ -n "$PROJECT_UUID" ] || die "Coolify project '$COOLIFY_PROJECT_NAME' not found"
  ok "project '$COOLIFY_PROJECT_NAME' = $PROJECT_UUID"

  local envs
  envs="$(cf GET "/projects/${PROJECT_UUID}/environments")"
  ENV_NAME="$(jq -r --arg n "$COOLIFY_ENV_NAME" \
    '(.[]? // .) | select(.name==$n) | .name' <<<"$envs" | head -1)"
  ENV_UUID="$(jq -r --arg n "$COOLIFY_ENV_NAME" \
    '(.[]? // .) | select(.name==$n) | .uuid' <<<"$envs" | head -1)"
  [ -n "$ENV_UUID" ] || die "environment '$COOLIFY_ENV_NAME' not found in project"
  ok "environment '$ENV_NAME' = $ENV_UUID"

  local servers
  servers="$(cf GET /servers)"
  SERVER_UUID="$(jq -r --arg n "$COOLIFY_SERVER_NAME" \
    '.[] | select(.name==$n) | .uuid' <<<"$servers" | head -1)"
  [ -n "$SERVER_UUID" ] || die "server '$COOLIFY_SERVER_NAME' not found"
  ok "server '$COOLIFY_SERVER_NAME' = $SERVER_UUID"
}

# ---- commands ---------------------------------------------------------------
cmd_create() {
  local repo="${1:-}"; shift || true
  [ -n "$repo" ] || die "usage: build-local-runner.sh create OWNER/REPO [options]"
  [[ "$repo" == */* ]] || die "repo must be OWNER/REPO, got '$repo'"

  local slug labels prefix ephemeral="false" docker="no"
  slug="$(printf '%s' "${repo##*/}" | tr '[:upper:]' '[:lower:]')"
  labels="${slug},coolify,local"
  prefix="github-runner-${slug}"

  while [ $# -gt 0 ]; do
    case "$1" in
      --labels)    labels="$2"; shift 2;;
      --prefix)    prefix="$2"; shift 2;;
      --docker)    docker="yes"; shift;;
      --ephemeral) ephemeral="true"; shift;;
      *) die "unknown option: $1";;
    esac
  done

  step "Verifying GitHub repo + auth"
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated (run: gh auth login)"
  local repo_json vis
  repo_json="$(gh_repo_json "$repo")" || die "repo '$repo' not found or no access via gh"
  vis="$(jq -r '.visibility' <<<"$repo_json")"
  ok "repo $repo ($vis)"
  if [ "$vis" != "private" ]; then
    warn "repo is $vis. Self-hosted runners on public/fork-heavy repos can run"
    warn "untrusted PR code on your host. Confirm this is intended before proceeding."
    read -r -p "Continue? [y/N] " a  || true
    [[ "$a" =~ ^[Yy]$ ]] || die "aborted"
  fi

  load_runner_pat
  step "Verifying runner PAT can mint registration tokens for $repo"
  local code; code="$(pat_can_register "$repo")"
  case "$code" in
    201) ok "PAT has Administration:write on $repo";;
    403) die "PAT lacks 'Administration: Read and write' on $repo (got 403)";;
    404) die "PAT cannot see $repo (404) — check repo selection on the fine-grained PAT";;
    *)   die "unexpected GitHub response minting registration token: HTTP $code";;
  esac

  load_coolify_token
  discover_targets

  local svc_name="github-runner-${slug}"
  step "Checking for an existing Coolify service named '$svc_name'"
  if cf GET /services | jq -e --arg n "$svc_name" '.[]?|select(.name==$n)' >/dev/null 2>&1; then
    die "A Coolify service '$svc_name' already exists. Tear it down first:
    build-local-runner.sh teardown $repo"
  fi

  step "Creating Coolify Docker-Compose service '$svc_name'"
  local compose compose_b64 body created uuid
  compose="$(build_compose "https://github.com/$repo" "$labels" "$prefix" "$ephemeral" "$docker")"
  compose_b64="$(printf '%s' "$compose" | b64)"
  body="$(jq -n \
    --arg name "$svc_name" \
    --arg project "$PROJECT_UUID" \
    --arg envn "$ENV_NAME" \
    --arg envu "$ENV_UUID" \
    --arg server "$SERVER_UUID" \
    --arg compose "$compose_b64" \
    '{name:$name, project_uuid:$project, environment_name:$envn,
      environment_uuid:$envu, server_uuid:$server, instant_deploy:false,
      docker_compose_raw:$compose}')"
  created="$(printf '%s' "$body" | cf POST /services)"
  uuid="$(jq -r '.uuid // .data.uuid // empty' <<<"$created")"
  [ -n "$uuid" ] || die "service create did not return a uuid. Response: $created"
  ok "service created: $uuid"

  step "Injecting ACCESS_TOKEN (stored as a Coolify secret, shown once)"
  jq -n --arg v "$RUNNER_PAT" \
    '{data:[{key:"ACCESS_TOKEN", value:$v, is_literal:true, is_shown_once:true}]}' \
    | cf PATCH "/services/${uuid}/envs/bulk" >/dev/null
  ok "ACCESS_TOKEN set"
  unset RUNNER_PAT

  step "Starting the runner service"
  cf GET "/services/${uuid}/start" >/dev/null
  ok "start requested"

  step "Waiting for the runner to come online on GitHub (timeout ${POLL_TIMEOUT_SECS}s)"
  local deadline=$(( $(date +%s) + POLL_TIMEOUT_SECS )) online=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    online="$(gh api "repos/$repo/actions/runners" 2>/dev/null \
      | jq --arg p "$prefix" \
      '[.runners[]? | select(.name|startswith($p)) | select(.status=="online")] | length' 2>/dev/null || echo 0)"
    [ "${online:-0}" -ge 1 ] && break
    printf '  %s…waiting%s\r' "$c_dim" "$c_rst" >&2
    sleep 6
  done

  echo >&2
  if [ "${online:-0}" -ge 1 ]; then
    ok "runner is ONLINE"
  else
    warn "runner not online yet. Check Coolify service logs ('$svc_name') and re-run:"
    warn "  build-local-runner.sh verify $repo"
  fi

  step "Summary"
  list_repo_runners "$repo" | jq -c . >&2 || true
  cat >&2 <<EOF

  Runner service : ${svc_name}  (Coolify uuid ${uuid})
  Labels         : self-hosted, Linux, X64, ${labels//,/, }
  Docker socket  : ${docker}$( [ "$docker" = no ] && echo '   (re-create with --docker if CI builds/runs containers)')

  Wire a workflow job to it:
      runs-on: [self-hosted, Linux, X64, ${slug}, coolify]

  Verify later : build-local-runner.sh verify   $repo
  Tear down    : build-local-runner.sh teardown $repo
EOF
}

cmd_verify() {
  local repo="${1:-}"
  [ -n "$repo" ] || die "usage: build-local-runner.sh verify OWNER/REPO"
  step "GitHub runners on $repo"
  local out; out="$(list_repo_runners "$repo")"
  if [ -z "$out" ]; then
    warn "no runners registered on $repo"
  else
    printf '%s\n' "$out" | jq -c . >&2
    printf '%s\n' "$out" | jq -e 'select(.status=="online")' >/dev/null 2>&1 \
      && ok "at least one runner online" || warn "no runner currently online"
  fi
}

cmd_teardown() {
  local repo="${1:-}"
  [ -n "$repo" ] || die "usage: build-local-runner.sh teardown OWNER/REPO"
  local slug svc_name; slug="$(printf '%s' "${repo##*/}" | tr '[:upper:]' '[:lower:]')"
  svc_name="github-runner-${slug}"

  load_coolify_token
  step "Deleting Coolify service '$svc_name'"
  local uuid
  uuid="$(cf GET /services | jq -r --arg n "$svc_name" '.[]?|select(.name==$n)|.uuid' | head -1)"
  if [ -n "$uuid" ]; then
    cf DELETE "/services/${uuid}" >/dev/null && ok "deleted Coolify service $uuid"
  else
    warn "no Coolify service named '$svc_name' found"
  fi

  step "Deregistering GitHub runners with prefix github-runner-${slug}"
  local ids
  ids="$(gh api "repos/$repo/actions/runners" 2>/dev/null \
    | jq -r --arg p "github-runner-${slug}" \
    '.runners[]? | select(.name|startswith($p)) | .id' 2>/dev/null || true)"
  if [ -z "$ids" ]; then
    ok "no stale GitHub runners to remove"
  else
    while read -r id; do
      [ -n "$id" ] || continue
      gh api -X DELETE "repos/$repo/actions/runners/$id" >/dev/null 2>&1 \
        && ok "removed GitHub runner $id" || warn "could not remove runner $id"
    done <<<"$ids"
  fi
}

# ---- dispatch ---------------------------------------------------------------
main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    create)   cmd_create "$@";;
    verify)   cmd_verify "$@";;
    teardown) cmd_teardown "$@";;
    ""|-h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//' >&2;;
    *) die "unknown command '$cmd' (expected: create | verify | teardown)";;
  esac
}
main "$@"
