#!/usr/bin/env bash
# sync_ladder_diagnostics.sh
#
# Harvests session-stamped range_inventory_ladder diagnostic files from all bot
# instances (both stacks) into per-exchange analysis directories for Jupyter.
#
#   Source:  {STACK_ROOT}/api/data/bots/instances/*/data/range_inventory_ladder_*_diagnostic_*.jsonl
#   Dest:    /mnt/sharedrive/apps/hummingbot/jupyter/notebooks/ladder/diagnostics/{exchange}/
#
# The exchange is read from the "connector" field inside the jsonl itself
# (first matching line), lowercased. Files whose connector cannot be
# determined go to .../diagnostics/unknown/.
#
# Existing destination files are overwritten (diagnostics are append-only,
# so re-copying a live file just refreshes it with the latest data).
#
# Cron example (every 15 minutes):
#   */15 * * * * /mnt/sharedrive/apps/hummingbot/scripts/sync_ladder_diagnostics.sh >> /mnt/sharedrive/apps/hummingbot/logs/sync_ladder_diagnostics.log 2>&1

set -u

STACK_ROOTS=(
    "/mnt/sharedrive/apps/hummingbot"
    "/mnt/sharedrive/apps/hummingbot_us"
)

DEST_BASE="/mnt/sharedrive/apps/hummingbot/jupyter/notebooks/ladder/diagnostics"

copied=0
skipped=0
failed=0

# Extract the connector name from the first line of a diagnostic jsonl.
# Avoids a jq dependency: matches "connector": "value" with grep/sed.
get_connector() {
    local file="$1"
    local conn
    conn=$(head -n 1 -- "$file" 2>/dev/null \
        | grep -o '"connector"[[:space:]]*:[[:space:]]*"[^"]*"' \
        | head -n 1 \
        | sed 's/.*"connector"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
    if [ -z "$conn" ]; then
        echo "unknown"
    else
        # lowercase and sanitize to a safe directory name
        echo "$conn" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '_' | sed 's/_*$//'
    fi
}

for root in "${STACK_ROOTS[@]}"; do
    instances_dir="$root/api/data/bots/instances"
    if [ ! -d "$instances_dir" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') NOTE: $instances_dir does not exist, skipping"
        continue
    fi

    # find handles "no matches" cleanly and any depth quirks
    while IFS= read -r -d '' src; do
        exchange=$(get_connector "$src")
        dest_dir="$DEST_BASE/$exchange"

        if ! mkdir -p -- "$dest_dir"; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: cannot create $dest_dir"
            failed=$((failed + 1))
            continue
        fi

        base=$(basename -- "$src")
        dest="$dest_dir/$base"

        # Skip the copy if the destination is already identical (size + mtime
        # won't cut it for append-only files, so compare sizes: if source is
        # not larger and content length matches, it hasn't grown).
        if [ -f "$dest" ]; then
            src_size=$(stat -c %s -- "$src" 2>/dev/null || echo -1)
            dest_size=$(stat -c %s -- "$dest" 2>/dev/null || echo -2)
            if [ "$src_size" = "$dest_size" ]; then
                skipped=$((skipped + 1))
                continue
            fi
        fi

        if cp -f -- "$src" "$dest"; then
            copied=$((copied + 1))
        else
            echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: failed to copy $src -> $dest"
            failed=$((failed + 1))
        fi
    done < <(find "$instances_dir" -mindepth 3 -maxdepth 3 -type f \
             -path "*/data/range_inventory_ladder_*_diagnostic_*.jsonl" -print0 2>/dev/null)
done

echo "$(date '+%Y-%m-%d %H:%M:%S') sync_ladder_diagnostics: copied=$copied skipped=$skipped failed=$failed"

# non-zero exit if anything failed, so cron mail / monitoring can notice
[ "$failed" -eq 0 ] || exit 1
exit 0
