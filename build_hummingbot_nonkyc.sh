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
# containers. Bot containers never read controllers from the image -- the mount
# shadows them -- so without this sync a rebuilt image runs with STALE strategy
# scripts. Update/add only (never deletes), checksum-based, with timestamped
# backups of every overwritten file.
#
# The destination is REQUIRED and has no default (CDX-010): the VPN and no-VPN
# trees both exist on the same share, so a defaulted build did not "skip" -- it
# succeeded into the WRONG tree, with no warning, and the operator only found out
# when the wrong stack's bots changed behaviour. Pass --controllers-dest DIR, or
# --no-controllers-sync to skip the sync deliberately.
#
# PRE-BUILD PURGE (default ON, disable with --no-purge): before the first
# docker build, every container -- running OR stopped -- that uses one of the
# images this script produces (hummingbot-nonkyc:TAG, hummingbot/hummingbot:latest,
# hummingbot-nonkyc-base:TAG) is stopped and REMOVED, and the old images are
# deleted. A container pins its original image ID forever, so without this a
# stopped bot could later `docker start` on a STALE build and old images pile
# up as dangling layers. The purge runs AFTER the cheap clone/verify steps, so
# an early failure never takes the stack down -- but once it runs, the stack
# stays down until the build succeeds and you bring it back up (compose up /
# redeploy bots), which then provably runs the image built here.
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
#
# CDX-010: NO DEFAULT. An unset destination is a hard error when the sync is
# enabled -- see validate_controllers_dest(). An explicit CONTROLLERS_DEST env
# var is still honoured; what is gone is the silent fallback to the VPN tree.
CONTROLLERS_DEST="${CONTROLLERS_DEST:-}"
SYNC_CONTROLLERS=1

# The two known controller trees, named in the fail-fast error so the operator
# can see at a glance that picking one is a decision, not a formality. Derived
# from the stack files: `- /mnt/sharedrive/apps/hummingbot:/humming_dir`
# ("hummingbot stack - vpn":176) and `- /mnt/sharedrive/apps/hummingbot_us:/humming_dir`
# ("hummingbot stack - no vpn":71), each + the api-init CTRL_DST suffix
# "$BASE/api/data/bots/controllers" (vpn:264 / no vpn:151).
KNOWN_CONTROLLERS_TREE_VPN="/mnt/sharedrive/apps/hummingbot/api/data/bots/controllers"
KNOWN_CONTROLLERS_TREE_NO_VPN="/mnt/sharedrive/apps/hummingbot_us/api/data/bots/controllers"

# Provenance: the manifest of the synced controller set, and its hash. Both are
# stamped onto the image as labels so a running container can be traced back to
# the exact controller tree that shipped with it.
CONTROLLERS_MANIFEST_NAME="controllers.manifest.sha256"
CONTROLLERS_MANIFEST_SHA256="unknown"
CONTROLLERS_MANIFEST_BODY=""

# Pre-build purge: stop + remove containers using the images this script
# rebuilds, then delete those images, so nothing can keep running (or later
# restart on) a stale build. See the header note. Disable with --no-purge.
PURGE_OLD=1

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
    --no-purge) PURGE_OLD=0; shift ;;
    --help|-h)
      echo "Usage: $0 [--tag TAG] [--no-cache] [--dir BUILD_DIR] [--controllers-dest DIR] [--no-controllers-sync] [--no-purge]"
      echo ""
      echo "  --tag TAG               Docker image tag (default: latest)"
      echo "  --no-cache              Force full Docker rebuild"
      echo "  --dir DIR               Working directory (default: /tmp/hummingbot-nonkyc-build)"
      echo "  --controllers-dest DIR  Where hummingbot-api keeps bot controllers."
      echo "                          REQUIRED unless --no-controllers-sync (no default:"
      echo "                          the VPN and no-VPN trees are siblings on one share,"
      echo "                          so a default silently syncs into the wrong stack)."
      echo "                            vpn:    $KNOWN_CONTROLLERS_TREE_VPN"
      echo "                            no-vpn: $KNOWN_CONTROLLERS_TREE_NO_VPN"
      echo "  --no-controllers-sync   Skip the post-build controller-strategy sync"
      echo "  --no-purge              Do NOT stop/remove containers or delete the old"
      echo "                          images before building (legacy behavior)"
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

