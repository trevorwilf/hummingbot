clear-host

# ====== TIER 0: NEW TESTING INFRASTRUCTURE VALIDATION ======

# 0a. Verify pytest markers are registered
python -m pytest --markers 2>&1 | Select-String -Pattern "quarantined|live_api|slow"

# 0b. Verify main lane excludes quarantined + live_api
python -m pytest --collect-only -m "not quarantined and not live_api" test/ 2>&1 | Select-Object -Last 5

# 0c. Verify quarantined lane collects the right tests
python -m pytest --collect-only -m "quarantined" test/ 2>&1 | Select-Object -Last 5

# 0d. Verify live_api lane collects only live tests
python -m pytest --collect-only -m "live_api" test/ 2>&1 | Select-Object -Last 5


# ====== TIER 1: DIRECTLY AFFECTED TEST SUITES ======

# 1a. Market Making Controller Base
python -m pytest test/hummingbot/strategy_v2/controllers/test_market_making_controller_base.py -v --tb=short 2>&1

# 1b. Executor Orchestrator
python -m pytest test/hummingbot/strategy_v2/executors/test_executor_orchestrator.py -v --tb=short 2>&1

# 1c. NonKYC Order Book Data Source
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_api_order_book_data_source.py -v --tb=short 2>&1

# 1d. Budget Checker
python -m pytest test/hummingbot/connector/test_budget_checker.py -v --tb=short 2>&1

# 1e. Perpetual Budget Checker
python -m pytest test/hummingbot/connector/derivative/test_perpetual_budget_checker.py -v --tb=short 2>&1


# ====== TIER 1.5: NEW TESTS FROM REMEDIATION ======

# 1.5a. Backtesting Engine Base
python -m pytest test/hummingbot/strategy_v2/backtesting/test_backtesting_engine_base.py -v --tb=short 2>&1

# 1.5b. Backtesting Data Provider
python -m pytest test/hummingbot/strategy_v2/backtesting/test_backtesting_data_provider.py -v --tb=short 2>&1

# 1.5c. PMM Dynamic Controller
python -m pytest test/hummingbot/strategy_v2/controllers/test_pmm_dynamic.py -v --tb=short 2>&1

# 1.5d. Strategy V2 Models - base
python -m pytest test/hummingbot/strategy_v2/models/test_base.py -v --tb=short 2>&1

# 1.5e. Strategy V2 Models - executors (CloseType)
python -m pytest test/hummingbot/strategy_v2/models/test_executors.py -v --tb=short 2>&1

# 1.5f. Strategy V2 Models - executor_actions
python -m pytest test/hummingbot/strategy_v2/models/test_executor_actions.py -v --tb=short 2>&1

# 1.5g. Strategy V2 Models - position_config
python -m pytest test/hummingbot/strategy_v2/models/test_position_config.py -v --tb=short 2>&1


# ====== TIER 2: RELATED SUBSYSTEM TESTS ======

# 2a. Full strategy_v2 controllers
python -m pytest test/hummingbot/strategy_v2/controllers/ -v --tb=short 2>&1

# 2b. Full strategy_v2 executors
python -m pytest test/hummingbot/strategy_v2/executors/ -v --tb=short 2>&1

# 2c. Full strategy_v2 backtesting (NEW)
python -m pytest test/hummingbot/strategy_v2/backtesting/ -v --tb=short 2>&1

# 2d. Full strategy_v2 models (NEW)
python -m pytest test/hummingbot/strategy_v2/models/ -v --tb=short 2>&1

# 2e. Strategy V2 base test
python -m pytest test/hummingbot/strategy/test_strategy_v2_base.py -v --tb=short 2>&1

# 2f. Full NonKYC connector tests (excluding live_api)
python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v --tb=short -m "not live_api" 2>&1

# 2g. NonKYC candles test
python -m pytest test/hummingbot/data_feed/candles_feed/nonkyc_spot_candles/ -v --tb=short 2>&1


# ====== TIER 3+4: BROAD REGRESSION + SUMMARY ======

python -m pytest test/hummingbot/strategy_v2/ test/hummingbot/connector/test_budget_checker.py test/hummingbot/connector/derivative/test_perpetual_budget_checker.py test/hummingbot/connector/exchange/nonkyc/ test/hummingbot/strategy/test_strategy_v2_base.py test/hummingbot/core/data_type/ -m "not quarantined and not live_api" --tb=line -q 2>&1


# ====== TIER 5: COVERAGE VERIFICATION ======

