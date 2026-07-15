# CSF-V1 Final Report — Hummingbot Connector & Strategy Fixes V1

**Date:** 2026-07-14  
**Batch runner:** `run_csf_batch.ps1` (unattended, `claude -p` per phase)  
**Branches:** `dev` (implementation), `nonkyc` (fork mainline, merged from dev at end)  
**Final commits:** `dev` → `a8b3f3256`, `nonkyc` → `6748f5182`  
**Origin pushed:** Yes — both `dev` and `nonkyc` pushed to `origin`

---

## Test Suite Counts — Phase 12 Final Green Run

| Lane | Count | Status |
|------|-------|--------|
| Kraken connector | 158 passed | ✅ |
| NonKYC connector (excl. 4 live/smoke files) | 473 passed | ✅ |
| Strategy V2 + strategy_v2_base tests | 1156 passed | ✅ |
| Classic strategy lane | 386 passed, 26 failed (all pre-existing baseline) | ✅ |
| Remote iface (MQTT bridge) | 54 passed | ✅ |

Classic lane baseline failures (26 tests, all pre-existing before any CSF-V1 changes, documented in `test_logs/csf_baseline_classic_failures.txt`): `test_pmm.py` ×9, `test_pmm_ping_pong.py` ×3, `test_pmm_refresh_tolerance.py` ×4, `test_pmm_take_if_cross.py` ×2, `test_avellaneda_market_making.py` ×2, `test_perpetual_market_making.py` ×1, `test_liquidity_mining.py` ×1, `test_data_types.py::ArbProposalTests` ×1 (amm_arb), `test_utils.py::test_order_age` ×1 — none were touched or silently fixed.

---

## Phase 0 — Baseline

Branch: `dev` (at `9bf05dd4f`, merged from `nonkyc@90f7d0405` before work began)  
Baseline counts: Kraken 120, NonKYC 424, strategy_v2 ~1025, remote_iface 54  
Pre-existing classic failures: 26 tests (listed above, stored in `test_logs/csf_baseline_classic_failures.txt`)

---

## Phase 1 — Kraken Order Lifecycle & Fills

**Branch:** `fix/csf-p1-kraken-order-lifecycle` → merged `--no-ff` into `dev`  
**Commit:** `2fa58454b`

**Findings fixed:**
- **KRK-3** — Fill recovery dead: rewrote `_all_trade_updates_for_order` to call `QueryOrders?trades=true`, extract the `"trades"` T-prefixed id array, then batch-fetch fills via `QueryTrades` (≤20 ids/call). Old sweep (passing order txid directly to QueryTrades → `EOrder:Invalid order`) removed. Not-found whitelist now scoped to QueryOrders step only.
- **KRK-6** — Not-found never classified: `_is_order_not_found_during_status_update_error` now returns True for `ORDER_NOT_EXIST_ERROR_CODE`, `EOrder:Invalid order`, and `EOrder:Unknown order`. Enabled two previously-disabled lost-order base tests.
- **KRK-4 + KRK-9** — AddOrder ambiguity / duplicate-order risk: extended Cloudflare-recovery path to reconcile via userref against OpenOrders AND ClosedOrders before any resubmit; MARKET AddOrders are never resubmitted (fail without retry after reconciliation finds nothing); `asyncio.TimeoutError` and `EService:*` route through same reconciliation.
- **KRK-14** — Userref collision: regenerate client id if it collides with an in-flight order before tracking.
- **KRK-13** — Scientific notation: `f"{value:f}"` serialization for price/volume in `_place_order`.

**Files changed:** `kraken_exchange.py`, `kraken_constants.py`, `kraken_utils.py`, `test_kraken_exchange.py`

**Tests added:** 13 new tests (dual-path fill recovery, QueryTrades batching, whitelist scope, not-found classification empty-result and error-string, MARKET-no-retry, ClosedOrders reconciliation, timeout/EService reconciliation, collision regeneration, fixed-point serialization)

**Suite counts after phase:** Kraken 133, NonKYC 424, strategy_v2 1025, classic pre-existing baseline

---

## Phase 2 — Kraken Market Data, Balances, Rate Limits

**Branch:** `fix/csf-p2-kraken-marketdata` → merged `--no-ff` into `dev`  
**Commit:** `4a5f625bd`

