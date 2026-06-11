#!/bin/bash
set -euo pipefail

# =============================================================================
# Build_hummingbot_api_nonkyc.sh
#
# Builds a custom hummingbot-api Docker image from:
#   API:  https://github.com/trevorwilf/hummingbot-api  (branch: nonkyc)
#   Bot:  https://github.com/trevorwilf/hummingbot      (branch: nonkyc)
#
# The stock hummingbot-api image installs hummingbot from PyPI into a conda
# environment (hummingbot-api), which does NOT include the NonKYC connector.
# This script builds the API image, then overlays it by pip-installing
# hummingbot from the NonKYC fork so the connector appears in the API's
# exchange list.
#
# ---------------------------------------------------------------------------
# CONDA SHARDED-REPODATA FIX (the "database is locked" build failure)
# ---------------------------------------------------------------------------
# The API repo's Dockerfile builds a conda environment inside continuumio/
# miniconda3:latest, which now ships conda-libmamba-solver >= 26.4.0. That
# version enables CEP-16 "sharded repodata" by default, caching metadata in a
# SQLite DB (repodata_shards.db) that a reader thread and a network writer
# thread hit concurrently. On 26.4.0 (and on ZFS/NFS, where SQLite WAL locking
# is unreliable) this races and dies with:
#     sqlite3.OperationalError: database is locked
# Fixed properly in 26.4.2, but miniconda3:latest still ships 26.4.0.
#
# Since we can't edit the repo's Dockerfile upstream, this script PATCHES the
# cloned Dockerfile at build time, injecting:
#     ENV CONDA_PLUGINS_USE_SHARDED_REPODATA=0
# after every FROM, forcing the solver back to monolithic repodata.json.
#
# Usage:
#   chmod +x Build_hummingbot_api_nonkyc.sh
#   ./Build_hummingbot_api_nonkyc.sh              # build with defaults
#   ./Build_hummingbot_api_nonkyc.sh --tag v2     # custom tag suffix
#   ./Build_hummingbot_api_nonkyc.sh --no-cache   # force full Docker rebuild
#   ./Build_hummingbot_api_nonkyc.sh --dir /tmp/x # custom working directory
# =============================================================================

# ── Configuration ──────────────────────────────────────────────────────────

# API repo
HB_API_REPO="https://github.com/trevorwilf/hummingbot-api.git"
HB_API_BRANCH="nonkyc"

# Hummingbot fork (for replacing the stock pip package)
HB_REPO="https://github.com/trevorwilf/hummingbot.git"
HB_BRANCH="nonkyc"

# Conda environment paths (known from the hummingbot-api image)
CONDA_ENV="hummingbot-api"
CONDA_PYTHON="/opt/conda/envs/${CONDA_ENV}/bin/python"
CONDA_PIP="/opt/conda/envs/${CONDA_ENV}/bin/pip"

# Key files/dirs to verify the API repo is correct
VERIFY_FILE="main.py"
VERIFY_DIR="services"

# Docker image name
IMAGE_NAME="hummingbot-api-nonkyc"
IMAGE_TAG="latest"

# Working directory for the build
BUILD_DIR="${BUILD_DIR:-/tmp/hummingbot-api-nonkyc-build}"

# Docker build flags
DOCKER_BUILD_FLAGS=""

# Name of the patched Dockerfile we generate inside the build context.
PATCHED_DOCKERFILE="Dockerfile.nonkyc-base"

# ── Parse CLI args ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)      IMAGE_TAG="$2"; shift 2 ;;
    --tag=*)    IMAGE_TAG="${1#*=}"; shift ;;
    --no-cache) DOCKER_BUILD_FLAGS="--no-cache"; shift ;;
    --dir)      BUILD_DIR="$2"; shift 2 ;;
    --dir=*)    BUILD_DIR="${1#*=}"; shift ;;
    --help|-h)
      echo "Usage: $0 [--tag TAG] [--no-cache] [--dir BUILD_DIR]"
      echo ""
      echo "  --tag TAG     Docker image tag (default: latest)"
      echo "  --no-cache    Force full Docker rebuild"
      echo "  --dir DIR     Working directory (default: /tmp/hummingbot-api-nonkyc-build)"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Functions ──────────────────────────────────────────────────────────────