# 5a. Coverage on strategy_v2 (now includes backtesting)
python -m coverage run -m pytest test/hummingbot/strategy_v2/ -m "not quarantined and not live_api" -q 2>&1
python -m coverage report --include="hummingbot/strategy_v2/backtesting/*,controllers/*" 2>&1

python -m pytest test/hummingbot/strategy_v2/controllers/test_wallet_balance_seeding.py -v 2>&1

# Run all market making controller tests (includes existing + new)
python -m pytest test/hummingbot/strategy_v2/controllers/test_market_making_controller_base.py test/hummingbot/strategy_v2/controllers/test_wallet_balance_seeding.py -v 2>&1

# Run full strategy_v2 test suite
python -m pytest test/hummingbot/strategy_v2/ -v 2>&1

# ====== TIER 6: EXPERT REVIEW — CORE INFRASTRUCTURE FIXES ======

# 6a. PnL / Performance Aggregation (Phase 1 — VWAP fix, no-mutation)
python -m pytest test/hummingbot/client/test_performance.py -v --tb=short 2>&1

# 6b. Kill Switch Self-Cancel Guard (Phase 2 — NEW file)
python -m pytest test/hummingbot/core/utils/test_kill_switch.py -v --tb=short 2>&1

# 6c. Trading Core (Phase 4 — duplicate start guard, strategy_task)
python -m pytest test/hummingbot/core/test_trading_core.py -v --tb=short 2>&1

# 6d. Connector Manager (Phase 11 — safe recreation, config preservation)
python -m pytest test/hummingbot/core/test_connector_manager.py -v --tb=short 2>&1

# 6e. Order Book Tracker (Phase 12 — async_stop, race fix)
python -m pytest test/hummingbot/core/data_type/test_order_book_tracker.py -v --tb=short 2>&1

# 6f. Retry/Backoff Consistency (Phase 9 — NEW file)
python -m pytest test/hummingbot/core/data_type/test_retry_backoff_consistency.py -v --tb=short 2>&1

# ====== TIER 7: EXPERT REVIEW — COMMAND & REMOTE INTERFACE FIXES ======

# 7a. Duplicate Start Guard (Phase 4 — NEW file)
python -m pytest test/hummingbot/client/command/test_start_command.py -v --tb=short 2>&1

# 7b. MQTT Contract Tests (Phase 3 — NEW file)
python -m pytest test/hummingbot/remote_iface/test_mqtt_contract.py -v --tb=short 2>&1

# 7c. Existing MQTT tests (regression after mqtt.py + messages.py changes)
python -m pytest test/hummingbot/remote_iface/test_mqtt.py -v --tb=short 2>&1

# 7d. Existing MQTT command test (regression)
python -m pytest test/hummingbot/client/command/test_mqtt_command.py -v --tb=short 2>&1

# 7e. Existing history command (uses performance.py — regression after PnL fix)
python -m pytest test/hummingbot/client/command/test_history_command.py -v --tb=short 2>&1

# 7f. Quickstart Headless Parsing (Phase 10 — NEW file)
python -m pytest test/test_quickstart_cli.py -v --tb=short 2>&1

# ====== TIER 8: EXPERT REVIEW — MEXC CONNECTOR ======

# 8a. MEXC Order Book Sequencing (Phase 5 — version-based update_id)
python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_order_book.py -v --tb=short 2>&1

# 8b. MEXC Protobuf Post Processor (Phase 6 — NEW file)
python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_post_processor.py -v --tb=short 2>&1

# 8c. MEXC Order Book Data Source (Phase 5 — update_id assertion change)
python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_api_order_book_data_source.py -v --tb=short 2>&1

# 8d. MEXC Exchange (regression)
python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_exchange.py -v --tb=short 2>&1

# 8e. MEXC User Stream (regression)
python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_user_stream_data_source.py -v --tb=short 2>&1

# 8f. MEXC Auth (regression)
python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_auth.py -v --tb=short 2>&1

# 8g. MEXC Web Utils (regression)
python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_web_utils.py -v --tb=short 2>&1

# 8h. Full MEXC connector suite
python -m pytest test/hummingbot/connector/exchange/mexc/ -v --tb=short 2>&1

# ====== TIER 9: EXPERT REVIEW — NONKYC CONNECTOR HARDENING ======

# 9a. NonKYC Web Utils (Phase 8 — strengthened factory assertions)
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_web_utils.py -v --tb=short 2>&1

# 9b. NonKYC Phase5d (Phase 8 — strengthened cancel-all body check)
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase5d.py -v --tb=short 2>&1

