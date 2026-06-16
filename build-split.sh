#!/usr/bin/env bash
set -euo pipefail

# Build the MNEMOS split umbrella image from four package source trees.
#
# The generated build context contains:
#   ./core     this mnemos-core repository at the selected build ref
#   ./pantheon gitlab.com/ncz-os/pantheon.git at main
#   ./knemon   gitlab.com/ncz-os/knemon.git at main
#   ./graeae   gitlab.com/ncz-os/graeae.git at main
#
# Runtime import contract, matching the old monorepo image:
#   import mnemos.core
#   import mnemos.domain.pantheon
#   import mnemos.domain.knemon
#   import mnemos.domain.graeae

usage() {
  cat <<'EOF'
Usage: ./build-split.sh [core-ref]

Builds mnemos-os:split-<core-short-sha> with Podman from a temporary split
context under .split-build/<core-short-sha>/.

Environment:
  ADDON_REMOTE_BASE          GitLab namespace base. Default: https://gitlab.com/ncz-os
  ADDON_REF                  Add-on branch/ref. Default: main
  MNEMOS_SPLIT_CONTEXT_DIR   Build context directory. Default: .split-build/<core-short-sha>
  IMAGE_TAG                  Output image tag. Default: mnemos-os:split-<core-short-sha>
  PODMAN                     Podman-compatible build command. Default: podman
  VERIFY_IMPORTS             Run import parity check after build. Default: 1

The Docker build command is run from inside the generated context:
  podman build -f Dockerfile.split -t mnemos-os:split-<ref> .
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT="$SCRIPT_DIR"

CORE_REF=${1:-${MNEMOS_CORE_REF:-HEAD}}
CORE_COMMIT=$(git -C "$REPO_ROOT" rev-parse --verify "${CORE_REF}^{commit}")
CORE_SHORT=$(git -C "$REPO_ROOT" rev-parse --short "$CORE_COMMIT")

ADDON_REMOTE_BASE=${ADDON_REMOTE_BASE:-https://gitlab.com/ncz-os}
ADDON_REMOTE_BASE=${ADDON_REMOTE_BASE%/}
ADDON_REF=${ADDON_REF:-main}
PODMAN=${PODMAN:-podman}
VERIFY_IMPORTS=${VERIFY_IMPORTS:-1}

IMAGE_TAG=${IMAGE_TAG:-mnemos-os:split-${CORE_SHORT}}
CONTEXT_DIR=${MNEMOS_SPLIT_CONTEXT_DIR:-"$REPO_ROOT/.split-build/${CORE_SHORT}"}
CONTEXT_PARENT=$(dirname "$CONTEXT_DIR")
CONTEXT_BASE=$(basename "$CONTEXT_DIR")
mkdir -p "$CONTEXT_PARENT"
CONTEXT_PARENT=$(cd "$CONTEXT_PARENT" && pwd -P)
CONTEXT_DIR="${CONTEXT_PARENT}/${CONTEXT_BASE}"

if [[ "$CONTEXT_DIR" == "$REPO_ROOT" || "$CONTEXT_DIR" == "/" || -z "$CONTEXT_DIR" ]]; then
  echo "Refusing unsafe MNEMOS_SPLIT_CONTEXT_DIR: $CONTEXT_DIR" >&2
  exit 2
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2
  exit 127
fi

if ! command -v "$PODMAN" >/dev/null 2>&1; then
  echo "$PODMAN is required; set PODMAN=/path/to/podman if needed" >&2
  exit 127
fi

echo "[split] context: $CONTEXT_DIR"
echo "[split] core:    $CORE_COMMIT"
echo "[split] add-ons: $ADDON_REMOTE_BASE/{pantheon,knemon,graeae}.git @ $ADDON_REF"
echo "[split] image:   $IMAGE_TAG"

rm -rf "$CONTEXT_DIR"
mkdir -p "$CONTEXT_DIR"

cp "$REPO_ROOT/Dockerfile.split" "$CONTEXT_DIR/Dockerfile.split"

cat >"$CONTEXT_DIR/.dockerignore" <<'EOF'
**/.git
**/.github
**/.venv
**/.env
**/.env.*
!**/.env.example
**/__pycache__
**/*.pyc
**/.pytest_cache
**/.ruff_cache
**/*.egg-info
**/build
**/dist
EOF

echo "[split] cloning core"
git clone --no-hardlinks "$REPO_ROOT" "$CONTEXT_DIR/core"
git -C "$CONTEXT_DIR/core" -c advice.detachedHead=false checkout --detach "$CORE_COMMIT"

clone_addon() {
  local name=$1
  local dest="$CONTEXT_DIR/$name"
  local remote="${ADDON_REMOTE_BASE}/${name}.git"

  echo "[split] cloning $name"
  git clone --depth 1 --branch "$ADDON_REF" "$remote" "$dest"
}

clone_addon pantheon
clone_addon knemon
clone_addon graeae

PANTHEON_COMMIT=$(git -C "$CONTEXT_DIR/pantheon" rev-parse --short HEAD)
KNEMON_COMMIT=$(git -C "$CONTEXT_DIR/knemon" rev-parse --short HEAD)
GRAEAE_COMMIT=$(git -C "$CONTEXT_DIR/graeae" rev-parse --short HEAD)

echo "[split] resolved add-on commits:"
echo "        pantheon $PANTHEON_COMMIT"
echo "        knemon   $KNEMON_COMMIT"
echo "        graeae   $GRAEAE_COMMIT"

(
  cd "$CONTEXT_DIR"
  "$PODMAN" build \
    --build-arg "MNEMOS_CORE_REF=$CORE_SHORT" \
    --build-arg "MNEMOS_PANTHEON_REF=$PANTHEON_COMMIT" \
    --build-arg "MNEMOS_KNEMON_REF=$KNEMON_COMMIT" \
    --build-arg "MNEMOS_GRAEAE_REF=$GRAEAE_COMMIT" \
    -f Dockerfile.split \
    -t "$IMAGE_TAG" \
    .
)

if [[ "$VERIFY_IMPORTS" == "1" ]]; then
  echo "[split] verifying namespace import parity"
  "$PODMAN" run --rm "$IMAGE_TAG" python -c 'import mnemos.core; import mnemos.domain.pantheon; import mnemos.domain.knemon; import mnemos.domain.graeae; print("split import check ok")'
fi

echo "[split] built $IMAGE_TAG"