log()  { echo "[build] $*"; }
warn() { echo "[build] ⚠  $*"; }
die()  { echo "[build] ✗  $*" >&2; exit 1; }
ok()   { echo "[build] ✓  $*"; }

check_prereqs() {
  for cmd in git docker awk; do
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is required but not found."
  done
  ok "Prerequisites: git, docker, awk"
}

# Helper: run a command in the base image, bypassing the uvicorn entrypoint
run_in_base() {
  docker run --rm --entrypoint "$1" "$BASE_TAG" "${@:2}"
}

# Patch a repo Dockerfile to disable conda sharded repodata (see header note).
# Injects `ENV CONDA_PLUGINS_USE_SHARDED_REPODATA=0` after EVERY FROM so the
# fix applies to all stages of a multi-stage build.
patch_dockerfile_conda_fix() {
  local src="$1" dst="$2"
  [ -f "$src" ] || die "Cannot patch — Dockerfile not found: $src"
  awk '
    { print }
    /^[[:space:]]*[Ff][Rr][Oo][Mm][[:space:]]/ {
      print "ENV CONDA_PLUGINS_USE_SHARDED_REPODATA=0"
    }
  ' "$src" > "$dst"
  if ! grep -q "CONDA_PLUGINS_USE_SHARDED_REPODATA" "$dst"; then
    die "Patch failed — no FROM line found in $src (unexpected Dockerfile shape)."
  fi
}

# ── Main ───────────────────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Hummingbot API (trevorwilf/nonkyc) — Custom Docker Build    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

check_prereqs

SRC_DIR="$BUILD_DIR/src"
mkdir -p "$BUILD_DIR"

# ── Step 1: Clone / update API repo ────────────────────────────────────────

log "Step 1/5: Clone/update API source repo"
if [ -d "$SRC_DIR/.git" ]; then
  log "  Updating existing clone..."
  cd "$SRC_DIR"
  git fetch origin "$HB_API_BRANCH" --depth=1
  git reset --hard "origin/$HB_API_BRANCH"
  git clean -fdx
else
  log "  Cloning $HB_API_REPO ($HB_API_BRANCH)..."
  rm -rf "$SRC_DIR"
  git clone --depth=1 --branch "$HB_API_BRANCH" "$HB_API_REPO" "$SRC_DIR"
fi

cd "$SRC_DIR"
HB_API_SHA="$(git rev-parse --short HEAD)"
ok "API source repo at $HB_API_SHA ($HB_API_BRANCH)"

# ── Step 2: Verify API structure ──────────────────────────────────────────

log "Step 2/5: Verifying hummingbot-api structure"
if [ ! -f "$SRC_DIR/$VERIFY_FILE" ]; then
  die "Expected file not found: $VERIFY_FILE — is this the right repo?"
fi

if [ ! -d "$SRC_DIR/$VERIFY_DIR" ]; then
  die "Expected directory not found: $VERIFY_DIR — is this the right repo?"
fi

PY_COUNT="$(find "$SRC_DIR" -maxdepth 2 -type f -name '*.py' | wc -l | tr -d ' ')"
if [ "${PY_COUNT:-0}" -lt 5 ]; then
  die "Only $PY_COUNT Python files found — API codebase seems incomplete."
fi
ok "API structure verified ($PY_COUNT Python files)"

# ── Step 3: Build base image from API repo Dockerfile ──────────────────────

log "Step 3/5: Building base API image from repo Dockerfile"
cd "$SRC_DIR"

if [ ! -f "$SRC_DIR/Dockerfile" ]; then
  die "Dockerfile not found in repo root!"
fi

