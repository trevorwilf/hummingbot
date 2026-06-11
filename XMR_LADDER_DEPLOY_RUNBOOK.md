# XMR Ladder — Prod Deploy Runbook (buy-order fix)

> Generated 2026-06-10. Companion to `Hummingbot_XMR_LADDER_BUY_FIX_claude_code_prompt.md`.
> This file is deployment-specific (LAN paths) — do NOT commit/push it.

## What was wrong (Phase 1 verification results)

| Location | Version | Evidence |
|---|---|---|
| Repo `controllers/market_making/range_inventory_ladder.py` | **NEW** ✓ | `self-balance model` ×2, `external quote delta detected` ×0 |
| Repo `order_executor.py` | **NEW** ✓ | `exact, controller-consumable fill accounting` ×1 |
| GitHub `origin/nonkyc` (cb70fc718) | **NEW** ✓ | same fingerprints verified via `git show` |
| Seeded controller `/mnt/sharedrive/apps/hummingbot/api/data/bots/controllers/market_making/range_inventory_ladder.py` | **OLD** ✗ | `external quote delta detected` ×1, `self-balance model` ×0, mtime 2026-04-16 |
| Bot instance `XMR_LADDER_NONKYC-20260605-1653-20260605-165330` | no per-instance controller copy (only conf/data/logs) — it loads the seeded dir above | |
| State file `data/range_inventory_ladder_xmr_usdt.json` | frozen since Jun 5 | `owned_quote=13.54652711`, `owned_base=0.518`, `tracked_fill_executor_ids=[]` |
| Diagnostic JSONL | **1.477 GB** | `range_inventory_ladder_xmr_usdt_diagnostic.jsonl` |