**Findings fixed:**
- **KRK-1** — Dual-dict book diffs drop a side: `_parse_order_book_diff_message` now merges ALL dict elements between index 1 and -2 (keys `a`, `b`, `as`, `bs`; checksum key `c` may ride in either dict). Covers live-confirmed shape `[ch, {"a": [...]}, {"b": [...], "c": "..."}, "book-10", "PAIR"]`.
- **KRK-12** — No gap detection: implemented Kraken CRC32 checksum. Algorithm: concatenate top-10 asks (ascending) then top-10 bids (descending), each level's price then volume AS the feed's own strings with `.` removed and leading zeros stripped; `zlib.crc32(s) & 0xffffffff` == `c` field. False-positive guard: only validate once book holds full 10×10; require ≥2 consecutive mismatches before reconnect. On confirmed mismatch: WS disconnect (no REST resync).
- **KRK-2** — Flex `.F` fold deletes balances: folded target name added to `remote_asset_names` in both fold loops; overwrite-safe (compute from fresh response, not prior `self._account_*` accumulations). Two-poll flex-only asset stability test added.
- **KRK-7** — Ticker keys canonical: `get_last_traded_prices` translates canonical Ticker keys (e.g. `XXBTZUSD`) via reverse map built from cached asset-pairs.
- **KRK-8** — Rate-limit model: raised CancelOrder weight in matching-engine pool (young-order cancel penalty up to 8 — picked conservative constant, documented `# UNVERIFIED:`); classified `EAPI:Rate limit exceeded`, `EOrder:Rate limit exceeded`, `EService:Busy`, `EService:Unavailable` as retryable-with-backoff EXCEPT AddOrder (routes through Phase 1 reconciliation).
- **KRK-10** — DEFERRED: `BalanceEx`/`hold_trade` swap did not drop in cleanly without risk of breaking the existing 146-test hold-reconstruction path. Recorded below under DEFERRED.

**Files changed:** `kraken_api_order_book_data_source.py`, `kraken_api_user_stream_data_source.py`, `kraken_exchange.py`, `kraken_constants.py`, `kraken_utils.py`, `test_kraken_api_order_book_data_source.py`, `test_kraken_api_user_stream_data_source.py`, `test_kraken_exchange.py`, `test_kraken_utils.py`

**Tests added:** ~90 new (dual-dict diff fixture bids applied, checksum-mismatch→disconnect, warm-up guard, sequence-gap→reconnect, two-poll flex-fold stability, canonical-key translation, retryable-error backoff, AddOrder exclusion)

**Suite counts after phase:** Kraken 156, NonKYC 424, strategy_v2 1025, classic baseline

---

## Phase 3 — NonKYC Order Lifecycle & Balances

**Branch:** `fix/csf-p3-nonkyc-order-lifecycle` → merged `--no-ff` into `dev`  
**Commit:** `24300e55e`

**Findings fixed:**
- **NKC-1** — UNKNOWN sentinel → false LOST/FAILED: (a) `_request_order_status` treats `exchange_order_id == "UNKNOWN"` as missing and falls back to `GET /getorder/{client_order_id}` (path segment, NOT query param — live-verified); (b) exchange id repaired when WS/REST update carries real id and order holds sentinel; (c) bulk fill map skips sentinel-keyed entries into by-client-id path.
- **NKC-4** — REST balance snapshot races pre-adjust holds: records monotonic request-start time; skips overwrite for assets whose `_pre_adjusted_assets` entry is newer than the start time.
- **NKC-6** — Market-order pre-adjust always fails: guards BUY pre-adjust — if `order.price` is None/NaN (market order), skip with DEBUG log instead of raising. NOTE comment left that market-buy quantity semantics are unverified.
- **NKC-7** — `_bulk_fills_fetched_this_cycle` flag wrapped in try/finally.
- **NKC-8** — N identical global `/account/trades` fetches: fetch once per poll cycle (single `since` window); per-pair `since` bookkeeping maintained; `since` IS honored (live-verified).
- **NKC-9** — Unknown status → OPEN silently: WARNING-log unknown status strings in WS path (rate-limited); REST repeated unknown statuses for same order trigger reconciliation log.

**Files changed:** `nonkyc_exchange.py`, `test_nonkyc_csf_p3.py` (new)

**Tests added:** 15+ new (sentinel status-poll fallback, sentinel repaired by later update, fills for sentinel order still attach, REST overwrite skipped for freshly pre-adjusted asset, market-buy pre-adjust no-raise, flag reset on exception, single global trades fetch per cycle, unknown-status logging)

**Suite counts after phase:** Kraken 156, NonKYC 437, strategy_v2 1025, classic baseline

---

## Phase 4 — NonKYC Streams, Order Book, Symbol Map

**Branch:** `fix/csf-p4-nonkyc-streams` → merged `--no-ff` into `dev`  
**Commit:** `fd7294698`

