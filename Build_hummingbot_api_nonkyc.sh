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
# ---------------------------------------------------------------------------
# CONTROLLER-STRATEGY SYNC
# ---------------------------------------------------------------------------
# After a SUCCESSFUL build, the bot fork's controllers/ tree (custom V2
# controller strategies) is synced to the host directory hummingbot-api mounts
# into bot containers (default: /mnt/sharedrive/apps/hummingbot/api/data/bots/
# controllers). Bot containers never read controllers from the image -- the
# mount shadows them -- so without this sync a rebuilt image runs with STALE
# strategy scripts. Update/add only (never deletes), checksum-based, with
# timestamped backups of every overwritten file.
#
# ---------------------------------------------------------------------------
# PRE-BUILD PURGE
# ---------------------------------------------------------------------------
# Default ON, disable with --no-purge: before the first docker build, every
# container -- running OR stopped -- that uses one of the images this script
# produces (hummingbot-api-nonkyc:TAG, hummingbot/hummingbot-api:latest,
# hummingbot-api-nonkyc-base:TAG) is stopped and REMOVED, and the old images
# are deleted. A container pins its original image ID forever, so without this
# a stopped container could later `docker start` on a STALE build and old
# images pile up as dangling layers. The purge runs AFTER the cheap
# clone/verify steps, so an early failure never takes the stack down -- but
# once it runs, the stack stays down until the build succeeds and you bring it
# back up (docker compose up -d), which then provably runs the image built here.
#
# Usage:
#   chmod +x Build_hummingbot_api_nonkyc.sh
#   ./Build_hummingbot_api_nonkyc.sh --controllers-dest /path  # REQUIRED: which
#                                                 # stack's controllers tree to sync
#   ./Build_hummingbot_api_nonkyc.sh --no-controllers-sync     # skip the sync
#   ./Build_hummingbot_api_nonkyc.sh --tag v2     # custom tag suffix
#   ./Build_hummingbot_api_nonkyc.sh --no-cache   # force full Docker rebuild
#   ./Build_hummingbot_api_nonkyc.sh --dir /tmp/x # custom working directory
#   ./Build_hummingbot_api_nonkyc.sh --no-purge   # keep old containers/images
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

# Controller-strategy sync: bot containers do NOT read controllers from the
# image -- hummingbot-api mounts this host directory over them -- so a rebuilt
# image alone never updates strategies. After a successful build, the bot
# fork's controllers/ tree is synced here (update/add only, checksum-based;
# replaced files are backed up to .backup-<timestamp>/ inside the destination;
# nothing at the destination is ever deleted). Note: this script never clones
# the bot fork for the image (pip installs it INSIDE Docker), so the sync does
# its own light sparse checkout of controllers/.
#
# CDX-010: NO DEFAULT. An unset destination is a hard error when the sync is
# enabled -- see validate_controllers_dest(). An explicit CONTROLLERS_DEST env
# var is still honoured; what is gone is the silent fallback to the VPN tree,
# which made a no-VPN build succeed into the VPN stack's live controllers.
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

# Provenance: the manifest of the synced controller set, its hash, and the bot
# fork commit they came from. All three are stamped onto the image as labels so
# a running container can be traced back to the exact controller tree.
CONTROLLERS_MANIFEST_NAME="controllers.manifest.sha256"
CONTROLLERS_MANIFEST_SHA256="unknown"
CONTROLLERS_MANIFEST_BODY=""
HBOT_COMMIT_SHA="unknown"

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
      echo "  --dir DIR               Working directory (default: /tmp/hummingbot-api-nonkyc-build)"
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
    log "  Stopping $count container(s) (30s grace for clean shutdown)..."
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
  dangling="$(docker images -q --filter dangling=true --filter "label=hummingbot-api.source.repo=$HB_API_REPO" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
  if [ -n "${dangling// /}" ]; then
    docker rmi $dangling >/dev/null 2>&1 || true
    ok "Pruned dangling image(s) from previous builds of this script."
  fi
}