**Why seeding never refreshed the controller (Phase 1.4 answer):** `range_inventory_ladder.py`
is not present in `.seed_checksums` at all — it is a *user-added* file that the version-aware
seeder (`seed_helpers.sh`) never managed. It is preserved by design, forever. The upstream
`hummingbot-api` image does not contain this controller, so no image update will ever refresh it.
**It must be synced manually after every controller change** (step 4 below). No checksum/stamp
update is needed or appropriate — do not add it to `.seed_checksums` (the seeder would then
try to reconcile it against an image that doesn't ship the file).

The bot image (`hummingbot-nonkyc:latest`) is a separate concern: bots take `order_executor.py`
(the fill-accounting fix) from the **image**, and `range_inventory_ladder.py` from the
**mounted seeded dir**. Both must be current.

## What changed in the repo (this session)

- `controllers/market_making/range_inventory_ladder.py` — per-level filter events
  (`range_ladder_*_level_filtered_blocked` / `*_filtered_not_passive`) now emit only on
  **transitions** (new `range_ladder_*_level_eligible_again` event on return to eligibility);
  heartbeat gains `not_passive_buy_level_ids` / `not_passive_sell_level_ids`. This kills the
  ~271 MB/day diagnostic spam. Filtering semantics unchanged.
- New tests: `test/hummingbot/strategy_v2/controllers/test_range_inventory_ladder_diag_rate_limit.py` (15 tests).

---

## Deploy steps

### 1. Push the repo (this PC)

```powershell
cd E:\tradingsoftware\hummingbot
git push origin nonkyc
```

### 2. Rebuild the image (TrueNAS shell, 192.168.1.54)

Run your `build_hummingbot_nonkyc.sh` (it clones GitHub `trevorwilf/hummingbot#nonkyc`,
so the push in step 1 is a hard prerequisite). Then verify the new markers are in the image:

```bash
docker run --rm --entrypoint find hummingbot-nonkyc:latest /home/hummingbot -name range_inventory_ladder.py 2>/dev/null
docker run --rm --entrypoint bash hummingbot-nonkyc:latest -c \
  'grep -c "self-balance model" /home/hummingbot/controllers/market_making/range_inventory_ladder.py; \
   grep -c "exact, controller-consumable fill accounting" /home/hummingbot/hummingbot/strategy_v2/executors/order_executor/order_executor.py'
# expect: 2 (or >=1) and 1
```

### 3. Stop the XMR ladder bot gracefully

Dashboard: http://192.168.1.54:8501 → stop `XMR_LADDER_NONKYC-20260605-1653`
(graceful stop cancels its 6–8 open sell orders).

Then run the Jupyter verification block (bottom of the prompt MD) and confirm
**zero open XMR-USDT orders** and XMR fully available. If orphan orders remain,
cancel them on the exchange before continuing.

### 4. Sync the new controller into the seeded dir

From this PC (SMB) — after the bot is stopped:

```powershell
Copy-Item E:\tradingsoftware\hummingbot\controllers\market_making\range_inventory_ladder.py `
  \\192.168.1.54\apps\hummingbot\api\data\bots\controllers\market_making\range_inventory_ladder.py
Remove-Item \\192.168.1.54\apps\hummingbot\api\data\bots\controllers\market_making\__pycache__ -Recurse -Force
```

Verify:

```powershell
Select-String -Path \\192.168.1.54\apps\hummingbot\api\data\bots\controllers\market_making\range_inventory_ladder.py `
  -Pattern 'self-balance model' -SimpleMatch | Measure-Object   # expect 2
Select-String -Path \\192.168.1.54\apps\hummingbot\api\data\bots\controllers\market_making\range_inventory_ladder.py `
  -Pattern 'external quote delta detected' -SimpleMatch | Measure-Object  # expect 0
```

Also refresh the stale staging copy so future setups don't reintroduce the old file:
`E:\tradingsoftware\dockerscripts\apps\hummingbot\api\data\bots\controllers\market_making\range_inventory_ladder.py`

### 5. State + diagnostic log

Two options — **A is simpler and recommended** if you normally deploy a fresh bot from the
dashboard (hummingbot-api archives a stopped bot; restarting the same instance is not the
normal flow):

**Option A — fresh deploy (state self-repairs):**
The NEW controller's first-init seeds `owned_quote`/`owned_base` from the **live wallet**
(bounded by `total_amount_quote: 183` / `claimed_base_value_quote: 175`). A fresh instance
therefore starts with a truthful ledger automatically — no manual state surgery. The old
1.47 GB diagnostic file stays in the archived instance dir; back up its tail and delete it:

```powershell
$old='\\192.168.1.54\apps\hummingbot\api\data\bots\instances\XMR_LADDER_NONKYC-20260605-1653-20260605-165330\data\range_inventory_ladder_xmr_usdt_diagnostic.jsonl'
# (adjust path if the stop moved the instance under ...\bots\archived\)
Get-Content $old -Tail 100000 | Set-Content "$old.tail.bak.jsonl"
Remove-Item $old
```

**Option B — repair in place (per the prompt, if you restart the same instance):**

```powershell
$f='\\192.168.1.54\apps\hummingbot\api\data\bots\instances\XMR_LADDER_NONKYC-20260605-1653-20260605-165330\data\range_inventory_ladder_xmr_usdt.json'
$epoch=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
Copy-Item $f "$f.pre_ledger_repair.$epoch.json"
$j = Get-Content $f -Raw | ConvertFrom-Json
$j.owned_quote = "<LIVE_USDT_AVAILABLE>"   # 22.40040911 at analysis time — re-derive from Jupyter block
$j.owned_base  = "<LIVE_XMR_TOTAL>"        # 0.49400000 at analysis time
$j | ConvertTo-Json -Depth 5 | Set-Content $f -Encoding utf8
```

Leave `initial_*`, `tracked_fill_executor_ids: []`, `schema_version: 10` untouched.
Truncate the diagnostic JSONL as in Option A.

### 6. Restart the bot

Deploy via dashboard with the same controller config (`range_inventory_ladder_xmr_V1`).
The new bot container uses the rebuilt image + the synced controller.

---

## Post-deploy verification (within ~15 min — Phase 5)

1. **Buy orders appear almost immediately** (bid ≈ 339, buys 305–324 all passive, USDT ≈ 22.4 free):
   - `Creating BUY order` lines in the bot log (Dozzle: http://192.168.1.54:8080)
   - `range_ladder_buy_action` events in the new diagnostic JSONL
   - expect buys at 324/321/318/315/312 (305 may drop below 1 USDT weight share)
2. **Heartbeat truthful**: `free_buy_budget_quote` tracks live wallet (≈ 22.4 minus throttle),
   NOT 13.54652711.
3. **On first fill**: `range_ladder_ledger_updated` event fires and the state file's `owned_*`
   move (mtime changes). This is the regression check for the original bug.
4. **JSONL growth collapsed**: no per-cycle `*_filtered_*` spam; check file size after 30 min
   (should be KB-scale, not ~190 MB/day). Filter events now appear only on transitions, plus
   `not_passive_buy_level_ids`/`not_passive_sell_level_ids` in each 300 s heartbeat.
5. The `notEnoughFunds` sell-side over-subscription cannot recur: the self-balance model sizes
   sells from the live wallet.