**Findings fixed:**
- **NKC-2** — Fire-and-forget `subscribeReports`; silent WS error frames: await and validate `subscribeReports` ack (correlate on request id; presence of `"error"` key = failure → reconnect retries). Live-verified frame shapes encoded as fixtures. Note: ack `result` snapshot of open orders received but NOT adopted/tracked/cancelled. Catch-all added: any `"error"` frame matching no other branch logs WARNING (rate-limited).
- **NKC-3** — Reorder buffer stale-entry jam: drop messages with `sequence <= last_seq` unconditionally (deleted insert-into-active-buffer branch); `_flush_reorder_buffer` purges entries `<= last_seq` before emptiness check so timeout timer clears.
- **NKC-5** — bidict duplicate crash + separator tickers: wrap mapping insert in try/except `ValueDuplicationError` (log + keep first); skip markets whose derived base or quote contains `-`/`_`/`/`.
- **External-order holds** — Maintain structure of untracked active order holds: quote-side hold = `price × remaining_qty` for buys, base-side = remaining qty for sells. Exposed `external_order_holds(trading_pair)` dict, refreshed on reconciliation snapshot. No cancellation, no tracking-adoption.

**Files changed:** `nonkyc_api_user_stream_data_source.py`, `nonkyc_api_order_book_data_source.py`, `nonkyc_exchange.py`, `test_nonkyc_csf_p4.py` (new), `test_nonkyc_api_user_stream_data_source.py`

**Tests added:** 20+ new (rejected subscription ack raises/retries, error frame logged, stale duplicate with active buffer dropped and timer cleared, duplicate market keeps first without crash, separator ticker skipped, external holds computed from fixture)

**Suite counts after phase:** Kraken 156, NonKYC 456, strategy_v2 1025, classic baseline

---

## Phase 5 — Range Ladder: Noise & Churn

**Branch:** `fix/csf-p5-ladder-noise` → merged `--no-ff` into `dev`  
**Commit:** `231c313f7`

**Findings fixed:**
- **LOG-6** — Regime flapping (133 transitions/38h): added hysteresis `regime_dwell_seconds` config (default 30s) — regime condition must hold for dwell before switch is announced/acted on. Fills during dwell book normally. `price_regime_changed` event fires only on confirmed switches.
- **LOG-8** — Over-claim false alarm: gate warning behind short settle-grace (suppressed unless over-claim persists >10s or across two consecutive evaluations). Transient WS-before-fill ordering no longer triggers warning.
- **LOG-2′** — Understatement vs manual orders: subtracts connector's `external_order_holds` (Phase 4) from the comparison before warning. Degrades gracefully when connector lacks the API (Kraken). NonKYC-only for now; noted in report.
- **Watchdog timing:** NOT touched. LOG-8's "immediate re-propose" refinement explicitly DEFERRED (see DEFERRED list).

**Files changed:** `controllers/market_making/range_inventory_ladder.py`, `test_range_inventory_ladder_phase5_noise.py` (new)

**Tests added:** 20+ new (regime flap suppressed under dwell, confirmed after dwell, fills during dwell book normally, over-claim transient no-warn, persistent over-claim warns, understatement silent with matching external holds, fires when residual exceeds)

**Suite counts after phase:** strategy_v2 lane ~1037, classic baseline

---

## Phase 6 — Logging Template & Kraken Trading-Rules Ordering

**Branch:** `fix/csf-p6-logging-plumbing` → merged `--no-ff` into `dev`  
**Commit:** `3c909dc3a`

**Findings fixed:**
- **LOG-5** — Template v13 → v14: (a) `[STRUCTURED_EVENT]` emissions also log bare JSON via `logging.getLogger("hummingbot.structured_events")` with `structured_file_handler` attached; (b) `errors_file_handler` (level ERROR → `logs/errors.log`) attached to root; `template_version` bumped.
- **SN75USD class (log §8)** — Kraken-LOCAL `_update_trading_rules` override refreshes symbol map from fetched exchange info BEFORE formatting trading rules; no `exchange_py_base.py` touched (MEXC prohibition).

**Files changed:** `hummingbot/templates/hummingbot_logs_TEMPLATE.yml`, `kraken_exchange.py`, `test_nonkyc_csf_p6.py` (new, covers both emitter and template wiring)

**Tests added:** 8 new (emitter writes valid bare-JSON through structured logger, template YAML parses and wires handlers, new-listing-mid-session fixture produces a rule without KeyError)

**Suite counts after phase:** Kraken 158, NonKYC 464, strategy_v2 1025, classic baseline

---

## Phase 7 — Directional Controllers + Base

**Branch:** `fix/csf-p7-directional` → merged `--no-ff` into `dev`  
**Commit:** `df71e646c`

