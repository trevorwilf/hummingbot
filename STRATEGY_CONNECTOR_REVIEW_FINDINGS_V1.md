# Strategy & Connector Review Findings — V1 (2026-07-14)

Read-only review. **No code was changed.** Scope:
- Classic (v1) strategies: pure/avellaneda/perpetual MM, XEMM, cross-exchange mining, amm_arb, spot-perp arb, hedge, liquidity mining, shared strategy infra
- V2 controllers: `controllers/directional_trading/*`, `controllers/generic/*`, `pmm_simple` (+ `directional_trading_controller_base.py`, the one framework file not covered by the July hardening)
- Connectors: **Kraken** and **NonKYC** only (MEXC explicitly excluded)
- Out of scope: `range_inventory_ladder.py`, `pmm_dynamic.py`, `dman_maker_v2.py`, strategy_v2 framework (all covered by `V2_STRATEGY_REVIEW_FINDINGS.md` / the merged 7-phase fixes)

Six parallel review agents + orchestrator verification: 4/4 code spot-checks on headline items confirmed; live API probes run read-only against Kraken (REST + public WS) and NonKYC (REST) — results below. Every finding whose status was changed by a probe is tagged.

Severity legend: CRITICAL = live money loss path; HIGH = wedge/unhedged/wrong-data path; MEDIUM = degraded correctness or robustness; LOW = latent/edge/hygiene.

---

## 0. Live probe results (2026-07-14)

Kraken (authed REST, read-only + 90s public WS book-10 on XBT/USD + ETH/USD):
1. **KRK-3 CONFIRMED** — `QueryTrades` with an ORDER txid → `["EOrder:Invalid order"]` (exactly the string the connector whitelists as "no fills"). REST fill recovery is dead. Fix path validated live: `QueryOrders?trades=true` returns a `trades` array (T-prefixed ids); `QueryTrades` with those ids returns full fills (fee/vol/price/time/ordertxid).
2. **KRK-1 CONFIRMED** — 11 of 6,765 book diffs in 90s were dual-dict `[{a},{b,c}]` (asks at index 1, bids at index 2). The connector reads only index 1 → those bid updates are dropped. Every diff carried checksum `c` (KRK-12 fix viable).
3. **KRK-7 CONFIRMED** — `Ticker` (all) keys are canonical (`XXBTZUSD`); `Ticker?pair=XBTUSD` also echoes result key `XXBTZUSD`. Altname matching can never hit legacy pairs.
4. **KRK-6 refined** — `QueryOrders` with a bogus txid returns `error: [], result: {}` (empty result, NOT an error) → the connector's own `IOError(ORDER_NOT_EXIST_ERROR_CODE ...)` is the not-found signature to classify.
5. **KRK-11 REFUTED** — fee/cost = 0.40% on both buys and sells (XMR/USD history) → fees quote-denominated both sides on this account. Non-issue.
6. **KRK-2 context** — account currently holds `.S`/`.B`/`.HOLD` sub-balances but **no `.F`** → the flex oscillation is LATENT today; mechanism confirmed by code trace (see finding).

NonKYC (authed REST, read-only; public /market/getlist):
7. **NKC-1 CONFIRMED** — `GET /getorder/UNKNOWN` → HTTP 400, `{code: 20002, message: "Not Found", description: "Order not found"}` → the 503-sentinel order feeds the not-found counter and gets falsely marked LOST/FAILED.
8. **NKC-5 DOWNGRADED to latent** — across all 347 live markets: 0 HB-pair collisions, 0 unsplittable symbols, 0 separator tickers, 0 primaryTicker/prefix mismatches. Also: the live schema has **no `secondaryTicker` field at all** — quote derivation always uses the symbol-split fallback (primary path is dead code).
9. **Field types clean** — `/balances` and `/account/trades` values are all strings (no `Decimal(float)` noise). `since` IS honored (1 vs 75 trades). BUY and SELL fee/notional = 0.20% → quote-denominated both sides, as `_extract_fee_token_and_amount` assumes. `alternateFeeAsset`/`alternateFee` fields exist (null in samples). `/account/orders?status=active` returned all 48 open orders, no pagination fields observed.

---

## 1. Kraken connector (`hummingbot/connector/exchange/kraken/`)

### [KRK-1] Combined ask+bid book diffs drop the second payload dict (bids silently lost) — **CONFIRMED LIVE**
Severity: HIGH | Confidence: CONFIRMED
File: `kraken_api_order_book_data_source.py:156-167`
`_parse_order_book_diff_message` reads only `raw_message[1]`. Kraken WS v1 sends both-side updates as `[ch, {"a":[...]}, {"b":[...],"c":...}, "book-N", "PAIR"]` — two separate dicts. Measured live: ~0.16% of diffs on busy pairs (11 in 90s across 2 pairs). Every occurrence silently drops one side → book drift, stale/ghost bid levels, potentially crossed local books; nothing detects it until the hourly REST snapshot reset (checksum ignored, KRK-12). Directly poisons ladder pricing on Kraken.
Fix: iterate all dict elements between index 1 and -2, merging `a`/`b`/`as`/`bs` before building `msg_dict`; derive `raw_update_id` from the merged set. Test fixtures only cover the single-dict shape — add a dual-dict fixture.

### [KRK-2] `_update_balances` deletes flex-only (`.F`) balances on every second poll — **LATENT (no .F held today)**
Severity: HIGH (latent) | Confidence: HIGH (traced end-to-end)
File: `kraken_exchange.py:699-736`
`remote_asset_names` only ever receives the raw key (`XBT.F`); the fold renames to `BTC` and deletes `.F` but never adds `BTC` to `remote_asset_names`. Flex-only asset (no co-held spot key): poll 2 first double-counts (stale folded value + fresh, via `.get(..., 0) + balance`), then the cleanup deletes the folded key entirely. Balance oscillates present → absent per poll. With ledger-funded budgets this would zero ladder budgets every other cycle. Co-held spot key case is correct. `test_update_balances_folds_flex_without_spot` runs a single poll so this isn't pinned.
Fix: add the folded target name to `remote_asset_names` in both fold loops, and overwrite rather than `+=` against possibly-stale state. Trigger appears the moment funds move into Kraken Earn Flex.

### [KRK-3] REST fill reconciliation queries QueryTrades with an ORDER txid — silent no-op — **CONFIRMED LIVE**
Severity: HIGH | Confidence: CONFIRMED
File: `kraken_exchange.py:614-643`
`_all_trade_updates_for_order` passes the O-prefixed order txid to `/0/private/QueryTrades`, which accepts only T-prefixed trade ids. Live: returns `EOrder:Invalid order` — the exact string the June-2026 whitelist swallows, so every REST fill poll returns "no fills". The entire REST fill-recovery path (including lost-order fills) is dead; fills rely wholly on WS ownTrades + reconnect snapshot replay. Orders closed during extended WS outages complete after `TRADE_FILLS_WAIT_TIMEOUT` with zero executed-amount data.
Fix (validated live): `QueryOrders` with `trades=true` → collect T-ids → `QueryTrades` batched (≤20). Keep the unknown-order whitelist only on the QueryOrders step.

### [KRK-4] Cloudflare-retry on AddOrder can resubmit an already-executed order (duplicate fill risk)
Severity: HIGH | Confidence: MEDIUM
File: `kraken_exchange.py:354-379`
On 5xx/10xx during AddOrder, recovery checks **OpenOrders** by userref; if empty it re-sends AddOrder. If the first request was accepted but no longer rests (MARKET always; aggressive limits often), the retry places a second order → duplicate execution.
Fix: check ClosedOrders too before re-sending; never blind-retry MARKET AddOrder — treat ambiguity as "reconcile via userref".

