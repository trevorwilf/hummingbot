#!/bin/bash
set -euo pipefail
cd /mnt/sharedrive/apps/hummingbot

VPN=/mnt/sharedrive/apps/hummingbot/api/data/bots/controllers
US=/mnt/sharedrive/apps/hummingbot_us/api/data/bots/controllers
ts=$(date +%Y%m%d_%H%M%S)
api_log="api_build_$ts.log"
bot_log="bot_build_$ts.log"

# Line-based docker output so the two logs interleave cleanly.
export BUILDKIT_PROGRESS=plain

# Launch both builds in the background — note the & at the END of each line.
# The bot build syncs controllers to the VPN tree; the API build skips the
# (identical) sync. Either build can be the one that syncs.
./Build_hummingbot_api_nonkyc.sh --no-cache --no-controllers-sync    >"$api_log" 2>&1 &
api_pid=$!
./build_hummingbot_nonkyc.sh     --no-cache --controllers-dest "$VPN" >"$bot_log" 2>&1 &
bot_pid=$!

echo "API build PID $api_pid -> $api_log"
echo "Bot build PID $bot_pid -> $bot_log"
echo "── live output (==> <== headers mark which build) ─────────────"

# Stream BOTH logs live; stopped once both builds exit.
tail -n +1 -f "$api_log" "$bot_log" &
tail_pid=$!

# Capture each exit code without set -e aborting at the first failing wait.
api_rc=0; bot_rc=0
wait "$api_pid" || api_rc=$?
wait "$bot_pid" || bot_rc=$?
kill "$tail_pid" 2>/dev/null || true
wait "$tail_pid" 2>/dev/null || true

echo
echo "API build exit=$api_rc ($api_log)"
echo "Bot build exit=$bot_rc ($bot_log)"
if [ "$api_rc" -ne 0 ] || [ "$bot_rc" -ne 0 ]; then
  echo "One or both builds FAILED — not mirroring controllers." >&2
  exit 1
fi

# Only the VPN tree was written (by the bot build); mirror it to the no-VPN
# tree. No rebuild: the controllers live in the mounted tree, not the image.
rsync -a --checksum --exclude='.backup-*' \
    --backup --backup-dir="$US/.backup-$ts" \
    "$VPN"/ "$US"/
echo "Done. Controllers mirrored $VPN -> $US"
