# Hummingbot Persistence, Session-Resumption & "Insufficient Funds on Relaunch" — Investigation

**Date:** 2026-07-15  **Engine:** `E:\tradingsoftware\hummingbot` @ `47c714a19` (branch `nonkyc`)  **API:** `E:\tradingsoftware\hummingbot-api` @ `0639c7a` (branch `nonkyc`)  **Exchange:** NonKYC

## Scope & Method

Evidence-based investigation of what survives when a bot is stopped, removed, recreated with the same name + controller YAML filenames, and relaunched against the same account and database — and why "insufficient funds" appears on relaunch.

Produced by a multi-agent workflow (all sub-agents on **Opus 4.8**): **15 parallel evidence agents** reading the engine + API source, **7 adversarial verifiers** stress-testing each root-cause hypothesis against code, and **3 synthesis writers**. Cross-checked against **5 independently-completed evidence reports** from a prior run and against **two crux facts the primary author verified by hand** (client-order-id non-reproducibility; V2 executor non-resume). Every substantive claim is tagged `[DOCUMENTED]` / `[CODE-CONFIRMED]` / `[KNOWN-DEFECT]` / `[HYPOTHESIS]` and cited to `file:line`, a doc URL, or an issue number. `Vn` markers reference the verdicts below.

## Verdict Summary (adversarial verification, all high-confidence)

| ID | Hypothesis tested | Verdict |
|----|-------------------|---------|
| **V1** | Non-reproducible client-order-ids + no cold-start adoption ⇒ prior orders become unrecognized orphans holding balance | **PARTIAL** — true *only* when the engine SQLite is not actually reused (fresh/renamed DB); with an intact DB, orders are restored from `MarketState` |
| **V2** | Strategy sizes new orders as if held/reserved balance were free ⇒ over-commit | **REFUTED** — `available` already excludes exchange-side holds; every sizing path clamps to it |
| **V3** | V2 never resumes live executors ⇒ fresh executors double-claim funds | **CONFIRMED** |
| **V4** | Startup race: orders placed before first balance snapshot | **PARTIAL** — strategy path is gated (REFUTED); the hummingbot-api *direct* order path is ungated (CONFIRMED) |
| **V5** | Uncommitted DB writes lost on hard kill ⇒ no record of in-flight orders | **PARTIAL** — orders + `MarketState` are committed per-event (durable); only live-executor/position running-state and `BotRun` close-out are lost |
| **V6** | Multiple controllers on one account each size against full balance, no global reservation | **CONFIRMED** |
| **V7** | Matching bot name + controller filenames is sufficient to resume | **REFUTED** |

## Bottom line (answers the core question)

**Matching the bot name + controller YAML filenames is _not_ sufficient to resume a session — and under the normal hummingbot-api deploy path it is actively defeated.** `[CODE-CONFIRMED V7]` The deploy routers append a `-YYYYMMDD-HHMMSS` timestamp to both the instance name and the generated script-config filename (`hummingbot-api/routers/bot_orchestration.py:501-504,589-590`), and the engine derives its SQLite filename from that config name (`hummingbot/core/trading_core.py:340-346`). So every "same-name" redeploy gets a **new instance directory with an empty `data/` and a fresh `.sqlite`** — the relaunched bot restores an empty in-flight-order tracker while the previous session's orders keep resting on NonKYC holding collateral. The new bot then sizes fresh orders against the (correctly holds-reduced) free balance, and their sum exceeds the wallet → NonKYC `20001` "Insufficient funds". That orphaned-order mechanism is the **single most likely cause** for this operator; duplicate executors after a non-graceful stop (V3) is the close second and co-occurs.

The engine's true re-attach anchors are the **`.sqlite` file** (from the *script-config source name*, not the bot name), the controller's **`id:` field** (inside the YAML, not the filename), the connector **`display_name`**, and the reused **`data/` bind-mount** — none of which the operator's name/filename matching guarantees.

---

## 1. State-Persistence Matrix

The engine (per-bot SQLite) and the API (single Postgres) are two independent persistence domains [CODE-CONFIRMED E1, A2]. For a Docker-launched bot, the engine's own SQLite inside the container is authoritative for that bot's trading state; the API's Postgres holds only orchestration/lifecycle rows plus 5-minute MQTT performance snapshots for that bot [CODE-CONFIRMED A2 §4]. "Survives container rm+recreate (same name, volume reused)" below assumes the data bind-mount is genuinely reused — which, under the normal API deploy path, it is **not**, because the instance name is timestamped and a fresh empty `data/` is created per deploy [CODE-CONFIRMED A1; see §2].

