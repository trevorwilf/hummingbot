clear-host

$ErrorActionPreference = 'Continue'
$script:failures = @()
$script:totalPassed = 0
$script:totalFailed = 0

function Run-TestLane {
    param([string]$Name, [string]$Command)

    Write-Host "`n====== $Name ======" -ForegroundColor Cyan
    Invoke-Expression $Command
    if ($LASTEXITCODE -ne 0) {
        $script:failures += $Name
        $script:totalFailed++
        Write-Host "FAILED: $Name (exit code $LASTEXITCODE)" -ForegroundColor Red
    } else {
        $script:totalPassed++
        Write-Host "PASSED: $Name" -ForegroundColor Green
    }
}

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

Run-TestLane "1a. Market Making Controller Base" "python -m pytest test/hummingbot/strategy_v2/controllers/test_market_making_controller_base.py -v --tb=short --junitxml=test-results/t1a.xml 2>&1"

Run-TestLane "1b. Executor Orchestrator" "python -m pytest test/hummingbot/strategy_v2/executors/test_executor_orchestrator.py -v --tb=short --junitxml=test-results/t1b.xml 2>&1"

Run-TestLane "1c. NonKYC Order Book Data Source" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_api_order_book_data_source.py -v --tb=short --junitxml=test-results/t1c.xml 2>&1"

Run-TestLane "1d. Budget Checker" "python -m pytest test/hummingbot/connector/test_budget_checker.py -v --tb=short --junitxml=test-results/t1d.xml 2>&1"

Run-TestLane "1e. Perpetual Budget Checker" "python -m pytest test/hummingbot/connector/derivative/test_perpetual_budget_checker.py -v --tb=short --junitxml=test-results/t1e.xml 2>&1"


# ====== TIER 1.5: NEW TESTS FROM REMEDIATION ======

Run-TestLane "1.5a. Backtesting Engine Base" "python -m pytest test/hummingbot/strategy_v2/backtesting/test_backtesting_engine_base.py -v --tb=short --junitxml=test-results/t1_5a.xml 2>&1"

Run-TestLane "1.5b. Backtesting Data Provider" "python -m pytest test/hummingbot/strategy_v2/backtesting/test_backtesting_data_provider.py -v --tb=short --junitxml=test-results/t1_5b.xml 2>&1"

Run-TestLane "1.5c. PMM Dynamic Controller" "python -m pytest test/hummingbot/strategy_v2/controllers/test_pmm_dynamic.py -v --tb=short --junitxml=test-results/t1_5c.xml 2>&1"

Run-TestLane "1.5d. Strategy V2 Models - base" "python -m pytest test/hummingbot/strategy_v2/models/test_base.py -v --tb=short --junitxml=test-results/t1_5d.xml 2>&1"

Run-TestLane "1.5e. Strategy V2 Models - executors" "python -m pytest test/hummingbot/strategy_v2/models/test_executors.py -v --tb=short --junitxml=test-results/t1_5e.xml 2>&1"

Run-TestLane "1.5f. Strategy V2 Models - executor_actions" "python -m pytest test/hummingbot/strategy_v2/models/test_executor_actions.py -v --tb=short --junitxml=test-results/t1_5f.xml 2>&1"

Run-TestLane "1.5g. Strategy V2 Models - position_config" "python -m pytest test/hummingbot/strategy_v2/models/test_position_config.py -v --tb=short --junitxml=test-results/t1_5g.xml 2>&1"


# ====== TIER 2: RELATED SUBSYSTEM TESTS ======

Run-TestLane "2a. Full strategy_v2 controllers" "python -m pytest test/hummingbot/strategy_v2/controllers/ -v --tb=short --junitxml=test-results/t2a.xml 2>&1"

Run-TestLane "2b. Full strategy_v2 executors" "python -m pytest test/hummingbot/strategy_v2/executors/ -v --tb=short --junitxml=test-results/t2b.xml 2>&1"

Run-TestLane "2c. Full strategy_v2 backtesting" "python -m pytest test/hummingbot/strategy_v2/backtesting/ -v --tb=short --junitxml=test-results/t2c.xml 2>&1"

Run-TestLane "2d. Full strategy_v2 models" "python -m pytest test/hummingbot/strategy_v2/models/ -v --tb=short --junitxml=test-results/t2d.xml 2>&1"

Run-TestLane "2e. Strategy V2 base test" "python -m pytest test/hummingbot/strategy/test_strategy_v2_base.py -v --tb=short --junitxml=test-results/t2e.xml 2>&1"

Run-TestLane "2f. Full NonKYC connector (no live_api)" "python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v --tb=short -m 'not live_api' --junitxml=test-results/t2f.xml 2>&1"

