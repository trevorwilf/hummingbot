# Hummingbot V2 Strategy Framework — Code Review Findings

**Date:** 2026-07-13
**Fork:** trevorwilf/hummingbot, branch `nonkyc`
**Reviewers:** 4 parallel read-only agents + manual adversarial verification of high-impact claims
**Scope:** ~9,400 lines across `hummingbot/strategy_v2/` (executors, orchestrator, controller bases, models, utils, backtesting), `hummingbot/data_feed/market_data_provider.py`, the executor/candles/reporting areas of `hummingbot/strategy/strategy_v2_base.py`, and the stock controllers (`pmm_simple`, `pmm_dynamic`, `dman_maker_v2`).

**Excluded:** `controllers/market_making/range_inventory_ladder.py` (already extensively hardened), recently-hardened config-loading / MQTT-publisher areas, and `lp_executor` (DEX/Gateway-only — not used on this spot-CEX stack).

**Deployment context these findings are judged against:** headless bots under hummingbot-api, spot CEXes only (NonKYC, Kraken, MEXC), up to 4 controllers per process sharing one MarketDataProvider, frequent restarts (image rebuilds), order books empty during WS reconnects, slow REST (700–2500 ms), late-settling fills, Kraken poll-only balances.

**Legend:** ✓ = personally re-verified against the source after the agent reported it.
No code was changed during this review.

---

## A. Affects what you run TODAY (range ladder → OrderExecutor → orchestrator)

### A1. ✓ HIGH — OrderExecutor can hang in SHUTTING_DOWN forever
**File:** `hummingbot/strategy_v2/executors/order_executor/order_executor.py:112-131`

```python
async def control_shutdown_process(self):
    if self._order:
        if self._order.is_open:
            self.cancel_order()
        elif self._order.is_filled:
            ...
            self.stop()
    else:
        ...
        self.stop()
    await self._sleep(5.0)
```

`control_shutdown_process` cancels the order when open, then just sleeps — **no timeout, no retry cap, no forced `stop()`**. It relies entirely on the cancel-confirmed event arriving to set `self._order = None` so the `else` branch can `stop()`. If that event is lost (dropped WS message during a reconnect — a real occurrence on NonKYC/Kraken), the executor spins in SHUTTING_DOWN indefinitely. There is also a **fall-through hole**: an order that is neither `is_open` nor `is_filled` (e.g. stuck `PENDING_CREATE`/`PENDING_CANCEL`) matches no branch and loops forever.

PositionExecutor has a 15s `_pending_close` fallback for exactly this; OrderExecutor has nothing.

**Why it matters here:** OrderExecutor is the executor type the range ladder spawns. A stuck one permanently occupies its level and, via `_shutdown_in_flight_keys`, defers every new create for that connector/pair/side. Controller-level watchdogs cannot kill it.

**Suggested fix direction:** record a shutdown-start timestamp; after N seconds force `stop()` (recording the order as held/failed) so the executor and its level are never pinned forever. Mirror PositionExecutor's existing pattern.

**→ Highest-value hardening for the production stack.**

---

### A2. ✓ HIGH — Orchestrator `stop()` can skip position/executor persistence
**File:** `hummingbot/strategy_v2/executors/executor_orchestrator.py:352-370`

```python
async def stop(self, max_executors_close_attempts: int = 3):
    for controller_id, executors_list in self.active_executors.items():
        for executor in executors_list:
            if not executor.is_closed:
                executor.early_stop()
    for i in range(max_executors_close_attempts):
        if all([executor.executor_info.is_done for executors_list in self.active_executors.values()
                for executor in executors_list]):
            continue
        await asyncio.sleep(2.0)
    self.store_all_positions()
    self.store_all_executors()
    self.active_executors.clear()
```

The shutdown poll builds `executor.executor_info` for **every** executor with no per-executor try/except. `executor_info` computes full PnL/fee/custom_info. If any executor raises there (a NaN path, or the half-initialized DCA in B6, or any `get_custom_info` error), the whole `stop()` aborts **before** `store_all_positions()` / `store_all_executors()` run — held inventory is silently not persisted across the restart.