# Pre-build purge: stop and REMOVE every container (running or stopped) that
# uses one of the images this script is about to rebuild, then delete the
# images themselves. A container pins its original image ID forever, so a
# merely-stopped container would (a) block `docker rmi` of the old image and
# (b) silently resurrect ON THE STALE BUILD if anything ever `docker start`s
# it. Removing the containers + images guarantees that everything brought back
# up after this build runs the image built here.
#
# Containers are matched BOTH by the image name recorded at create time
# (normalized for a docker.io/ prefix; catches containers created from this
# tag before a rebuild reassigned it to a new ID) and by the tags' CURRENT
# image IDs (catches containers started from a different tag on the same
# image). Everything matched is logged before it is touched. Never fatal:
# on a first build there is simply nothing to purge.
purge_old_image_and_containers() {
  local tags=("$@")
  local tag id ids=""

  log "Pre-build purge: retiring containers and images for: ${tags[*]}"

  # Current image IDs behind the tags (a tag may not exist yet on a first build).
  for tag in "${tags[@]}"; do
    id="$(docker image inspect --format '{{.Id}}' "$tag" 2>/dev/null || true)"
    if [ -n "$id" ]; then
      case " $ids " in
        *" $id "*) ;;
        *) ids="$ids $id" ;;
      esac
    fi
  done

  # Find every container -- running OR stopped -- that uses these images.
  local all_cids matched="" count=0
  local cid cname cimgid cimgname cstate hit
  all_cids="$(docker ps -aq 2>/dev/null || true)"
  if [ -n "$all_cids" ]; then
    while IFS='|' read -r cid cname cimgid cimgname cstate; do
      [ -n "$cid" ] || continue
      cname="${cname#/}"
      cimgname="${cimgname#docker.io/}"
      hit=0
      for tag in "${tags[@]}"; do
        if [ "$cimgname" = "$tag" ]; then hit=1; break; fi
        # A bare image name (no tag) at create time means :latest.
        if [ "${tag##*:}" = "latest" ] && [ "$cimgname" = "${tag%%:*}" ]; then hit=1; break; fi
      done
      if [ "$hit" -eq 0 ] && [ -n "$ids" ]; then
        for id in $ids; do
          if [ "$cimgid" = "$id" ]; then hit=1; break; fi
        done
      fi
      if [ "$hit" -eq 1 ]; then
        log "    matched container: $cname ($cstate, image: $cimgname)"
        matched="$matched $cid"
        count=$((count + 1))
      fi
    done < <(docker inspect --format '{{.Id}}|{{.Name}}|{{.Image}}|{{.Config.Image}}|{{.State.Status}}' $all_cids 2>/dev/null || true)
  fi

  if [ "$count" -eq 0 ]; then
    ok "No containers reference the image(s) being rebuilt."
  else
    log "  Stopping $count container(s) (30s grace for clean bot shutdown)..."
    docker stop -t 30 $matched >/dev/null 2>&1 || true
    log "  Removing them (a kept container would restart on the STALE image)..."
    docker rm $matched >/dev/null 2>&1 || true
    ok "Stopped and removed $count container(s) — recreate them after the build."
  fi

  # Delete the images themselves so the rebuild starts from a clean name.
  for tag in "${tags[@]}"; do
    if docker image inspect "$tag" >/dev/null 2>&1; then
      if docker rmi "$tag" >/dev/null 2>&1; then
        ok "Removed old image: $tag"
      else
        # Still referenced by something unmatched (e.g. another tag on the same
        # ID): force-untag so the rebuild owns the name regardless.
        docker rmi -f "$tag" >/dev/null 2>&1 || true
        warn "Force-untagged $tag (its layers were still referenced elsewhere)."
      fi
    else
      log "  Image not present (first build?): $tag"
    fi
  done

  # Sweep now-dangling leftovers from PREVIOUS builds of this script (matched
  # by its own build label) so superseded layers don't accumulate on disk.
  local dangling
  dangling="$(docker images -q --filter dangling=true --filter "label=hummingbot.source.repo=$HB_REPO" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
  if [ -n "${dangling// /}" ]; then
    docker rmi $dangling >/dev/null 2>&1 || true
    ok "Pruned dangling image(s) from previous builds of this script."
  fi
}