Run-TestLane "2g. NonKYC candles test" "python -m pytest test/hummingbot/data_feed/candles_feed/nonkyc_spot_candles/ -v --tb=short --junitxml=test-results/t2g.xml 2>&1"


# ====== TIER 3+4: BROAD REGRESSION + SUMMARY ======

Run-TestLane "3+4. Broad Regression" "python -m pytest test/hummingbot/strategy_v2/ test/hummingbot/connector/test_budget_checker.py test/hummingbot/connector/derivative/test_perpetual_budget_checker.py test/hummingbot/connector/exchange/nonkyc/ test/hummingbot/strategy/test_strategy_v2_base.py test/hummingbot/core/data_type/ -m 'not quarantined and not live_api' --tb=line -q --junitxml=test-results/t3_4.xml 2>&1"


# ====== TIER 5: COVERAGE VERIFICATION ======

Run-TestLane "5a. Coverage strategy_v2" "python -m coverage run -m pytest test/hummingbot/strategy_v2/ -m 'not quarantined and not live_api' -q --junitxml=test-results/t5a.xml 2>&1"

python -m coverage report --include="hummingbot/strategy_v2/backtesting/*,controllers/*" 2>&1

Run-TestLane "5b. Wallet Balance Seeding" "python -m pytest test/hummingbot/strategy_v2/controllers/test_wallet_balance_seeding.py -v --junitxml=test-results/t5b.xml 2>&1"

Run-TestLane "5c. Market Making + Wallet tests" "python -m pytest test/hummingbot/strategy_v2/controllers/test_market_making_controller_base.py test/hummingbot/strategy_v2/controllers/test_wallet_balance_seeding.py -v --junitxml=test-results/t5c.xml 2>&1"

Run-TestLane "5d. Full strategy_v2 suite" "python -m pytest test/hummingbot/strategy_v2/ -v --junitxml=test-results/t5d.xml 2>&1"

# ====== TIER 6: EXPERT REVIEW — CORE INFRASTRUCTURE FIXES ======

Run-TestLane "6a. PnL / Performance" "python -m pytest test/hummingbot/client/test_performance.py -v --tb=short --junitxml=test-results/t6a.xml 2>&1"

Run-TestLane "6b. Kill Switch" "python -m pytest test/hummingbot/core/utils/test_kill_switch.py -v --tb=short --junitxml=test-results/t6b.xml 2>&1"

Run-TestLane "6c. Trading Core" "python -m pytest test/hummingbot/core/test_trading_core.py -v --tb=short --junitxml=test-results/t6c.xml 2>&1"

Run-TestLane "6d. Connector Manager" "python -m pytest test/hummingbot/core/test_connector_manager.py -v --tb=short --junitxml=test-results/t6d.xml 2>&1"

Run-TestLane "6e. Order Book Tracker" "python -m pytest test/hummingbot/core/data_type/test_order_book_tracker.py -v --tb=short --junitxml=test-results/t6e.xml 2>&1"

Run-TestLane "6f. Retry/Backoff Consistency" "python -m pytest test/hummingbot/core/data_type/test_retry_backoff_consistency.py -v --tb=short --junitxml=test-results/t6f.xml 2>&1"

# ====== TIER 7: EXPERT REVIEW — COMMAND & REMOTE INTERFACE FIXES ======

Run-TestLane "7a. Start Command" "python -m pytest test/hummingbot/client/command/test_start_command.py -v --tb=short --junitxml=test-results/t7a.xml 2>&1"

Run-TestLane "7b. MQTT Contract" "python -m pytest test/hummingbot/remote_iface/test_mqtt_contract.py -v --tb=short --junitxml=test-results/t7b.xml 2>&1"

Run-TestLane "7c. MQTT Tests" "python -m pytest test/hummingbot/remote_iface/test_mqtt.py -v --tb=short --junitxml=test-results/t7c.xml 2>&1"

Run-TestLane "7d. MQTT Command" "python -m pytest test/hummingbot/client/command/test_mqtt_command.py -v --tb=short --junitxml=test-results/t7d.xml 2>&1"

Run-TestLane "7e. History Command" "python -m pytest test/hummingbot/client/command/test_history_command.py -v --tb=short --junitxml=test-results/t7e.xml 2>&1"

Run-TestLane "7f. Quickstart CLI" "python -m pytest test/test_quickstart_cli.py -v --tb=short --junitxml=test-results/t7f.xml 2>&1"

# ====== TIER 8: EXPERT REVIEW — MEXC CONNECTOR ======