| State element | Where stored | Written when | Survives process exit? | Survives container rm+recreate (same name, volume reused) | Reconstructed from exchange? | Evidence |
|---|---|---|---|---|---|---|
| Bot definition / script config (`--conf`) | local file (`bots/instances/<inst>/conf/scripts/*.yml`); copied at deploy | At deploy (copied into instance conf) | Yes (host file) | Yes if same instance dir reused; else fresh copy | No | [CODE-CONFIRMED A1 `docker_service.py:230-274`, E6] |
| Controller config (YAML) | local file (`conf/controllers/<name>.yml`); copied per-instance | At deploy (copied); hot-reloadable at runtime | Yes (host file) | Yes if same dir reused | No | [CODE-CONFIRMED E6 `strategy_v2_base.py:129`, A1 `docker_service.py:234-260`] |
| Controller runtime record (`Controllers` row) | engine-SQLite (`Controllers`) | On `initialize_controllers` / `store_controller_config`, **insert-only, one row per launch** | Yes | Yes (in the .sqlite file) | No | [CODE-CONFIRMED E2 `markets_recorder.py:363-372`, E3 `controllers.py:12-13`] |
| Executor config | engine-SQLite (`Executors.config` JSON) | On create + on store (upsert on `Executors.id`) | Yes | Yes | No | [CODE-CONFIRMED E3 `executors.py:21`, E2 `markets_recorder.py:315-328`] |
| Executor runtime state / `is_active` | engine-SQLite (`Executors.status/is_active`) — **but read-only for reporting** | Only at graceful `stop()` (`store_all_executors`) or on completion (`StoreExecutorAction`); **no periodic live store** | Partially — only if last store fired; lost on SIGKILL for a live executor | Row survives, but **is NEVER used to resume a live executor** — feeds only PnL cache | No live resume; new executors created fresh | [CODE-CONFIRMED V3, V5; E4 `executor_orchestrator.py:205,207-212`; E2 `:387,391,1027-1029`] |
| Open / in-flight orders | engine-SQLite (`MarketState.saved_state`) + exchange-only (resting order) | Inline in the same txn as every order-create/fill/cancel event (per-event checkpoint, fsync'd) | **Yes** (committed synchronously per event) | **Yes — restored into `_order_tracker` on start** | Yes (order rests on exchange), but **not auto-adopted** if untracked | [CODE-CONFIRMED V1, V5; E2 `:432-451,518,654,837,945`; E5 `client_order_tracker.py:159-165`] |
| Order tracking (`MarketState`) | engine-SQLite (`MarketState`, keyed `(config_file_path, market)`) | Per order-lifecycle event | Yes | Yes | N/A | [CODE-CONFIRMED E2, E5; `market_state.py:9-17`] |
| Filled orders (`TradeFill`) | engine-SQLite (`TradeFill`, composite PK `market,order_id,exchange_trade_id`) | On each `OrderFilled` event (per-event commit) | Yes | Yes; dedupes on replay via composite PK | Yes — global `/account/trades` re-poll dedupes | [CODE-CONFIRMED E3 `trade_fill.py:28,33,41`, E2 `:562`] |
| Positions | engine-SQLite (`Position`, upsert on `(controller_id,connector,pair,side)`) + API-Postgres (`position_holds`) | Engine: at graceful `stop()` (`store_all_positions`). API: on POSITION_HOLD close | Engine: only if graceful stop; API: on close event | Yes (row survives); re-attached as **inert `PositionHold`**, not a live executor | Exchange net inventory reflected via balances, but position row is DB-sourced | [CODE-CONFIRMED E3, E4 `:218-284`, A2 §2b; V5] |
| Realized PnL | engine-SQLite (`Executors.net_pnl_quote`, `Position.realized_pnl_quote`) | On store/completion; `realized_pnl_offset` seeded at startup | Yes | Yes — re-loaded into PnL cache / `PositionHold.realized_pnl_offset` | No | [CODE-CONFIRMED E4 `:243-266`, E2 `:335-361`] |
| Unrealized PnL | in-memory (live executor) | Computed each tick by live executor | **No** — lost on exit | No (no live executor rebuilt) | Recomputed live once new executors run | [CODE-CONFIRMED E4 "LOST/orphaned"; V3] |
| Account balances | exchange-only (authoritative) → in-memory (`_account_balances`/`_account_available_balances`); API snapshots to Postgres `account_states` every 5 min | REST poll / WS balance event; API dump every 5 min | No (in-memory refetched); API snapshot persists | Refetched live from exchange | **Yes — always re-fetched from exchange** (free/held split incl. prior-order holds) | [CODE-CONFIRMED B1 `nonkyc_exchange.py:1957-1958`, `connector_base.pyx:49-50`; A2 §2c] |
| Budget allocations | **nowhere persisted** — recomputed each tick from live wallet | Each tick (`_preflight_budget_check`, `reset_locked_collateral`) | No | No — always re-derived from current balances | Derived from live exchange balances | [CODE-CONFIRMED E4 `:603-808,640`; V7 point 3] |
| Reconciliation / dedupe state (persisted trade-ids) | engine-SQLite (`MarketState.saved_state`, NonKYC `_PROCESSED_TRADE_IDS_STATE_KEY`) | Persisted with tracking states on each event | Yes | Yes — dedupe set survives restart | N/A | [CODE-CONFIRMED E5 `nonkyc_exchange.py:200-215`] |
| Bot run / session record | engine-SQLite (`BotRun`, per-launch UUID) + API-Postgres (`bot_runs`, per-deploy row) | Engine: on `MarketsRecorder.start()`; ends on `stop()`. API: on deploy | Yes; `ended_ts_ms`/`stop_reason` only on graceful stop (NULL after SIGKILL = crash marker) | Yes (new row per launch/deploy; never wiped) | No | [CODE-CONFIRMED E3 `bot_run.py:7-20`, E2 `:228-302`; A1 `bot_orchestration.py:547-556`] |
| Client-order-id continuity | **not reproducible** — embeds live µs nonce + PID/PPID | Generated per order at placement | N/A — new ids every process | No — cannot regenerate a prior id | Prior id recognized ONLY via restored `MarketState`, not regeneration | [CODE-CONFIRMED V1; E5 `utils.py:46-47,71-73`] |
| Exchange account identity / credentials | local file (`bots/credentials/<account>/connectors/<connector>.yml`, encrypted with global `CONFIG_PASSWORD`); copied into instance conf at deploy | Operator-set; copied at each deploy | Yes (host file) | Yes if same `credentials_profile` reused | Exchange knows account only by the API key inside the yml | [CODE-CONFIRMED A3 `accounts_service.py:578,91`, `docker_service.py:213,228`] |
| Logs | local file (`bots/instances/<inst>/logs/`) + host bind-mount | Continuously | Yes (host file) | Yes if same dir reused; archived on stop-and-archive | No | [CODE-CONFIRMED A1 `docker_service.py:302,312`] |

## 2. Bot & Controller Resumption Rules

### What makes a relaunch "the same bot/session"

There is no single bot-identity token. Re-attachment to prior **trading state** requires a conjunction of independent facts, and the identifiers the operator most naturally reuses (bot name, controller YAML filename) are **not** the ones the engine keys on [CODE-CONFIRMED V7; E6 §7].

**Identifier inventory — STABLE vs REGENERATED:**

| Identifier | Role | Stable or regenerated on relaunch/redeploy | Evidence |
|---|---|---|---|
| Engine `.sqlite` file name | Coarse bot boundary; from `db_name = _config_source` (V2 script config, ext stripped) | STABLE only if the `--conf` script-config name is unchanged | [CODE-CONFIRMED E6 `trading_core.py:340-346`, `sql_connection_manager.py:60-61`] |
| `config_file_path` | Row-level restore key for `MarketState`/`TradeFill`/`Order` lookups; = `_strategy_file_name` | STABLE only if strategy file name unchanged | [CODE-CONFIRMED E2 `trading_core.py:546`, E6 §5] |
| `Controllers.id` (Text) | The YAML `id:` field; the **only** key the orchestrator uses to reclaim executors/positions | STABLE if YAML `id:` unchanged; **independent of the YAML filename** | [CODE-CONFIRMED E6 `controller_base.py:68`, `executor_orchestrator.py:209-210`] |
| `Controllers.controller_id` (Integer PK) | Autoincrement surrogate; **naming trap — unrelated** to `Executors.controller_id` | REGENERATED every store (insert-only) | [CODE-CONFIRMED E3 `controllers.py:12-13`] |
| `Executors.id` (Text PK) | base58(SHA256(timestamp+random)); upsert key | STABLE per stored executor config; a *new* executor gets a new id | [CODE-CONFIRMED E3 `data_types.py:29-37`] |
| `Executors.controller_id` / `Position.controller_id` (Text) | Points to `Controllers.id` (the YAML `id:` string) — the durable re-attach link | STABLE if YAML `id:` unchanged | [CODE-CONFIRMED E6 §2, §4] |
| `client_order_id` | Per-order id; embeds live µs nonce + `md5(uname+pid+ppid)` | REGENERATED every process (PID/PPID change); **never reproducible** | [CODE-CONFIRMED V1; E5 `utils.py:46-47,71-73`] |
| `BotRun.id` (engine) / `bot_runs.id` (API) | Per-session / per-deploy record | REGENERATED every launch/deploy (new UUID / new PG row) | [CODE-CONFIRMED E3 `trading_core.py:352`; A1 `bot_run_repository.py:27-43`] |
| API `instance_name` → container → data dir | Bot name the operator types | **REGENERATED — timestamp appended** (`<name>-YYYYMMDD-HHMMSS`) → new dir, new container, empty `data/` | [CODE-CONFIRMED A1 `bot_orchestration.py:501-504,589-593`] |
| Generated script-config filename (controller deploys) | Drives the engine `.sqlite` name | REGENERATED — also timestamped → **fresh empty .sqlite per deploy** | [CODE-CONFIRMED A1 `bot_orchestration.py:502`; V7] |
| `account_name` / `credentials_profile` | Exchange account binding | STABLE — operator supplies the same string; the durable exchange-identity anchor | [CODE-CONFIRMED A3 `bot_orchestration.py:552,607`] |
| `CONFIG_PASSWORD` | Global credential-decryption key | STABLE (env var) | [CODE-CONFIRMED A3 `config.py:89-92`] |
| Docker `instance_id` / MQTT bot id | Engine instance label / MQTT namespace | REGENERATED (tracks timestamped instance name) | [CODE-CONFIRMED A3 `docker_service.py:278`, `mqtt_manager.py:126-128`] |

### Is matching bot name + controller filenames SUFFICIENT to resume in-flight orders / executors / positions / budgets?

**No — REFUTED on four independent counts [CODE-CONFIRMED V7 (high)].**

1. **Wrong keys.** Re-attachment keys on (a) the same `.sqlite` file (from the *script-config source* name, not the bot name) and (b) the controller's `id:` field *inside* the YAML (not the YAML filename). A YAML renamed but keeping `id:X` re-attaches; the same filename with a changed `id:` does not [CODE-CONFIRMED `executor_orchestrator.py:209-210`, `trading_core.py:340-346`].
2. **Live executors are never resumed.** `active_executors` starts empty; stored `Executors` rows feed only the PnL cache (`_update_cached_performance`), and no `ExecutorBase` is reconstructed. Triple-barrier (SL/TP/time-limit) state of a running executor is lost, and controllers create fresh executors on the next tick → **double exposure** on top of any still-resting prior orders [CODE-CONFIRMED V3 (high); `executor_orchestrator.py:205,207-212,899`].
3. **Budgets never persist.** Re-derived from scratch each tick against the live wallet; there is no persisted per-executor reservation to resume [CODE-CONFIRMED V7 point 3; `executor_orchestrator.py:603-808,640`].
4. **Deployment reality defeats even nominal name-matching.** The API deploy routers timestamp both the instance name and the generated script-config filename, so a same-named redeploy gets a **new empty `data/` and a fresh `.sqlite`** — re-attaching to nothing [CODE-CONFIRMED A1; V7 `bot_orchestration.py:501-504`].

**What DOES resume (when keys align):** only open/lost in-flight **orders** (rehydrated into the connector's `_order_tracker` from `MarketState.saved_state`), the NonKYC trade-id dedupe set, `TradeFill` history, and **inert `PositionHold`** accounting objects (realized-PnL offset + net inventory) — no live executors, no barriers, no budgets [CODE-CONFIRMED V7 point 4; E2 §4; E4 §"What DOES survive"].

**What additionally would be required to genuinely resume trading state:**
- Reuse the **exact same instance directory** (bypass the timestamping deploy path — e.g. restart the still-present container via `POST /docker/start-container/{name}` rather than redeploying) so the same `data/<config>.sqlite` is opened [CODE-CONFIRMED A1 §5, X3 §5].
- Keep the **script-config source name** and the **strategy file name** unchanged (drives `.sqlite` name and `config_file_path`) [CODE-CONFIRMED E6 §5].
- Keep each controller's **`id:` field** unchanged [CODE-CONFIRMED E6 §4].
- Keep the **connector `display_name`** unchanged (part of the `MarketState` key) [CODE-CONFIRMED E5].
- Accept that **live executors still will not resume** — the only safe pattern is a **graceful stop that flattens/cancels first**, then relaunch, because `ExecutorOrchestrator.stop()` early-stops executors and cancels orders; a crash/SIGKILL/`skip_order_cancellation` leaves orphaned resting orders that the relaunched process does not adopt or cancel by default [CODE-CONFIRMED V3, V7; `executor_orchestrator.py:371-395`; `nonkyc_exchange.py:944-945`].

## 3. Startup & Exchange-Reconciliation Timeline

Ordered sequence for a Docker-launched NonKYC V2 bot from container start to first order. Bracketed markers flag where orphaned prior orders and balance races bite.

1. **Container start → engine boot.** The API launches the container with a bind-mounted `/home/hummingbot/data` [CODE-CONFIRMED A1 `docker_service.py:311`]. Under the normal (timestamped) deploy path this `data/` is **empty**, so no prior `MarketState`/`TradeFill`/`Executors` rows exist [CODE-CONFIRMED A1; V7]. **⚠ BITE #1 (orphaned orders):** if any orders from a prior session are still resting on the exchange but the DB is fresh/empty (or `config_file_path`/`.sqlite` name changed), those orders are unrecognized from the outset [CODE-CONFIRMED V1 precondition; E5 §2 caveat].

2. **DB open + schema.** `SQLConnectionManager` opens `data/<config>.sqlite` (created if absent) and `create_all`s the schema; a new `BotRun` row (fresh UUID) is written [CODE-CONFIRMED E1 `sql_connection_manager.py:76-77`; E2 `:228-238`].

3. **`MarketsRecorder` warm-up.** Constructor back-fills each connector's dedupe structures from the DB (`get_trades_for_config`, `get_orders_for_config_and_market`) keyed on `config_file_path`, preventing duplicate-fill re-processing [CODE-CONFIRMED E2 `:105-112`].

4. **Connector network start.** `clock.add_iterator(connector)` → `_check_network_loop`; on first CONNECTED, `start_network()` spawns `_status_polling_task` and the user-stream tasks [CODE-CONFIRMED B1 `trading_core.py:682-688`, `exchange_py_base.py:716`].

5. **Trading-rules load.** NonKYC `/market` info populates `min_order_size`/`min_notional`; until loaded, order attempts fail locally with min-size/notional `ValueError`s (not exchange calls) [CODE-CONFIRMED B3 `nonkyc_exchange.py:1175-1184`, `exchange_py_base.py:450-462`].

6. **First balance snapshot.** `_status_polling_loop` waits on `_poll_notifier`; the first eligible tick triggers `_update_all_balances` (REST `GET /balances`), or a WS balance event lands first. `_account_available_balances[asset]` = exchange **free** (already reduced by prior-order `held`), `_account_balances[asset]` = free + held [CODE-CONFIRMED B1 `nonkyc_exchange.py:1957-1958,1985-1986`]. This means a fresh start's available balance **correctly excludes** collateral locked by pre-existing exchange orders — no missing subtraction, no double-count [CODE-CONFIRMED V2 (REFUTED over-commit-via-holds); B1 §2]. **⚠ BITE #2 (API-direct path only):** the hummingbot-api `POST /trading/orders` path has **no readiness gate and no forced balance refresh** — an order issued before this first snapshot sizes against an empty/stale cache and can hit a spurious/real exchange insufficient-funds reject (code 20001) [CODE-CONFIRMED V4 (API path CONFIRMED); B1 §3; `accounts_service.py:929`, `nonkyc_exchange.py:793-795`].

7. **In-flight-order restore.** `TradingCore._start_strategy_execution` calls `restore_market_states(strategy_file_name, market)` once per market, loading `MarketState.saved_state` by `(config_file_path, display_name)` and rebuilding open orders into `_order_tracker` with their original `client_order_id`/`exchange_order_id`; `is_open` → tracked, `is_failure` → lost-orders [CODE-CONFIRMED V1, V5; E5 `client_order_tracker.py:159-165`; `trading_core.py:544-546`]. If the DB is intact and keys match, prior orders **are recognized** and their later fills/cancels reconcile normally — the client-id being non-reproducible is irrelevant here [CODE-CONFIRMED V1 (PARTIAL); E5 §2].

8. **No cold-start exchange-wide order enumeration / adoption.** Routine polling only updates orders already in the tracker; the base connector never fetches all exchange open orders to adopt unknowns [CODE-CONFIRMED E5 §3 `exchange_py_base.py:1018-1023`]. NonKYC's `_reconcile_active_orders_after_reconnect` (which WARNs about orphans and books their held balance read-only, but never adopts or cancels) is triggered **only on WS reconnect**, not on cold startup [CODE-CONFIRMED V1 point 3; `nonkyc_api_user_stream_data_source.py:232-233`]. **⚠ BITE #3 (orphan holds persist):** untracked prior-session orders keep holding exchange collateral, are not auto-cancelled (`_cancel_exchange_orphans` defaults False), and only surface after the first WS drop [CODE-CONFIRMED B3 §3; `nonkyc_exchange.py:944-945`].

9. **Readiness gating (strategy path).** The strategy `tick()` sets `ready_to_trade = all(ex.ready for ex in connectors)` and returns without `on_tick()` until every connector is ready; connector `ready` requires `len(_account_balances) > 0` (i.e. a completed first balance snapshot) plus `user_stream_initialized` and order books [CODE-CONFIRMED V4 (strategy path REFUTED); B1 §3; `strategy_v2_base.py:356-361`, `exchange_py_base.py:183,194`]. A second `market_data_provider.ready` gate and the fork's `_startup_gate` (set only after `_seed_wallet_balances`, which itself defers if no valid mid-price) stack on top [CODE-CONFIRMED V4; `controller_base.py:275-276`, `strategy_v2_base.py:414-416,808-814`]. **No strategy order can precede the first balance snapshot** [CODE-CONFIRMED V4].

10. **Controller / executor init.** Controllers load and re-attach cached PnL + inert `PositionHold` by matching `controller_id` (YAML `id:`) against `strategy.controllers`; `active_executors` starts empty and **no live executor is reconstructed** [CODE-CONFIRMED V3, E4 `:205,207-212`].

11. **First order proposal.** On the first ready tick, controllers see an **empty live-executor list** and emit `CreateExecutorAction`s; `_preflight_budget_check` sizes each candidate against the (holds-reduced) live `get_available_balance`, dropping/resizing over-budget actions with `reason="insufficient_balance"` [CODE-CONFIRMED B2 `executor_orchestrator.py:713-748`; V2]. **⚠ BITE #4 (double exposure + budget race):** (a) fresh executors place new orders on top of any still-resting orphaned prior orders → double exposure [CODE-CONFIRMED V3]; (b) two+ controllers on the same account each size against the same flat balance with no cross-controller reservation (each preflight resets `_locked_collateral` and sees only its own batch), so their combined orders can exceed the wallet → the later order gets an exchange insufficient-funds reject [CODE-CONFIRMED V6 (high); `executor_orchestrator.py:640,785`, `budget_checker.py:33`]. The fork's `ledger_funded_budgets` mitigation applies **only to the range-ladder controller** and bounds each controller individually, not their sum [CODE-CONFIRMED V6; `range_inventory_ladder.py:3891-3896`].

**Net timing summary of where startup "insufficient funds" originates:** the exchange-side free/held split is always correct on a fresh poll, so the hypothesis of "reserved balance sized as free" is refuted [CODE-CONFIRMED V2]. The real startup insufficient-funds signatures are: (i) **orphaned prior-session orders** still holding collateral the strategy cannot see because the DB was empty/renamed (BITE #1/#3) [CODE-CONFIRMED V1, B3 §3]; (ii) **API-direct orders before the first balance poll** (BITE #2) [CODE-CONFIRMED V4]; (iii) **multi-controller same-account over-commit** in a single tick (BITE #4) [CODE-CONFIRMED V6]; and (iv) **duplicate fresh executors** stacking orders after a non-graceful shutdown (BITE #4) [CODE-CONFIRMED V3]. The diagnostic fingerprint the code itself emits is the `INSUFFICIENT FUNDS CONTEXT` WARNING (large `held` relative to `avail`) plus a post-reconnect `orphans` WARNING and non-zero `external_order_holds` [CODE-CONFIRMED B3 `nonkyc_exchange.py:829-858,262-266`].

---

## 4. PostgreSQL vs SQLite Comparison

There are **two independent databases** in this deployment, in two different repos, and conflating them is the root of most confusion around "the same database." Only one of them matters for whether a bot resumes.

- **Engine SQLite** — one `.sqlite` file *per bot instance*, written by Hummingbot's native `MarketsRecorder` **inside the bot container**. This is the authoritative store of that bot's orders, trade fills, order status, executors, controllers, positions, and the in-flight-order checkpoint. [CODE-CONFIRMED] `hummingbot/model/sql_connection_manager.py:57-63`, `hummingbot/connector/markets_recorder.py:85-135`.
- **API Postgres** — a *single* database (`hummingbot_api`, user `hbot`) owned by the hummingbot-api orchestration layer. It holds deployment/lifecycle rows and the API's own reporting/analytics tables, fed by the API's in-process event listeners and periodic dump loops — **not** by copying the engine SQLite. [CODE-CONFIRMED] `hummingbot-api/database/models.py`, `hummingbot-api/database/connection.py:16-17,45-46`.

| Aspect | Engine SQLite (per-bot) | API Postgres (single, orchestration) |
|---|---|---|
| Location | Host: `<BOTS_PATH>/bots/instances/<instance>/data/<config-basename>.sqlite`, bind-mounted to `/home/hummingbot/data` [CODE-CONFIRMED] `hummingbot-api/services/docker_service.py:301,311`; name derived from V2 `_config_source`, extension stripped [CODE-CONFIRMED] `hummingbot/core/trading_core.py:337-346` | Named Docker volume `postgres-data:/var/lib/postgresql/data` on container `hummingbot-postgres` (image `postgres:16`) [CODE-CONFIRMED] `hummingbot-api/docker-compose.yml:76-88,109` |
| What it stores | `Order`, `TradeFill`, `OrderStatus`, `Executors`, `Controllers`, `Position`, `MarketState` (the in-flight-order checkpoint blob), `BotRun`, `MarketData`, `Metadata` [CODE-CONFIRMED] `hummingbot/model/*` | `bot_runs`, `orders`, `trades`, `executors`, `position_holds`, `account_states`/`token_states`, `controller_performance_snapshots`, `funding_payments`, gateway tables [CODE-CONFIRMED] `hummingbot-api/database/models.py:8-471` |
| Who writes | The **engine `MarketsRecorder` inside the bot container**, via event listeners on the connector [CODE-CONFIRMED] `hummingbot/connector/markets_recorder.py:123-135,212-215` | The **API process itself**: `OrdersRecorder`/`FundingRecorder` listening on the *API's own* connectors, `ExecutorService` for API-hosted executors, periodic account/performance dumps [CODE-CONFIRMED] `hummingbot-api/services/orders_recorder.py:42-63`, `executor_service.py`, `accounts_service.py`, `bots_orchestrator.py:378-419` |
| Does it capture a Docker bot's live fills? | Yes — this is the primary and complete ledger for that bot [CODE-CONFIRMED] | **No.** `OrdersRecorder` only listens on API-owned connectors, not container bots; Docker-bot fills never land in Postgres `orders`/`trades`. Docker bots surface only via 5-min MQTT performance snapshots and `bot_runs` lifecycle rows [CODE-CONFIRMED] `hummingbot-api/services/unified_connector_service.py:608-612`, `bots_orchestrator.py:386` |
| Sync trigger / timing | Per-event, synchronous. Each order-lifecycle event is one committed transaction; the `MarketState` in-flight checkpoint is re-written **inside the same transaction** as each fill/create/cancel [CODE-CONFIRMED] `markets_recorder.py:478-479,518,654,837,945` | Mixed: orders/trades event-driven in-process (fire-and-forget task per event); a 60s order-status reconcile loop; account_states every **5 min**; controller_performance_snapshots every **5 min** [CODE-CONFIRMED] `accounts_service.py:94,276`, `bots_orchestrator.py:386` |
| Lag vs. exchange | ~0 for committed events (each fill fsync'd before handler returns) [CODE-CONFIRMED] | Balances ≤5 min stale, controller perf ≤5 min stale, order-status ≤60s stale; between snapshots Postgres is stale vs. both exchange and engine SQLite [CODE-CONFIRMED/INFERRED] `accounts_service.py:94,276` |
| Commit / durability | Plain synchronous SQLAlchemy engine `sqlite:///<path>` — no WAL, no PRAGMA overrides, so SQLAlchemy's default SQLite (rollback journal, `synchronous=FULL`) applies: **every commit is fsync'd before it returns** [CODE-CONFIRMED] `hummingbot/client/config/client_config_map.py:289`, `hummingbot/model/sql_connection_manager.py:75` (grep found no `journal_mode`/`connect_args`) | Async engine `postgresql+asyncpg://...`, `pool_pre_ping=True`, `pool_recycle=1800`; writes are best-effort with swallowed exceptions on several paths [CODE-CONFIRMED] `hummingbot-api/database/connection.py:19-35`, `bots_orchestrator.py:550-560` |
| Behavior on hard kill (SIGKILL / abrupt `docker rm`) | Orders/fills/order-status and the `MarketState` checkpoint are **safe** — anything committed before the kill survives [CODE-CONFIRMED, V5]. **Lost:** live-executor/live-position running-state and `BotRun.ended_ts_ms`/`stop_reason` (persisted only at graceful `stop()`) [CODE-CONFIRMED] `executor_orchestrator.py:387,391`; `markets_recorder.py:299-302` | A crash between an event and its fire-and-forget task loses that Postgres row; executors left `RUNNING` at API restart are force-tombstoned to `TERMINATED/SYSTEM_CLEANUP`, not resumed [CODE-CONFIRMED] `hummingbot-api/services/orders_recorder.py:77-79`, `executor_service.py:229-259`, `executor_repository.py:534-575` |
| Source of truth for **resumption** | **This is it.** On relaunch, `restore_market_states` reloads the connector's in-flight orders from `MarketState.saved_state`; the orchestrator re-attaches PnL/positions from `Executors`/`Position` rows [CODE-CONFIRMED] `hummingbot/core/trading_core.py:544-546`, `executor_orchestrator.py:207-230` | **Never used to resume a Docker bot.** For Docker bots it is reporting/lifecycle/archival only. It *is* authoritative for the API's **own** in-process executors/orders/position-holds (`_load_existing_orders`, `recover_positions_from_db`) — a different execution model than container bots [CODE-CONFIRMED] `unified_connector_service.py:867-894`, `executor_service.py:176-224` |
| Archiving relationship | The `.sqlite` is `shutil.move`d intact into `bots/archived/<instance>/data/` at stop-and-archive; later read read-only for reports [CODE-CONFIRMED] `hummingbot-api/utils/bot_archiver.py:52-53`, `file_system.py:474-496` | Archiving does **not** parse SQLite into Postgres — the tarball keeps the SQLite as-is; Postgres only records `bot_runs.deployment_status=ARCHIVED` [CODE-CONFIRMED] `bots_orchestrator.py:663-694` |

### What the operator means by "same database"

The operator relaunches "against the same account and database." For a **Docker-deployed bot, "the database" that governs resumption is the engine SQLite inside that bot's instance directory — not the API Postgres.** [CODE-CONFIRMED] The API Postgres is never read to resume a container bot; it is a reporting/lifecycle store. So:

- The **API Postgres is neither required nor sufficient** for a Docker bot to resume its trading state. It could be wiped and the bot would still restore its orders from its own SQLite (and vice-versa: an intact Postgres does nothing for a bot whose SQLite is fresh). [CODE-CONFIRMED, V7]
- The **engine SQLite is required but not sufficient.** Required: without the same `.sqlite` file (same `_config_source`-derived name, same bind-mounted `data/` dir, same connector `display_name`, same controller `id:`), the in-flight-order tracker restores empty and prior orders become unrecognized orphans [CODE-CONFIRMED, V1/V7] `trading_core.py:340-346,544-546`; `client_order_tracker.py:159-165`. Not sufficient: even with the SQLite intact, **live V2 executors are not resumed** — only the connector's order tracker and PnL/position accounting are [CODE-CONFIRMED, V3] `executor_orchestrator.py:205,207-212`.

**Critical deployment caveat [CODE-CONFIRMED, A1/V7]:** through the normal hummingbot-api deploy endpoints, a "same-named" redeploy **does not reuse the prior SQLite at all**. Both deploy routers append a `-YYYYMMDD-HHMMSS` timestamp to the instance name *and* to the generated V2 script-config filename (`hummingbot-api/routers/bot_orchestration.py:501-504,589-590`). Since the engine's `.sqlite` name is derived from that timestamped config source, each redeploy gets a **new instance dir with an empty `data/` and a fresh `.sqlite`** — so from the API's perspective, "same name" is a cold start that re-attaches to nothing. Genuine SQLite reuse only happens if a caller bypasses the routers and reuses an existing non-archived `bots/instances/<name>/` directory by exact name (`docker_service.py:215` guards only `makedirs`, so an existing `data/` is kept while `conf/` is wiped) [CODE-CONFIRMED] `docker_service.py:215-228`.

### Switching the ENGINE to Postgres (DBOtherMode) — does it change resumption?

The engine *code* supports pointing its trade DB at Postgres/MySQL/etc. via `db_mode: other_db_engine` in `conf_client.yml` (`db_engine: postgresql`, `db_host`, `db_port`, `db_username`, `db_password`, `db_name`), producing `postgresql://user:pass@host:port/db` [CODE-CONFIRMED] `client_config_map.py:292-334,902-913`; [DOCUMENTED] https://hummingbot.org/client/global-configs/external-db/. This is **separate from the API Postgres** — it replaces the *engine's* SQLite, not the API's `hummingbot_api`.

Resumption mechanics are unchanged in principle (restore still keys on `MarketState (config_file_path, market.display_name)` and controller `id:`), **but there is a serious caveat** [INFERRED, E1 §2]: `DBOtherMode.get_url` ignores the strategy-derived `db_name` (`client_config_map.py:322` uses only host/user/`db_name` config field), so the per-bot separation that SQLite gives via distinct file paths **collapses** — every engine bot pointed at the same `DBOtherMode` shares one Postgres database/schema, risking cross-bot `MarketState`/`Order` collisions. **Recommendation:** for multi-bot NonKYC deployments, keep the engine on default SQLite (one file per bot); use Postgres only at the API layer.

## 5. Ranked Root-Cause Analysis — "Insufficient Funds" After Relaunch

Ranked by likelihood **given the verdicts**, for an operator who relaunches with the same name, same controller YAML filenames, same account/DB, and made no manual exchange changes. Note up front: several intuitive explanations were **refuted** and are ranked low with an explanation of why they don't fire.

---

### Rank 1 — Orphaned prior-session orders holding exchange-side balance (DB not actually reused)
**Status:** [CODE-CONFIRMED] mechanism, gated on a [CODE-CONFIRMED] deployment precondition. (V1 PARTIAL, V7 REFUTED-of-the-naive-claim.)

**Mechanism.** The relaunched process only re-recognizes prior orders if it reopens the *same* `.sqlite` (same `_config_source`-derived name, same `data/` dir, same connector `display_name`) and restores `MarketState.saved_state` into `_order_tracker` [CODE-CONFIRMED] `trading_core.py:544-546`, `client_order_tracker.py:159-165`. If that DB is fresh/empty at relaunch, the tracker starts empty; client order IDs are non-reproducible (embed a live microsecond nonce + PID/PPID, `hummingbot/connector/utils.py:71-73,46-47`), so the process cannot re-derive prior IDs. The prior orders still rest on the exchange holding collateral, but the bot doesn't track them — and NonKYC never adopts or cancels these orphans by default (only WARNs, `nonkyc_exchange.py:256-266`; `_cancel_exchange_orphans` defaults False, `:944-945`). The strategy then sizes new orders against the *free* balance the exchange reports (correctly reduced by those holds, V2), so the *sum* of resting orphan orders + new orders can exceed the wallet → the later order is rejected with NonKYC `20001` "Insufficient funds" [CODE-CONFIRMED] `nonkyc_exchange.py:793-795`.

**Preconditions (the decisive one is the deployment path).** The persisted DB must be unavailable at relaunch: fresh/wiped/renamed `.sqlite`, different `config_file_path`, different connector `display_name`, or a container without the bind-mounted `data/`. **Through the normal hummingbot-api deploy routers this is the DEFAULT**, because the timestamped instance/config names guarantee a new empty `data/` and fresh `.sqlite` per deploy [CODE-CONFIRMED] `bot_orchestration.py:501-504,589-590`, `docker_service.py:215`. Additionally requires that prior orders were still resting on the exchange (not cancelled by a graceful stop).

**Diagnostic signature.** At startup, `INSUFFICIENT FUNDS CONTEXT` WARNINGs where logged `held` (= `total − avail`) is large relative to `avail` [CODE-CONFIRMED] `nonkyc_exchange.py:829-858`; on the first WS reconnect, a `Post-reconnect reconciliation: N ... orphans ... These hold balance on the exchange; cancel manually` WARNING [CODE-CONFIRMED] `:262-266`; non-empty `external_order_holds()` [CODE-CONFIRMED] `:301-359`; and on the exchange, resting orders whose IDs don't match any the new bot placed. On disk: the bot's instance dir is a *new* timestamped directory with an empty/near-empty `data/<config>.sqlite`, while the prior bot's orders live under the old timestamped instance (or `bots/archived/<old>/`).

**Fix / mitigation.**
1. **Before relaunch, cancel all open orders** — either graceful stop (which cancels tracked orders) or manual cancel on NonKYC. The code itself prescribes manual cancel for orphans (`:265`).
2. To make a redeploy genuinely reuse prior state, **reuse the exact same instance directory** (bypass the timestamping path) so the same `.sqlite`/`config_file_path`/`display_name` are reopened — otherwise accept that every API redeploy is a cold start.
3. As a last resort, enable `_cancel_exchange_orphans=True` **only with a dedicated API key** (it cancels exchange orders not in the local tracker per symbol, `:931-935`) — dangerous on a shared key.

---

### Rank 2 — Duplicate executors after a non-graceful stop (double exposure on one account)
**Status:** [CODE-CONFIRMED] (V3 CONFIRMED.)

**Mechanism.** V2 does not resume live executors. `active_executors[controller_id] = []` starts empty; `get_all_executors()` feeds only `_update_cached_performance` (PnL/volume), never a live `ExecutorBase` [CODE-CONFIRMED] `executor_orchestrator.py:205,207-212`. On the first ready tick, controllers see zero live executors and emit `CreateExecutorAction` → fresh orders on top of any pre-restart orders still resting [CODE-CONFIRMED] `strategy_v2_base.py:903,908`, `market_making_controller_base.py:1019`, `executor_orchestrator.py:898-899`. The connector may re-track the prior orders (via `MarketState`), but no executor re-adopts them, so new orders are added regardless — the combined claim can exceed the wallet → insufficient funds for the later order.

**Preconditions.** A V2 strategy had **live executors with open orders at a non-graceful shutdown** (SIGKILL / `kill -9` / power loss / OOM / abrupt `docker rm`, or a stop with `skip_order_cancellation`). A **clean** `ExecutorOrchestrator.stop()` `early_stop`s executors and cancels orders, avoiding this [CODE-CONFIRMED] `executor_orchestrator.py:371-395`. Requires the same DB/controller `id:` so the controller re-loads and its cached position re-attaches, plus the connector becoming ready so `on_tick` runs.

**Diagnostic signature.** Exchange shows two sets of resting orders at overlapping price levels after relaunch (old + new). Logs show fresh `CreateExecutorAction`/executor creation immediately after the readiness gate opens, with **no** log line indicating a prior executor was resumed (there is no such path). Engine SQLite `Executors` table has prior rows with `is_active/status=RUNNING` that are never re-instantiated. `Position` rows re-attach (realized-PnL offset) but place no orders.

**Fix / mitigation.** Always stop bots **gracefully** (dashboard STOP / API `stop-bot`, which cancel orders and flatten) before relaunch — the documented happy path is flat-then-restart [DOCUMENTED] https://hummingbot.org/dashboard/instances/. Avoid `skip_order_cancellation:true` unless you have an out-of-band adoption plan. There is no engine-level fix that resumes live executors — this is a design gap [KNOWN-DEFECT] hummingbot#6779, and the closest reported analogs are hummingbot#7489/#7230.

---

### Rank 3 — Overlapping controller budgets on one account (same-process or same-account)
**Status:** [CODE-CONFIRMED] (V6 CONFIRMED.)

**Mechanism.** One flat `_account_available_balances` dict per connector, keyed by asset with no controller scoping [CODE-CONFIRMED] `connector_base.pyx:50,406`. Each controller has its own `total_amount_quote` with no cross-controller coordination [CODE-CONFIRMED] `controller_base.py:71`. `BudgetChecker._locked_collateral` resets at the start and end of every preflight, and each controller's actions are consumed as a **separate batch** (`actions[0].controller_id`, one controller per `execute_actions`), so controller A's claim is invisible when controller B is checked [CODE-CONFIRMED] `budget_checker.py:33,55,60`, `strategy_v2_base.py:885-888`, `executor_orchestrator.py:640,785`. Both size against ~the full wallet; their sum exceeds it → the later fill gets `20001` [CODE-CONFIRMED] `order_candidate.py:184-186`, `nonkyc_exchange.py:793-795`.

**Preconditions.** ≥2 controllers/executors on the **same connector/account** competing for the same collateral asset, combined `total_amount_quote` > free wallet, emitting create-actions close in time (most acute in the same tick / at cold start before the first balance poll settles). Not both range-ladder controllers with `ledger_funded_budgets` partitioned so `Σ owned ≤ wallet` (that fork mitigation is scoped only to `range_inventory_ladder.py` and bounds each controller individually, not the sum) [CODE-CONFIRMED] `range_inventory_ladder.py:3891-3906`. Relevant here because the operator relaunches multiple NonKYC controllers.

**Diagnostic signature.** Insufficient-funds rejects concentrated at startup/first tick when multiple controllers share one connector; `budget_preflight_dropped`/`budget_preflight_resized` structured events with `reason="insufficient_balance"` on the *later* controller's batch [CODE-CONFIRMED] `executor_orchestrator.py:738-744`; each controller's own preflight logs look individually fine (each sees the full balance). Distinguished from Rank 1 by the **absence** of orphan warnings / large `held`.

**Fix / mitigation.** Set per-controller `total_amount_quote` so their sum ≤ wallet; or use `range_inventory_ladder` with `ledger_funded_budgets` (default ON) and seed the ledgers partitioned so `Σ owned_* ≤ wallet`; or use `shared_account_quote_quota` as a hard cap [CODE-CONFIRMED] `range_inventory_ladder.py:96-113,3912-3916`; or give each controller its own account/credential.

---

### Rank 4 — Startup / stale-balance timing race (API-direct path only)
**Status:** [CODE-CONFIRMED] for the API-direct path; the strategy path is [REFUTED]. (V4 PARTIAL.)

**Mechanism.** The **strategy path is safe** — three stacked gates (connector `ready` requiring `len(_account_balances) > 0`, `market_data_provider.ready`, and the fork's `_startup_gate` after wallet-seed) all block any order proposal until the first balance snapshot exists [CODE-CONFIRMED, V4] `strategy_v2_base.py:356-361`, `exchange_py_base.py:183,194`, `controller_base.py:275-276`. The race exists only on the **hummingbot-api direct `buy`/`sell` path**, which has **no `connector.ready` check and no forced balance refresh** before placing [CODE-CONFIRMED] `hummingbot-api/services/accounts_service.py:870,929`, `trading_service.py:268`. An order fired in the cold-start window (before the first `_update_balances`) hits the exchange with a caller-supplied amount; if that exceeds real free balance (e.g. because prior/orphan orders hold collateral not yet observed), the exchange returns `20001` [CODE-CONFIRMED] `nonkyc_exchange.py:793-795`. Note this is *not* the "sized against zero balance then over-committed" mechanism — the API path uses a caller-supplied amount, so it's a spurious/real insufficient-funds reject, not an over-sizing bug.

**Preconditions.** Only fires for **API-initiated (non-strategy) orders** issued within seconds of connector construction, without a prior `add_market` for that pair (which would force a refresh, `trading_service.py:177-179`). For a strategy/controller-driven bot (this operator's `v2_with_controllers`), this rank does **not** apply.

**Diagnostic signature.** Insufficient-funds reject on an order placed via `POST /trading/orders` (or equivalent direct API call) immediately after a connector is added, with no preceding balance-poll log line; strategy-driven bots show the "…is not ready. Please wait…" gate log instead of premature orders. Related upstream reports: [KNOWN-DEFECT] hummingbot#7827 (stale balance after cancel), #7068 (Kraken), #7499 (`validate_sufficient_balance` on restart).

**Fix / mitigation.** For the API-direct path, call `add_market`/force a balance refresh before the first order, or add a readiness/`_update_balances` gate. For strategy bots, no action needed. For poll-only lag generally, allow the balance poll (SHORT interval ~5s) to settle before deploying dependent orders [CODE-CONFIRMED] `exchange_py_base.py:40-41`.

---

### Rank 5 — Uncommitted DB writes lost on hard kill (as a *funds* cause)
**Status:** [REFUTED] as the stated cause for orders; [CODE-CONFIRMED] only for live-executor/position running-state. (V5 PARTIAL.)

**Mechanism / why it's low.** In-flight orders and `MarketState` are committed **synchronously per event** inside the same fsync'd transaction as each fill/create/cancel (default SQLite `synchronous=FULL`, no WAL) — a SIGKILL between events loses nothing already committed, and relaunch restores them [CODE-CONFIRMED] `markets_recorder.py:478-479,518,654,837,945`, `client_config_map.py:289`. So the claim "hard kill → no record of prior in-flight orders → can't reconcile" is **refuted**. What *is* lost on a non-graceful kill is live-executor/position running-state and `BotRun.ended_ts_ms/stop_reason` (persisted only at graceful `stop()`) [CODE-CONFIRMED] `executor_orchestrator.py:387,391` — but that feeds Rank 2 (duplicate executors), and its impact on funds is via non-resume design, **not** lost durability. Committing more durably would not change the outcome.

**Diagnostic signature.** `BotRun.ended_ts_ms = NULL` on the prior session row (crash marker) [CODE-CONFIRMED] `markets_recorder.py:299-302` — useful as *evidence of a hard kill* that then triggers Rank 1/2, not as an independent funds cause.

**Fix / mitigation.** Prefer graceful stops. Do not expect a durability tweak (e.g. WAL) to resolve insufficient-funds — the issue is not lost order writes.

---

### Rank 6 — Min-notional / precision / fee rejections misread as funds errors
**Status:** [CODE-CONFIRMED] as a *distinct* error class often confused with funds errors.

**Mechanism.** Sub-minimum orders are rejected locally *before any exchange call* with messages like "Order amount … is lower than minimum order size" / "… minimum notional size" [CODE-CONFIRMED] `exchange_py_base.py:450-462`. The base insufficient-funds classifier matches the very broad substring `"balance"` (plus "insufficient", "not enough", "oversold", `20001`, `30005`), so unrelated errors containing "balance" get labeled "Insufficient funds / inventory oversubscription" [CODE-CONFIRMED] `exchange_py_base.py:515-518`. `PositionExecutor` treats below-min local rejects as **terminal** alongside true insufficient-funds, so in retry behavior they look identical [CODE-CONFIRMED] `position_executor.py:31-54`. Additionally, buy-side fee headroom can push the last rung just over `avail` → a genuine exchange reject that is a fee-sizing issue, not a shortfall [CODE-CONFIRMED] `range_inventory_ladder.py:3918-3930`.

**Diagnostic signature.** The exception text says "lower than minimum order size/notional size" (local, no exchange round-trip) rather than NonKYC `20001`; or the rejected amount is exactly at a min-size/precision boundary; or only the *last/largest* rung fails while smaller ones succeed (fee headroom). Distinguished from Rank 1/3 by the absence of large `held`/orphan warnings and the presence of min-size/notional strings.

**Fix / mitigation.** Increase `total_amount_quote` above the min-notional threshold (controllers already pre-skip sub-min levels with a "Increase total_amount_quote" warning, `market_making_controller_base.py:845-887`); reserve fee headroom in buy sizing; read the exact error string before assuming a funds shortfall.

---

### Refuted-as-primary candidate — "Reserved balance not attributed to self"
**Status:** [REFUTED] (V2 REFUTED, high.)

The hypothesis that the strategy sizes new orders as if reserved (held) balance were free is **contradicted by the code**: NonKYC reports `available` = exchange free (already reduced by holds) and every sizing path — V2 preflight, classic PMM budget constraint, range-ladder ledger floor — clamps against this holds-reduced figure and scales down/drops over-budget orders [CODE-CONFIRMED] `nonkyc_exchange.py:1957,1985`, `connector_base.pyx:406`, `order_candidate.py:184`, `pure_market_making.pyx:866`, `range_inventory_ladder.py:3895`. If a prior order is *not* restored (fresh DB), the classic strategy under-commits (conservative), not over-commits. This is not a real root cause; it is folded into Rank 1 (the real issue is the *orphan* holding balance, correctly reflected as reduced free, colliding with new orders).

---

### Single most likely cause for THIS operator

**Rank 1 — orphaned prior-session orders holding exchange-side balance, because the "same" relaunch does not actually reuse the prior engine SQLite.** [CODE-CONFIRMED mechanism + CODE-CONFIRMED deployment precondition]

The operator's own description — *same name, same YAML filenames, same account/DB, no manual exchange changes* — is exactly the scenario where the hummingbot-api timestamping defeats the intended reuse: each redeploy of the "same" bot creates a new timestamped instance directory and a fresh empty `.sqlite`, so the relaunched bot starts with an empty in-flight-order tracker while the prior session's orders still rest on NonKYC holding collateral [CODE-CONFIRMED] `bot_orchestration.py:501-504,589-590`, `docker_service.py:215`, `trading_core.py:340-346`. NonKYC neither adopts nor cancels those orphans by default (`_cancel_exchange_orphans=False`), and only warns on WS reconnect [CODE-CONFIRMED] `nonkyc_exchange.py:256-266,944-945`. The free balance the new bot sees is genuinely reduced by those holds, so its fresh orders exceed available funds → `20001` insufficient funds.

Confirm it fast by checking, at startup: (a) `held` ≫ `avail` in the `INSUFFICIENT FUNDS CONTEXT` WARNING; (b) resting orders on NonKYC that the new bot's IDs don't match; (c) the new bot's `data/` dir is a fresh timestamped path with an empty `.sqlite`. Remedy: **cancel all open orders on NonKYC before every relaunch** (or stop gracefully so the prior session cancels its own), and if true resumption is wanted, reuse the exact same instance directory rather than redeploying under a name that gets re-timestamped. Rank 2 (duplicate executors) is the close second and co-occurs whenever the prior stop was non-graceful.

---

## 6. Read-Only Diagnostic Checklist

Everything below is **read-only**: no cancels, no edits, no `docker rm`. Collect the artifacts in the order given — logs first (they timestamp the failure), then engine SQLite (authoritative for the bot's own view), then API Postgres (orchestration view), then the live exchange (ground truth), then config (to explain *why* the state is what it is).

Repo roots: engine = `E:\tradingsoftware\hummingbot`, API = `E:\tradingsoftware\hummingbot-api`. Inside a bot container the engine paths map to `/home/hummingbot/{data,logs,conf}`; on the host they live under `E:\tradingsoftware\hummingbot-api\bots\instances\<instance>\{data,logs,conf}` [CODE-CONFIRMED `hummingbot-api/services/docker_service.py:301,311`].

### (a) LOGS — files and grep patterns

[CODE-CONFIRMED] The engine writes to `log_file_path: E:\tradingsoftware\hummingbot\logs` (`conf/conf_client.yml:21`). Standard files: `logs/logs_<strategy>.log` (human log) and `logs/structured_events.jsonl` (machine events, `hummingbot/logger/structured_event_logger.py`). For a containerized bot, the same files live under `bots\instances\<instance>\logs\`.

Collect these (PowerShell; all read-only). The literal strings match code emit sites.

```powershell
$L = "E:\tradingsoftware\hummingbot\logs"   # or bots\instances\<instance>\logs

# 1. The actual exchange-side insufficient-funds reject + the per-asset context dump
#    (nonkyc_exchange.py:793-795 classifier; :852 context WARNING with avail/held/total)
Select-String -Path "$L\*.log" -Pattern "Insufficient funds","INSUFFICIENT FUNDS CONTEXT","20001","order_reject_insufficient"

# 2. Orphaned prior-session orders holding balance (nonkyc_exchange.py:262-266)
#    Only fires on WS reconnect, NOT cold start — its presence means a WS drop already happened.
Select-String -Path "$L\*.log" -Pattern "not tracked locally \(orphans\)","Post-reconnect reconciliation","hold balance on the exchange"

# 3. Balance-settling gate (order creation paused; nonkyc_exchange.py:161)
Select-String -Path "$L\*.log" -Pattern "Balance settling: ACTIVE","order creation paused"

# 4. Budget preflight drops/resizes (executor_orchestrator.py:735,756) — LOCAL, not exchange
Select-String -Path "$L\*.log" -Pattern "insufficient balance","Budget preflight adjustments","Not enough budget to open position"

# 5. Order lifecycle around the failure — created / cancelled / failed
Select-String -Path "$L\*.log" -Pattern "Created .*order","Order .* has failed","Successfully cancelled"

# 6. First balance snapshot / readiness (proves whether a poll landed before orders)
Select-String -Path "$L\*.log" -Pattern "is not ready. Please wait","account_balance"

# 7. Executor / controller startup (fresh executors created after relaunch)
Select-String -Path "$L\*.log" -Pattern "Restarting controller","Creating .*Executor","INSUFFICIENT_BALANCE"
```

Structured events (JSONL) — same signals, machine-parseable. Event names are literal in code [CODE-CONFIRMED `nonkyc_exchange.py:809 order_submit_rejected`, `:162 balance_settling_entered`, `executor_orchestrator.py:738 budget_preflight_dropped`, `:765 budget_preflight_resized`]:

```powershell
Select-String -Path "$L\structured_events.jsonl" `
  -Pattern "order_submit_rejected","budget_preflight_dropped","budget_preflight_resized","balance_settling_entered"
```

Interpretation [INFERRED, grounded in B3/V1]: a large `held` relative to `avail` in the `INSUFFICIENT FUNDS CONTEXT` line + an orphan reconciliation WARNING = **prior-session orders still resting on the exchange holding collateral** (V1 confirmed case). A `budget_preflight_dropped` with `reason=insufficient_balance` but **no** exchange `20001` = the LOCAL guard caught it (no real reject); the strategy will retry next tick.

### (b) ENGINE SQLite queries

[CODE-CONFIRMED] The DB file is `data/<config-basename>.sqlite`, where the basename is the `--conf` script config with `.yml` stripped (`trading_core.py:340-346`, `sql_connection_manager.py:60-61`). For the sample configs here that is `data/conf_v2_with_controllers_nonkyc_xmr_usdt_ema.sqlite`. Open read-only (`file:...?mode=ro`) so you can never mutate it:

```powershell
# find the file first
Get-ChildItem "E:\tradingsoftware\hummingbot\data\*.sqlite","E:\tradingsoftware\hummingbot-api\bots\instances\*\data\*.sqlite"

$DB = "E:\tradingsoftware\hummingbot\data\conf_v2_with_controllers_nonkyc_xmr_usdt_ema.sqlite"
sqlite3 "file:$DB?mode=ro" ".mode column" ".headers on"
```

Then run (schema from E3):

```sql
-- In-flight / open orders the ENGINE thinks it owns (client_order_id = Order.id).
-- These are what restore_market_states re-adopts; compare against the exchange (§d).
SELECT id AS client_order_id, exchange_order_id, market, symbol, trade_type,
       last_status, creation_timestamp, bot_run_id
FROM "Order"
WHERE last_status NOT IN ('FILLED','CANCELED','FAILED','EXPIRED')
ORDER BY creation_timestamp DESC;

-- The persisted MarketState blob = the set of open/lost orders restored on relaunch.
-- If saved_state is '{}' or NULL, the connector starts with an EMPTY tracker (V1/V5 caveat).
SELECT config_file_path, market, timestamp, length(saved_state) AS blob_len,
       substr(saved_state,1,300) AS head
FROM MarketState ORDER BY timestamp DESC;

-- Executors marked active in the DB. NOTE (V3/E4): the V2 framework does NOT resume these
-- as live objects — is_active here is a stale snapshot from the prior session, not proof of
-- a running executor. Rows with is_active=1 whose orders are still open on the exchange are
-- the double-exposure candidates.
SELECT id, controller_id, type, status, is_active, is_trading, close_type,
       filled_amount_quote, net_pnl_quote, timestamp, close_timestamp
FROM Executors WHERE is_active = 1 ORDER BY timestamp DESC;

-- Positions re-attached across relaunch (upsert key = controller_id+connector+pair+side).
SELECT controller_id, connector_name, trading_pair, side, amount,
       breakeven_price, realized_pnl_quote, cum_fees_quote, timestamp
FROM Position ORDER BY timestamp DESC;

-- Controllers table APPENDS one row per launch (no upsert, autoincrement PK).
-- Count rows per id to see how many times this bot has been (re)launched.
SELECT id, type, COUNT(*) AS launch_rows, MAX(timestamp) AS last_launch
FROM Controllers GROUP BY id, type;

-- Session history: each BotRun = one launch. ended_ts_ms IS NULL => that session was
-- SIGKILLed / crashed (never graceful-stopped) => executors/positions from it were NOT
-- persisted at stop and left orders resting (V5 confirmed case).
SELECT id, started_ts_ms, ended_ts_ms, stop_reason, strategy_name, config_file_path
FROM BotRun ORDER BY started_ts_ms DESC LIMIT 10;

-- Recent fills (exchange-anchored composite PK => dedupe-safe across relaunch).
SELECT market, order_id, exchange_trade_id, symbol, trade_type, amount, price,
       timestamp, bot_run_id
FROM TradeFill ORDER BY timestamp DESC LIMIT 20;
```

Decision points [INFERRED from E2/E3]: `MarketState.saved_state` non-empty + open `Order` rows + those `exchange_order_id`s still live on the exchange (§d) ⇒ the connector *will* re-adopt them (no duplication from the connector layer). `Executors.is_active=1` with a `BotRun.ended_ts_ms IS NULL` for the same session ⇒ live executors were orphaned at a hard kill; the relaunch will create fresh ones on top (V3).

### (c) API Postgres queries

[CODE-CONFIRMED] Container `hummingbot-postgres`, db `hummingbot_api`, user `hbot`, pw `hummingbot-api` (`hummingbot-api/docker-compose.yml:76-88`). This is the orchestration/reporting DB — **it does not hold the Docker bots' fills** (those are in the per-bot SQLite; A2). Use it to see deploy history and whether names collided.

```bash
docker exec -it hummingbot-postgres psql -U hbot -d hummingbot_api
```

```sql
-- All deploys of a given user-facing name. Because the API appends -YYYYMMDD-HHMMSS
-- (A1: bot_orchestration.py:501-504,589-590), each redeploy is a NEW row / instance_name.
-- Many rows for one base name = repeated redeploys, each a fresh empty sqlite (V7).
SELECT id, bot_name, instance_name, account_name, strategy_type,
       deployment_status, run_status, deployed_at, stopped_at
FROM bot_runs
WHERE bot_name LIKE 'nonkyc%' OR instance_name LIKE 'nonkyc%'
ORDER BY deployed_at DESC;

-- Sessions that never cleanly stopped (crash markers on the API side).
SELECT instance_name, deployment_status, run_status, deployed_at, stopped_at
FROM bot_runs WHERE run_status NOT IN ('STOPPED') ORDER BY deployed_at DESC;

-- How many distinct bots point at the SAME exchange account (overlapping-balance hazard, A3/V6).
SELECT account_name, COUNT(DISTINCT instance_name) AS distinct_instances, COUNT(*) AS runs
FROM bot_runs GROUP BY account_name ORDER BY runs DESC;

-- API-hosted executors/orders (only populated for API-DRIVEN trading, not Docker bots).
SELECT executor_id, account_name, controller_id, executor_type, status, is_active,
       net_pnl_quote, updated_at FROM executors WHERE status = 'RUNNING';

SELECT client_order_id, exchange_order_id, account_name, connector_name, trading_pair,
       status, filled_amount, created_at FROM orders
WHERE status IN ('SUBMITTED','OPEN') ORDER BY created_at DESC;

-- Position-hold rows: two bots sharing account_name + default controller_id 'main' on one pair
-- COLLIDE on this unique key (A3) — conflated balances/PnL.
SELECT account_name, connector_name, trading_pair, controller_id, status,
       realized_pnl_quote, executor_ids FROM position_holds WHERE status = 'ACTIVE';
```

### (d) Exchange / live account data (ground truth, read-only)

[CODE-CONFIRMED] The connector reads these same endpoints. Query them directly with your NonKYC key to see what the exchange *actually* holds — this is authoritative for "is there orphaned balance."

- **Open orders**: `GET /account/orders?status=active` — the exact call the connector uses for orphan detection (`nonkyc_exchange.py:235-238`, `ACCOUNT_ORDERS_PATH_URL`). Cross-reference each returned order's id against the engine SQLite `Order.exchange_order_id` / `MarketState.saved_state` from §b. Any live order **not** in the engine's tracker = an orphan holding balance [V1 confirmed].
- **Balances (available vs total)**: `GET /balances` (`nonkyc_exchange.py:1941`, `USER_BALANCES_PATH_URL`). Compute `held = total_reported − available` per asset. NonKYC's `held` field = "funds locked in open orders" [DOCUMENTED `nonkyc_exchange.py:1952-1956`]. A large `held` on the quote asset with few tracked orders = collateral locked by orders the bot doesn't see.

```bash
# read-only; requires signed auth per the NonKYC auth scheme
curl -s "https://api.nonkyc.io/api/v2/account/orders?status=active"   # sign per nonkyc_auth
curl -s "https://api.nonkyc.io/api/v2/balances"
```

The arithmetic that matters [INFERRED, grounded in B1/B3]: if exchange `available < (what your controller's total_amount_quote wants to deploy)` **and** `held` is large, prior/orphan orders are the cause. If exchange `available` is fine but the bot still logs insufficient funds, suspect a stale-cache/timing race (V4 API-direct path) not orphans.

### (e) Config / bindings to capture

Capture these files verbatim (they explain the identity keys from V7/E6):

- Script config: `conf/scripts/conf_v2_with_controllers_nonkyc_xmr_usdt_ema.yml` — its filename (minus `.yml`) **is the SQLite DB name** (`trading_core.py:340-346`). Note `controllers_config:` (the list of controller YAML filenames).
- Controller YAMLs: `conf/controllers/nonkyc_xmr_usdt_ema_regime_hold_v1.yml` etc. — capture the **`id:` field** (e.g. `id: nonkyc_xmr_usdt_ema_regime_hold_v1`). This `id`, not the filename, is the re-attach key (`executor_orchestrator.py:210`; V7). Also capture each controller's `connector_name` and `total_amount_quote` (sum them per account to spot over-subscription; V6).
- `conf/conf_client.yml` — capture `db_mode` (default `DBSqliteMode`; `client_config_map.py:772`) and `instance_id`.
- Account binding: which `credentials_profile` the deploy used (= `bot_runs.account_name`), and the connector YAML under `bots/credentials/<account>/connectors/nonkyc.yml` [CODE-CONFIRMED A3 — the API key inside is the true exchange identity]. Note if **more than one** instance used the same profile.
- Volume mounts: confirm `bots/instances/<instance>/data` is bind-mounted to `/home/hummingbot/data` (`docker_service.py:311`) and whether a prior same-named non-archived instance dir exists (the only reuse path; V7/A1).

---

## 7. Controlled Reproduction Matrix

Each row isolates one candidate cause. Run against a throwaway account/pair with tiny sizes. "Volume reused" means relaunch into the **same** engine `data/` dir with the **same** `.sqlite` (bypass the API's timestamping, or restart the same container); "volume wiped/fresh" means a new `data/` with no `.sqlite` (the normal API redeploy behavior — A1/V7). Map to ranked causes: **C1** = orphaned prior orders holding balance (V1, B3); **C2** = V2 executor non-resume → duplicate executors (V3, V4-strategy refuted so this is the real double-exposure path); **C3** = overlapping controller budgets on one account (V6); **C4** = API-direct cold-start race (V4-API); **C5** = hard-kill loses live-executor/position state at graceful-stop-only sites (V5).

| # | Scenario | Setup | Action | Expected if that cause is real | Observed signal to record |
|---|---|---|---|---|---|
| 1 | **Control: zero open orders at stop** | Single controller. Let it reach flat/idle (no resting orders). | Graceful `stop` (cancels orders), then relaunch, **volume reused**. | No insufficient funds; no orphans. Baseline. | §d shows 0 open orders; no `INSUFFICIENT FUNDS CONTEXT`; `MarketState.saved_state` ≈ `{}`. If this fails, the problem is not relaunch-specific. |
| 2 | **Open orders left, graceful stop** | Single controller with resting bids/asks. | `stop` **with** order cancellation (default), relaunch, **volume reused**. | Orders cancelled at stop → no orphans, no double-claim. Confirms graceful stop is the mitigation (V1/V3). | §d = 0 open orders after stop; relaunch places fresh orders cleanly; no `20001`. |
| 3 | **Open orders left, HARD KILL** (isolates C1+C2+C5) | Single controller with resting orders + one live executor. | `docker kill` / `docker rm -f` (SIGKILL, no graceful stop), relaunch, **volume reused**. | Prior orders **still on book** (V1): connector re-adopts orders it persisted (via MarketState), but the executor that owned them is NOT resumed (V3) → controller creates a fresh executor → new orders on top → later order hits `20001`. `BotRun.ended_ts_ms` NULL (V5). | §d shows prior orders still open; SQLite `Executors.is_active=1` from prior session; log shows fresh `Creating ...Executor` + `INSUFFICIENT FUNDS CONTEXT` with large `held`. **Confirms C1+C2+C5.** |
| 4 | **Open orders left, hard kill, VOLUME WIPED** (isolates C1 pure) | Same as #3. | Hard kill, relaunch into **fresh/empty `data/`** (normal API redeploy). | Connector tracker restores EMPTY (no MarketState) → prior orders are pure orphans, only WARNED on WS reconnect, never adopted/cancelled (V1 caveat, B3). Held balance invisible → new orders rejected. | Log: `Post-reconnect reconciliation: N ... orphans` **after** first WS drop; `external_order_holds` nonzero; `20001` on placement. **Confirms C1** and shows wiped-volume makes it worse. |
| 5 | **Two controllers, one account, same asset** (isolates C3) | Two controllers on the **same** connector/account, each `total_amount_quote` summing > free wallet, **not** ledger-partitioned range-ladder. | Fresh start, let both propose in the same tick. | Both size against the full shared `_account_available_balances` (V6); combined orders exceed wallet; the later order rejected. | Two near-simultaneous create batches; second gets `20001`; `budget_preflight` passed both because each batch saw full balance. **Confirms C3.** Independent of relaunch. |
| 6 | **Two controllers, ledger-funded range-ladder** (C3 mitigation check) | Two `range_inventory_ladder` controllers, `ledger_funded_budgets: true`, seeded so Σ owned ≤ wallet. | Fresh start, both propose. | Each clamps to `min(owned_free, avail)` (V6, `range_inventory_ladder.py:3891-3896`); no over-commit **if** ledgers partitioned. | No `20001`; each controller's deployed notional ≤ its owned ledger. Confirms the mitigation and that stock controllers lack it. |
| 7 | **Single controller, volume reused vs wiped** (isolates re-attach key) | Same controller `id:`, same `--conf` name. | Relaunch (a) reusing `data/`+`.sqlite`; (b) into fresh `data/`. | (a) re-adopts orders/positions (V1/V7); (b) starts cold, prior orders orphaned. Proves the .sqlite file — not the bot name — is the anchor. | (a) `MarketState` restored, positions continue; (b) empty tracker, orphan warnings. **Distinguishes C1-with-DB from C1-without-DB.** |
| 8 | **API-direct order, cold start** (isolates C4) | API `POST /trading/orders` (or `/executors` direct path) issued immediately after connector construction, before first `_update_balances`. | Place a caller-sized order in the first ~5s window. | No readiness gate on the API-direct path (V4); order sizes against empty/stale cache; exchange rejects if amount > true free. | `20001` with no preceding balance snapshot log; only on the API-direct path, never the strategy path. **Confirms C4.** |
| 9 | **Balance drained near used amount** (edge, amplifies C1/C3) | Free balance ≈ exactly the strategy's intended deploy. | Relaunch with any small residual hold from #3/#5. | Even a tiny orphan hold tips the next order below required collateral → `20001`. | Small `held`, `avail` just under required; single order rejected while others succeed. Shows sensitivity, not a distinct cause. |
| 10 | **Min-notional edge** (rules out false positive) | Order size at/below `min_order_size`/`min_notional`. | Place. | Local rejection with "lower than minimum order/notional size" (`exchange_py_base.py:450-462`) — **not** an exchange funds reject, and it never reaches the exchange (B3). | Log says "minimum order size/notional", **no** `20001`, no balance context. Eliminates budget as cause. |

Reading the matrix: if #3/#4 reproduce your symptom, the root cause is **C1 (orphaned prior orders) + C2 (executor non-resume)** — a hard-kill/relaunch problem. If only #5 reproduces, it is **C3 (overlapping budgets)** — a configuration problem independent of relaunch. If only #8 reproduces, it is **C4 (API-direct cold-start race)**. #10 rules out a mislabeled min-notional rejection.

---

## 8. Safe Recovery & Operating Procedure

Ordered to guarantee **no duplicate orders/executors/positions**. Each step cites the code reality that makes it necessary. Destructive operations are flagged ⚠.

### Before stopping (record + flatten cleanly)

1. **Prefer a graceful stop, and let it cancel orders.** [CODE-CONFIRMED] The engine `stop` cancels active orders and runs the orchestrator's graceful sweep (`executor_orchestrator.py:371-395`: `early_stop()` all executors → `store_all_executors()` → `store_all_positions()`). This is the *only* path that both cancels resting orders and persists final executor/position state (V5). Via the API use `POST /bot-orchestration/stop-bot` **without** `skip_order_cancellation` (default cancels; X1/X3). Avoid `skip_order_cancellation: true` unless you have a deliberate reason — it leaves orders resting with no documented reconciliation contract (X1).
2. **Confirm the stop actually completed graceful shutdown.** [CODE-CONFIRMED V5] After stop, check engine SQLite: `SELECT ended_ts_ms, stop_reason FROM BotRun ORDER BY started_ts_ms DESC LIMIT 1;` — `ended_ts_ms` must be **non-NULL**. NULL means the process was killed before the sweep ran, so executors/positions were **not** persisted and orders may still be resting.
3. **Never rely on SIGKILL / `docker rm -f` / `docker kill` as your stop.** [CODE-CONFIRMED V3/V5] A hard kill bypasses `stop()`, so live executors are orphaned, their triple-barrier state is lost, and their orders stay on the book. That is the primary trigger for the whole failure.

### Before relaunching (verify the exchange is clean)

4. **List live open orders on the exchange and reconcile against the engine's view.** [CODE-CONFIRMED §6d] Call `GET /account/orders?status=active`. Cross-check each `exchange_order_id` against engine SQLite `Order.exchange_order_id` for open rows and against `MarketState.saved_state`. Any live order absent from both = an **orphan holding balance** that the relaunched bot will not adopt by default (V1, B3).
5. **Check available vs held balance.** [CODE-CONFIRMED §6d] `GET /balances`; compute `held = total − available`. If `held` is large relative to what your tracked orders justify, orphaned collateral is the cause of the insufficient-funds errors (B3).
6. **If orphans exist, cancel them safely and deliberately — do not let the bot do it silently.** [CODE-CONFIRMED B3/V1] The connector does **not** auto-cancel orphans (`_cancel_exchange_orphans` defaults False; `nonkyc_exchange.py:944-945`). Cancel them yourself before relaunch, either:
   - **Manually on the exchange** by order id (the code's own recommended remedy — `nonkyc_exchange.py:265` "cancel manually"), or
   - Per-symbol via the exchange's cancel-all-orders endpoint if you are certain no other bot/manual order shares the account.
   ⚠ Do **not** enable `_cancel_exchange_orphans=True` on a **shared** API key — it will cancel *any* untracked order on the account, including other bots' and manual orders. Only use it with a dedicated single-bot key.
7. **Wait until orphans are gone and balances settled** before relaunch (re-run steps 4–5 until open-order list matches expectations and `held` drops).

### Preserve vs reset the engine SQLite / volume

8. **Decide re-attach vs cold start explicitly — this is the single most important choice.** [CODE-CONFIRMED V7/E6]
   - **To CONTINUE prior positions/orders**, you must keep **all three** anchors: same `.sqlite` file (same `--conf` script config basename), same `data/` directory, and unchanged controller `id:` field. Then the connector re-adopts open orders (via `MarketState`) and positions re-attach (via `controller_id`). ⚠ Note the API's normal redeploy path **timestamps the instance name and script-config filename**, producing a **fresh empty `.sqlite`** and re-attaching to nothing (A1/V7). To truly resume, restart the **existing container** (`docker start <container>`), or reuse the existing non-archived instance dir by exact name — do not redeploy through the timestamping router.
   - **To start CLEAN** (recommended after any hard-kill incident with unresolved orphans), first complete steps 4–6 (cancel every orphan on the exchange), then relaunch into a fresh volume. ⚠ **Preserve, do not delete, the old `.sqlite`** — move/copy it aside for forensics rather than `rm`. Deleting it loses your fill history and the record of what was open.
9. **Do not delete a `.sqlite` while its orders are still live on the exchange.** [CODE-CONFIRMED V1] That converts tracked orders into permanent orphans (the exact #4 scenario in §7). Cancel on the exchange first (step 6), *then* archive the DB.

### Avoid overlapping controller budgets (one account)

10. **One account → one budget owner, or explicit per-controller allocation.** [CODE-CONFIRMED V6/B2] Multiple controllers on the same connector share one flat `_account_available_balances` with **no global reservation**; each sizes against the full free balance and their sum can exceed the wallet (V6). Choose one:
    - Run a **single** controller per exchange account/asset; or
    - Set each controller's `total_amount_quote` so the **sum across all controllers on that account ≤ free wallet** (there is no engine-side enforcement — you must budget it yourself); or
    - Use `range_inventory_ladder` controllers with `ledger_funded_budgets: true` **and** seed the ledgers so Σ owned ≤ wallet (V6/B2 — the `min(owned_free, avail)` clamp only bounds each controller individually, so partitioning at seed time is mandatory).
11. **Do not run two bots on the same `credentials_profile` unless you have partitioned budgets.** [CODE-CONFIRMED A3] Nothing prevents two containers from sharing one API key and drawing down the same exchange balance; with the default `controller_id: "main"` on the same pair they also collide on one `position_holds` row (A3/V6).

### db_mode recommendation

12. **Keep engine `db_mode` on the default SQLite (per-bot file).** [CODE-CONFIRMED E1] Each bot gets its own `data/<config>.sqlite`, which is what the per-instance re-attach model depends on. ⚠ Do **not** point multiple engine bots at one shared `DBOtherMode` (Postgres/MySQL): `DBOtherMode.get_url` ignores the per-bot `db_name`, so all bots would collide in one shared database/schema (E1). The API's own Postgres is separate and is not the engine's trade DB — leave it as configured.

### Pre-launch checklist (run every relaunch)

- [ ] Previous session stopped **gracefully**; `BotRun.ended_ts_ms` non-NULL (step 2).
- [ ] `GET /account/orders?status=active` returns **only** orders you intend to keep; all orphans cancelled (steps 4, 6).
- [ ] `GET /balances`: `available` is sufficient for the intended deploy; `held` explained by known orders (step 5).
- [ ] Re-attach decision made: either all three anchors preserved (same `.sqlite` + `data/` + controller `id:`), or a clean-start with old `.sqlite` archived aside — **not** deleted while orders live (steps 8–9).
- [ ] Sum of `total_amount_quote` across all controllers on the account ≤ free wallet, or ledger-partitioned range-ladder in use (step 10).
- [ ] Only one budget owner per account, or budgets explicitly partitioned; no accidental second bot on the same `credentials_profile` (step 11).
- [ ] `db_mode` = default SQLite; each bot has its own `data/` bind mount (step 12).
- [ ] Relaunch method matches the re-attach decision: `docker start <existing-container>` to resume, or a fresh deploy to cold-start (remember the API timestamps names → fresh DB; A1/V7).
- [ ] If using the API-direct order path, do not issue orders in the first few seconds after connector construction (no readiness gate there; V4/C4).

Grounding note on why this ordering works: the connector *does* durably persist and restore its own open in-flight orders per-event (V1/V5 refuted the "writes are lost" fear), so a graceful stop + intact `.sqlite` gives a clean resume. The residual risk is entirely at the **executor** layer (never resumed as live objects — V3) and the **orphan** layer (never auto-adopted/cancelled — V1/B3). Steps 1–7 close the orphan gap on the exchange; steps 8–9 keep the connector's own restore path intact; steps 10–12 close the shared-balance gap. A hard kill defeats all of this, which is why steps 1–3 are first.


---

## Appendix — Evidence provenance

- **Engine (SQLite domain):** `sql_connection_manager.py`, `client_config_map.py` (DBSqliteMode/DBOtherMode), `__init__.py` (`data_path`), `trading_core.py` (recorder init, `restore_market_states`), `connector/markets_recorder.py`, `model/{order,order_status,trade_fill,position,executors,controllers,bot_run,market_state,metadata}.py`, `strategy_v2/executors/executor_orchestrator.py`, `executor_base.py`, `data_types.py`, `connector/client_order_tracker.py`, `connector/utils.py`, `core/utils/tracking_nonce.py`, `connector/exchange/nonkyc/nonkyc_exchange.py`, `budget_checker.py`.
- **API (Postgres domain):** `routers/bot_orchestration.py`, `services/docker_service.py`, `services/bots_orchestrator.py`, `services/accounts_service.py`, `services/orders_recorder.py`, `services/executor_service.py`, `database/{models,connection}.py`, `database/repositories/*`, `utils/{bot_archiver,file_system,hummingbot_database_reader}.py`, `docker-compose.yml`, `init-db.sql`.
- **Independently hand-verified crux facts:** client-order-id embeds a live microsecond nonce + `md5(uname+pid+ppid)` → non-reproducible across processes (`connector/utils.py:46-47,71-73`); `ExecutorOrchestrator._initialize_cached_performance` resets `active_executors=[]` and only caches PnL/positions — no live executor is rebuilt (`executor_orchestrator.py:197-230`).
- **Full agent transcripts / salvaged reports:** `<session>/salvage_fable/` (E1 db-storage, E2 markets-recorder, E3 models-schema, E4 executor-restart-CRUX, A1 api-lifecycle) and the Opus synthesis sections (`opus_S1/S2/S3`, `opus_verdicts.json`).