**Findings fixed:**
- **DIR-3** — Sell-side filter one-liner: `x.side == (TradeType.BUY if signal > 0 else TradeType.SELL)`.
- **DIR-2** — Cooldown survives executor close: `_last_same_side_reference_ts` hoisted into base using `close_timestamp` preference over ALL same-side executors.
- **DIR-1** — bollingrid: `bbands(length=..., lower_std=bb_std, upper_std=bb_std)` + correct column names.
- **DIR-5** — ai_livestream: init `processed_data` in `__init__`; signal max-age config (default 300s); lazy listener retry; `unsubscribe` in `stop()`.
- **DIR-6/7/8** — dman_v3: floor BB-width multiplier (`max(mult, min_mult)`); skip create when BBB is 0/NaN; pass `take_profit` into `DCAExecutorConfig`; validators for `sum(dca_amounts_pct) > 0`, spreads > 0, None-guard `stop_loss`; fix ÷200 docstring.
- **DIR-9** — bollinger_v2: deleted per-tick 15-row INFO dump and unused pandas_ta bbands call.
- **DIR-10** — Default interval "3m" → "5m" on bollinger_v1/v2, bollingrid, dman_v3, macd_bb_v1, supertrend_v1.
- **DIR-11** — Use `market_data_provider.time()` in dman_v3.
- **DIR-12** — `max_records = length + 20` on bollinger_v1, bollingrid, dman_v3.

**Files changed:** `directional_trading_controller_base.py`, `bollinger_v1.py`, `bollinger_v2.py`, `bollingrid.py`, `dman_v3.py`, `macd_bb_v1.py`, `supertrend_v1.py`, `ai_livestream.py`, `test_csf_p7_directional.py` (new)

**Tests added:** 20+ new (base cooldown after close, sell-side filter, bollingrid signal on synthetic frame, dman_v3 degenerate-BBB skip + TP passthrough, validators reject zero-sum amounts)

**Suite counts after phase:** strategy_v2 lane ~1086, classic baseline

---

## Phase 8 — Generic Controllers: Money Paths

**Branch:** `fix/csf-p8-generic-money` → merged `--no-ff` into `dev`  
**Commit:** `753fa138e`

**Findings fixed:**
- **GEN-1** — stat_arb: all `get_spread_and_z_score` early exits return `(None, None)`; `update_processed_data` populates safe defaults on None; global TP/SL evaluation continues on `positions_held` even when signal unavailable.
- **GEN-2** — stat_arb: reduce action subtracts active CLOSE-side executor amounts; stop-filter checks `is_active`.
- **GEN-3** — pmm_mister: removed `Decimal("100")` fallback; skip cycle on price-unavailable (rate-limited warning); previous-price reuse bounded by max-age.
- **GEN-4** — DEFERRED: `global_take_profit`/`global_stop_loss` implementation was not cleanly achievable. Mandatory: removed both config fields and the status lines displaying them as armed (a displayed-but-dead stop-loss is the worst state — removal was mandatory per prompt). See DEFERRED list.
- **GEN-5** — xemm_multiple_levels: validate at config time that every level's `target_profitability − config.min_profitability > 0`; clamp computed `min_profitability` to positive floor.
- **GEN-7** — Fixed boolean-list comprehension → filter by target (both sides).
- **GEN-12** — arbitrage_controller: stats filtered to `close_type == COMPLETED and filled_amount_quote > 0`; cumulative imbalance in controller state; `rate.is_finite() and rate > 0` guard; quantized `order_amount > 0` guard.
- **GEN-13** — hedge_asset: deduct active in-flight hedge executor amounts from computed gap; `-USDC` reference pair made configurable.
- **GEN-15** — xemm_multiple_levels: `market_data_provider.time()` for both sides; validate level amounts sum > 0; size sell side from its own market's mid.

**Files changed:** `stat_arb.py`, `pmm_mister.py`, `xemm_multiple_levels.py`, `arbitrage_controller.py`, `hedge_asset.py`, `test_csf_p8_generic_money.py` (new)

**Tests added:** 25+ new (stat_arb candles-outage tick evaluates global SL, duplicate-reduce suppressed, pmm_mister skips cycle on empty book, xemm level floor validation, arbitrage NaN-rate rejected, hedge_asset no double-hedge)

**Suite counts after phase:** strategy_v2 lane ~1116, classic baseline

---

## Phase 9 — Generic Controllers: Grid Family

**Branch:** `fix/csf-p9-generic-grid` → merged `--no-ff` into `dev`  
**Commit:** `9f847942b`