**Why it matters here:** on a restart-heavy stack, one degraded executor drops the DB persistence of *all* positions/executors, so held inventory disappears from the ledger across an image rebuild.

**Note:** the `continue`-vs-`break` in the same loop is a wart, not the bug — it caps waiting at ~6s either way (when all done it skips the sleep; when not done it sleeps). The real risk is the unguarded `executor_info`.

**Suggested fix direction:** wrap the per-executor `executor_info.is_done` read in try/except so one bad executor can't abort persistence.

---

### A3. ✓ MEDIUM — Budget preflight: fail-closed drop on transient prices + in-place `config.amount` mutation
**File:** `hummingbot/strategy_v2/executors/executor_orchestrator.py:598-704`

Two issues:

**(1) Fail-closed on empty-book price.** For market-order actions with no price, the validation price comes from:
```python
price = self.strategy.market_data_provider.get_price_by_type(
    connector_name, config.trading_pair,
    PriceType.BestAsk if config.side == TradeType.BUY else PriceType.BestBid)
```
On an empty book during reconnect this **raises** (spot connectors raise `EnvironmentError`, they do not return NaN), and the catch-all at line 698 drops the action as `preflight_error`. Legitimate creates die purely because the book was momentarily empty.

**(2) In-place config mutation on resize** (line 683):
```python
config.amount = adjusted.amount
```
This mutates the controller's own config object. Any deferred/re-proposed action then carries the shrunken amount, not the original intent.

**Why it matters here:** the ladder's *limit* orders carry prices, so they are largely shielded from (1); market-order paths (rebalance, heals) are not. The ladder pins intents separately, which shields it from (2). Both bite generic controllers harder.

---

### A4. MEDIUM — `process_order_completed_event` None-dereference
**File:** `hummingbot/strategy_v2/executors/order_executor/order_executor.py:245-253` (also `:121-122`)

```python
if self._order and self._order.order_id == event.order_id:
    ...
    self._held_position_orders.append(self._order.order.to_json())
```

The guard checks `self._order` (the TrackedOrder) is truthy, then dereferences `self._order.order.to_json()`. `TrackedOrder.order` is None if the connector already evicted the completed order from its active tracker (common with late fill delivery on slow REST). The AttributeError escapes into the connector's shared event forwarder, which can disrupt other executors' event processing on the same bot.

---

### A5. MEDIUM — NaN reaches the dashboard / MQTT payloads (two gaps)

**(a) Unguarded positions report** — `executor_orchestrator.py:973-975`:
```python
mid_price = ...get_price_by_type(...MidPrice)
positions_summary.append(position.get_position_summary(mid_price))
```
`generate_performance_report` guards this at line 1045 (`mid_price if not mid_price.is_nan() else Decimal("0")`), but `get_positions_report` passes NaN straight through into `get_position_summary`, producing NaN `unrealized_pnl_quote`.

**(b) MQTT serializer emits invalid JSON** — `hummingbot/remote_iface/mqtt.py:64`:
```python
elif isinstance(val, (Decimal, float)): return float(val)
```
`Decimal("NaN")` → `float('nan')`, and `ujson.dumps({'x': float('nan')})` emits `{"x":NaN}` — a bare `NaN` token that strict JSON parsers reject. `close_type_counts` also serializes enum *keys* as `"CloseType.X"` strings rather than stable values.

**Why it matters here:** directly relevant to the dashboard-status pipeline you just repaired — this is the next class of "performance data silently not arriving / unparseable."

---

### A6. LOW — `is_trading` is always False for OrderExecutors
**File:** `hummingbot/strategy_v2/executors/order_executor/order_executor.py:375-397`

All PnL/fee getters return `Decimal("0")` by design (POSITION_HOLD accounting), so the inherited `is_trading` (`is_active and net_pnl_quote != 0`) never fires. Any controller keying refresh/reconcile logic on `executor_info.is_trading` misreads a filled-but-held OrderExecutor as idle. The range ladder uses `custom_info["filled_amount_base"]` instead — fine — but it is a trap for future controllers.

---

