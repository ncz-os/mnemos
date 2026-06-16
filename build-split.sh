#!/usr/bin/env bash
set -euo pipefail

# Build the MNEMOS split umbrella image from four package source trees.
#
# The generated build context contains:
#   ./core     this mnemos-core repository at the selected build ref
#   ./pantheon gitlab.com/ncz-os/pantheon.git at a resolved full commit
#   ./knemon   gitlab.com/ncz-os/knemon.git at a resolved full commit
#   ./graeae   gitlab.com/ncz-os/graeae.git at a resolved full commit
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
context under .split-build/<core-short-sha>/, unless BUILD_TMP or
MNEMOS_SPLIT_CONTEXT_DIR is set.

Environment:
  ADDON_REMOTE_BASE          GitLab namespace base. Default: https://gitlab.com/ncz-os
  ADDON_REF                  Default add-on branch/ref to resolve. Default: main
  ADDON_PANTHEON_REF         Pantheon branch/tag/SHA override. Default: ADDON_REF
  ADDON_KNEMON_REF           Knemon branch/tag/SHA override. Default: ADDON_REF
  ADDON_GRAEAE_REF           Graeae branch/tag/SHA override. Default: ADDON_REF
  BUILD_TMP                  Explicit temp root allowed for build contexts.
  MNEMOS_SPLIT_CONTEXT_DIR   Build context directory. Default: <root>/<core-short-sha>
  MNEMOS_SMOKE_WITH_DB       Run Oracle DB smoke with explicit DB env values. Default: 0
  IMAGE_TAG                  Output image tag. Default: mnemos-os:split-<core-short-sha>
  PODMAN                     Podman-compatible build command. Default: podman
  VERIFY_IMPORTS             Run deploy gate after build. Default: 1

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
ADDON_PANTHEON_REF=${ADDON_PANTHEON_REF:-$ADDON_REF}
ADDON_KNEMON_REF=${ADDON_KNEMON_REF:-$ADDON_REF}
ADDON_GRAEAE_REF=${ADDON_GRAEAE_REF:-$ADDON_REF}
PODMAN=${PODMAN:-podman}
VERIFY_IMPORTS=${VERIFY_IMPORTS:-1}

if ! command -v realpath >/dev/null 2>&1 || ! realpath -m / >/dev/null 2>&1; then
  echo "realpath -m is required" >&2
  exit 127
fi

reject_symlink_components() {
  local target=$1
  local current="/"
  local rest="${target#/}"
  local component

  while [[ -n "$rest" ]]; do
    component=${rest%%/*}
    if [[ "$component" == "$rest" ]]; then
      rest=""
    else
      rest=${rest#*/}
    fi

    case "$component" in
      ""|".")
        continue
        ;;
      "..")
        current=$(dirname "$current")
        continue
        ;;
    esac

    if [[ "$current" == "/" ]]; then
      current="/$component"
    else
      current="$current/$component"
    fi
    if [[ -L "$current" ]]; then
      echo "Refusing path with symlink component: $current" >&2
      exit 2
    fi
  done
}