# Apply the conda sharded-repodata fix (prevents "database is locked").
log "  Patching Dockerfile to disable conda sharded repodata..."
patch_dockerfile_conda_fix "$SRC_DIR/Dockerfile" "$SRC_DIR/$PATCHED_DOCKERFILE"
ok "Patched Dockerfile -> $PATCHED_DOCKERFILE"

BASE_TAG="${IMAGE_NAME}-base:${IMAGE_TAG}"
log "  Base image: $BASE_TAG"

docker build $DOCKER_BUILD_FLAGS \
  -t "$BASE_TAG" \
  -f "$PATCHED_DOCKERFILE" \
  --label "org.opencontainers.image.description=hummingbot-api (trevorwilf/nonkyc) base" \
  --label "hummingbot-api.source.repo=$HB_API_REPO" \
  --label "hummingbot-api.source.branch=$HB_API_BRANCH" \
  --label "hummingbot-api.source.sha=$HB_API_SHA" \
  --label "hummingbot-api.build.date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  .

ok "Base API image built: $BASE_TAG"

# ── Step 4: Verify hummingbot in base image ───────────────────────────────

log "Step 4/5: Verifying hummingbot package in base image"

# Verify the conda python path works (bypass uvicorn entrypoint)
run_in_base "$CONDA_PYTHON" -c "import hummingbot; print('hummingbot package found')" \
  || die "hummingbot package not found at $CONDA_PYTHON"
ok "Python: $CONDA_PYTHON"