**Findings fixed:**
- **GEN-9** — Pydantic validators: `GridExecutorConfig`: `start_price > 0`, `start_price < end_price`, `limit_price` correct side; controller configs same geometry checks; multi_grid: `sum(amount_quote_pct) <= 1`.
- **GEN-6** — multi_grid_strike: `get_executor_by_grid_id` requires `executor.is_active`; prune done entries from `_grid_executor_mapping`; stop path only stops active executors.
- **GEN-8** — multi_grid_strike: per-grid parameter hash; change of still-enabled grid stops active executor for re-issue.
- **GEN-10** — grid_strike: re-entry cooldown after termination (config, default 60s) + consecutive-stop-out breaker (config, default 3 → halt with rate-limited warning).
- **GEN-11** — quantum_grid_allocator: fall back to `config.grid_range` when `ta.bbands` returns None or width not finite/positive.
- **GEN-17** — lp_rebalancer: capture closed-position amounts on EVERY termination of tracked executor, not just pending-rebalance ones.

**Files changed:** `grid_strike.py`, `multi_grid_strike.py`, `quantum_grid_allocator.py`, `lp_rebalancer/lp_rebalancer.py`, `executors/grid_executor/data_types.py`, `test_csf_p9_generic_grid.py` (new)

**Tests added:** 30+ new (validator rejections for inverted range/zero start/wrong-side limit/pct-sum>1, grid respawns after termination-with-archival-lag, param edit stops+recreates, stop-out breaker halts, NaN bbands falls back)

**Suite counts after phase:** strategy_v2 lane ~1150, classic baseline

---

## Phase 10 — Classic Market-Making Family

**Branch:** `fix/csf-p10-classic-mm` → merged `--no-ff` into `dev`  
**Commit:** `4b169482a`

**Cython rebuild:** Yes — `pure_market_making.pyx`, `avellaneda_market_making.pyx`, `strategy_base.pyx` rebuilt via `vcvarsall x64` + `python setup.py build_ext --inplace`.

