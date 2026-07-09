#!/bin/bash
set -euo pipefail

# =============================================================================
# build_hummingbot_nonkyc.sh
#
# Builds a custom hummingbot Docker image from a SINGLE repo/branch:
#   https://github.com/trevorwilf/hummingbot  (branch: nonkyc)
#
# Additionally bakes in a Postgres driver (psycopg2-binary) so DB mode works.
#
# After a SUCCESSFUL build, the repo's controllers/ tree (custom V2 controller
# strategies) is synced to the host directory hummingbot-api mounts into bot
# containers (default: /mnt/sharedrive/apps/hummingbot/api/data/bots/controllers).
# Bot containers never read controllers from the image -- the mount shadows
# them -- so without this sync a rebuilt image runs with STALE strategy scripts.
# Update/add only (never deletes), checksum-based, with timestamped backups of
# every overwritten file. Control with --controllers-dest / --no-controllers-sync.
#
# IMPORTANT: psycopg2-binary is installed into the CONDA ENVIRONMENT
# (/opt/conda/envs/hummingbot), not the base conda python. Hummingbot runs
# inside `conda activate hummingbot`, so packages must be installed there.
#
# ---------------------------------------------------------------------------
# CONDA SHARDED-REPODATA FIX (the "database is locked" build failure)
# ---------------------------------------------------------------------------
# The repo's own Dockerfile runs `conda env create` inside continuumio/
# miniconda3:latest, which now ships conda-libmamba-solver >= 26.4.0. That
# version enables CEP-16 "sharded repodata" by default, caching metadata in a
# SQLite DB (repodata_shards.db) that a reader thread and a network writer
# thread hit concurrently. On 26.4.0 (and on filesystems like ZFS/NFS where
# SQLite WAL locking is unreliable) this races and dies with:
#     sqlite3.OperationalError: database is locked
# Fixed properly in 26.4.2, but miniconda3:latest still ships 26.4.0.
#
# We can't edit the repo's Dockerfile upstream, so this script PATCHES the
# cloned Dockerfile at build time, injecting:
#     ENV CONDA_PLUGINS_USE_SHARDED_REPODATA=0
# after every FROM. That forces the solver back to monolithic repodata.json
# (slightly slower metadata fetch, completely reliable).
# =============================================================================

# ── Configuration ──────────────────────────────────────────────────────────

# SINGLE SOURCE REPO (per your request)
HB_REPO="https://github.com/trevorwilf/hummingbot.git"
HB_BRANCH="nonkyc"

# Connector path inside the hummingbot source tree (we still verify it exists)
CONNECTOR_REL_PATH="hummingbot/connector/exchange/nonkyc"

# Docker image name — this is what your compose file should reference
IMAGE_NAME="hummingbot-nonkyc"
IMAGE_TAG="latest"

# Working directory for the build
BUILD_DIR="${BUILD_DIR:-/tmp/hummingbot-nonkyc-build}"

# Docker build flags
DOCKER_BUILD_FLAGS=""

# Postgres driver to bake in (permanently fixes "no psycopg2/psycopg/asyncpg")
PG_DRIVER_PIP_PACKAGE="psycopg2-binary"

# Conda environment where hummingbot actually runs
# The bot entrypoint does `conda activate hummingbot` before launching,
# so ALL pip packages must go here — NOT the base conda env.
CONDA_ENV="hummingbot"
CONDA_PYTHON="/opt/conda/envs/${CONDA_ENV}/bin/python"
CONDA_PIP="/opt/conda/envs/${CONDA_ENV}/bin/pip"

# Name of the patched Dockerfile we generate inside the build context.
PATCHED_DOCKERFILE="Dockerfile.nonkyc-base"

# Controller-strategy sync: bot containers do NOT read controllers from the
# image -- hummingbot-api mounts this host directory over them -- so a rebuilt
# image alone never updates strategies. After a successful build, the repo's
# controllers/ tree is synced here (update/add only, checksum-based; replaced
# files are backed up to .backup-<timestamp>/ inside the destination; nothing
# at the destination is ever deleted).
CONTROLLERS_DEST="${CONTROLLERS_DEST:-/mnt/sharedrive/apps/hummingbot/api/data/bots/controllers}"
SYNC_CONTROLLERS=1