### [KRK-5] WS `openOrders` delta updates without `userref` are dropped (no exchange-order-id fallback)
Severity: MEDIUM | Confidence: MEDIUM
File: `kraken_exchange.py:596-612`
Minimal status deltas (`{"OGTT3Y-...": {"status": "closed"}}` — the exact shape in the user-stream test fixtures) carry no `userref`; lookup key becomes `""` and the transition is dropped, deferring to the REST poll (up to 120s while WS heartbeats keep the connection "healthy").
Fix: fall back to `all_fillable_orders_by_exchange_order_id` when the userref lookup misses.

### [KRK-6] Order-not-found during status update never classified — phantom orders immortal — **error shape confirmed live**
Severity: MEDIUM | Confidence: HIGH
File: `kraken_exchange.py:146-147, 653-655`
`_is_order_not_found_during_status_update_error` returns False unconditionally, though `_request_order_status` deliberately raises `IOError(f"{ORDER_NOT_EXIST_ERROR_CODE} ...")` (and live-probe shows unknown txids yield an EMPTY result, hitting exactly that raise). `process_order_not_found` is never invoked from the status poll — unknown orders stay in `in_flight_orders` forever, occupying strategy budget/level slots. The two disabled upstream lost-order tests pin this gap.
Fix: return True when the message contains `ORDER_NOT_EXIST_ERROR_CODE` / `EOrder:Invalid order` / `EOrder:Unknown order`; re-enable the disabled base tests.

### [KRK-7] Multi-pair `get_last_traded_prices` matches Ticker-all keys against altnames — **CONFIRMED LIVE**
Severity: MEDIUM | Confidence: CONFIRMED
File: `kraken_exchange.py:752-771`
Ticker responses key by canonical pair name (`XXBTZUSD`) even when queried by altname (`XBTUSD`). The `if symbol in symbol_to_pair` match silently omits legacy pairs in the ≥2-pair path.
Fix: build a canonical-key → altname reverse map from AssetPairs and translate response keys (or query per-pair and match positionally).

### [KRK-8] Rate-limit model ignores Kraken's decay counters and age-based cancel penalties; rate-limit errors non-retryable
Severity: MEDIUM | Confidence: MEDIUM
File: `kraken_constants.py:27-32`, `kraken_utils.py:92-166`, `kraken_exchange.py:319-384`
Fixed 60s token buckets vs Kraken's decaying counter; CancelOrder weighted 1 vs matching-engine penalty up to ~8 for young orders (exactly the ladder refresh-wave pattern). `EAPI:Rate limit exceeded` / `EOrder:Rate limit exceeded` / `EService:Busy` raise immediately → hard order failure on first hit.
Fix: raise cancel weights, cap bursts with a short-interval linked pool, add rate-limit/EService errors to the retryable set with backoff (never blind-retrying AddOrder per KRK-4).

### [KRK-9] AddOrder ambiguity on request timeout not covered by the recovery path
Severity: MEDIUM | Confidence: MEDIUM
File: `kraken_exchange.py:354-381`
Recovery triggers only on the Cloudflare IOError pattern; an `asyncio.TimeoutError` (or `EService:*`) propagates → order marked FAILED while possibly live on the exchange → untracked orphan order.
Fix: run the same userref reconciliation on timeout/EService before declaring failure.

### [KRK-10] Held-funds reconstruction from OpenOrders understates holds vs BalanceEx
Severity: LOW | Confidence: MEDIUM
File: `kraken_exchange.py:668-697`
Limit-only, no fee reserve, non-atomic Balance+OpenOrders pair → slight available overstatement → marginal insufficient-funds rejections at full deployment.
Fix: use `/0/private/BalanceEx` (`hold_trade`) in one call.

### [KRK-11] Fill fee currency hardcoded to quote — **REFUTED LIVE (non-issue)**
File: `kraken_exchange.py:508-522`
Probe: fee/cost = 0.40% on both sides → quote-denominated on this account. Keep as a note: if account fee preference (`fcib`) ever changes, revisit; optionally force `fciq` oflag at placement for determinism.