# CDX-010: the controllers destination must be EXPLICIT.
#
# The old default pointed at the VPN tree. Because the no-VPN tree is its
# sibling on the same share, a `--no-vpn` operator who forgot the flag did not
# get a "destination missing" skip -- the default existed, so the sync SUCCEEDED
# into the other stack's live controllers. Explicit-or-fail removes the class.
#
# Called from Main BEFORE the pre-build purge and before any docker build, so a
# missing destination costs nothing: no container is stopped, no image deleted.
validate_controllers_dest() {
  if [ "$SYNC_CONTROLLERS" != "1" ]; then
    log "Controller sync disabled (--no-controllers-sync): no destination required."
    return 0
  fi
  if [ -z "${CONTROLLERS_DEST:-}" ]; then
    echo "" >&2
    echo "[build] ✗  No controllers destination set." >&2
    echo "" >&2
    echo "  This build syncs controller strategies onto a LIVE stack's share, so the" >&2
    echo "  destination has no default -- the two known trees are siblings and picking" >&2
    echo "  the wrong one silently rewrites the other stack's strategies:" >&2
    echo "" >&2
    echo "    vpn stack:     --controllers-dest $KNOWN_CONTROLLERS_TREE_VPN" >&2
    echo "    no-vpn stack:  --controllers-dest $KNOWN_CONTROLLERS_TREE_NO_VPN" >&2
    echo "" >&2
    echo "  Or pass --no-controllers-sync to skip the sync deliberately." >&2
    echo "" >&2
    die "Refusing to build: pass --controllers-dest DIR or --no-controllers-sync."
  fi
  if [ ! -d "$CONTROLLERS_DEST" ]; then
    echo "" >&2
    echo "[build] ✗  Controllers destination is not an existing directory:" >&2
    echo "             $CONTROLLERS_DEST" >&2
    echo "" >&2
    echo "  Refusing to create it: a typo'd path would be created happily and the" >&2
    echo "  sync would land where no stack ever reads it. Known trees:" >&2
    echo "    vpn stack:     $KNOWN_CONTROLLERS_TREE_VPN" >&2
    echo "    no-vpn stack:  $KNOWN_CONTROLLERS_TREE_NO_VPN" >&2
    echo "" >&2
    die "Refusing to build: --controllers-dest must name an existing directory."
  fi
  ok "Controllers destination: $CONTROLLERS_DEST"
}

# Build the controller manifest for $1 (the controllers source tree).
#
# Format (one line per file, ordered by path):  <sha256><2 spaces><relative/path>
# Sorted by PATH, not by line: a path ordering is stable across content changes,
# so a manifest diff reads as "these files changed", not as a full reshuffle.
#
# The set is exactly what sync_controllers copies -- the same find predicate --
# so the manifest cannot claim files the sync did not place.
#
# Each digest is taken from STDIN (`sha256sum < "$rel"`) rather than by passing
# the path: sha256sum prints "<hash> *<path>" for a path argument on hosts where
# it defaults to binary mode, which is NOT the format above. Reading stdin and
# formatting the line here makes the output host-independent.
controllers_manifest_body() {
  local src="$1"
  ( cd "$src" || exit 1
    while IFS= read -r -d '' rel; do
      printf '%s  %s\n' "$(sha256sum < "$rel" | awk '{print $1}')" "$rel"
    done < <(find . -type f -name '*.py' -not -path '*/__pycache__/*' -printf '%P\0' \
             | LC_ALL=C sort -z)
  )
}

