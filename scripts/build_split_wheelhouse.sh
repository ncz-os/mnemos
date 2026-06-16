#!/usr/bin/env bash
# Build a self-contained wheelhouse for the split-install matrix test.
#
# Produces, into $WH:
#   * the four first-party wheels (mnemos-core + the 3 add-ons), and
#   * the full third-party dependency closure (so the matrix can install
#     each subset offline with `pip install --no-index --find-links=$WH`).
#
# Usage: build_split_wheelhouse.sh [WHEELHOUSE_DIR] [ADDON_BASE]
#   WHEELHOUSE_DIR default /tmp/mnemos-wheelhouse
#   ADDON_BASE     default /tmp  (expects ncz-pantheon, ncz-knemon, ncz-graeae)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WH="${1:-/tmp/mnemos-wheelhouse}"
ADDON_BASE="${2:-/tmp}"
PY="${PYTHON:-python3}"

PANTHEON="$ADDON_BASE/ncz-pantheon"
KNEMON="$ADDON_BASE/ncz-knemon"
GRAEAE="$ADDON_BASE/ncz-graeae"
for d in "$PANTHEON" "$KNEMON" "$GRAEAE"; do
  [ -d "$d" ] || { echo "ERROR: add-on repo missing: $d" >&2; exit 2; }
done

rm -rf "$WH"; mkdir -p "$WH"

echo "[wheelhouse] building first-party wheels"
if command -v uv >/dev/null 2>&1; then
  uv build --wheel -o "$WH" "$REPO_ROOT"  >/dev/null
  uv build --wheel -o "$WH" "$PANTHEON"   >/dev/null
  uv build --wheel -o "$WH" "$KNEMON"     >/dev/null
  uv build --wheel -o "$WH" "$GRAEAE"     >/dev/null
else
  "$PY" -m pip wheel --no-deps -w "$WH" "$REPO_ROOT" "$PANTHEON" "$KNEMON" "$GRAEAE" >/dev/null
fi

echo "[wheelhouse] resolving full dependency closure from first-party wheel metadata"
# Authoritative closure: let pip resolve the four first-party wheels' OWN declared
# dependencies (pyproject [project.dependencies], not just requirements.txt) plus
# oracledb (added in the deploy image). `pip wheel` BUILDS any sdist-only deps into
# wheels locally using build backends fetched at build time, producing an all-wheel
# wheelhouse so the matrix installs fully offline (`--no-index`) without needing
# build backends (e.g. scikit-build-core) present. find-links lets the first-party
# inter-deps (pantheon -> graeae/knemon/core) resolve among the wheels just built.
"$PY" -m pip wheel --find-links "$WH" -w "$WH" \
  mnemos-core mnemos-pantheon mnemos-knemon mnemos-graeae "oracledb>=4.0.1" >/dev/null
echo "[wheelhouse] done: $(ls -1 "$WH" | wc -l | tr -d ' ') artifacts in $WH"
