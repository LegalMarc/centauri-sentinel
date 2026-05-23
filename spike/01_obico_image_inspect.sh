#!/usr/bin/env bash
# Inspect candidate Obico ML container images for ARM64 support.
#
# Usage:
#   ./01_obico_image_inspect.sh                # uses default candidates
#   ./01_obico_image_inspect.sh <image:tag>    # inspect a specific image
#
# Requires: docker (with buildx), jq.

set -euo pipefail

CANDIDATES=(
  "thespaghettidetective/ml_api:latest"
  "ghcr.io/thespaghettidetective/obico-ml-api:latest"
  "obico/ml-api:latest"
)

if [[ $# -ge 1 ]]; then
  CANDIDATES=("$@")
fi

inspect() {
  local image="$1"
  echo "--- $image ---"
  if ! docker buildx imagetools inspect "$image" --raw 2>/dev/null \
       | jq -r '.manifests[]? | "\(.platform.os)/\(.platform.architecture)"' 2>/dev/null; then
    # Single-arch images don't have a .manifests array.
    docker buildx imagetools inspect "$image" 2>&1 \
      | grep -iE 'platform|mediatype|digest' | head -20 || true
  fi
  echo
}

for c in "${CANDIDATES[@]}"; do
  inspect "$c" || echo "  (failed to inspect $c — image may not exist at this name)"
done

cat <<EOF

=== VERIFIED ASSUMPTION: obico-ml image ===
Fill in for docs/verified-assumptions.md:
  image:        <chosen image:tag>
  registry:     <docker.io | ghcr.io | other>
  amd64:        <yes | no>
  arm64:        <yes | no>
  source:       'docker buildx imagetools inspect <image>' on $(date -u +%Y-%m-%dT%H:%M:%SZ)
  notes:        <any caveats: needs build-from-source for arm64, tag stability, etc.>
===
EOF