Run-TestLane "8a. MEXC Order Book" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_order_book.py -v --tb=short --junitxml=test-results/t8a.xml 2>&1"

Run-TestLane "8b. MEXC Post Processor" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_post_processor.py -v --tb=short --junitxml=test-results/t8b.xml 2>&1"

Run-TestLane "8c. MEXC Order Book Data Source" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_api_order_book_data_source.py -v --tb=short --junitxml=test-results/t8c.xml 2>&1"

Run-TestLane "8d. MEXC Exchange" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_exchange.py -v --tb=short --junitxml=test-results/t8d.xml 2>&1"

Run-TestLane "8e. MEXC User Stream" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_user_stream_data_source.py -v --tb=short --junitxml=test-results/t8e.xml 2>&1"

Run-TestLane "8f. MEXC Auth" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_auth.py -v --tb=short --junitxml=test-results/t8f.xml 2>&1"

Run-TestLane "8g. MEXC Web Utils" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_web_utils.py -v --tb=short --junitxml=test-results/t8g.xml 2>&1"

Run-TestLane "8h. Full MEXC connector" "python -m pytest test/hummingbot/connector/exchange/mexc/ -v --tb=short --junitxml=test-results/t8h.xml 2>&1"

# ====== TIER 9: EXPERT REVIEW — NONKYC CONNECTOR HARDENING ======

Run-TestLane "9a. NonKYC Web Utils" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_web_utils.py -v --tb=short --junitxml=test-results/t9a.xml 2>&1"

Run-TestLane "9b. NonKYC Phase5d" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase5d.py -v --tb=short --junitxml=test-results/t9b.xml 2>&1"

Run-TestLane "9c. NonKYC Live API gate" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py -v --tb=short --junitxml=test-results/t9c.xml 2>&1"

# ====== TIER 9.5: EXPERT REVIEW — CONNECTOR BUG FIXES (Mar 2026) ======

Run-TestLane "9.5a. NonKYC Expert Review Fixes" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_expert_review_fixes.py -v --tb=short --junitxml=test-results/t9_5a.xml 2>&1"

Run-TestLane "9.5b. NonKYC Time Sync Integration" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_time_sync_integration.py -v --tb=short --junitxml=test-results/t9_5b.xml 2>&1"

Run-TestLane "9.5c. NonKYC Auth" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_auth.py -v --tb=short --junitxml=test-results/t9_5c.xml 2>&1"

Run-TestLane "9.5d. NonKYC Exchange" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_exchange.py -v --tb=short --junitxml=test-results/t9_5d.xml 2>&1"

Run-TestLane "9.5e. NonKYC Order Book" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_order_book.py -v --tb=short --junitxml=test-results/t9_5e.xml 2>&1"

Run-TestLane "9.5f. NonKYC Bug Fixes" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_bugfixes.py -v --tb=short --junitxml=test-results/t9_5f.xml 2>&1"

Run-TestLane "9.5g. NonKYC Phase tests" "python -m pytest `
    test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase2_fixes.py `
    test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase3_optimization.py `
    test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase5a.py `
    test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase5c.py `
    test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase6.py `
    test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase7a.py `
    test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase7b.py `
    test/hummingbot/connector/exchange/nonkyc/test_nonkyc_phase7c.py `
    -v --tb=short --junitxml=test-results/t9_5g.xml 2>&1"

Run-TestLane "9.5h. NonKYC Utils + User Stream" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_utils.py test/hummingbot/connector/exchange/nonkyc/test_nonkyc_api_user_stream_data_source.py -v --tb=short --junitxml=test-results/t9_5h.xml 2>&1"

Run-TestLane "9.5i. MEXC Expert Review Fixes" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_expert_review_fixes.py -v --tb=short --junitxml=test-results/t9_5i.xml 2>&1"

Run-TestLane "9.5j. MEXC Utils" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_utils.py -v --tb=short --junitxml=test-results/t9_5j.xml 2>&1"

Run-TestLane "9.5i2. NonKYC standalone auth" "python test/hummingbot/connector/exchange/nonkyc/nonkyc_auth_test.py 2>&1"

Run-TestLane "9.5j2. Candles Factory" "python -m pytest test/hummingbot/data_feed/candles_feed/test_candles_factory.py -v --tb=short --junitxml=test-results/t9_5j2.xml 2>&1"

Run-TestLane "9.5j3. Market Data Provider" "python -m pytest test/hummingbot/data_feed/test_market_data_provider.py -v --tb=short --junitxml=test-results/t9_5j3.xml 2>&1"

Run-TestLane "9.5k. Full NonKYC connector (no live_api)" "python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v --tb=short -m 'not live_api' --junitxml=test-results/t9_5k.xml 2>&1"