# Fetch the bot fork's controllers/ tree so the sync has a source. This script
# never clones the bot fork for the image itself (pip installs it INSIDE
# Docker), so a light sparse checkout is done here; falls back to a full
# shallow clone on older git without sparse-checkout support. Sets
# HBOT_CONTROLLERS_SRC on success.
fetch_controllers_source() {
  local dir="$BUILD_DIR/hbot-controllers-src"
  rm -rf "$dir"
  if git clone --quiet --depth=1 --branch "$HB_BRANCH" --filter=blob:none --sparse "$HB_REPO" "$dir" 2>/dev/null \
     && git -C "$dir" sparse-checkout set controllers 2>/dev/null; then
    :
  else
    warn "Sparse clone unsupported by this git — falling back to a full shallow clone."
    rm -rf "$dir"
    git clone --quiet --depth=1 --branch "$HB_BRANCH" "$HB_REPO" "$dir"
  fi
  HBOT_CONTROLLERS_SRC="$dir/controllers"
  # Full SHA for the image label: it is the only durable link from a running
  # container back to the exact engine revision its controllers came from.
  HBOT_COMMIT_SHA="$(git -C "$dir" rev-parse HEAD)"
  ok "Controllers source at $(git -C "$dir" rev-parse --short HEAD) ($HB_BRANCH)"
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

# Sync the bot fork's controller strategy scripts into the directory
# hummingbot-api mounts into bot containers (CONTROLLERS_DEST). Bot containers
# never read controllers from the image -- the mount shadows them -- so this
# keeps the live strategy scripts in lockstep with the image that was just built.
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
echo "║  Hummingbot API (trevorwilf/nonkyc) — Custom Docker Build    ║"
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

# ── Pre-build: retire containers + images from previous builds ─────────────
# Placed AFTER the cheap clone/verify steps so an early failure never takes
# the stack down; from here on this build owns the image tags.

if [ "$PURGE_OLD" = "1" ]; then
  purge_old_image_and_containers \
    "${IMAGE_NAME}:${IMAGE_TAG}" \
    "hummingbot/hummingbot-api:latest" \
    "${IMAGE_NAME}-base:${IMAGE_TAG}"
else
  log "Pre-build purge disabled (--no-purge): existing containers/images are left alone."
fi

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

# Verify the MQTT bridge dependency swap: aiomqtt imports and the retired
# commlib-py is absent. They are mutually exclusive (paho-mqtt 2.x vs <2), so a
# broken swap must fail the BUILD, not surface at runtime.
RUN ${CONDA_PYTHON} -c "import aiomqtt; print('aiomqtt OK')"
RUN if ${CONDA_PIP} show commlib-py > /dev/null 2>&1; then \\
      echo 'ERROR: commlib-py is still installed — the aiomqtt swap is incomplete'; exit 1; \\
    else echo 'commlib-py absent OK'; fi

# Verify the NonKYC connector is present
RUN ${CONDA_PYTHON} -c "\\
from hummingbot.connector.exchange.nonkyc import nonkyc_utils; \\
print('NonKYC connector verified:', hasattr(nonkyc_utils, 'KEYS')); \\
"

# The API code must import cleanly against the FORK's hummingbot. Upstream API
# merges can grow imports that only newer upstream hummingbot provides (2026-07-12:
# hummingbot.connector.gateway.gateway crash-looped every container start).
# 'import deps' pulls every service module, so fork/API drift fails the BUILD
# here instead of at deploy. All module-level code is side-effect-free (config
# Settings have defaults; docker/db clients connect lazily in __init__).
RUN cd /hummingbot-api && ${CONDA_PYTHON} -c "import deps; print('API service imports OK against fork hummingbot')"

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

# ── Provenance: controllers manifest + bot fork commit ─────────────────────
# CDX-010: fetched and hashed BEFORE the build, because the hash is stamped onto
# the image below. This is the same checkout the post-build sync then copies
# from, so the label and the synced files are the same bytes by construction --
# re-fetching after the build could pick up a newer commit and make the label a
# lie. With --no-controllers-sync nothing is fetched and the labels stay
# "unknown": an honest absence, not a fabricated value.
if [ "$SYNC_CONTROLLERS" = "1" ]; then
  fetch_controllers_source
  compute_controllers_manifest "$HBOT_CONTROLLERS_SRC"
else
  log "Controller sync disabled — image provenance labels will read 'unknown'."
fi

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
  --label "nonkyc.hummingbot_commit=$HBOT_COMMIT_SHA" \
  --label "nonkyc.controllers_manifest_sha256=$CONTROLLERS_MANIFEST_SHA256" \
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

log "  Verifying commlib-py is absent (mutually exclusive with aiomqtt)..."
if docker run --rm --entrypoint "$CONDA_PIP" "$FULL_TAG" show commlib-py > /dev/null 2>&1; then
  die "commlib-py is still installed in the image — the aiomqtt swap is incomplete!"
else
  ok "commlib-py absent"
fi

log "  Verifying API service imports against the fork's hummingbot..."
docker run --rm --entrypoint "$CONDA_PYTHON" -w /hummingbot-api "$FULL_TAG" -c "
import deps
print('API service imports OK')
" 2>&1 && ok "API import check passed" \
  || die "API code cannot import against the fork's hummingbot — fork/API drift (see the ModuleNotFoundError above)!"

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

# ── Post-build: sync controller strategies to the API bots directory ───────
# Runs only when the build fully succeeded (any die above skips this), so the
# mounted controller scripts and the image stay in version lockstep.

log "Post-build: syncing controller strategies"
if [ "$SYNC_CONTROLLERS" = "1" ]; then
  # Source already fetched (and hashed) before the build, above -- deliberately
  # NOT re-fetched here, so the tree that is synced is the tree the image's
  # provenance labels attest to.
  sync_controllers "$HBOT_CONTROLLERS_SRC"
else
  log "Controller sync disabled (--no-controllers-sync)."
fi

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
echo "  Ctrl dir:   ${CONTROLLERS_DEST:-<none>} (sync $( [ "$SYNC_CONTROLLERS" = "1" ] && echo enabled || echo disabled ))"
echo "  Ctrl set:   $CONTROLLERS_MANIFEST_SHA256"
echo "  HBot commit: $HBOT_COMMIT_SHA"
echo ""
echo "  Compose can use:"
echo "    image: $FULL_TAG"
echo "    image: hummingbot/hummingbot-api:latest"
echo ""
if [ "$PURGE_OLD" = "1" ]; then
  echo "  NOTE: containers that used the previous image were stopped and REMOVED"
  echo "        before this build. Bring the stack back up (docker compose up -d) —"
  echo "        everything recreated now runs the image above."
  echo ""
fi

docker image inspect "$FULL_TAG" --format='{{.Size}}' 2>/dev/null | \
  awk '{printf "Image size: %.0f MB\n", $1/1024/1024}' || true