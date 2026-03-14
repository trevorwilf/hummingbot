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