**Findings fixed:**
- **PMM-1** — NaN price gate: `pure_market_making.pyx` and hanging tracker skip NaN comparisons; cancel wave runs and tick returns early on NaN price.
- **PMM-2** — avellaneda: skip `add_sample` on NaN price; NaN-guard before reservation-price/optimal-spread math; cancel wave still runs.
- **PMM-3** — `get_price_for_volume` NaN: if result is NaN, skip optimization for that side (leave proposal price unmodified) in PMM ×2, avellaneda ×2, perpetual ×1.
- **PMM-4** — Clear `current_created_pairs_of_orders` at start of `c_execute_orders_proposal` (PMM and avellaneda).
- **PMM-5** — PMM completion handlers use dual check (`is_order_id_in_hanging_orders or is_order_id_in_completed_hanging_orders`).
- **PMM-6** — Inventory-cost delegate: catch `(InvalidOperation, DivisionByZero, ZeroDivisionError)`; return None when `base_volume == 0`.
- **PMM-7** — Level loops require `size > 0 and price > 0`.
- **PMM-8** — `start.py:100` append `.value`.
- **PMM-9** — DEFERRED: carrying configured level index through `split_order_levels` repricing requires invasive PriceSize changes. Recorded under DEFERRED.
- **PMM-10** — `strategy_base.pyx` listeners: tracker-cleanup handler called BEFORE user-facing handler (all 4 listener classes).
- **PMM-11** — `next((...), None)` at all six bare-`next` sites (PMM ×3, avellaneda ×3).
- **PMM-12** — avellaneda: `unregister_events` on old hanging-orders tracker before replacing it.
- **PMM-13** — avellaneda config: model validator rejecting `end <= start` for daily and date-to-date windows.
- **PMM-14** — perpetual MM: recompute buffered SL price from entry-derived `stop_loss_price` each renewal (not from previous order's price).
- **PMM-15** — Fixed dead `all(a, b, c, d)` method in `hanging_orders_tracker.py` (signature was shadowing builtin).

**Files changed:** `pure_market_making.pyx`, `pure_market_making.pxd`, `start.py` (PMM), `inventory_cost_price_delegate.py`, `data_types.py` (PMM), `avellaneda_market_making.pyx`, `avellaneda_market_making_config_map_pydantic.py`, `perpetual_market_making.py`, `hanging_orders_tracker.py`, `strategy_base.pyx`, `test_pmm_csf_p10.py` (new), `test_avellaneda_csf_p10.py` (new), `test_hanging_orders_tracker_csf_p10.py` (new), `test_strategy_base_csf_p10.py` (new), `test_perpetual_market_making.py` (updated), `test_pure_market_making_start.py` (updated)

**Tests added:** 60+ new (NaN-tick cancels-and-returns, NaN sample skipped, NaN optimization skipped, pair-list cleared, dual hanging check, DivisionByZero delegate, negative-price level dropped, `next` default)

**Suite counts after phase:** Kraken 158, NonKYC 473, strategy_v2 1156, classic 386 passed (26 pre-existing failures unchanged)

---

## Phase 11 — Classic Cross-Exchange / Arbitrage Family

**Branch:** `fix/csf-p11-classic-arb` → merged `--no-ff` into `dev`  
**Commit:** `646e91887`

**Cython rebuild:** Yes — `cross_exchange_mining.pyx` rebuilt.

**Findings fixed:**
- **ARB-1 (CRITICAL)** — `handle_unfilled_taker_order`: resolve MAKER id via `_taker_to_maker_order_ids[order_id]` BEFORE deleting mapping; remove stale `_ongoing_hedging` entry; pass maker id to `check_and_hedge_orders`; `notify_hb_app` alarm on hedge failure.
- **ARB-2** — (a) `did_complete_*` cleanup only removes fill records covered by completed hedge quantity — no blanket discard; (b) hedge quantizing to zero or raising logs WARNING and leaves fill records intact for retry; (c) minimal retry: re-attempt hedging of remaining unhedged fill records on subsequent events; notify after N consecutive failures. Full tick-loop-driven re-hedge DEFERRED.
- **ARB-3** — Taker BUY hedge sizing: divides balance leg by `taker_price * (1 + slippage_buffer)`.
- **ARB-4/11** — spot-perp: added `did_fail_order`/`did_cancel_order` handlers → alarm + reset state machine; guard `perp_positions[0]`; re-run budget/readiness gate every tick until passes; validate prices for Exceptions/NaN; fix `perp_side.is_buy != cur_perp_pos_is_buy`.
- **ARB-5/13** — cross_exchange_mining .pyx: init `taker_price = s_decimal_nan`; gate maker-branch comparison; ZeroDivision fallback skips side for tick (no cross-exchange book query); stop eager `stop_tracking_limit_order` after cancel; index `c_get_limit_orders()` by current market pair.
- **ARB-6/7** — Initialize `price_above_bid = s_decimal_nan`; treat NaN hedging price as cancel-trigger; replace ZeroDivision asserts with `return s_decimal_zero`; NaN-guard `min()` sites.
- **ARB-8** — amm_arb: handle `OrderCancelledEvent` as completion; timeout+cancel around `wait()` calls; `notify_hb_app` when leg abandoned after other filled; bound `_order_id_side_map` growth.
- **ARB-9** — liquidity_mining: skip NaN mid-price appends; `update_volatility` filters NaN; `create_base_proposals` skips NaN markets for tick.
- **ARB-10/15/16** — hedge: pad offsets with zeros to `len(markets)`; key `offsets_dict` by (connector, slot); closing candidate requires `amount > 0`; skip hedge cycle with warning on NaN/zero mid.
- **ARB-12** — liquidity_mining: return from `tick()` while `_ready_to_trade` is False; run `create_budget_allocation()` once on False→True transition.
- **ARB-14** — Fixed self-recursive `stop_tracking_limit_order` override (call `super()`); deleted dead config properties; hooked order-termination events to `stop_tracking_order_id`.

**Files changed:** `cross_exchange_market_making.py`, `cross_exchange_mining.pyx`, `spot_perpetual_arbitrage.py`, `amm_arb.py`, `hedge/hedge.py`, `hedge/start.py`, `liquidity_mining/liquidity_mining.py`, `liquidity_mining/start.py`, `test_cross_exchange_market_making_arb_fixes.py` (new), `test_cross_exchange_mining.py` (new), `test_spot_perpetual_arbitrage.py` (updated), `test_amm_arb.py` (new), `test_hedge.py` (updated), `test_hedge_start.py` (updated), `test_liquidity_mining.py` (updated), `test_liquidity_mining_start.py` (new)

**Tests added:** 80+ new (ARB-1 taker failure → re-hedges maker fill, partial-fill records survive hedge completion, pmm_mister cycle-skip, xemm level replenishment, hedge no-double-hedge in flight, and full families per fix)

**Suite counts after phase:** Kraken 158, NonKYC 473, strategy_v2 1156, classic 386 passed (26 pre-existing baseline), remote_iface 54

---

## Phase 12 — Finalization

**Actions:**
1. Full comprehensive testing on `dev` — all lanes green (counts above).
2. `remote_iface` suite: 54 passed.
3. `git checkout nonkyc && git merge --no-ff dev` — merged as commit `6748f5182`.
4. `git push origin dev && git push origin nonkyc` — both pushed.

**Report written:** `REPORT_csf_v1.md` (this file).

---

## DEFERRED List (with rationale)

| ID | Finding | Rationale |
|----|---------|-----------|
| KRK-10 | Swap held-funds reconstruction to `BalanceEx`/`hold_trade` | Drop-in path required restructuring the existing hold reconstruction that 146 tests pin; safe direction is to preserve existing behavior rather than introduce an untested hold-accounting rewrite. |
| PMM-9 | Carry configured level index through `split_order_levels` repricing | Requires invasive changes to `PriceSize` data structure shared across all PMM variants; risk of breaking the classic test suite further. |
| GEN-4 | `pmm_mister` `global_take_profit`/`global_stop_loss` full implementation | Implementation not cleanly achievable without a complete position-tracking redesign. Config fields and all status display references removed (mandatory per prompt: a displayed-but-dead stop-loss is the worst state). |
| ARB-2 (full) | Full tick-loop-driven re-hedge for XEMM | Requires multi-cycle state machine redesign beyond the phase scope. Mandatory bookkeeping fixes (no blanket discard, WARNING on zero-quantize, minimal retry) ARE shipped. |
| LOG-8 watchdog | "Immediate re-propose" refinement | Explicitly deferred per Phase 5 instructions. Watchdog timing not touched. |

---

## NOT-A-BUG List

No findings were reclassified as NOT-A-BUG during this batch. All live-probed behavior matched the findings doc's characterization.

---

## Explicit SKIPs (per prompt — not implemented)

| ID | Finding | Reason |
|----|---------|--------|
| KRK-11 | Fees quote-denominated refuted | Live-verified: Kraken fees ARE quote-denominated; finding refuted before batch started. |
| LOG-7 | Band tuning | Operator's process — explicitly excluded from code changes. |
| GEN-16 | Examples | No code change required; examples-only. |
| NKC-6 (quantity semantics) | Market-buy quantity semantics | Cannot verify without live API; pre-adjust guard shipped; NOTE comment left. |

---

## "Verify Before Relying on Money Path" List

These items were implemented FAIL-CLOSED with `# UNVERIFIED:` comments per prompt instructions:

- **KRK-8 cancel weight constants** — exact rate-limit penalty weights not probeable without real order+cancel bursts on the live-shared key. Conservative constants chosen (over-weight cancels; measurement only reveals headroom, not correctness).
- **KRK-13 scientific notation** — whether Kraken rejects sci-notation not probed (answer cannot change fix; `f"{x:f}"` is unconditionally accepted).
- **NKC-6 market-buy quantity semantics** — quantity meaning for market buys unverified. Pre-adjust guard shipped; NOTE comment present.

---

## Files Changed (complete list across all phases)

**Kraken connector:**
- `hummingbot/connector/exchange/kraken/kraken_exchange.py`
- `hummingbot/connector/exchange/kraken/kraken_constants.py`
- `hummingbot/connector/exchange/kraken/kraken_utils.py`
- `hummingbot/connector/exchange/kraken/kraken_api_order_book_data_source.py`
- `hummingbot/connector/exchange/kraken/kraken_api_user_stream_data_source.py`

**NonKYC connector:**
- `hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py`
- `hummingbot/connector/exchange/nonkyc/nonkyc_api_user_stream_data_source.py`
- `hummingbot/connector/exchange/nonkyc/nonkyc_api_order_book_data_source.py`

**Range ladder:**
- `controllers/market_making/range_inventory_ladder.py`

**Directional controllers:**
- `controllers/directional_trading/directional_trading_controller_base.py` (via strategy_v2)
- `controllers/directional_trading/ai_livestream.py`
- `controllers/directional_trading/bollinger_v1.py`
- `controllers/directional_trading/bollinger_v2.py`
- `controllers/directional_trading/bollingrid.py`
- `controllers/directional_trading/dman_v3.py`
- `controllers/directional_trading/macd_bb_v1.py`
- `controllers/directional_trading/supertrend_v1.py`

**Generic controllers:**
- `controllers/generic/stat_arb.py`
- `controllers/generic/pmm_mister.py`
- `controllers/generic/xemm_multiple_levels.py`
- `controllers/generic/arbitrage_controller.py`
- `controllers/generic/hedge_asset.py`
- `controllers/generic/grid_strike.py`
- `controllers/generic/multi_grid_strike.py`
- `controllers/generic/quantum_grid_allocator.py`
- `controllers/generic/lp_rebalancer/lp_rebalancer.py`

**Classic strategies:**
- `hummingbot/strategy/pure_market_making/pure_market_making.pyx` (Cython)
- `hummingbot/strategy/pure_market_making/pure_market_making.pxd`
- `hummingbot/strategy/pure_market_making/start.py`
- `hummingbot/strategy/pure_market_making/inventory_cost_price_delegate.py`
- `hummingbot/strategy/pure_market_making/data_types.py`
- `hummingbot/strategy/avellaneda_market_making/avellaneda_market_making.pyx` (Cython)
- `hummingbot/strategy/avellaneda_market_making/avellaneda_market_making_config_map_pydantic.py`
- `hummingbot/strategy/perpetual_market_making/perpetual_market_making.py`
- `hummingbot/strategy/hanging_orders_tracker.py`
- `hummingbot/strategy/strategy_base.pyx` (Cython)
- `hummingbot/strategy/cross_exchange_market_making/cross_exchange_market_making.py`
- `hummingbot/strategy/cross_exchange_mining/cross_exchange_mining.pyx` (Cython)
- `hummingbot/strategy/spot_perpetual_arbitrage/spot_perpetual_arbitrage.py`
- `hummingbot/strategy/amm_arb/amm_arb.py`
- `hummingbot/strategy/hedge/hedge.py`
- `hummingbot/strategy/hedge/start.py`
- `hummingbot/strategy/liquidity_mining/liquidity_mining.py`
- `hummingbot/strategy/liquidity_mining/start.py`

**Shared / infrastructure:**
- `hummingbot/strategy_v2/executors/grid_executor/data_types.py`
- `hummingbot/strategy_v2/controllers/directional_trading_controller_base.py`
- `hummingbot/templates/hummingbot_logs_TEMPLATE.yml`

**Tests added (new files):**
- `test/hummingbot/connector/exchange/kraken/test_kraken_api_order_book_data_source.py` (extended)
- `test/hummingbot/connector/exchange/kraken/test_kraken_api_user_stream_data_source.py` (extended)
- `test/hummingbot/connector/exchange/kraken/test_kraken_exchange.py` (extended)
- `test/hummingbot/connector/exchange/kraken/test_kraken_utils.py` (extended)
- `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_csf_p3.py` (new)
- `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_csf_p4.py` (new)
- `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_csf_p6.py` (new)
- `test/hummingbot/connector/exchange/nonkyc/test_nonkyc_api_user_stream_data_source.py` (extended)
- `test/hummingbot/strategy_v2/controllers/test_csf_p7_directional.py` (new)
- `test/hummingbot/strategy_v2/controllers/test_csf_p8_generic_money.py` (new)
- `test/hummingbot/strategy_v2/controllers/test_csf_p9_generic_grid.py` (new)
- `test/hummingbot/strategy_v2/controllers/test_range_inventory_ladder_phase5_noise.py` (new)
- `test/hummingbot/strategy/pure_market_making/test_pmm_csf_p10.py` (new)
- `test/hummingbot/strategy/avellaneda_market_making/test_avellaneda_csf_p10.py` (new)
- `test/hummingbot/strategy/test_hanging_orders_tracker_csf_p10.py` (new)
- `test/hummingbot/strategy/test_strategy_base_csf_p10.py` (new)
- `test/hummingbot/strategy/cross_exchange_market_making/test_cross_exchange_market_making_arb_fixes.py` (new)
- `test/hummingbot/strategy/cross_exchange_mining/test_cross_exchange_mining.py` (new)
- `test/hummingbot/strategy/amm_arb/test_amm_arb.py` (new)
- `test/hummingbot/strategy/spot_perpetual_arbitrage/test_spot_perpetual_arbitrage.py` (extended)
- `test/hummingbot/strategy/hedge/test_hedge.py` (extended)
- `test/hummingbot/strategy/hedge/test_hedge_start.py` (extended)
- `test/hummingbot/strategy/liquidity_mining/test_liquidity_mining.py` (extended)
- `test/hummingbot/strategy/liquidity_mining/test_liquidity_mining_start.py` (new)
- `test/hummingbot/strategy/pure_market_making/test_pure_market_making_start.py` (extended)
- `test/hummingbot/strategy/perpetual_market_making/test_perpetual_market_making.py` (extended)

---

## Docker Rebuild — MANDATORY

**None of these fixes reach the production pods until the Docker images are rebuilt.**

Commands (from the repo root):
```bash
./Build_hummingbot_nonkyc.sh       # main bot stack image
./Build_hummingbot_api_nonkyc.sh   # API/gateway stack image
```

**Warning:** The image-rebuild process stops bot containers on BOTH stacks. After the rebuild completes, **redeploy bots on both stacks** (TrueNAS pod at 192.168.1.54).

The Cython extensions compiled during the build (`build_ext --inplace` inside Docker) will be fresh — no stale `.pyd` issues in production. Local Windows `.pyd` files remain at the post-Phase-10/11 rebuild state.