## B. Affects the stock V2 strategies IF you deploy them (pmm/grid/dca/twap/xemm/arbitrage)

### B1. ✓ CRITICAL (grid) — GridExecutor construction divides by mid-price with no guard, in `__init__`
**File:** `hummingbot/strategy_v2/executors/grid_executor/grid_executor.py:60,129,140-145`

`_generate_grid_levels()` runs inside the **constructor** (line 60), outside the try-wrapped control loop:
```python
price = self.get_price(..., PriceType.MidPrice)
...
min_base_amount = max(
    min_notional_with_margin / price,
    min_base_increment * Decimal(str(math.ceil(float(min_notional) / float(min_base_increment * price)))))
```
Empty book → price call raises, or NaN → `math.ceil(float(NaN))` raises `ValueError`, or divisions poison every level. `validate_sufficient_balance` does the same. Because it's at construction, the failure propagates to whoever creates the executor.

Also cosmetic-but-real: line 137 `min_notional * Decimal("1.05")  # 20% margin for safety` — the multiplier is 5%, not 20%.

---

### B2. HIGH — Stop-losses silently disarm on NaN prices (grid + DCA)
**Files:** `grid_executor.py:596`, `dca_executor.py:330`

```python
# grid
if self.config.triple_barrier_config.stop_loss:
    return self.position_pnl_pct <= -self.config.triple_barrier_config.stop_loss
```
`NaN <= -stop_loss` is `False`. Neither executor has the `_is_valid_price` guards that PositionExecutor got after its NaN incident. On a NaN-returning path this is a silent stop-loss disarm while a position is open. (On spot, the empty book usually raises earlier and aborts the whole tick — same net effect: no barrier evaluation during an outage.)

---

### B3. ✓ HIGH — TWAP has two real bugs
**File:** `hummingbot/strategy_v2/executors/twap_executor/twap_executor.py`

**(1) Plan-wipe on a single failure** — line 169:
```python
self._order_plan = {timestamp: None for timestamp, order in self._order_plan.items() if order == active_order}
```
Keeps **only** the failed slot (set to None) and **drops every other** scheduled/placed/filled entry. One failure mid-TWAP silently abandons the rest of the schedule and loses accounting for already-filled slices → under-executes the intended notional.

**(2) Unit-mismatched validation** — line 38:
```python
if self.config.order_amount_quote < trading_rules.min_order_size:
```
`order_amount_quote` is quote units; `min_order_size` is base units. Validates neither notional nor size correctly → slices can pass then be rejected by the exchange, which triggers bug (1).

Also: TWAP places orders with no NaN-amount gate (grid has `amount > 0`, TWAP does not — `create_order` computes `amount = (...)/price` → NaN order submitted).

---

### B4. HIGH — Controller-base cycle aborts on empty book
**File:** `hummingbot/strategy_v2/controllers/market_making_controller_base.py:1004-1006`

```python
reference_price = self.market_data_provider.get_price_by_type(
    self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
self.processed_data = {"reference_price": Decimal(reference_price), ...}
```
No empty-book / NaN guard. Spot connectors `raise EnvironmentError("Order book is empty...")` while `connector.ready` stays latched True (status only checks `order_books_initialized`). The exception aborts the cycle; the per-controller loop catches it, so blast radius is one aborted tick — but for the whole duration of a WS reconnect, pmm/dman controllers stop managing resting orders, silently. (`Decimal(reference_price)` would also raise `InvalidOperation` if a NaN ever reached it.)

**Suggested fix direction:** skip the cycle gracefully with a rate-limited log (like the range ladder does) instead of letting it abort.

---