### [KRK-12] No book checksum validation, no private-feed sequence validation
Severity: LOW→MEDIUM (raised by KRK-1's confirmation) | Confidence: HIGH
File: `kraken_api_order_book_data_source.py:156-175`, `kraken_exchange.py:483-506`
Checksum `c` (present on every diff, verified live) and ownTrades/openOrders `sequence` are both discarded; only self-heal is the hourly snapshot reset.
Fix: validate CRC32 top-10 checksum, disconnect on mismatch (mirrors NonKYC's gap-disconnect pattern); track `sequence` per private channel, resubscribe on gap. This also converts KRK-1 damage from silent to self-healing.

### [KRK-13] `str(Decimal)` emits scientific notation for sub-1e-6 prices/amounts
Severity: LOW | Confidence: HIGH (notation verified; Kraken tolerance unprobed — AddOrder validate=true probe skipped as unnecessary for now)
File: `kraken_exchange.py:298-305`
Fix: serialize with `f"{value:f}"`.

### [KRK-14] 31-bit numeric userref space allows birthday collisions with live orders
Severity: LOW | Confidence: MEDIUM
File: `kraken_exchange.py:239-242` + `connector/utils.py:86-93`
Collision silently overwrites the tracked order (plain dict assignment) → orphaned live order. ~1e-3/day at ladder rates.
Fix: regenerate id when already present in `all_fillable_orders` before tracking.

### Kraken — reviewed and sound
Auth signing/nonce (microsecond, monotonic, race-free; urlencode byte-identical to aiohttp form-encoding); WS token refreshed per (re)subscribe; openOrders snapshot `continue` behavior; ownTrades snapshot replay (dedupe-safe, actually recovers outage fills); symbol mapping incl. XBT/XDG, `.d` dark pools, status filter, legacy allow-list; trading rules (tick/ordermin/costmin); cancel classification; `_api_request_with_retry` semantics; debounced post-fill balance refresh; monotonic book update-id clamp.

---

## 2. NonKYC connector (`hummingbot/connector/exchange/nonkyc/`)

### [NKC-1] "UNKNOWN" exchange-order-id sentinel poisons REST status polling → false LOST/FAILED on a live order — **CONFIRMED LIVE (error shape)**
Severity: HIGH | Confidence: CONFIRMED mechanism; trigger is the rare 503-on-create path
File: `nonkyc_exchange.py:576-583, 1689-1696, 1498-1501`; `core/data_type/in_flight_order.py:365`
After a 503 on createorder the order gets `exchange_order_id="UNKNOWN"`. `_place_cancel` special-cases the sentinel; `_request_order_status` does NOT — it polls `/getorder/UNKNOWN` → 400/20002 "Order not found" (probed), incrementing the not-found counter → ~3 cycles → LOST → FAILED/removed, while WS reports may show it live and filling (WS updates match by `userProvidedId` but can't repair the id: `update_with_order_update` only replaces `None`). Strategy re-places the level → duplicate live exposure; the bulk fill map keyed by "UNKNOWN" drops its fills into the untracked branch where over-claim validation fails → fills skipped, never marked processed.
Fix: in `_request_order_status`, treat the sentinel like a missing id (fall back to client_order_id — `/getorder` accepts it per the cancel-path comment); allow exchange-id overwrite when current value is the sentinel (or normalize sentinel to None at the tracker boundary).

### [NKC-2] `subscribeReports` subscription is fire-and-forget; WS error frames silently swallowed
Severity: MEDIUM | Confidence: MEDIUM
File: `nonkyc_api_user_stream_data_source.py:56-62`; `nonkyc_exchange.py:1262-1288`
Login is ack-validated; subscriptions are not. A rejected reports subscription = no real-time order/fill events with zero diagnostics; if balance frames keep `last_recv_time` fresh, base class selects LONG_POLL_INTERVAL (120s) → fills seen up to 2 min late while everything "looks" healthy. Server error frames (`{"id": N, "error": {...}}`) fall through every listener branch unlogged.
Fix: await/validate the subscribeReports ack like login; add a catch-all WARNING log for any frame with an `error` key.

### [NKC-3] Reorder buffer: stale duplicates inserted into an active buffer can never flush and jam the timeout timer → hair-trigger spurious resyncs
Severity: MEDIUM | Confidence: HIGH (mechanism)
File: `nonkyc_api_order_book_data_source.py:199-205, 238-279`
A `sequence <= last_seq` message can never fill a forward gap, yet the late-arrival branch inserts it into an active buffer; `_flush_reorder_buffer` only pops `last_seq + 1`, so the stale entry keeps the buffer non-empty and `_reorder_timer_start` alive → the next single out-of-order message instantly exceeds the 2s timeout → unsubscribe/resubscribe churn. Stale entries also count toward the 50-entry resync cap. Only the no-active-buffer case is pinned by test.
Fix: drop `sequence <= last_seq` unconditionally; purge `<= last_seq` entries in the flush before the emptiness check.

### [NKC-4] Stale REST `/balances` snapshot can clobber fresher local pre-adjust holds
Severity: MEDIUM | Confidence: MEDIUM
File: `nonkyc_exchange.py:1714-1752` vs `:624-676`
`_update_balances` unconditionally overwrites; only WS balance handlers consult `_pre_adjusted_assets`. An order placed during the REST round trip has its local hold erased until the next WS balanceUpdate / poll — in degraded (REST-only) mode this is a recurring window where co-deployed controllers see inflated available and over-place.
Fix: skip/re-apply the delta for assets whose pre-adjust timestamp is newer than the REST request start.

### [NKC-5] Symbol-map build crashes on duplicate (base, quote); dash tickers corrupt quote derivation — **DOWNGRADED: LATENT (0 occurrences in 347 live markets)**
Severity: LOW (latent; was MEDIUM) | Confidence: HIGH mechanism, live-refuted trigger
File: `nonkyc_exchange.py:1836-1855, 1300`
`bidict` raises `ValueDuplicationError` on duplicate HB pairs — uncaught → symbol map unbuildable → connector dead. Live scan: no duplicates, no separator tickers today; also live schema has no `secondaryTicker` field, so quote ALWAYS comes from the symbol-split fallback (primary path dead code).
Fix (cheap insurance): try/except around the mapping insert (log + keep first); skip/sanitize separator tickers.

### [NKC-6] Market orders: BUY pre-adjust always fails (price None/NaN); market-buy quantity semantics unverified
Severity: LOW | Confidence: HIGH (pre-adjust), LOW (semantics)
File: `nonkyc_exchange.py:649-661, 526-533`
`order.amount * order.price` raises for market orders (caught, logged) → no local hold on any market buy. `quantity` always sent as base amount; if NonKYC market buys expect quote quantity (HitBTC lineage offers both), sizing would be wrong — unpinned.
Fix: guard pre-adjust with a valid-price check; confirm market-buy semantics before any strategy uses MARKET orders here.

### [NKC-7] `_bulk_fills_fetched_this_cycle` lacks try/finally — exception leaves it stuck True
Severity: LOW | Confidence: HIGH
File: `nonkyc_exchange.py:1126-1130`
Fix: wrap in try/finally.

### [NKC-8] Fill poll issues N identical global `/account/trades` requests per cycle
Severity: LOW | Confidence: HIGH
File: `nonkyc_exchange.py:1502-1529`
The code's own comment says the symbol filter is ignored and every per-pair response is the same global list; responses 2..N are dedupe no-ops costing 20 weight each every 10s — meaningful on multi-pair deployments on an exchange with a 429 history.
Fix: fetch once per cycle.

### [NKC-9] Unknown order status strings default to OPEN — a terminal-but-unmapped status strands the order (no lost-order escape since /getorder succeeds)
Severity: LOW | Confidence: MEDIUM
File: `nonkyc_constants.py:78-98`; `nonkyc_exchange.py:1368, 1698-1702`
Fix: WARNING-log unknown statuses in the WS path too; treat repeated unknown REST statuses as reconciliation triggers.

### NonKYC — reviewed and sound
Auth (GET/POST/WS — sorted raw joins byte-identical to yarl, minified-JSON body-equals-signed, thread-locked monotonic nonce); in-session + persisted fill dedupe (airtight across restart); global-trades attribution (orderid-authoritative, 3-day floor, FIFO-bounded persisted dedupe); order-book WS-resubscribe recovery with generation counters and REST-snapshot suppression; timeout-vs-nonce classification; tickers snapshot cache (single-flight + TTL); balance-recheck re-entrancy guard; trading-rule parsing vs pinned live schema.

---

## 3. Classic strategies — cross-exchange / arbitrage family

All in-scope files are byte-identical to `master` — inherited upstream behavior, not fork drift.

### [ARB-1] XEMM: failed/cancelled/expired taker hedge permanently wedges the strategy, maker fill left UNHEDGED
Severity: CRITICAL | Confidence: HIGH (orchestrator-verified)
File: `cross_exchange_market_making.py:614-625` (with `:1789-1803, :463-469, :1725-1727`)
Three compounding defects in `handle_unfilled_taker_order`: (a) the `_ongoing_hedging` entry for the failed taker is never removed → the "retry" finds zero unhedged fills and does nothing; (b) `ready_for_new_trades()` returns False forever → `main()` never runs again (no new orders, no profitability checks, no cancels) — silent permanent wedge with an open unhedged position; (c) the retry passes the TAKER id where a maker id is expected → even if (a) were fixed, `place_order` would crash on `_maker_to_taker_order_ids[maker_order_id]` KeyError after the hedge order is live. Any taker rejection (insufficient funds, min-notional, network) triggers this.
Fix: look up the maker id via `_taker_to_maker_order_ids[order_id]` before deleting; remove the stale `_ongoing_hedging` entry via `.inverse`; pass the maker id; alarm on hedge failure.

### [ARB-2] XEMM: hedge submission has no retry/reconciliation — exceptions, zero-quantized amounts, and partial hedges silently drop fills
Severity: HIGH | Confidence: HIGH
File: `cross_exchange_market_making.py:601-605, 847-859, 885-909, 932-940, 708-722, 775-789`
`check_and_hedge_orders` runs only from events, never the tick loop; a raise (oracle rate None → TypeError; thin book NaN → InvalidOperation) leaves the fill unhedged with only a log. Hedge quantized to zero → INFO only, bot keeps adding maker exposure. Worst: delete-then-filter cleanup in `did_complete_*` erases ALL fill records not attached to an ongoing hedge — including the shortfall of a balance-capped partial hedge — permanently erasing the debt.
Fix: drive unhedged fill records from the tick loop until drained; only remove records for confirmed-hedged quantity; escalate after N failures.

### [ARB-3] XEMM: balance-capped taker BUY hedge ignores the slippage buffer in its own sizing → exchange rejects the order
Severity: HIGH | Confidence: MEDIUM
File: `cross_exchange_market_making.py:932-936` vs `:962-963` (correct pattern at `:1123-1124`)
Amount ≈ `balance/taker_price × 0.995` but priced at `taker_price × 1.05` → requires ~1.045× balance → rejection → triggers ARB-1's wedge.
Fix: divide the balance leg by the slippage-adjusted price (mirror line 1123-1124).

### [ARB-4] spot-perp arb: no failure/cancel handlers — failed leg wedges the state machine with a naked position
Severity: HIGH | Confidence: HIGH
File: `spot_perpetual_arbitrage.py:220-232, 419-424, 526-530`
Only `did_complete_*` handlers exist; a failed/stuck leg leaves state Opening/Closing forever (main() early-returns every tick), one-sided position, no alarm. Also `perp_positions[0]` IndexError-per-tick if the position vanishes while Opened.
Fix: failure/cancel handlers that unwind/alarm + reset; order-age cancel for unfilled taker limits; guard the position indexing.

### [ARB-5] cross_exchange_mining: unbound `taker_price` crashes the rebalance exactly when funds run out
Severity: HIGH | Confidence: HIGH
File: `cross_exchange_mining.pyx:345-377`
Zero taker-side balance → `taker_qty=0` → `taker_price` never assigned → `UnboundLocalError` every tick → the strategy's rebalance/hedge-completion mechanism never executes precisely in the depleted-balance scenario it exists to fix. (Cython — needs rebuild after fix.)
Fix: initialize `taker_price = s_decimal_nan` and gate the maker-branch comparison.

### [ARB-6] XEMM: `UnboundLocalError` on `price_above_bid` when the maker bid book is empty
Severity: MEDIUM | Confidence: HIGH
File: `cross_exchange_market_making.py:1148-1196`
Fix: initialize `price_above_bid = s_decimal_nan` with the other three (ask side is properly guarded — clear oversight).

### [ARB-7] XEMM: NaN hedging price raises `InvalidOperation` in `check_if_still_profitable` → maker orders NOT cancelled while unhedgeable
Severity: MEDIUM | Confidence: HIGH
File: `cross_exchange_market_making.py:1487-1489` (also asserts at `:1081/:1116`, `min()` at `:1091/:1126`)
Thin-but-not-empty taker book → NaN → per-tick crash → the opposite of the intended cancel-on-unprofitable behavior.
Fix: treat NaN like None (cancel); replace the ZeroDivisionError asserts with `return s_decimal_zero`; NaN-guard the `min()`s.

### [ARB-8] amm_arb: no cancelled/stuck-order handling — main task hangs forever on `wait()`
Severity: MEDIUM | Confidence: HIGH
File: `amm_arb.py:289-329, 445-488`; `amm_arb/data_types.py:169-171`
No `did_cancel_order` handler, no timeout on `completed_event.wait()` → strategy dead until restart, possibly with one leg filled. `_order_id_side_map` grows unboundedly.
Fix: treat cancellation as completion; timeout + cancel around waits; notify on abandoned legs.

### [ARB-9] liquidity_mining: one NaN mid-price poisons the volatility window → tick dead for up to ~50 min
Severity: MEDIUM | Confidence: HIGH
File: `liquidity_mining.py:560-587, 130-138, 283-301`
NaN appended to `_mid_prices` on empty book → `max/min` raise `InvalidOperation` in `update_volatility` every tick until the sample ages out (300×10s defaults) → live orders unmanaged the whole time.
Fix: skip NaN appends; skip NaN markets in `create_base_proposals`.

### [ARB-10] hedge: `zip(markets, offsets)` silently drops trading pairs when fewer offsets than markets
Severity: MEDIUM | Confidence: HIGH
File: `hedge/start.py:25-31` (prompt contradiction: `hedge_config_map_pydantic.py:53-55`)
Excess markets are dropped from monitoring entirely (unhedged exposure), though the config prompt invites exactly that input. `offsets_dict` keyed by connector name can also overwrite when hedge connector doubles as a monitored connector.
Fix: pad offsets with zeros to len(markets); key by (connector, slot).

### [ARB-11] spot-perp arb: one-shot startup gating permanently disables trading after a transient startup condition
Severity: MEDIUM | Confidence: HIGH
File: `spot_perpetual_arbitrage.py:142-190, 247-248`
`_trading_started` set before the budget check; a False budget check leaves `_ready_to_start` False with all gates latched → main() never scheduled; funding later doesn't recover. Gathered prices not validated for Exceptions/NaN. Latent `perp_side != cur_perp_pos_is_buy` object-vs-bool comparison.
Fix: re-run gating each tick until it passes; validate gathered prices; `perp_side.is_buy != ...`.

### [ARB-12] liquidity_mining: leftover open orders at start → budget allocation re-runs every tick, wiping fill-adjusted budgets
Severity: MEDIUM | Confidence: MEDIUM
File: `liquidity_mining.py:116-128, 275-278`
`_ready_to_trade` stays False while the bot trades anyway; `create_budget_allocation()` re-runs per tick, wiping `did_fill_order`'s budget adjustments; a lost cancel on a restored order leaves it live and untracked.
Fix: return from tick while not ready (re-issuing cancels); allocate budgets once on the False→True transition.

### [ARB-13] cross_exchange_mining: ZeroDivision fallback queries the WRONG exchange's book; eager untrack can leak live orders; multi-pair `limit_orders[0]` bug
Severity: LOW | Confidence: HIGH
File: `cross_exchange_mining.pyx:262-271, 323-330, 250-253, 568-573, 418-425`
Fix: skip the tick side on maker-book ZeroDivision; rely on cancelled events instead of immediate `stop_tracking_limit_order`; index limit orders by market pair.

### [ARB-14] XEMM: broken `stop_tracking_limit_order` override (self-recursion with wrong args), dead config properties, unbounded tracker growth
Severity: LOW | Confidence: HIGH
File: `cross_exchange_market_making.py:1739-1745, 180-190, 224-225`; `order_id_market_pair_tracker.pyx:72-85`
Fix: `super().stop_tracking_limit_order(...)`; hook order-termination events to `stop_tracking_order_id`; delete/fix dead properties.

### [ARB-15] hedge: zero-amount closing candidate submitted (perp HEDGE mode only); unguarded mid-price division
Severity: LOW | Confidence: MEDIUM
File: `hedge/hedge.py:546-559`
Fix: `amount > 0` check before appending; skip cycle with warning on NaN/zero mid.

### [ARB-16] liquidity_mining start: mixed token-position market lists silently truncated
Severity: LOW | Confidence: HIGH
File: `liquidity_mining/start.py:14-17`
Fix: warn/reject when markets are excluded by token-position selection.

### Arb family — reviewed and sound
`maker_taker_market_pair.py`, `market_trading_pair_tuple.py`; amm_arb profit/budget/slippage math (fails safe); spot-perp `arb_proposal.py`; hedge tick sequencing + spot-path sizing; liquidity_mining fill-budget accounting; XEMM pending-create bookkeeping; `order_tracker.pyx` in-flight-cancel invariants.
Cython note: only `cross_exchange_mining.pyx` and the two `order_id_market_pair_tracker.pyx` need rebuild; XEMM main strategy, hedge, liquidity_mining, amm_arb, spot-perp are pure Python.

---

## 4. Classic strategies — market-making family

Context: the clock catches per-iterator exceptions, so a raise in `c_tick` silently aborts the REST OF THAT TICK (usually cancel wave + creation) while live orders stay on the exchange. Verified: ordered comparisons on `Decimal("NaN")` raise `InvalidOperation`; `math.ceil(Decimal("NaN"))` raises ValueError.

### [PMM-1] Decimal-NaN price aborts the PMM tick before the cancel wave when optional features are enabled
Severity: HIGH | Confidence: HIGH
File: `pure_market_making.pyx:850-862, 890-918, 939-945`; `hanging_orders_tracker.py:250`; `moving_price_band.py:72,80`
With price bands / moving price band / inventory skew / hanging orders enabled, a NaN `get_price()` (one-sided book, reconnects) raises before `c_cancel_active_orders` → quotes sit stale at dislocated prices every tick until the book recovers. Vanilla PMM is guarded (`is_nan()` at :790/:805/:813).
Fix: early NaN gate at top of tick (cancel-all-and-return), or guard each comparison site. Cython rebuild required.

### [PMM-2] Avellaneda: one NaN mid-price sample poisons the volatility buffer → quoting frozen ~200s per incident
Severity: HIGH | Confidence: HIGH
File: `avellaneda_market_making.pyx:674-675, 734, 755-766`
NaN enters `_avg_vol`; `vol != 0` passes for NaN; `max(NaN, …)` raises in reservation-price calc every tick until the sample leaves the 200-sample buffer; tick aborts before the cancel wave.
Fix: skip `add_sample` on NaN; NaN-guard before reservation-price math. Cython rebuild required.

### [PMM-3] Order optimization: `ceil(NaN)` ValueError when book depth < optimization depth — persistent tick abort on thin books
Severity: HIGH | Confidence: HIGH
File: `pure_market_making.pyx:999-1006, 1022-1029`; `avellaneda_market_making.pyx:1044-1051, 1062-1069`; `perpetual_market_making.py:785-792`
`get_price_for_volume` returns NaN on a **thin but non-empty** book (chronic on small pairs). Avellaneda ships `order_optimization_enabled=True` by default.
Fix: skip optimization for the side when `result_price.is_nan()`.

### [PMM-4] Hanging-orders pair list not cleared when both sides fill → mis-indexed pairs, orders silently never hang
Severity: MEDIUM | Confidence: HIGH
File: `pure_market_making.pyx:1273-1299`; `hanging_orders_tracker.py:354-360`; same in avellaneda `:1297-1323`
Fix: clear `current_created_pairs_of_orders` at the start of every `c_execute_orders_proposal`.

### [PMM-5] PMM misclassifies hanging-order fills under nondeterministic listener order (Avellaneda has the dual-check fix; PMM doesn't)
Severity: MEDIUM | Confidence: MEDIUM-HIGH
File: `pure_market_making.pyx:1093-1106, 1133-1146` vs avellaneda `:1174-1175`
Fix: port Avellaneda's `is_order_id_in_hanging_orders or is_order_id_in_completed_hanging_orders` check.

### [PMM-6] InventoryCostPriceDelegate: `DivisionByZero` uncaught (only `InvalidOperation`) — inventory_cost mode wedges after base fully sold
Severity: MEDIUM | Confidence: HIGH
File: `inventory_cost_price_delegate.py:31-35, 69`
Fix: catch `(InvalidOperation, DivisionByZero, ZeroDivisionError)` or return None on zero base_volume.

### [PMM-7] Deep buy levels can go negative-priced; budget constraint then INCREASES quote budget
Severity: MEDIUM-LOW | Confidence: HIGH mechanism
File: `pure_market_making.pyx:805-812, 939-953`
Fix: require `size > 0 and price > 0` in the level loops (override branch already does).

### [PMM-8] `should_wait_order_cancel_confirmation` passes the ConfigVar object (missing `.value`) — setting false silently ignored
Severity: MEDIUM-LOW | Confidence: CONFIRMED (orchestrator-verified)
File: `pure_market_making/start.py:100`
Always-truthy → always waits (fail-safe direction, dead knob).
Fix: append `.value`.

### [PMM-9] `split_order_levels` + order optimization: spread-list indices misalign after ping-pong/band/quantization drops levels
Severity: MEDIUM-LOW | Confidence: MEDIUM
File: `pure_market_making.pyx:1012-1017, 1035-1040`
Fix: carry the original level index on PriceSize.

### [PMM-10] Exception in strategy completion handler skips tracker cleanup → filled order tracked forever, strategy stops quoting
Severity: LOW | Confidence: MEDIUM
File: `strategy_base.pyx:34-43`
Listener calls user-facing handler (notify/log — MQTT notifier is the plausible raiser on this fork) before tracker cleanup; PubSub catches per-listener.
Fix: cleanup first, or try/except around the strategy-side handler.

### [PMM-11] Bare `next()` + clock treats StopIteration as fatal — latent bot-wide kill switch
Severity: LOW | Confidence: MEDIUM
File: `pure_market_making.pyx:707, 1274, 1297`; `avellaneda_market_making.pyx:580, 1298, 1321`; `clock.pyx:120-122`
An escaping StopIteration makes `Clock.run_til` RETURN — halting ticks for every connector and strategy in the process.
Fix: `next((...), None)` — guards already exist after each site.

### [PMM-12] Avellaneda replaces HangingOrdersTracker on config toggle without unregistering the old one
Severity: LOW | Confidence: MEDIUM-HIGH
File: `avellaneda_market_making.pyx:393-399`
Fix: unregister + migrate before replacing.

### [PMM-13] Timeframe execution: no `end > start` validation; overnight daily windows never trade
Severity: LOW | Confidence: HIGH
File: `conditional_execution_state.py:93, 115-118`
Fix: model validator + wrap-around support or explicit rejection.

### [PMM-14] Perpetual MM: stop-loss renewal compounds the slippage buffer geometrically (dormant on this spot-only fork)
Severity: LOW | Confidence: MEDIUM-HIGH
File: `perpetual_market_making.py:603-612, 626-629` (also swapped top_ask/top_bid names at 588-589; early-return skips sell-side closes at 951-952)
Fix: recompute from the entry-derived stop price each renewal.

### [PMM-15] `is_hanging_order_in_strategy_active_orders`: `all()` called with 4 positional args (dead code)
Severity: LOW | Confidence: HIGH
File: `hanging_orders_tracker.py:273-277`
Fix: tuple-wrap or delete.

### MM family — reviewed and sound
`order_tracker.pyx` cancel dedupe/expiry; budget-constraint balance walk (positive prices); `c_filter_out_takers` NaN guards; timers; `inventory_skew_calculator.pyx`; ring buffer / instant volatility; price delegates; config validators (spread/refresh bounds); `order_age` µs→s consistency.
Cython rebuild required for: pure_market_making, avellaneda, order trackers, strategy_base, ring_buffer, trading_intensity, delegates, clock/pubsub (if touched). Pure Python: hanging_orders_tracker, conditional_execution_state, moving_price_band, inventory_cost delegate, perpetual_market_making, start.py files.

---

## 5. V2 directional controllers (`controllers/directional_trading/`)

Verified empirically against installed pandas 3.0.1 / pandas_ta 0.4.71b0 / talib 0.6.8.

### [DIR-1] bollingrid is completely dead: pandas_ta 0.4.x renamed BB columns and ignores `std=`
Severity: HIGH | Confidence: HIGH (empirically verified)
File: `bollingrid.py:94-96`
`bbands(std=...)` kwarg silently ignored (always 2.0σ); columns are now `BBP_100_2.0_2.0` → lookup KeyErrors every tick → never trades + ERROR spam. Siblings were migrated upstream (commit 2d51a60fa); bollingrid was added later and missed.
Fix: `bbands(length=..., lower_std=bb_std, upper_std=bb_std)` + new column names (mirror bollinger_v1).

### [DIR-2] Base class: cooldown vacuous once same-side executors close → immediate loss-reentry churn
Severity: HIGH | Confidence: CONFIRMED (orchestrator-verified)
File: `hummingbot/strategy_v2/controllers/directional_trading_controller_base.py:189-195`
Cooldown measured only against ACTIVE executors' creation timestamps; after a stop-loss closes, `max_timestamp=0` → re-entry next tick while the signal persists (signals hold for a whole candle). The fork's two controllers explicitly work around this locally (`_last_same_side_reference_ts`) — hoist that into the base. Affects all 7 upstream controllers.

### [DIR-3] Base class: side filter matches ALL executors for sell signals (operator precedence)
Severity: MEDIUM | Confidence: CONFIRMED (orchestrator-verified)
File: `directional_trading_controller_base.py:191`
`(x.side == TradeType.BUY if signal > 0 else TradeType.SELL)` — for sell signals evaluates to the truthy enum → matches every active executor. `max_executors_per_side` becomes "max total" for shorts; cooldown keys off either side. Fails conservative but silently distorts mixed-direction strategies.
Fix: `x.side == (TradeType.BUY if signal > 0 else TradeType.SELL)`.

### [DIR-4] ema_regime_hold_v1: max_records 3000@4h / 6000@5m can block `ready` for EVERY controller in the process + REST hot loop
Severity: HIGH | Confidence: MEDIUM (mechanism verified; trigger depends on pair history depth)
File: `ema_regime_hold_v1.py:77-81`
`CandlesBase.ready` requires the deque literally FULL; `MarketDataProvider.ready` ANDs all feeds and gates every controller's control_task. 3000 4h bars ≈ 500+ days; if history is shallower, `fill_historical_candles` spins with no sleep at throttler rate forever while the whole bot never trades.
Fix: size max_records to actual warm-up (~250-400 bars @4h; ~300+margin @5m); shrink request when exchange history is younger than the span.

### [DIR-5] ai_livestream: ML signal never expires; listener init is one-shot; KeyError spam pre-first-message
Severity: MEDIUM | Confidence: HIGH
File: `ai_livestream.py:30-58`
Stale ±1 signal keeps opening executors every cooldown indefinitely if the publisher dies; MQTT-listener creation failure is swallowed with no retry (silent never-trades); `processed_data` starts `{}` → per-tick KeyError until first message.
Fix: max-age on the signal; lazy listener retry; initialize `processed_data = {"signal": 0}`.

### [DIR-6] dman_v3: BB-width dynamic scaling degenerates on flat windows — stop-loss silently DISARMED, trailing becomes instant-close
Severity: MEDIUM | Confidence: HIGH mechanics
File: `dman_v3.py:172-197`
`BBB→0` ⇒ `stop_loss=0` (falsy → DCA executor skips SL entirely) and `TrailingStop(0,0)` (activates on first uptick, closes on first downtick). Doc/code 2× mismatch on the ÷200 semantics. Exactly the flat/thin regime this fork trades.
Fix: floor the multiplier; skip create when BBB is 0/NaN; fix the docstring.

### [DIR-7] dman_v3: configured `take_profit` silently ignored (not passed into DCAExecutorConfig)
Severity: MEDIUM | Confidence: HIGH
File: `dman_v3.py:198-211`
Operator sets TP 2% (prompted, defaulted) — never armed; only trailing/SL/time-limit exit.
Fix: pass `take_profit` through (scaled when dynamic), or hide the prompt.

### [DIR-8] dman_v3: validators miss sum-zero DCA amounts (per-create DivisionByZero) and None stop_loss under dynamic_target (TypeError)
Severity: LOW | Confidence: HIGH
File: `dman_v3.py:96-109, 133-135, 187-188`
Fix: `sum(dca_amounts_pct) > 0`, spreads > 0, None-guard the dynamic branch.

### [DIR-9] bollinger_v2: leftover debug — 15-row DataFrame logged at INFO every second (+ duplicate BB computation)
Severity: LOW | Confidence: HIGH
File: `bollinger_v2.py:111-114`
Fix: delete the log block and the unused pandas_ta call.

### [DIR-10] Upstream controllers default to interval "3m" — unsupported on both NonKYC and Kraken feeds
Severity: LOW | Confidence: HIGH
File: bollinger_v1/v2, bollingrid, dman_v3, macd_bb_v1, supertrend_v1 (defaults)
Accepting prompt defaults on this fork's exchanges → feed skipped by the hardened initialize_candles → `get_candles_df` raises per tick, never trades. Omitting `candles_connector`/`candles_trading_pair` keys entirely also bypasses the mode="before" validators (default None fails CandlesConfig).
Fix: default to 5m on this fork; validate interval against the feed's supported set.

### [DIR-11] dman_v3 stamps configs with `time.time()` instead of provider time — backtest fill misalignment
Severity: LOW | Confidence: MEDIUM
File: `dman_v3.py:199`
Fix: `self.market_data_provider.time()`.

### [DIR-12] bollinger_v1/bollingrid/dman_v3: `max_records = bb_length` leaves zero warm-up margin (works today, one dropped candle from permanent NaN/no-trade)
Severity: LOW | Confidence: HIGH
Fix: `bb_length + 20` like macd_bb_v1.

### Directional — reviewed and sound
`mean_reversion_bb_rsi_v1.py` (fork) — strongest file in the directory (NaN-gated entries, close-timestamp cooldown, daily cap, fail-closed spread gate). `ema_regime_hold_v1.py` (fork) — sound apart from DIR-4. `ta_utils.py`, macd_bb_v1, supertrend_v1, bollinger_v1 signal logic and column names verified. ai_livestream MQTT plumbing survived the aiomqtt migration. Base-class sizing math all-Decimal.

---

## 6. V2 generic controllers (`controllers/generic/` + `pmm_simple`)

Key cross-cutting fact (verified): the orchestrator budget preflight **skips every config without an `.amount` attribute** — Grid/XEMM/Arbitrage executor configs bypass the amount>0 and price-available guards entirely; their only backstop is the executor's own balance validation.

### [GEN-1] stat_arb: `get_spread_and_z_score()` returns None on three paths → per-tick TypeError, global TP/SL silently disarmed
Severity: HIGH | Confidence: HIGH
File: `stat_arb.py:247` (bare returns at `:345-347, :355-358, :397-399`; SL gate at `:116-120`)
Empty candles / short lookback / zero spread-std → unpack TypeError every tick → `determine_executor_actions` (which contains the global position TP/SL) never runs during a candles outage — risk management dies exactly when needed.
Fix: return `(None, None)` from early exits; keep the global TP/SL check running even when the signal is unavailable.

### [GEN-2] stat_arb: no in-flight guard on position-reduce paths → duplicate full-size market closes every tick
Severity: HIGH | Confidence: HIGH
File: `stat_arb.py:116-151, 222-239`
Reduce actions sized `position.amount` re-emit each tick until fills settle into the ledger (seconds on poll-based fills) → over-close and position flip. Stop-filter also re-sends StopExecutorAction for terminated executors.
Fix: subtract active CLOSE order-executor amounts from the gap (or in-flight flag per position).

### [GEN-3] pmm_mister: fabricated `Decimal("100")` reference-price fallback + unbounded stale-price quoting
Severity: HIGH | Confidence: HIGH (path), MEDIUM (hit-probability)
File: `pmm_mister.py:451-460`
On price-unavailable before first success, quotes a full LIMIT_MAKER ladder around literal 100 and sizes `amount_quote/100`; during outages keeps quoting a frozen price with no staleness bound.
Fix: skip the cycle on price-unavailable; bound previous-price age.

### [GEN-4] pmm_mister: `global_stop_loss`/`global_take_profit` are display-only; hanging flow strips TPs → positions accumulate with NO exit
Severity: HIGH | Confidence: CONFIRMED (orchestrator-verified: only usages are config + format_status/chart)
File: `pmm_mister.py:55-56, 823-829, 1022-1027, 426-442, 114-127`
Per-executor barriers omit stop_loss; effectivization cancels the TP after 120s converting to held position; the "global" SL/TP is never enforced — zero loss-cutting while the dashboard displays SL as armed. Default `take_profit=0.0001` is below round-trip maker fees (structurally negative).
Fix: implement global TP/SL against positions_held (with a GEN-2-style in-flight guard) or delete the fields/status lines.

### [GEN-5] xemm_multiple_levels: default config arms level 1 with a 0% profitability floor (market-order taker leg)
Severity: HIGH | Confidence: HIGH
File: `xemm_multiple_levels.py:181-182, 202-203` (defaults `:31-46`)
`min_profitability = target - config.min_profitability = 0.003-0.003 = 0` — maker rides to 0% then hedges via MARKET; slippage beyond top-of-book = realized loss. The field named min_profitability is actually a band width.
Fix: explicit absolute floor field + validation `min(level targets) > floor`.

### [GEN-6] multi_grid_strike: terminated executor blocks its grid's respawn until GLOBAL archival; stops re-sent to terminated executors
Severity: HIGH (silent stop-trading) | Confidence: HIGH
File: `multi_grid_strike.py:103-146, 179-183`
`get_executor_by_grid_id` returns the corpse (executors_info retains done executors until they fall out of the newest-100 across ALL controllers) → grid never respawns on quiet deployments; disabled-grid path sends StopExecutorAction to terminated executors.
Fix: require `executor.is_active` in the lookup; prune mapping on termination; guard the stop path.

### [GEN-7] xemm_multiple_levels: per-level active check builds a list of booleans — levels never replenish independently
Severity: MEDIUM | Confidence: HIGH
File: `xemm_multiple_levels.py:176-178, 198-199`
Comprehension maps instead of filtering → `len(...)==0` only when NO active executors → ladder decays 3→2→1→0 then respawns in batch; one never-filling level suppresses the whole side.
Fix: filter — `[e for e in xs if e.config.target_profitability == target]`.

### [GEN-8] multi_grid_strike: editing an enabled grid's parameters is detected but never applied
Severity: MEDIUM | Confidence: HIGH
File: `multi_grid_strike.py:126-136` (updatable fields `:18-23`)
Hot-editing start/limit prices on a live deployment silently does nothing (change handler only stops REMOVED/disabled grids).
Fix: per-grid parameter hash; stop the active executor on change so the create path re-issues.

### [GEN-9] Grid controllers pass unvalidated geometry into GridExecutorConfig (no validators anywhere) + preflight bypass
Severity: MEDIUM | Confidence: HIGH
File: `grid_strike.py:30-32, 83-104`; `multi_grid_strike.py:15-23, 147-168`; `quantum_grid_allocator.py:400-466`; `grid_executor/data_types.py:13-38`
No `start < end`, `start > 0`, or limit-side checks: `start=0` → ZeroDivisionError at construction; inverted range → silent single-level "grid" with negative step; wrong-side limit → instant limit-breach stop per creation. `amount_quote_pct` never validated to sum ≤ 1; grid configs skip the budget preflight (no `.amount`).
Fix: pydantic validators on controller configs and ideally GridExecutorConfig itself.

### [GEN-10] grid_strike: zero-cooldown re-entry after termination → stop-out/re-enter loop around limit price
Severity: MEDIUM | Confidence: HIGH
File: `grid_strike.py:79-105`
Fix: re-entry cooldown keyed on last termination + close_type; max-consecutive-stop-out breaker.

### [GEN-11] quantum_grid_allocator: `dynamic_grid_range` NaN/None path — empty-df guard insufficient
Severity: MEDIUM | Confidence: HIGH (verified `Decimal(np.nan)` → `Decimal('NaN')` → executor ValueError)
File: `quantum_grid_allocator.py:128-135, 302-303`
1 ≤ rows < bb_length → `ta.bbands` returns None → per-tick TypeError; NaN BBB → NaN grid prices → create fails every cycle while positions may be held. Opt-in flag.
Fix: fall back to `config.grid_range` when bb is None or width not finite/positive.

### [GEN-12] arbitrage_controller: stats over archival-coupled buffer; FAILED executors count; NaN rate passes `if not rate`; shared cooldown stalls both directions
Severity: MEDIUM | Confidence: HIGH
File: `arbitrage_controller.py:113-127, 148-156, 172-187`
Default `max_executors_imbalance=1` + one persistently profitable direction = full-strategy stall until archival flushes. `Decimal('NaN')` passes the falsy check; configs bypass preflight (no connector_name/amount) → never-trading executor blocks both directions.
Fix: filter by close_type COMPLETED + filled>0; keep imbalance in controller state; `rate.is_finite() and rate > 0` + quantized amount > 0 guards.

### [GEN-13] hedge_asset: cooldown is the only double-hedge guard (10s default vs fill-settle latency); hardcoded `-USDC` market registration
Severity: MEDIUM | Confidence: HIGH (code), MEDIUM (frequency)
File: `hedge_asset.py:46-49, 91-125`
Fix: deduct in-flight hedge executor amounts from the gap; configurable reference pair (a nonexistent pair blocks connector readiness for the whole bot).

### [GEN-14] pmm_v1: refresh-tolerance compares mismatched lists → tolerance feature always refreshes multi-level configs; outage cancels all aged orders
Severity: LOW | Confidence: HIGH
File: `pmm_v1.py:568-591, 621-644, 212-224`
Fix: per-level comparison; skip refresh pass when reference_price ≤ 0.

### [GEN-15] xemm/arb misc: `time.time()` for sell configs (backtest-hostile), zero-sum level amounts → per-tick DivisionByZero, both legs sized from maker mid
Severity: LOW | Confidence: HIGH
File: `xemm_multiple_levels.py:206, 168-169, 180/201, 191/212`

### [GEN-16] examples/: unconditional periodic market orders — keep out of production deploy lists
Severity: LOW | File: `examples/basic_order_example.py:34-48`, `examples/full_trading_example.py:59-132`

### [GEN-17] lp_rebalancer: failure-terminated executor recreates at full configured size (Gateway CLMM only — out of stack)
Severity: LOW | File: `lp_rebalancer/lp_rebalancer.py:222-244`

### Generic — reviewed and sound
pmm_simple (thin, correct); grid_strike single-executor lifecycle; arbitrage/xemm gas-token init inert on this stack; xemm/grid executor fork hardening confirmed in place; pmm_v1 fill-detection semantics + inventory-skew port; quantum allocator portfolio math (validators, guards, one-grid-per-asset); hedge_asset side mapping; example monitors read-only.

---

## 7. Suggested fix priority (if/when a fix pass is commissioned)

**Tier 1 — production now (Kraken + NonKYC connectors):**
KRK-1 (merge dual-dict book payloads) · KRK-3 (fill recovery via QueryOrders trades=true) · KRK-12 (checksum → detect KRK-1-class drift) · KRK-6 (classify not-found) · NKC-1 (UNKNOWN sentinel) · KRK-4/KRK-9 (AddOrder ambiguity) · NKC-2 (subscription ack) · NKC-3 (reorder-buffer stale entries) · KRK-2 (flex fold — before using Kraken Earn Flex) · NKC-8 (single global trades fetch)

**Tier 2 — cheap base-class/latent fixes with broad protection:**
DIR-2 + DIR-3 (one-liners in directional base) · PMM-11 (`next(..., None)` — bot-wide kill switch) · NKC-5/NKC-7 (insurance) · KRK-13/KRK-14

**Tier 3 — before deploying the specific strategy:**
XEMM classic: ARB-1/2/3/6/7 (do NOT run classic XEMM before these) · spot-perp: ARB-4/11 · cross_exchange_mining: ARB-5 · liquidity_mining: ARB-9/12 · PMM w/ optional features: PMM-1/3/4/5/6 · Avellaneda: PMM-2/3 · grid family: GEN-6/8/9/10/11 · xemm_multiple_levels: GEN-5/7/15 · stat_arb: GEN-1/2 · pmm_mister: GEN-3/4 · dman_v3: DIR-6/7/8 · bollingrid: DIR-1 · ema_regime_hold: DIR-4

Note: fixes to `.pyx` files (pure_market_making, avellaneda, cross_exchange_mining, order trackers, clock) require `python setup.py build_ext --inplace` locally; Docker builds recompile from source. Images must be rebuilt for any fix to reach the pods.

---

## 8. Production instance log review (2026-07-14, addendum)

Source: `instances/KRAKEN_LADDER_V1-20260712-2302-*.zip` and `instances/NONKYC_LADDER_V1-20260713-0657-*.zip` — ~38h of live logs each (2026-07-13 ~05:00 → 2026-07-14 ~19:00), 2 Kraken ladder controllers (XMR/USD, XPL/USD) + 4 NonKYC ladder controllers (XMR, DASH, BELLS, SUN vs USDT).

**Overall health: good.** Zero order failures, zero insufficient-funds events, zero unhandled tick exceptions, all WS/DNS outages recovered automatically, live config hot-reload of the Kraken XMR controller worked (orders cancelled + rebuilt), fixes-V1 machinery visibly working (60 `watchdog_suppressed_fully_deployed` events on NonKYC XMR = converged-gate; no watchdog spin). Fill activity: NonKYC 29 fills (XMR 16, DASH 7, BELLS 6, SUN 0), Kraken ~12 (XMR 12, XPL 0).

### [LOG-1] Kraken private REST throttler pegged 24/7 — and half of it is the dead QueryTrades path (KRK-3)
Severity: HIGH (operational) — 1,368 `QueryTrades` + 1,149 `QueryOrders` "limit almost reached (34/35 per 60s)" warnings, ~100/hour around the clock; `/0/public/Ticker` (1/s) pegged too (909). Every QueryTrades call returns nothing (KRK-3, live-confirmed): half the private budget is burned on structurally dead calls, and open-order capacity is capped near ~35 before the poll sweep exceeds a minute. Fixing KRK-3 via `QueryOrders?trades=true` removes the entire QueryTrades sweep AND restores REST fill recovery in one change. Also note 105 cancels/38h on Kraken — fine today, but Kraken's young-order cancel penalty (KRK-8) makes cancel-heavy waves the next ceiling when scaling.

### [LOG-2] Two orphan orders from the 2026-07-11 session held ~20.5 USDT hostage for 30+ hours on NonKYC
Severity: HIGH (operational) — Post-reconnect reconciliation flagged orphans `6a4c528f…`/`6a4c527f…` (ObjectId timestamps = 2026-07-11, the *previous* deployment) at 07-13 08:31 with "cancel manually"; they were still active at the last reconciliation 30+ hours later. Their held USDT is exactly the chronic "possible ledger understatement" all four controllers report (identical 20.51 across three controllers simultaneously at 07-13 05:47) — dead capital plus warning noise. Kraken shows the same signature on XPL (21.8 → 9.6 USD unattributed). ACTION: cancel the two NonKYC orders manually; audit Kraken for leftover/manual orders. Improvement: auto-cancel (or one-keystroke cancel) for orphans carrying our own HBOT userProvidedId prefix; escalate the orphan log from once-per-reconnect DEBUG "known orphan(s)" back to a periodic WARNING while they persist.

### [LOG-3] NKC-4 (REST-balance vs pre-adjust race) empirically confirmed in production
Severity: MEDIUM — 7 "Large balance mismatch detected (1 assets)" warnings, each timestamped the same millisecond as an order placement + local pre-adjust; the REST snapshot from before the order lands after the hold is applied. The second-pass reconciliation self-heals every time, so today it is noise + a brief wrong-balance window — but it is exactly the NKC-4 mechanism; fixing NKC-4 eliminates the class.

### [LOG-4] Recurring host-level DNS outages hitting both pods
Severity: MEDIUM (infrastructure, not code) — name-resolution failure bursts at 07-13 11:02 and 21:03 (NonKYC), 07-14 04:14–04:16 (NonKYC, ~24 events) and 07-14 11:02:00 (Kraken, 18 events in one second); the same windows break the rate-oracle sources (Binance errors, CoinGecko 429 retries). Both bots recovered unaided each time (user-stream retry loop, WS resubscribe, order-status warnings only). Fix on TrueNAS: local caching resolver / secondary nameservers for the pods. Most observed WS reconnects trace to these windows.

### [LOG-5] Structured-events and errors log routing is dead in the log template (v13)
Severity: LOW-MEDIUM — `structured_file_handler` (→ `logs/structured_events.jsonl`) is defined but attached to no logger, and `[STRUCTURED_EVENT]` payloads are emitted through connector/controller loggers into the main+forensic logs; `structured_events.jsonl` is 0 bytes on both instances. `errors.log` has no handler at all (also 0 bytes despite 20 ERRORs). Fix `hummingbot_logs.yml` template: attach the structured handler to the loggers that emit `[STRUCTURED_EVENT]` (or route emissions through `hummingbot.structured_events`), and add an ERROR-level file handler.

### [LOG-6] Kraken XMR regime flapping: 133 transitions, median 12s apart
Severity: LOW-MEDIUM — perfectly symmetric `between_ladders ↔ inside_sell_band` (33/33) and `↔ inside_buy_band` (33/33); price parked exactly at the first sell level (331.5). No hysteresis on regime detection: every sub-tick oscillation across a band edge logs a regime change and re-evaluates the plan (XMR-Kraken: 125 creates vs 12 fills, ~10:1 churn; XMR-NonKYC 268:16). Add a dwell-time or penetration deadband before switching regimes, and debounce the diagnostic event.

### [LOG-7] Dead capital: SUN (NonKYC) and XPL (Kraken) took zero fills in 38h
Severity: LOW (capital efficiency) — SUN placed only 12 orders (2 sell levels filtered "not passive", plan budget shaved 4×); XPL similar (16 creates, 1 min-notional compression). Bands may sit too far from traded range, or the pairs are too quiet for the configured shape. Consider re-banding or reallocating the ~$470 combined seed.

### [LOG-8] Minor / working-as-designed observations
- XMR-NonKYC under-deployment watchdog fired 11× (~every 3.5h); each firing = up to ~5min of one fundable level idle before the rescue wave. No spinning (fixes-V1 backoff visible). Candidate refinement: trigger the balance-refresh + re-propose immediately on ledger update instead of waiting out the 300s window.
- NonKYC REST latency: createorder avg 500–738ms (peak 1.9s), cancelorder avg ~720ms (peak 1.4s) — refresh waves are seconds long serially; relevant when scaling level counts.
- Dynamic fee detection reports real NonKYC maker fee 0.2% vs configured default 0.15% — runtime uses the dynamic rate (correct); keep offline/backtest math at 0.2%.
- One-shot benign noise on Kraken: SN75USD new-listing KeyError (base `_update_trading_rules` formats rules *before* refreshing the symbol map — reorder to kill this class), one invalid-nonce (self-healed by retry), one "waiting for exchange order id" fill-poll skip.
- MQTT bridge: NonKYC initial connect timed out (broker not ready at bot start), connected 11s later, publisher re-armed; one later reconnect. Kraken connected first try. aiomqtt migration behaving.
- XMR-NonKYC ledger "over-claim" warning (07-14 13:35) was a transient ordering artifact — WS balance delta arrived before the fill hit the ladder ledger; self-healed in 1s. Consider a short settle-grace before that warning fires.