# Show connector directory
HB_CONNECTOR_DIR=$(run_in_base "$CONDA_PYTHON" -c "
import hummingbot.connector.exchange as ex
import os
print(os.path.dirname(ex.__file__))
" 2>/dev/null || echo "")

if [ -z "$HB_CONNECTOR_DIR" ]; then
  die "Could not find hummingbot connector directory in base image!"
fi
ok "Connector directory: $HB_CONNECTOR_DIR"

# List current connectors for reference
log "  Stock connectors:"
run_in_base "$CONDA_PYTHON" -c "
import os, hummingbot.connector.exchange as ex
connectors = sorted([d for d in os.listdir(os.path.dirname(ex.__file__)) if not d.startswith('_')])
print('  ' + ', '.join(connectors))
" 2>/dev/null || true

# ── Step 5: Overlay — replace stock hummingbot with NonKYC fork ────────────

log "Step 5/5: Building final image with NonKYC hummingbot overlay"

FULL_TAG="${IMAGE_NAME}:${IMAGE_TAG}"
WRAP_DOCKERFILE="$BUILD_DIR/Dockerfile.nonkyc"

cat > "$WRAP_DOCKERFILE" <<EOF
FROM ${BASE_TAG}

USER root

# Defensive: keep sharded repodata off in case any conda step runs here too.
ENV CONDA_PLUGINS_USE_SHARDED_REPODATA=0

# Install git + build tools (needed for pip install from git repo + Cython/C++ extensions)
RUN apt-get update && apt-get install -y --no-install-recommends git g++ gcc make && rm -rf /var/lib/apt/lists/*

# Replace the stock hummingbot pip package with the NonKYC fork.
# Uses the conda env's pip so it installs into the right site-packages.
# --force-reinstall ensures it fully replaces the existing install.
RUN ${CONDA_PIP} install --no-cache-dir --upgrade pip && \\
    ${CONDA_PIP} install --no-cache-dir --force-reinstall \\
      "hummingbot @ git+${HB_REPO}@${HB_BRANCH}" && \\
    ${CONDA_PIP} install --no-cache-dir --upgrade "paho-mqtt>=2.0"

# Verify paho-mqtt v2 is intact (aiomqtt requires paho.mqtt.enums from v2+)
RUN ${CONDA_PYTHON} -c "from paho.mqtt.enums import CallbackAPIVersion; print('paho-mqtt v2 OK')"

# Verify the NonKYC connector is present
RUN ${CONDA_PYTHON} -c "\\
from hummingbot.connector.exchange.nonkyc import nonkyc_utils; \\
print('NonKYC connector verified:', hasattr(nonkyc_utils, 'KEYS')); \\
"

EOF

# Detect the correct runtime user from the base image
DETECTED_USER=$(docker run --rm --entrypoint sh --user root "$BASE_TAG" -c '
  for u in hummingbot app; do
    if id -u "$u" >/dev/null 2>&1; then echo "$u"; exit 0; fi
  done
  echo root
' 2>/dev/null || echo "root")

log "  Detected runtime user: $DETECTED_USER"
echo "USER $DETECTED_USER" >> "$WRAP_DOCKERFILE"

docker build $DOCKER_BUILD_FLAGS \
  -t "$FULL_TAG" \
  -f "$WRAP_DOCKERFILE" \
  --label "org.opencontainers.image.description=hummingbot-api (trevorwilf/nonkyc) + NonKYC connector" \
  --label "hummingbot-api.source.repo=$HB_API_REPO" \
  --label "hummingbot-api.source.branch=$HB_API_BRANCH" \
  --label "hummingbot-api.source.sha=$HB_API_SHA" \
  --label "hummingbot-api.nonkyc.repo=$HB_REPO" \
  --label "hummingbot-api.nonkyc.branch=$HB_BRANCH" \
  --label "hummingbot-api.build.date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$BUILD_DIR"

ok "Final image built: $FULL_TAG"

# ── Sanity checks ─────────────────────────────────────────────────────────

log "  Verifying NonKYC connector in final image..."
docker run --rm --entrypoint "$CONDA_PYTHON" "$FULL_TAG" -c "
from hummingbot.connector.exchange.nonkyc import nonkyc_utils
print('NonKYC KEYS:', nonkyc_utils.KEYS)
" 2>&1 && ok "NonKYC connector check passed" \
  || die "NonKYC connector NOT found in final image!"

log "  Verifying paho-mqtt v2 + aiomqtt compatibility..."
docker run --rm --entrypoint "$CONDA_PYTHON" "$FULL_TAG" -c "
from paho.mqtt.enums import CallbackAPIVersion
import aiomqtt
print('aiomqtt + paho-mqtt v2 OK')
" 2>&1 && ok "paho-mqtt/aiomqtt check passed" \
  || die "paho-mqtt/aiomqtt compatibility BROKEN — aiomqtt will fail at runtime!"

log "  Verifying connector appears in exchange list..."
docker run --rm --entrypoint "$CONDA_PYTHON" "$FULL_TAG" -c "
import os, hummingbot.connector.exchange as ex
connectors = sorted([d for d in os.listdir(os.path.dirname(ex.__file__)) if not d.startswith('_')])
assert 'nonkyc' in connectors, f'nonkyc not in {connectors}'
print('Connectors:', ', '.join(connectors))
print('nonkyc present: YES')
" 2>&1 && ok "Exchange list check passed" \
  || warn "NonKYC not in exchange list"

# Tag as hummingbot/hummingbot-api:latest for compose compatibility
docker tag "$FULL_TAG" hummingbot/hummingbot-api:latest
ok "Tagged hummingbot/hummingbot-api:latest -> $FULL_TAG"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Build Complete!                                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  Image:      $FULL_TAG"
echo "  API Repo:   $HB_API_REPO"
echo "  API Branch: $HB_API_BRANCH ($HB_API_SHA)"
echo "  HBot Fork:  $HB_REPO ($HB_BRANCH)"
echo "  Conda Env:  $CONDA_ENV"
echo ""
echo "  Compose can use:"
echo "    image: $FULL_TAG"
echo "    image: hummingbot/hummingbot-api:latest"
echo ""

docker image inspect "$FULL_TAG" --format='{{.Size}}' 2>/dev/null | \
  awk '{printf "Image size: %.0f MB\n", $1/1024/1024}' || true