Run-TestLane "9.5l. Full MEXC connector" "python -m pytest test/hummingbot/connector/exchange/mexc/ -v --tb=short --junitxml=test-results/t9_5l.xml 2>&1"


# ====== TIER 10: EXPERT REVIEW — BROAD REGRESSION SWEEP ======

Run-TestLane "10a. All remediation files" "python -m pytest `
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
    -m 'not quarantined and not live_api' --tb=line -q --junitxml=test-results/t10a.xml 2>&1"

Run-TestLane "10b. Full client subsystem" "python -m pytest test/hummingbot/client/ -m 'not quarantined and not live_api' --tb=line -q --junitxml=test-results/t10b.xml 2>&1"

Run-TestLane "10c. Full core subsystem" "python -m pytest test/hummingbot/core/ -m 'not quarantined and not live_api' --tb=line -q --junitxml=test-results/t10c.xml 2>&1"

Run-TestLane "10d. Full remote_iface subsystem" "python -m pytest test/hummingbot/remote_iface/ -m 'not quarantined and not live_api' --tb=line -q --junitxml=test-results/t10d.xml 2>&1"

# ====== TIER 11: EXPERT REVIEW — COVERAGE ON CHANGED FILES ======

Run-TestLane "11a. Coverage on changed files" "python -m coverage run -m pytest `
    test/hummingbot/client/test_performance.py `
    test/hummingbot/core/utils/test_kill_switch.py `
    test/hummingbot/remote_iface/test_mqtt_contract.py `
    test/hummingbot/remote_iface/test_mqtt.py `
    test/hummingbot/client/command/test_start_command.py `
    test/hummingbot/client/command/test_history_command.py `
    test/hummingbot/connector/exchange/mexc/ `
    test/hummingbot/connector/exchange/nonkyc/ `
    test/hummingbot/core/data_type/test_retry_backoff_consistency.py `
    test/hummingbot/core/data_type/test_order_book_tracker.py `
    test/hummingbot/core/test_connector_manager.py `
    test/hummingbot/core/test_trading_core.py `
    test/test_quickstart_cli.py `
    -m 'not quarantined and not live_api' -q --junitxml=test-results/t11a.xml 2>&1"

python -m coverage report --include=`
"hummingbot/client/performance.py,`
hummingbot/core/utils/kill_switch.py,`
hummingbot/remote_iface/mqtt.py,`
hummingbot/remote_iface/messages.py,`
hummingbot/client/command/start_command.py,`
hummingbot/connector/exchange/mexc/mexc_order_book.py,`
hummingbot/connector/exchange/mexc/mexc_post_processor.py,`
hummingbot/connector/exchange/mexc/mexc_exchange.py,`
hummingbot/connector/exchange/mexc/mexc_constants.py,`
hummingbot/connector/exchange/mexc/mexc_api_order_book_data_source.py,`
hummingbot/connector/exchange/mexc/mexc_api_user_stream_data_source.py,`
hummingbot/connector/exchange/mexc/mexc_web_utils.py,`
hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py,`
hummingbot/connector/exchange/nonkyc/nonkyc_auth.py,`
hummingbot/connector/exchange/nonkyc/nonkyc_web_utils.py,`
hummingbot/connector/exchange/nonkyc/nonkyc_api_order_book_data_source.py,`
hummingbot/connector/exchange/nonkyc/nonkyc_api_user_stream_data_source.py,`
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

Run-TestLane "12a. NonKYC Live API gate" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py -v --tb=short --junitxml=test-results/t12a.xml 2>&1"

Run-TestLane "12b. NonKYC Live Connector Smoke" "python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_connector_smoke.py -v --tb=short --junitxml=test-results/t12b.xml 2>&1"

Run-TestLane "12c. MEXC Public Contract Smoke" "python -m pytest test/hummingbot/connector/exchange/mexc/test_mexc_public_contract_smoke.py -v --tb=short --junitxml=test-results/t12c.xml 2>&1"

Run-TestLane "12d. Release Gate" "python -m pytest -m 'not quarantined' test/ -v --tb=long --junitxml=test-results/release.xml 2>&1"

# ====== SUMMARY ======

Write-Host "`n====== SUMMARY ======" -ForegroundColor Cyan
Write-Host "Passed: $script:totalPassed" -ForegroundColor Green
Write-Host "Failed: $script:totalFailed" -ForegroundColor $(if ($script:totalFailed -gt 0) { "Red" } else { "Green" })
if ($script:failures.Count -gt 0) {
    Write-Host "Failed lanes:" -ForegroundColor Red
    $script:failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
exit 0