safe_resolved_path() {
  local target=$1
  local target_real
  local home_real=""
  if [[ -n "${HOME:-}" && -d "$HOME" ]]; then
    home_real=$(cd "$HOME" && pwd -P)
  fi

  if [[ -z "$target" || "$target" != /* ]]; then
    echo "Refusing unsafe path (not absolute): ${target:-<empty>}" >&2
    exit 2
  fi

  reject_symlink_components "$target"
  target_real=$(realpath -m "$target")
  reject_symlink_components "$target_real"

  if [[ "$target_real" == "/" || "$target_real" == "$REPO_ROOT" || "$target_real" == "$ALLOWED_CONTEXT_ROOT" ]]; then
    echo "Refusing unsafe path: $target_real" >&2
    exit 2
  fi
  if [[ -n "$home_real" && "$target_real" == "$home_real" ]]; then
    echo "Refusing unsafe path: $target_real" >&2
    exit 2
  fi
  case "$target_real/" in
    "$ALLOWED_CONTEXT_ROOT"/*) ;;
    *)
      echo "Refusing path outside allowed build root $ALLOWED_CONTEXT_ROOT: $target_real" >&2
      exit 2
      ;;
  esac

  printf '%s\n' "$target_real"
}

require_safe_path() {
  safe_resolved_path "$1" >/dev/null
}

safe_rm_rf() {
  local target=$1
  local target_real
  target_real=$(safe_resolved_path "$target")
  rm -rf "$target_real"
}

DEFAULT_CONTEXT_ROOT="$REPO_ROOT/.split-build"
if [[ -n "${BUILD_TMP:-}" ]]; then
  BUILD_TMP_ABS=$BUILD_TMP
  if [[ "$BUILD_TMP_ABS" != /* ]]; then
    BUILD_TMP_ABS="$(pwd -P)/$BUILD_TMP_ABS"
  fi
  reject_symlink_components "$BUILD_TMP_ABS"
  mkdir -p "$BUILD_TMP_ABS"
  reject_symlink_components "$BUILD_TMP_ABS"
  DEFAULT_CONTEXT_ROOT=$(realpath -m "$BUILD_TMP_ABS")
else
  reject_symlink_components "$DEFAULT_CONTEXT_ROOT"
  mkdir -p "$DEFAULT_CONTEXT_ROOT"
  reject_symlink_components "$DEFAULT_CONTEXT_ROOT"
  DEFAULT_CONTEXT_ROOT=$(realpath -m "$DEFAULT_CONTEXT_ROOT")
fi

IMAGE_TAG=${IMAGE_TAG:-mnemos-os:split-${CORE_SHORT}}
CONTEXT_DIR=${MNEMOS_SPLIT_CONTEXT_DIR:-"$DEFAULT_CONTEXT_ROOT/${CORE_SHORT}"}
if [[ "$CONTEXT_DIR" != /* ]]; then
  CONTEXT_DIR="$REPO_ROOT/$CONTEXT_DIR"
fi
ALLOWED_CONTEXT_ROOT="$DEFAULT_CONTEXT_ROOT"
CONTEXT_DIR=$(safe_resolved_path "$CONTEXT_DIR")

redact_url() {
  printf '%s\n' "$1" | sed -E 's#(https?://)[^/@]+@#\1***@#; s#(ssh://)[^/@]+@#\1***@#'
}

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
echo "[split] add-ons: $(redact_url "$ADDON_REMOTE_BASE")/{pantheon,knemon,graeae}.git"
echo "[split] refs:    pantheon=$ADDON_PANTHEON_REF knemon=$ADDON_KNEMON_REF graeae=$ADDON_GRAEAE_REF"
echo "[split] image:   $IMAGE_TAG"

safe_rm_rf "$CONTEXT_DIR"
mkdir -p "$CONTEXT_DIR"

cp "$REPO_ROOT/Dockerfile.split" "$CONTEXT_DIR/Dockerfile.split"

cat >"$CONTEXT_DIR/.dockerignore" <<'EOF'
**/.git
**/.github
**/.venv
**/.env
**/.env.*
!**/.env.example
split-provenance.env
.split-build/
**/.split-build
**/.split-build/*/
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
safe_rm_rf "$CONTEXT_DIR/core/.git"

is_full_sha() {
  [[ "$1" =~ ^[0-9a-fA-F]{40}$ ]]
}

resolve_addon_ref() {
  local dest=$1
  local ref=$2
  local fetch_log=$3
  local ls_log
  local resolved_ref=""
  local resolved_sha=""
  local candidate_sha
  local candidate_ref
  local head_ref=""
  local tag_ref=""
  local tag_peeled_ref=""
  local -a patterns=()

  if is_full_sha "$ref"; then
    printf '%s\n' "$ref"
    return 0
  fi

  if [[ "$ref" == refs/heads/* ]]; then
    head_ref=$ref
  elif [[ "$ref" == refs/tags/* ]]; then
    tag_ref=$ref
    tag_peeled_ref="${ref}^{}"
  elif [[ "$ref" == refs/* ]]; then
    echo "Unsupported add-on ref '$ref'; use a full SHA, refs/heads/<name>, or refs/tags/<name>" >&2
    return 1
  else
    head_ref="refs/heads/$ref"
    tag_ref="refs/tags/$ref"
    tag_peeled_ref="refs/tags/$ref^{}"
  fi

  ls_log=$(mktemp "$CONTEXT_DIR/git-ls-remote.XXXXXX")
  [[ -n "$head_ref" ]] && patterns+=("$head_ref")
  [[ -n "$tag_ref" ]] && patterns+=("$tag_ref")
  [[ -n "$tag_peeled_ref" ]] && patterns+=("$tag_peeled_ref")

  if ! git -C "$dest" ls-remote origin "${patterns[@]}" >"$ls_log" 2>"$fetch_log"; then
    redact_url "$(cat "$fetch_log")" >&2
    rm -f "$ls_log"
    return 1
  fi

  while read -r candidate_sha candidate_ref; do
    case "$candidate_ref" in
      "$head_ref")
        if [[ -n "$resolved_ref" && "$resolved_ref" != "$head_ref" ]]; then
          echo "Ambiguous add-on ref '$ref'; use refs/heads/... or refs/tags/..." >&2
          rm -f "$ls_log"
          return 1
        fi
        resolved_ref=$candidate_ref
        resolved_sha=$candidate_sha
        ;;
      "$tag_peeled_ref")
        if [[ -n "$resolved_ref" && "$resolved_ref" != "$tag_ref" ]]; then
          echo "Ambiguous add-on ref '$ref'; use refs/heads/... or refs/tags/..." >&2
          rm -f "$ls_log"
          return 1
        fi
        resolved_ref=$tag_ref
        resolved_sha=$candidate_sha
        ;;
      "$tag_ref")
        if [[ -n "$resolved_ref" && "$resolved_ref" != "$tag_ref" ]]; then
          echo "Ambiguous add-on ref '$ref'; use refs/heads/... or refs/tags/..." >&2
          rm -f "$ls_log"
          return 1
        fi
        resolved_ref=$candidate_ref
        if [[ -z "$resolved_sha" ]]; then
          resolved_sha=$candidate_sha
        fi
        ;;
    esac
  done <"$ls_log"
  rm -f "$ls_log"

  if [[ -z "$resolved_sha" ]]; then
    echo "Could not resolve add-on ref '$ref' to an exact remote head or tag" >&2
    return 1
  fi
  printf '%s\n' "$resolved_sha"
}

fetch_resolved_commit() {
  local dest=$1
  local commit=$2
  local fetch_log=$3

  if git -C "$dest" fetch -q --depth 1 origin "$commit" >"$fetch_log" 2>&1; then
    return 0
  fi

  redact_url "$(cat "$fetch_log")" >&2
  echo "[split] by-SHA fetch failed; falling back to full add-on fetch" >&2
  if ! git -C "$dest" fetch -q origin \
      '+refs/heads/*:refs/remotes/origin/*' \
      '+refs/tags/*:refs/tags/*' >"$fetch_log" 2>&1; then
    redact_url "$(cat "$fetch_log")" >&2
    return 1
  fi
  git -C "$dest" rev-parse --verify "$commit^{commit}" >/dev/null
}

clone_addon() {
  local name=$1
  local repo=$2
  local ref=$3
  local dest="$CONTEXT_DIR/$name"
  local remote="${ADDON_REMOTE_BASE}/${repo}.git"
  local commit
  local fetch_log

  echo "[split] resolving $name from $(redact_url "$remote") @ $ref" >&2
  git init "$dest" >/dev/null
  git -C "$dest" remote add origin "$remote"
  fetch_log=$(mktemp "$CONTEXT_DIR/git-fetch.XXXXXX")
  if ! commit=$(resolve_addon_ref "$dest" "$ref" "$fetch_log"); then
    rm -f "$fetch_log"
    return 1
  fi
  if ! fetch_resolved_commit "$dest" "$commit" "$fetch_log"; then
    rm -f "$fetch_log"
    return 1
  fi
  commit=$(git -C "$dest" rev-parse --verify "$commit^{commit}")
  rm -f "$fetch_log"
  git -C "$dest" -c advice.detachedHead=false checkout --detach "$commit" >/dev/null
  git -C "$dest" remote remove origin >/dev/null 2>&1 || true
  safe_rm_rf "$dest/.git"
  printf '%s\n' "$commit"
}

PANTHEON_COMMIT=$(clone_addon pantheon pantheon "$ADDON_PANTHEON_REF")
KNEMON_COMMIT=$(clone_addon knemon knemon "$ADDON_KNEMON_REF")
GRAEAE_COMMIT=$(clone_addon graeae graeae "$ADDON_GRAEAE_REF")

echo "[split] resolved add-on commits:"
echo "        pantheon $PANTHEON_COMMIT"
echo "        knemon   $KNEMON_COMMIT"
echo "        graeae   $GRAEAE_COMMIT"

audit_namespace_collisions() {
  local tmp
  local pkg
  local root
  local file
  local rel
  local duplicates

  tmp=$(mktemp "$CONTEXT_DIR/mnemos-files.XXXXXX")
  for pkg in core pantheon knemon graeae; do
    root="$CONTEXT_DIR/$pkg/mnemos"
    [[ -d "$root" ]] || continue
    for rel in "__init__.py" "domain/__init__.py" "api/__init__.py" "api/routes/__init__.py"; do
      if [[ -e "$root/$rel" ]]; then
        echo "[split] forbidden PEP420 namespace marker: $pkg/mnemos/$rel" >&2
        rm -f "$tmp"
        exit 1
      fi
    done
    while IFS= read -r -d '' file; do
      rel=${file#"$CONTEXT_DIR/$pkg/"}
      printf '%s\t%s\n' "$rel" "$pkg" >>"$tmp"
    done < <(find "$root" \( -type f -o -type l \) -print0)
  done

  duplicates=$(cut -f1 "$tmp" | sort | uniq -d || true)
  if [[ -n "$duplicates" ]]; then
    echo "[split] duplicate mnemos namespace files detected:" >&2
    while IFS= read -r rel; do
      [[ -n "$rel" ]] || continue
      grep -F "${rel}"$'\t' "$tmp" >&2
    done <<<"$duplicates"
    rm -f "$tmp"
    exit 1
  fi
  rm -f "$tmp"
}

audit_namespace_collisions

{
  echo "# Source-able KEY=VALUE file; parse with: . split-provenance.env"
  printf 'MNEMOS_CORE_REF=%q\n' "$CORE_COMMIT"
  printf 'MNEMOS_PANTHEON_REF=%q\n' "$PANTHEON_COMMIT"
  printf 'MNEMOS_KNEMON_REF=%q\n' "$KNEMON_COMMIT"
  printf 'MNEMOS_GRAEAE_REF=%q\n' "$GRAEAE_COMMIT"
  printf 'ADDON_PANTHEON_REPO=%q\n' "ncz-pantheon"
  printf 'ADDON_KNEMON_REPO=%q\n' "ncz-knemon"
  printf 'ADDON_GRAEAE_REPO=%q\n' "ncz-graeae"
} >"$CONTEXT_DIR/split-provenance.env"

(
  cd "$CONTEXT_DIR"
  "$PODMAN" build \
    --build-arg "MNEMOS_CORE_REF=$CORE_COMMIT" \
    --build-arg "MNEMOS_PANTHEON_REF=$PANTHEON_COMMIT" \
    --build-arg "MNEMOS_KNEMON_REF=$KNEMON_COMMIT" \
    --build-arg "MNEMOS_GRAEAE_REF=$GRAEAE_COMMIT" \
    -f Dockerfile.split \
    -t "$IMAGE_TAG" \
    .
)

CLEANUP_CONTAINERS=()
cleanup_containers() {
  local cname
  for cname in "${CLEANUP_CONTAINERS[@]:-}"; do
    "$PODMAN" rm -f "$cname" >/dev/null 2>&1 || true
  done
}
trap cleanup_containers EXIT

show_container_failure() {
  local cname=$1
  echo "[split] container logs for $cname:" >&2
  "$PODMAN" logs "$cname" >&2 || true
}

wait_for_health() {
  local cname=$1
  local i
  for i in {1..60}; do
    if "$PODMAN" exec "$cname" curl -fsS --max-time 2 http://127.0.0.1:5002/health >/dev/null 2>&1; then
      return 0
    fi
    if [[ "$("$PODMAN" inspect -f '{{.State.Running}}' "$cname" 2>/dev/null || true)" != "true" ]]; then
      show_container_failure "$cname"
      return 1
    fi
    sleep 1
  done
  show_container_failure "$cname"
  echo "[split] timed out waiting for $cname /health" >&2
  return 1
}

JSON_VALIDATOR_CONTAINER=""
JSON_VALIDATOR_KIND=""

ensure_json_validator() {
  local cname=$1
  if [[ "$JSON_VALIDATOR_CONTAINER" == "$cname" && -n "$JSON_VALIDATOR_KIND" ]]; then
    return 0
  fi
  if "$PODMAN" exec "$cname" sh -c 'command -v python3 >/dev/null 2>&1 && python3 -m json.tool --help >/dev/null 2>&1'; then
    JSON_VALIDATOR_CONTAINER=$cname
    JSON_VALIDATOR_KIND=python3
    return 0
  fi
  if "$PODMAN" exec "$cname" sh -c 'command -v jq >/dev/null 2>&1'; then
    JSON_VALIDATOR_CONTAINER=$cname
    JSON_VALIDATOR_KIND=jq
    return 0
  fi
  echo "[split] no JSON validator found in $cname (tried python3 -m json.tool, jq)" >&2
  return 127
}

validate_json_file() {
  local cname=$1
  local json_file=$2
  local stderr_file=$3
  ensure_json_validator "$cname" || return $?
  case "$JSON_VALIDATOR_KIND" in
    python3)
      "$PODMAN" exec "$cname" python3 -m json.tool "$json_file" >/dev/null 2>"$stderr_file"
      ;;
    jq)
      "$PODMAN" exec "$cname" jq empty "$json_file" >/dev/null 2>"$stderr_file"
      ;;
    *)
      echo "[split] unknown JSON validator: $JSON_VALIDATOR_KIND" >&2
      return 127
      ;;
  esac
}

show_raw_body_snippet() {
  local cname=$1
  local json_file=$2
  echo "[split] raw response body snippet ($json_file, first 2000 bytes):" >&2
  "$PODMAN" exec "$cname" sh -c 'head -c 2000 "$1"' sh "$json_file" >&2 || true
  echo >&2
}

report_json_parse_failure() {
  local cname=$1
  local label=$2
  local json_file=$3
  local stderr_file=$4
  echo "[split] $label response was not valid JSON (validator: $JSON_VALIDATOR_KIND)" >&2
  echo "[split] JSON validator stderr:" >&2
  if [[ -s "$stderr_file" ]]; then
    sed 's/^/[split]   /' "$stderr_file" >&2
  else
    echo "[split]   <empty>" >&2
  fi
  show_raw_body_snippet "$cname" "$json_file"
  show_container_failure "$cname"
}

expect_json_status() {
  local cname=$1
  local method=$2
  local path=$3
  local expected=$4
  local outfile=$5
  local json_stderr
  local rc
  shift 5
  local status
  status=$("$PODMAN" exec "$cname" curl -sS --max-time 10 -o "$outfile" -w '%{http_code}' -X "$method" "$@" "http://127.0.0.1:5002$path")
  if [[ "$status" != "$expected" ]]; then
    echo "[split] $method $path returned HTTP $status, expected $expected" >&2
    "$PODMAN" exec "$cname" cat "$outfile" >&2 || true
    show_container_failure "$cname"
    return 1
  fi
  json_stderr=$(mktemp)
  if validate_json_file "$cname" "$outfile" "$json_stderr"; then
    rm -f "$json_stderr"
    return 0
  else
    rc=$?
    if [[ "$rc" -eq 127 ]]; then
      rm -f "$json_stderr"
      show_container_failure "$cname"
      return 1
    fi
    report_json_parse_failure "$cname" "$method $path" "$outfile" "$json_stderr"
    rm -f "$json_stderr"
    return 1
  fi
}

verify_module_imports() {
  echo "[split] verifying wheel metadata"
  "$PODMAN" run --rm "$IMAGE_TAG" python -m pip check

  echo "[split] verifying exact split module imports"
  "$PODMAN" run --rm "$IMAGE_TAG" python -c 'import importlib; import importlib.metadata as metadata; import pkgutil; core_root = importlib.import_module("mnemos.core"); modules = ["mnemos", "mnemos.api.main", "mnemos.core"]; modules.extend(module.name for module in pkgutil.walk_packages(core_root.__path__, "mnemos.core.")); optional_modules = {"mnemos-pantheon": ("mnemos.api.routes.pantheon", "mnemos.domain.pantheon.catalog"), "mnemos-knemon": ("mnemos.api.routes.ledger", "mnemos.api.routes.knemon_dashboard", "mnemos.api.routes.knemon_router", "mnemos.api.routes.knemon_utilization", "mnemos.domain.knemon.router"), "mnemos-graeae": ("mnemos.api.routes.providers", "mnemos.api.routes.consultations", "mnemos.domain.graeae.engine")}; installed = []; missing = [];
for dist, dist_modules in optional_modules.items():
    try:
        metadata.version(dist)
    except metadata.PackageNotFoundError:
        missing.append(dist)
    else:
        installed.append(dist)
        modules.extend(dist_modules)
[importlib.import_module(module) for module in modules]; print(f"split module import check ok (optional installed={installed}, skipped={missing})")'
}

run_universal_http_smoke() {
  local cname="mnemos-split-smoke-${CORE_SHORT}-$$"
  "$PODMAN" rm -f "$cname" >/dev/null 2>&1 || true
  CLEANUP_CONTAINERS+=("$cname")

  echo "[split] booting HTTP smoke container with sqlite backend"
  "$PODMAN" run -d \
    --name "$cname" \
    -e MNEMOS_AUTH_ENABLED=false \
    -e MNEMOS_PERSISTENCE_BACKEND=sqlite \
    -e MNEMOS_DATABASE_DSN=sqlite:////tmp/mnemos-split-smoke.db \
    "$IMAGE_TAG" >/dev/null

  wait_for_health "$cname"
  expect_json_status "$cname" GET /health 200 /tmp/health.json
  expect_json_status "$cname" GET /openapi.json 200 /tmp/openapi.json
  expect_json_status "$cname" GET /v1/models 200 /tmp/models.json
  expect_json_status "$cname" POST /v1/chat/completions 400 /tmp/chat.json \
    -H 'Content-Type: application/json' \
    -d '{"model":"gpt-4o","messages":[],"mnemos_inject_memory":false}'

  "$PODMAN" rm -f "$cname" >/dev/null
}

run_oracle_memory_smoke_if_configured() {
  if [[ "${MNEMOS_SMOKE_WITH_DB:-0}" != "1" ]]; then
    echo "[split] Oracle CRUD/search smoke disabled; set MNEMOS_SMOKE_WITH_DB=1 to pass DB env"
    return 0
  fi

  local candidate="${MNEMOS_DATABASE_DSN:-${MNEMOS_DATABASE_URL:-${ORACLE_DSN:-}}}"
  if [[ -z "$candidate" ]]; then
    echo "[split] Oracle DSN not present; skipping Oracle CRUD/search smoke"
    return 0
  fi
  if [[ -z "${ORACLE_DSN:-}" && "$candidate" != oracle:* && "$candidate" != oracle+oracledb:* ]]; then
    echo "[split] Oracle DSN not present; skipping Oracle CRUD/search smoke"
    return 0
  fi

  local cname="mnemos-split-oracle-smoke-${CORE_SHORT}-$$"
  local marker="split-deploy-gate-${CORE_SHORT}-$(date +%s)-$$"
  local status
  local memory_id
  local json_stderr
  local rc
  local -a db_env=(
    -e MNEMOS_AUTH_ENABLED=false
    -e MNEMOS_PERSISTENCE_BACKEND=oracle
  )
  local -a db_mounts=()

  if [[ -n "${MNEMOS_DATABASE_DSN:-}" ]]; then
    db_env+=(-e "MNEMOS_DATABASE_DSN=$MNEMOS_DATABASE_DSN")
  fi
  if [[ -n "${MNEMOS_DATABASE_URL:-}" ]]; then
    db_env+=(-e "MNEMOS_DATABASE_URL=$MNEMOS_DATABASE_URL")
  fi
  if [[ -n "${ORACLE_DSN:-}" ]]; then
    db_env+=(-e "ORACLE_DSN=$ORACLE_DSN")
  fi
  if [[ -n "${TNS_ADMIN:-}" ]]; then
    db_env+=(-e "TNS_ADMIN=$TNS_ADMIN")
    if [[ -e "$TNS_ADMIN" ]]; then
      db_mounts+=(-v "$TNS_ADMIN:$TNS_ADMIN:ro")
    else
      echo "[split] TNS_ADMIN=$TNS_ADMIN does not exist on host; wallet-based DSNs are unsupported in this smoke mode"
    fi
  fi
  if [[ -n "${NLS_LANG:-}" ]]; then
    db_env+=(-e "NLS_LANG=$NLS_LANG")
  fi

  "$PODMAN" rm -f "$cname" >/dev/null 2>&1 || true
  CLEANUP_CONTAINERS+=("$cname")

  echo "[split] booting Oracle CRUD/search smoke container"
  "$PODMAN" run -d \
    --name "$cname" \
    "${db_env[@]}" \
    "${db_mounts[@]}" \
    "$IMAGE_TAG" >/dev/null

  wait_for_health "$cname"

  "$PODMAN" exec "$cname" python3 -c 'import json, sys; json.dump({"content": sys.argv[1], "category": "deploy-smoke", "namespace": "default", "metadata": {"probe": "split-deploy-gate"}}, open("/tmp/memory-create-request.json", "w"))' "$marker"
  status=$("$PODMAN" exec "$cname" curl -sS --max-time 30 -o /tmp/memory-create.json -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    --data-binary @/tmp/memory-create-request.json \
    http://127.0.0.1:5002/memories)
  if [[ "$status" != "201" && "$status" != "200" ]]; then
    echo "[split] Oracle memory create returned HTTP $status" >&2
    "$PODMAN" exec "$cname" cat /tmp/memory-create.json >&2 || true
    show_container_failure "$cname"
    return 1
  fi
  json_stderr=$(mktemp)
  if validate_json_file "$cname" /tmp/memory-create.json "$json_stderr"; then
    rm -f "$json_stderr"
  else
    rc=$?
    if [[ "$rc" -eq 127 ]]; then
      rm -f "$json_stderr"
      show_container_failure "$cname"
      return 1
    fi
    report_json_parse_failure "$cname" "Oracle memory create" /tmp/memory-create.json "$json_stderr"
    rm -f "$json_stderr"
    return 1
  fi
  memory_id=$("$PODMAN" exec "$cname" python -c 'import json; print(json.load(open("/tmp/memory-create.json"))["id"])')

  "$PODMAN" exec "$cname" python3 -c 'import json, sys; json.dump({"query": sys.argv[1], "limit": 5, "semantic": False, "namespace": "default"}, open("/tmp/memory-search-request.json", "w"))' "$marker"
  expect_json_status "$cname" POST /memories/search 200 /tmp/memory-search.json \
    -H 'Content-Type: application/json' \
    --data-binary @/tmp/memory-search-request.json
  "$PODMAN" exec "$cname" python -c 'import json, sys; payload=json.load(open("/tmp/memory-search.json")); target=sys.argv[1]; ids=[row.get("id") for row in payload.get("memories", [])]; raise SystemExit(0 if target in ids else 1)' "$memory_id"

  status=$("$PODMAN" exec "$cname" curl -sS --max-time 10 -o /tmp/memory-delete.txt -w '%{http_code}' -X DELETE "http://127.0.0.1:5002/memories/$memory_id")
  if [[ "$status" != "204" ]]; then
    echo "[split] Oracle memory delete returned HTTP $status" >&2
    "$PODMAN" exec "$cname" cat /tmp/memory-delete.txt >&2 || true
    show_container_failure "$cname"
    return 1
  fi

  "$PODMAN" rm -f "$cname" >/dev/null
}

if [[ "$VERIFY_IMPORTS" == "1" ]]; then
  verify_module_imports
  run_universal_http_smoke
  run_oracle_memory_smoke_if_configured
fi

echo "[split] built $IMAGE_TAG"
echo "[split] deploy by immutable image digest after push, for example: <registry>/<repo>@sha256:<digest>"