### B5. HIGH — Div-by-zero config paths that pass pydantic
- `market_making_controller_base.py:230-236` — `total_pct = sum(buy_amounts_pct) + sum(sell_amounts_pct)`; if all zero → `ZeroDivisionError`/`DivisionByZero` every cycle in `get_spreads_and_amounts_in_quote`.
- `dman_maker_v2.py:71` — `[Decimal(amount) / sum(self.config.dca_amounts) ...]`; `sum == 0` → raises at controller construction.
- No `gt=0` on `total_amount_quote` (`controller_base.py:71-78`); no positivity/`<1` constraint on spreads — a negative or `>1` buy spread yields `order_price = reference_price * (1 + side_multiplier * spread)` that is negative or inverts sides. Only accidentally caught by the min-notional check *if* trading rules are loaded (`market_making_controller_base.py:1022-1024`).
- Silent surprise: dman's `dca_spreads`/`dca_amounts` are computed once in `__init__` and are **not** in the `is_updatable` set — YAML hot-edits to them are silently ignored.

**Suggested fix direction:** add validators — `total_amount_quote > 0`, `sum(amounts_pct) > 0`, `sum(dca_amounts) > 0`, spreads `> 0`, plus explicit guards on the two division sites.

---

### B6. MEDIUM — DCA constructed-then-stopped half-initialized; PositionExecutor retry check missing in SHUTTING_DOWN
- `dca_executor.py:44-58` — the min-order-size failure path calls `close_execution_by(CloseType.FAILED)` (→ `stop()` → sets TERMINATED) **before** `_open_orders`/`_close_orders`/`_current_retries` are assigned (lines 56-58). Later status/`get_custom_info` queries raise AttributeError (also an A2 trigger).
- `position_executor.py:369 vs 414` — `evaluate_max_retries()` runs only in the RUNNING branch; `control_shutdown_process` increments `_current_retries` but never checks it. A close order that can't fill on a thin book wedges in SHUTTING_DOWN with no terminal stop (only the 15s deferred-cancel fallback exists, which doesn't cover a live-but-unfillable close order).

---

### B7. MEDIUM — pmm_dynamic NaN indicators
**File:** `controllers/market_making/pmm_dynamic.py:89-102`

```python
macd_signal = -(macd - macd.mean()) / macd.std()
...
"reference_price": Decimal(candles["reference_price"].iloc[-1]),
"spread_multiplier": Decimal(candles["spread_multiplier"].iloc[-1])
```
Flat closes on an illiquid pair (plausible on NonKYC) → `macd.std() == 0` → NaN/inf `reference_price`/`spread_multiplier` → either `Decimal(NaN)` raises (cycle aborts) or garbage flows into `get_price_and_amount`. No `is_nan()` check anywhere in the override. (`.iloc[-1]` itself is safe — `market_data_provider.ready` requires the candles deque full.)

---

### B8. MEDIUM — Arbitrage & XEMM money-safety gaps
- `arbitrage_executor.py:193-194,313-319` — both market legs fire with no atomicity; the failed-order **event handler** re-places immediately at stale prices, and the `_cumulative_failures > max_retries` gate only runs later in `control_task`. A one-sided fill leaves an unhedged spot position; after 3 retries it gives up FAILED with the position still open.
- `xemm_executor.py:183,283-292` — the taker hedge is a MARKET order with **no worst-price bound** (thin-book slippage eats the "profit"); `_maker_target_price = _taker_result_price / (1 - target_profitability - _tx_cost_pct)` has no guard against the denominator → 0/negative (maker price explodes/flips).

---

### B9. ✓ MEDIUM — Fork's refresh policy stops *trading* executors on age (DECISION, not clearly a bug)
**File:** `hummingbot/strategy_v2/controllers/market_making_controller_base.py:934-947`

```python
def is_refresh_eligible(x) -> bool:
    if not x.is_active: return False
    age = now - x.timestamp
    if age <= refresh_time: return False
    if not x.is_trading: return True   # original behavior
    # Partially filled / trading executors ALSO refresh-eligible past refresh time
    return True
```
This is a **deliberate fork change** (stock HB only refreshes non-trading executors). For pmm-style strategies it force-cycles live/filled positions every `executor_refresh_time` (default 300s) — cancelling resting TPs and re-quoting, burning fees and queue position. Flagged for a decision: if the intent was ladder-specific, note that it is now global to every market-making controller subclass.

---

### B10. MEDIUM — One bad candles config kills the whole bot at construction
**Files:** `strategy_v2_base.py:283`, `controllers/controller_base.py:228`, `market_data_provider.py:228`