# ── Parse CLI args ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)      IMAGE_TAG="$2"; shift 2 ;;
    --tag=*)    IMAGE_TAG="${1#*=}"; shift ;;
    --no-cache) DOCKER_BUILD_FLAGS="--no-cache"; shift ;;
    --dir)      BUILD_DIR="$2"; shift 2 ;;
    --dir=*)    BUILD_DIR="${1#*=}"; shift ;;
    --controllers-dest)   CONTROLLERS_DEST="$2"; shift 2 ;;
    --controllers-dest=*) CONTROLLERS_DEST="${1#*=}"; shift ;;
    --no-controllers-sync) SYNC_CONTROLLERS=0; shift ;;
    --help|-h)
      echo "Usage: $0 [--tag TAG] [--no-cache] [--dir BUILD_DIR] [--controllers-dest DIR] [--no-controllers-sync]"
      echo ""
      echo "  --tag TAG               Docker image tag (default: latest)"
      echo "  --no-cache              Force full Docker rebuild"
      echo "  --dir DIR               Working directory (default: /tmp/hummingbot-nonkyc-build)"
      echo "  --controllers-dest DIR  Where hummingbot-api keeps bot controllers"
      echo "                          (default: $CONTROLLERS_DEST)"
      echo "  --no-controllers-sync   Skip the post-build controller-strategy sync"
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

# Helper: run a command in the image, bypassing any entrypoint
run_in_image() {
  local img="$1"; shift
  docker run --rm --entrypoint "$1" "$img" "${@:2}"
}

# Patch a repo Dockerfile to disable conda sharded repodata (see header note).
# Injects `ENV CONDA_PLUGINS_USE_SHARDED_REPODATA=0` after EVERY FROM so the
# fix applies to all stages of a multi-stage build (builder + release).
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

# Sync the repo's controller strategy scripts into the directory hummingbot-api
# mounts into bot containers (CONTROLLERS_DEST). Bot containers never read
# controllers from the image -- the mount shadows them -- so this keeps the
# live strategy scripts in lockstep with the image that was just built.
#   - update/add ONLY: nothing at the destination is ever deleted
#   - content comparison (cmp), not mtimes: fresh clones don't force copies
#   - every file being overwritten is first backed up to
#     $CONTROLLERS_DEST/.backup-<timestamp>/<same relative path>
#   - new files inherit the destination directory's owner (containers must
#     be able to read them) and 644 permissions
# Never fatal: a missing source or destination logs a warning and skips.
sync_controllers() {
  local src="$1"
  local dest="$CONTROLLERS_DEST"

  if [ "$SYNC_CONTROLLERS" != "1" ]; then
    log "Controller sync disabled (--no-controllers-sync)."
    return 0
  fi
  if [ ! -d "$src" ]; then
    warn "Controllers source not found: $src — skipping controller sync."
    return 0
  fi
  if [ ! -d "$dest" ]; then
    warn "Controllers destination not found: $dest — skipping controller sync."
    warn "Set CONTROLLERS_DEST or --controllers-dest if your API data dir differs."
    return 0
  fi

  log "Syncing controller strategies: $src -> $dest"
  local stamp backup_dir dest_owner
  local changed=0 added=0
  stamp="$(date +%Y%m%d_%H%M%S)"
  backup_dir="$dest/.backup-$stamp"
  dest_owner="$(stat -c '%u:%g' "$dest" 2>/dev/null || echo "")"

  local srcf rel dstf
  while IFS= read -r -d '' srcf; do
    rel="${srcf#"$src"/}"
    dstf="$dest/$rel"
    if [ -f "$dstf" ] && cmp -s "$srcf" "$dstf"; then
      continue                                    # unchanged — leave untouched
    fi
    if [ -f "$dstf" ]; then
      mkdir -p "$backup_dir/$(dirname "$rel")"    # keep the replaced original
      cp -p "$dstf" "$backup_dir/$rel"
      changed=$((changed + 1))
      log "    updated: $rel"
    else
      added=$((added + 1))
      log "    added:   $rel"
    fi
    mkdir -p "$(dirname "$dstf")"
    cp "$srcf" "$dstf"
    chmod 644 "$dstf" 2>/dev/null || true
    if [ -n "$dest_owner" ]; then
      chown "$dest_owner" "$dstf" 2>/dev/null || true
    fi
  done < <(find "$src" -type f -name '*.py' -not -path '*/__pycache__/*' -print0)

  if [ "$((changed + added))" -eq 0 ]; then
    ok "Controllers already up to date: $dest"
  else
    ok "Controller sync: $changed updated, $added added -> $dest"
    if [ "$changed" -gt 0 ]; then
      log "  Replaced originals backed up to: $backup_dir"
    fi
    log "  NOTE: bots already running keep the old code until they are restarted."
  fi
}