# 9c. NonKYC Live API gate (Phase 7 — now has pytest.fail on FAIL > 0)
# NOTE: This requires NONKYC_API_KEY and NONKYC_API_SECRET env vars.
# Without them, tests skip/warn. With them, failures actually fail pytest.
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py -v --tb=short 2>&1

# ====== TIER 10: EXPERT REVIEW — BROAD REGRESSION SWEEP ======

# 10a. All files modified or created by the remediation, single run
python -m pytest `
    test/hummingbot/client/test_performance.py `
    test/hummingbot/core/utils/test_kill_switch.py `
    test/hummingbot/remote_iface/test_mqtt_contract.py `
    test/hummingbot/client/command/test_start_command.py `
    test/hummingbot/connector/exchange/mexc/ `
    test/hummingbot/core/data_type/test_retry_backoff_consistency.py `
    test/hummingbot/core/data_type/test_order_book_tracker.py `
    test/hummingbot/core/test_connector_manager.py `
    test/hummingbot/core/test_trading_core.py `
    test/test_quickstart_cli.py `
    test/hummingbot/connector/exchange/nonkyc/ `
    test/hummingbot/remote_iface/test_mqtt.py `
    test/hummingbot/client/command/test_history_command.py `
    test/hummingbot/client/command/test_mqtt_command.py `
    -m "not quarantined and not live_api" --tb=line -q 2>&1

# 10b. Full client subsystem (catches PnL + command regressions)
python -m pytest test/hummingbot/client/ -m "not quarantined and not live_api" --tb=line -q 2>&1

# 10c. Full core subsystem (catches kill switch + tracker + connector manager + trading core)
python -m pytest test/hummingbot/core/ -m "not quarantined and not live_api" --tb=line -q 2>&1

# 10d. Full remote_iface subsystem
python -m pytest test/hummingbot/remote_iface/ -m "not quarantined and not live_api" --tb=line -q 2>&1

# ====== TIER 11: EXPERT REVIEW — COVERAGE ON CHANGED FILES ======

# 11a. Coverage on all production files touched by remediation
python -m coverage run -m pytest `
    test/hummingbot/client/test_performance.py `
    test/hummingbot/core/utils/test_kill_switch.py `
    test/hummingbot/remote_iface/test_mqtt_contract.py `
    test/hummingbot/remote_iface/test_mqtt.py `
    test/hummingbot/client/command/test_start_command.py `
    test/hummingbot/client/command/test_history_command.py `
    test/hummingbot/connector/exchange/mexc/ `
    test/hummingbot/core/data_type/test_retry_backoff_consistency.py `
    test/hummingbot/core/data_type/test_order_book_tracker.py `
    test/hummingbot/core/test_connector_manager.py `
    test/hummingbot/core/test_trading_core.py `
    test/test_quickstart_cli.py `
    -m "not quarantined and not live_api" -q 2>&1

python -m coverage report --include=`
"hummingbot/client/performance.py,`
hummingbot/core/utils/kill_switch.py,`
hummingbot/remote_iface/mqtt.py,`
hummingbot/remote_iface/messages.py,`
hummingbot/client/command/start_command.py,`
hummingbot/connector/exchange/mexc/mexc_order_book.py,`
hummingbot/connector/exchange/mexc/mexc_post_processor.py,`
hummingbot/core/connector_manager.py,`
hummingbot/core/data_type/order_book_tracker.py,`
hummingbot/core/data_type/order_book_tracker_data_source.py,`
hummingbot/core/data_type/user_stream_tracker_data_source.py,`
hummingbot/core/trading_core.py,`
bin/hummingbot_quickstart.py" 2>&1

# ====== TIER 12: RELEASE GATE — RUN BEFORE DEPLOYMENT ======
# These require real credentials and are NOT part of the default CI lane.
# Set env vars first:
#   $env:NONKYC_API_KEY = "your_key"
#   $env:NONKYC_API_SECRET = "your_secret"

# 12a. NonKYC strict live smoke (Phase 7 gate — now fails pytest on FAIL > 0)
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py -v --tb=short 2>&1

# 12b. NonKYC strict live probe (standalone script from expert reviewer)
python nonkyc_strict_live_probe.py 2>&1

# 12c. MEXC public probe (standalone script from expert reviewer — no keys needed)
python mexc_public_probe.py --symbol BTCUSDT 2>&1

# 12d. Makefile release target (all tests including live)
make test_release 2>&1