`CandlesFactory.get_candle` raises `UnsupportedConnectorException` for unmapped/typo'd connectors, and nothing catches it in strategy `__init__`. A single controller's bad candles feed prevents all 4 controllers from starting.

**Suggested fix direction:** wrap `initialize_candles` in try/except so a bad feed disables that feed and logs, instead of aborting construction for everyone.

---

### B11. LOW — MarketDataProvider check-then-act race
**File:** `hummingbot/data_feed/market_data_provider.py:326-337`

```python
if self._non_trading_connectors_started.get(connector_name, False): return True
...
await connector.start_network()
self._non_trading_connectors_started[connector_name] = True
```
The started-flag is read before the `await` and written after. Two controllers sharing the provider can both see `False`, both call `start_network()`, double-starting the tracker/WS and leaking a task. Needs a per-connector lock or in-flight flag before the await.

---

## C. Backtesting correctness (you vet configs with this — silent-wrong-result bugs only)

### C1. MEDIUM — DCA simulator applies fills from the wrong (leaked) timestamp
**File:** `hummingbot/strategy_v2/backtesting/executors_simulator/dca_executor_simulator.py:126-132`

```python
df_filtered.loc[entry_timestamp:, f'filled_amount_quote_{i}'] = dca_stage['amount']
```
`entry_timestamp` is the loop variable left over from an **earlier** `for i in range(len(config.prices))` loop; after it ends, it holds only the LAST stage's entry timestamp. Every stage in the second loop writes its volume/PnL starting from that single leaked timestamp instead of its own `dca_stage['entry_timestamp']`. The `if`/`else` branches are byte-identical except the `else` sets `close_type`/breaks. Result: DCA volume/PnL is backdated to the wrong bar → wrong equity curve.

### C2. MEDIUM — Position simulator undercounts fees (single-leg)
**File:** `hummingbot/strategy_v2/backtesting/executors_simulator/position_executor_simulator.py:45,49`

`trade_cost` is subtracted once from returns and charged once in `cum_fees_quote`, modeling one fill. A real position pays fees on entry **and** exit (~2×). Reported `filled_amount_quote` is doubled for round-trip volume (line 80) but the fee is not → simulated net PnL is optimistic by ~one leg of fees. Biases config selection toward strategies that are marginal after real round-trip cost.

### C3. MEDIUM — Trailing-stop sim may KeyError
**File:** `hummingbot/strategy_v2/backtesting/executors_simulator/position_executor_simulator.py:53-65`

The `'ts'` column is only created by `df.loc[<mask>, 'ts'] = ...`; if the mask is all-False (price never exceeds the trailing trigger), pandas may not create the column, and line 65's `df_filtered['ts']` raises `KeyError`. Pandas-version dependent (may auto-create as all-NaN in some versions). Affects trailing-stop position configs whose trigger never fires.

---

## D. Suggested priority order (if/when fixes are requested)

1. **A1** — OrderExecutor shutdown timeout/retry cap. Direct production exposure under the running ladder; small, mirrors PositionExecutor's existing 15s pattern.
2. **A2** — per-executor try/except in orchestrator `stop()`. Protects ledger persistence on every restart.
3. **A5** — NaN sanitization in the MQTT serializer + `get_positions_report`. Protects the dashboard pipeline just repaired.
4. **B3 / B5 / B2** — TWAP bugs, config validators, barrier NaN guards. Do before deploying any stock strategy with real money.
5. **B9** — decide the refresh-trading-executors policy (global side-effect of a fork change).
6. **C1–C3** — simulator fixes before the next backtest-driven config decision.

---

## E. Verified SOUND (so future review knows what's already covered)

