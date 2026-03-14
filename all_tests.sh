echo "====== TIER 0: NEW TESTING INFRASTRUCTURE VALIDATION ======"
echo ""
echo "=== 0a. Verify pytest markers are registered ==="
python -m pytest --markers 2>&1 | grep -E "quarantined|live_api|slow"
echo ""
echo "=== 0b. Verify main lane excludes quarantined + live_api ==="
python -m pytest --collect-only -m "not quarantined and not live_api" test/ 2>&1 | tail -5
echo ""
echo "=== 0c. Verify quarantined lane collects the right tests ==="
python -m pytest --collect-only -m "quarantined" test/ 2>&1 | tail -5
echo ""
echo "=== 0d. Verify live_api lane collects only live tests ==="
python -m pytest --collect-only -m "live_api" test/ 2>&1 | tail -5
echo ""

echo "====== TIER 1: DIRECTLY AFFECTED TEST SUITES ======"
echo ""
echo "=== 1a. Market Making Controller Base (P1 ordering + P2 rebalance) ==="
python -m pytest test/hummingbot/strategy_v2/controllers/test_market_making_controller_base.py -v --tb=short 2>&1
echo ""
echo "=== 1b. Executor Orchestrator (P1 ordering + budget preflight) ==="
python -m pytest test/hummingbot/strategy_v2/executors/test_executor_orchestrator.py -v --tb=short 2>&1
echo ""
echo "=== 1c. NonKYC Order Book Data Source (P3 resync robustness) ==="
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_api_order_book_data_source.py -v --tb=short 2>&1
echo ""
echo "=== 1d. Budget Checker (regression — not modified but used by P1) ==="
python -m pytest test/hummingbot/connector/test_budget_checker.py -v --tb=short 2>&1
echo ""
echo "=== 1e. Perpetual Budget Checker (regression) ==="
python -m pytest test/hummingbot/connector/derivative/test_perpetual_budget_checker.py -v --tb=short 2>&1
echo ""

echo "====== TIER 1.5: NEW TESTS FROM REMEDIATION ======"
echo ""
echo "=== 1.5a. Backtesting Engine Base (prepare_market_data + summarize_results) ==="
python -m pytest test/hummingbot/strategy_v2/backtesting/test_backtesting_engine_base.py -v --tb=short 2>&1
echo ""
echo "=== 1.5b. Backtesting Data Provider (ensure_epoch_index) ==="
python -m pytest test/hummingbot/strategy_v2/backtesting/test_backtesting_data_provider.py -v --tb=short 2>&1
echo ""
echo "=== 1.5c. PMM Dynamic Controller (config defaults + feature determinism) ==="
python -m pytest test/hummingbot/strategy_v2/controllers/test_pmm_dynamic.py -v --tb=short 2>&1
echo ""
echo "=== 1.5d. Strategy V2 Models — base ==="
python -m pytest test/hummingbot/strategy_v2/models/test_base.py -v --tb=short 2>&1
echo ""
echo "=== 1.5e. Strategy V2 Models — executors (CloseType) ==="
python -m pytest test/hummingbot/strategy_v2/models/test_executors.py -v --tb=short 2>&1
echo ""
echo "=== 1.5f. Strategy V2 Models — executor_actions ==="
python -m pytest test/hummingbot/strategy_v2/models/test_executor_actions.py -v --tb=short 2>&1
echo ""
echo "=== 1.5g. Strategy V2 Models — position_config ==="
python -m pytest test/hummingbot/strategy_v2/models/test_position_config.py -v --tb=short 2>&1
echo ""

echo "====== TIER 2: RELATED SUBSYSTEM TESTS ======"
echo ""
echo "=== 2a. Full strategy_v2 controllers ==="
python -m pytest test/hummingbot/strategy_v2/controllers/ -v --tb=short 2>&1
echo ""
echo "=== 2b. Full strategy_v2 executors ==="
python -m pytest test/hummingbot/strategy_v2/executors/ -v --tb=short 2>&1
echo ""
echo "=== 2c. Full strategy_v2 backtesting (NEW) ==="
python -m pytest test/hummingbot/strategy_v2/backtesting/ -v --tb=short 2>&1
echo ""
echo "=== 2d. Full strategy_v2 models (NEW) ==="
python -m pytest test/hummingbot/strategy_v2/models/ -v --tb=short 2>&1
echo ""
echo "=== 2e. Strategy V2 base test ==="
python -m pytest test/hummingbot/strategy/test_strategy_v2_base.py -v --tb=short 2>&1
echo ""
echo "=== 2f. Full NonKYC connector tests (excluding live_api) ==="
python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v --tb=short -m "not live_api" 2>&1
echo ""
echo "=== 2g. NonKYC candles test ==="
python -m pytest test/hummingbot/data_feed/candles_feed/nonkyc_spot_candles/ -v --tb=short 2>&1
echo ""

echo "====== TIER 3+4: BROAD REGRESSION + SUMMARY ======"
python -m pytest test/hummingbot/strategy_v2/ test/hummingbot/connector/test_budget_checker.py test/hummingbot/connector/derivative/test_perpetual_budget_checker.py test/hummingbot/connector/exchange/nonkyc/ test/hummingbot/strategy/test_strategy_v2_base.py test/hummingbot/core/data_type/ -m "not quarantined and not live_api" --tb=line -q 2>&1

echo ""
echo "====== TIER 5: COVERAGE VERIFICATION ======"
echo ""
echo "=== 5a. Coverage on strategy_v2 (now includes backtesting) ==="
coverage run -m pytest test/hummingbot/strategy_v2/ -m "not quarantined and not live_api" -q 2>&1
coverage report --include="hummingbot/strategy_v2/backtesting/*,controllers/*" 2>&1