# ── Main ───────────────────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Hummingbot (trevorwilf/nonkyc) — Custom Docker Build        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

check_prereqs

SRC_DIR="$BUILD_DIR/src"
mkdir -p "$BUILD_DIR"

# ── Step 1: Clone / update single repo ─────────────────────────────────────

log "Step 1/5: Clone/update source repo"
if [ -d "$SRC_DIR/.git" ]; then
  log "  Updating existing clone..."
  cd "$SRC_DIR"
  git fetch origin "$HB_BRANCH" --depth=1
  git reset --hard "origin/$HB_BRANCH"
  git clean -fdx
else
  log "  Cloning $HB_REPO ($HB_BRANCH)..."
  rm -rf "$SRC_DIR"
  git clone --depth=1 --branch "$HB_BRANCH" "$HB_REPO" "$SRC_DIR"
fi

cd "$SRC_DIR"
HB_SHA="$(git rev-parse --short HEAD)"
ok "Source repo at $HB_SHA ($HB_BRANCH)"

# ── Step 2: Verify connector exists ────────────────────────────────────────

log "Step 2/5: Verifying NonKYC connector exists"
if [ ! -d "$SRC_DIR/$CONNECTOR_REL_PATH" ]; then
  die "Connector directory not found: $CONNECTOR_REL_PATH"
fi

PY_COUNT="$(find "$SRC_DIR/$CONNECTOR_REL_PATH" -type f -name '*.py' | wc -l | tr -d ' ')"
if [ "${PY_COUNT:-0}" -lt 3 ]; then
  die "Only $PY_COUNT Python files found in $CONNECTOR_REL_PATH — connector seems incomplete."
fi
ok "Connector present ($PY_COUNT Python files)"

# ── Step 3: Build base image from repo Dockerfile ──────────────────────────

log "Step 3/5: Building base image from repo Dockerfile"
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
  --label "org.opencontainers.image.description=hummingbot (trevorwilf/nonkyc) base" \
  --label "hummingbot.source.repo=$HB_REPO" \
  --label "hummingbot.source.branch=$HB_BRANCH" \
  --label "hummingbot.source.sha=$HB_SHA" \
  --label "hummingbot.build.date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  .

ok "Base image built: $BASE_TAG"

# ── Step 4: Detect conda env in base image ─────────────────────────────────

log "Step 4/5: Detecting conda environment layout"

# Verify the conda env exists in the image
run_in_image "$BASE_TAG" "$CONDA_PYTHON" -c "import sys; print('Conda env python:', sys.executable)" \
  || die "Conda env python not found at $CONDA_PYTHON — image layout may have changed."
ok "Conda env: $CONDA_ENV ($CONDA_PYTHON)"