- Per-controller control loops genuinely isolate exceptions — one controller's raise cannot kill another (`runnable_base.py:67-74`, each controller its own `control_loop` task).
- `executor_info` runs every reported value through `_safe_decimal`, coercing non-finite Decimals to 0 — internal NaN misreports as 0 rather than crashing the API payload (risk is behavioral barrier-disarm, not serialization — except the MQTT path in A5(b)).
- PositionExecutor's NaN guards from the earlier incident are complete on stop-loss / take-profit / `trade_pnl_pct` / `get_net_pnl_pct` (`_is_valid_price`, `.is_finite()` checks present).
- PositionExecutor close path caps amount to available base/quote before placing, with a `CloseType.FAILED` dust-terminal path preventing insufficient-funds retry storms.
- Deferred-close-after-cancel state machine is sound with its 15s timeout fallback; `process_order_failed_event` classifies terminal vs transient correctly and doesn't double-count.
- `place_order` provenance tagging is wrapped so tagging failures never break order placement; TrackedOrders are only assigned after `place_order` returns an id (no phantom tracked orders).
- `_seed_wallet_balances` is single-shot per controller, guards NaN/zero reference price, defers correctly, skips perpetuals — no double-seed across retries or multiple controllers.
- Candle-feed teardown is leak-free (`CandlesBase.stop()` cancels the listen task; `MarketDataProvider.stop()` stops all and clears; `get_candles_feed` stops the old feed before replacing).
- Config list-length mismatches (spreads vs amounts) ARE validated by pydantic before runtime (`parse_and_validate_amounts`, dman's `parse_and_validate_dca_amounts`).
- The custom market-data-staleness machinery (`_check_market_data_freshness`, `executors_to_early_stop`) is robustly try/except-wrapped with hard/soft thresholds and rate-limited logging.
- `PerformanceReport` mutable defaults are per-instance (pydantic v2 deep-copies), not shared across instances.
- PnL-% divisions in `generate_performance_report` and breakeven divisions in `get_position_summary` are div-by-zero guarded.
- `store_actions_proposal` retains the newest `closed_executors_buffer` done executors and archives only older overflow (buffer is global across controllers by design).
- Grid `process_order_canceled_event`/`process_order_failed_event` reset the correct level slot and re-allow re-proposal; cancel accounting is sound.
- DCA `control_shutdown_process` re-fetches order state and handles the fill-after-cancel phantom-fill case on slow-REST exchanges.

---

## F. Per-executor lifecycle quick-reference (a=early_stop with live orders, b=place_order raises, c=NaN price)

- **OrderExecutor** — a: cancels open order, or holds a filled one (POSITION_HOLD) — but can hang if the cancel event is lost (A1). b: in try-wrapped loop, logged+retried, no untracked order. c: N/A (ladder supplies prices).
- **PositionExecutor** — a: cancels opens, market-closes filled position (or holds). b: caught by loop. c: **guarded** (`_is_valid_price`) — barriers/PnL return 0 safely. (SHUTTING_DOWN retry gap = B6.)
- **GridExecutor** — a: cancels open+close, market-closes position if ≥ min (or holds). b: in try-wrapped loop, retried. c: sizing NaN rejected by `amount>0`, but **stop-loss disarms** (B2) and construction can throw (B1).
- **DCAExecutor** — a: cancels opens + holds, or market-closes the delta (dust remainder silently NOT closed). b: caught. c: **stop-loss disarms** on NaN net_pnl (B2); half-init crash on min-size (B6).
- **TWAPExecutor** — a: cancels opens, stops (does not market-close accumulated base — it's an accumulator). b: failed *event* wipes the whole plan (B3). c: NaN amount order submitted (no gate) (B3).
- **XEMMExecutor** — a: cancels maker; an in-flight taker (market) may be left untracked. b: raise in `place_taker_order` propagates into the event dispatcher, not the loop. c: `_maker_target_price` can blow up / NaN (B8).
- **ArbitrageExecutor** — a: stops immediately, does NOT reconcile in-flight legs. b: failed *event* re-places instantly with no gate (double-place) (B8). c: NaN passes the `if not price` guard, profitability NaN, won't fire (safe) but log-floods.
- **LPExecutor** — DEX/Gateway-only, out of scope for this stack; parks awaiting manual intervention on max retries (no leak). Needs its own review if DEX is ever enabled.

---

*Generated from a 4-agent parallel review with manual verification. To continue exploring in a new chat, point Claude at this file; the file:line references are current as of branch `nonkyc` on 2026-07-13.*