# Compute the manifest + its hash into CONTROLLERS_MANIFEST_BODY / _SHA256.
compute_controllers_manifest() {
  local src="$1"
  [ -d "$src" ] || die "Controllers source not found: $src — cannot compute the manifest."
  CONTROLLERS_MANIFEST_BODY="$(controllers_manifest_body "$src")"
  [ -n "$CONTROLLERS_MANIFEST_BODY" ] || die "Controllers manifest is empty for $src — refusing to stamp an image with a meaningless provenance label."
  CONTROLLERS_MANIFEST_SHA256="$(printf '%s\n' "$CONTROLLERS_MANIFEST_BODY" | sha256sum | awk '{print $1}')"
  ok "Controllers manifest: $(printf '%s\n' "$CONTROLLERS_MANIFEST_BODY" | wc -l | tr -d ' ') files, sha256 $CONTROLLERS_MANIFEST_SHA256"
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
#   - a controllers.manifest.sha256 recording the synced set is written into
#     the destination; the same hash is stamped on the image as a label
# CDX-010: FATAL, not skippable. This used to warn-and-return-0 on a missing
# source or destination, which is the silent-success failure mode the finding is
# about: the build reported success while the live strategies stayed stale. The
# only way to skip is now the explicit --no-controllers-sync.
sync_controllers() {
  local src="$1"
  local dest="$CONTROLLERS_DEST"

  if [ "$SYNC_CONTROLLERS" != "1" ]; then
    log "Controller sync disabled (--no-controllers-sync)."
    return 0
  fi
  if [ ! -d "$src" ]; then
    die "Controllers source not found: $src — refusing to report a successful build that synced nothing."
  fi
  if [ ! -d "$dest" ]; then
    die "Controllers destination vanished since validation: $dest — refusing to skip silently."
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

  # Provenance: record the synced set at the destination. Written unconditionally
  # (even when no file changed) so a destination that predates this feature, or
  # one drifted by hand, still gains a truthful manifest. Same backup contract as
  # any other replaced file.
  [ -n "$CONTROLLERS_MANIFEST_BODY" ] || compute_controllers_manifest "$src"
  local manifest_path="$dest/$CONTROLLERS_MANIFEST_NAME"
  if [ -f "$manifest_path" ] && ! printf '%s\n' "$CONTROLLERS_MANIFEST_BODY" | cmp -s - "$manifest_path"; then
    mkdir -p "$backup_dir"
    cp -p "$manifest_path" "$backup_dir/$CONTROLLERS_MANIFEST_NAME"
  fi
  printf '%s\n' "$CONTROLLERS_MANIFEST_BODY" > "$manifest_path"
  chmod 644 "$manifest_path" 2>/dev/null || true
  if [ -n "$dest_owner" ]; then
    chown "$dest_owner" "$manifest_path" 2>/dev/null || true
  fi
  ok "Controllers manifest -> $manifest_path (sha256 $CONTROLLERS_MANIFEST_SHA256)"

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

# CDX-010: settle the controllers destination BEFORE anything destructive or
# expensive happens -- no clone, no purge (which stops and REMOVES live bot
# containers), no build. A missing --controllers-dest must cost the operator a
# re-run, never a stopped bot or a sync into the wrong stack's tree.
validate_controllers_dest

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

# ── Provenance: controllers manifest + source commit ───────────────────────
# CDX-010: computed from the tree that is about to be synced, BEFORE the build,
# because the resulting hash is stamped onto the image as a label. Full (not
# short) SHA: the label is the only durable link from a running container back
# to the exact engine revision, and a short SHA can collide as the repo grows.
HB_SHA_FULL="$(git -C "$SRC_DIR" rev-parse HEAD)"
compute_controllers_manifest "$SRC_DIR/controllers"

# ── Pre-build: retire containers + images from previous builds ─────────────
# Placed AFTER the cheap clone/verify steps so an early failure never takes
# the stack down; from here on this build owns the image tags.

if [ "$PURGE_OLD" = "1" ]; then
  purge_old_image_and_containers \
    "${IMAGE_NAME}:${IMAGE_TAG}" \
    "hummingbot/hummingbot:latest" \
    "${IMAGE_NAME}-base:${IMAGE_TAG}"
else
  log "Pre-build purge disabled (--no-purge): existing containers/images are left alone."
fi

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

# Verify the MQTT bridge dependency swap: aiomqtt (paho-mqtt 2.x) must import and
# the retired commlib-py must be absent. aiomqtt and commlib-py are mutually
# exclusive (paho-mqtt 2.x vs <2), so a broken swap must fail the BUILD, not runtime.
RUN ${CONDA_PYTHON} -c "import aiomqtt; from paho.mqtt.enums import CallbackAPIVersion; print('aiomqtt + paho-mqtt v2 OK')"
RUN if ${CONDA_PIP} show commlib-py > /dev/null 2>&1; then \\
      echo 'ERROR: commlib-py is still installed — the aiomqtt swap is incomplete'; exit 1; \\
    else echo 'commlib-py absent OK'; fi

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
  --label "nonkyc.hummingbot_commit=$HB_SHA_FULL" \
  --label "nonkyc.controllers_manifest_sha256=$CONTROLLERS_MANIFEST_SHA256" \
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
log "  [4/5] Confirming install location..."
INSTALL_LOC=$(run_in_image "$FULL_TAG" "$CONDA_PYTHON" -c "
import psycopg2, os
print(os.path.dirname(psycopg2.__file__))
" 2>/dev/null || echo "UNKNOWN")
if echo "$INSTALL_LOC" | grep -q "envs/${CONDA_ENV}"; then
  ok "psycopg2 installed in correct conda env: $INSTALL_LOC"
else
  warn "psycopg2 location unexpected: $INSTALL_LOC (expected envs/${CONDA_ENV})"
fi

# 5. MQTT bridge dependency swap: aiomqtt present, commlib-py gone.
log "  [5/5] MQTT bridge dependency (aiomqtt in, commlib-py out)..."
run_in_image "$FULL_TAG" "$CONDA_PYTHON" -c "import aiomqtt; from paho.mqtt.enums import CallbackAPIVersion; print('aiomqtt + paho-mqtt v2 OK')" \
  && ok "aiomqtt imports in conda env" \
  || die "aiomqtt NOT importable — the MQTT bridge will fail at runtime!"
if run_in_image "$FULL_TAG" "$CONDA_PIP" show commlib-py > /dev/null 2>&1; then
  die "commlib-py is still installed in the image — the aiomqtt swap is incomplete!"
else
  ok "commlib-py absent (mutually exclusive with aiomqtt/paho-mqtt 2.x)"
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
echo "  Ctrl dir: ${CONTROLLERS_DEST:-<none>} (sync $( [ "$SYNC_CONTROLLERS" = "1" ] && echo enabled || echo disabled ))"
echo "  Ctrl set: $CONTROLLERS_MANIFEST_SHA256"
echo "  Commit:   $HB_SHA_FULL"
echo ""
echo "  Compose can use:"
echo "    image: $FULL_TAG"
echo "    image: hummingbot/hummingbot:latest"
echo ""
if [ "$PURGE_OLD" = "1" ]; then
  echo "  NOTE: containers that used the previous image were stopped and REMOVED"
  echo "        before this build. Bring the stack back up (docker compose up -d /"
  echo "        redeploy bots) — everything recreated now runs the image above."
  echo ""
fi

docker image inspect "$FULL_TAG" --format='{{.Size}}' 2>/dev/null | \
  awk '{printf "Image size: %.0f MB\n", $1/1024/1024}' || true