# Detect the correct runtime user from the base image
DETECTED_USER=$(docker run --rm --user root "$BASE_TAG" sh -c '
  for u in hummingbot app; do
    if id -u "$u" >/dev/null 2>&1; then echo "$u"; exit 0; fi
  done
  echo root
' 2>/dev/null || echo "root")
log "  Detected runtime user: $DETECTED_USER"

# ── Step 5: Overlay image — Postgres driver into conda env ─────────────────

log "Step 5/5: Building final image with Postgres driver (into conda env)"

FULL_TAG="${IMAGE_NAME}:${IMAGE_TAG}"
WRAP_DOCKERFILE="$BUILD_DIR/Dockerfile.pg"

cat > "$WRAP_DOCKERFILE" <<EOF
FROM ${BASE_TAG}

USER root

# Defensive: keep sharded repodata off in case any conda step runs here too.
ENV CONDA_PLUGINS_USE_SHARDED_REPODATA=0

# Install psycopg2-binary into the CONDA ENVIRONMENT, not the base python.
# The bot entrypoint runs: conda activate hummingbot && ./bin/hummingbot_quickstart.py
# So the driver MUST be in /opt/conda/envs/hummingbot/lib/python3.x/site-packages/
RUN ${CONDA_PIP} install --no-cache-dir --upgrade pip \\
 && ${CONDA_PIP} install --no-cache-dir ${PG_DRIVER_PIP_PACKAGE}

# Verify psycopg2 imports from the conda env python (not base python)
RUN ${CONDA_PYTHON} -c "import psycopg2; print('psycopg2 OK in conda env: ${CONDA_ENV}')"

# Verify NonKYC connector loads
RUN ${CONDA_PYTHON} -c "\\
from hummingbot.connector.exchange.nonkyc import nonkyc_utils; \\
print('NonKYC connector verified:', hasattr(nonkyc_utils, 'KEYS')); \\
"

USER ${DETECTED_USER}
EOF

docker build $DOCKER_BUILD_FLAGS \
  -t "$FULL_TAG" \
  -f "$WRAP_DOCKERFILE" \
  --label "org.opencontainers.image.description=hummingbot (trevorwilf/nonkyc) + ${PG_DRIVER_PIP_PACKAGE}" \
  --label "hummingbot.source.repo=$HB_REPO" \
  --label "hummingbot.source.branch=$HB_BRANCH" \
  --label "hummingbot.source.sha=$HB_SHA" \
  --label "hummingbot.pg.driver=${PG_DRIVER_PIP_PACKAGE}" \
  --label "hummingbot.pg.installed_to=conda:${CONDA_ENV}" \
  --label "hummingbot.build.date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$BUILD_DIR"

ok "Final image built: $FULL_TAG"

# ── Post-build verification ───────────────────────────────────────────────

log "Running post-build verification..."

# 1. psycopg2 in conda env
log "  [1/4] Postgres driver in conda env..."
run_in_image "$FULL_TAG" "$CONDA_PYTHON" -c "import psycopg2; print('psycopg2 OK')" \
  && ok "psycopg2 imports in conda env" \
  || die "psycopg2 NOT importable from $CONDA_PYTHON — install went to wrong location!"

# 2. NonKYC connector
log "  [2/4] NonKYC connector..."
run_in_image "$FULL_TAG" "$CONDA_PYTHON" -c "
from hummingbot.connector.exchange.nonkyc import nonkyc_utils
print('NonKYC KEYS:', nonkyc_utils.KEYS)
" 2>&1 && ok "NonKYC connector check passed" \
  || die "NonKYC connector NOT found in final image!"

# 3. Connector in exchange list
log "  [3/4] Exchange list..."
run_in_image "$FULL_TAG" "$CONDA_PYTHON" -c "
import os, hummingbot.connector.exchange as ex
connectors = sorted([d for d in os.listdir(os.path.dirname(ex.__file__)) if not d.startswith('_')])
assert 'nonkyc' in connectors, f'nonkyc not in {connectors}'
print('Connectors:', ', '.join(connectors))
print('nonkyc present: YES')
" 2>&1 && ok "Exchange list check passed" \
  || warn "NonKYC not in exchange list"

# 4. Confirm psycopg2 is NOT only in base env (catch the old bug)
log "  [4/4] Confirming install location..."
INSTALL_LOC=$(run_in_image "$FULL_TAG" "$CONDA_PYTHON" -c "
import psycopg2, os
print(os.path.dirname(psycopg2.__file__))
" 2>/dev/null || echo "UNKNOWN")
if echo "$INSTALL_LOC" | grep -q "envs/${CONDA_ENV}"; then
  ok "psycopg2 installed in correct conda env: $INSTALL_LOC"
else
  warn "psycopg2 location unexpected: $INSTALL_LOC (expected envs/${CONDA_ENV})"
fi

# Tag as hummingbot/hummingbot:latest for compose compatibility
docker tag "$FULL_TAG" hummingbot/hummingbot:latest
ok "Tagged hummingbot/hummingbot:latest -> $FULL_TAG"

# ── Post-build: sync controller strategies to the API bots directory ───────
# Runs only when the build fully succeeded (any die above skips this), so the
# mounted controller scripts and the image stay in version lockstep.

log "Post-build: syncing controller strategies"
sync_controllers "$SRC_DIR/controllers"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Build Complete!                                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  Image:    $FULL_TAG"
echo "  Repo:     $HB_REPO"
echo "  Branch:   $HB_BRANCH ($HB_SHA)"
echo "  PG drv:   $PG_DRIVER_PIP_PACKAGE"
echo "  Conda:    $CONDA_ENV ($CONDA_PYTHON)"
echo "  User:     $DETECTED_USER"
echo "  Ctrl dir: $CONTROLLERS_DEST (sync $( [ "$SYNC_CONTROLLERS" = "1" ] && echo enabled || echo disabled ))"
echo ""
echo "  Compose can use:"
echo "    image: $FULL_TAG"
echo "    image: hummingbot/hummingbot:latest"
echo ""

docker image inspect "$FULL_TAG" --format='{{.Size}}' 2>/dev/null | \
  awk '{printf "Image size: %.0f MB\n", $1/1024/1024}' || true