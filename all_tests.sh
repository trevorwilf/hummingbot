#!/usr/bin/env bash
# all_tests.sh — Comprehensive test runner for NonKYC + MEXC connectors
# Run from the repo root: bash all_tests.sh
#
# All output is logged to test_logs/all_tests_YYYYMMDD_HHMMSS.log

set -uo pipefail

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1: ENVIRONMENT CHECK & LOG SETUP
# ═════════════════════════════════════════════════════════════════════════════
LOG_DIR="test_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/all_tests_$(date +%Y%m%d_%H%M%S).log"
# Duplicate ALL stdout+stderr to both terminal and log file
exec > >(tee -a "$LOG_FILE") 2>&1

echo "================================================================"
echo "SECTION 1: ENVIRONMENT CHECK"
echo "================================================================"
echo "Date:           $(date)"
echo "Python:         $(python --version 2>&1)"
echo "Pytest:         $(python -m pytest --version 2>&1 | head -1)"
echo "Log file:       $(pwd)/$LOG_FILE"
echo ""

HAVE_NONKYC_KEYS=false
if [ -n "${NONKYC_API_KEY:-}" ] && [ -n "${NONKYC_API_SECRET:-}" ]; then
    HAVE_NONKYC_KEYS=true
fi

HAVE_MEXC_KEYS=false
if [ -n "${MEXC_API_KEY:-}" ] && [ -n "${MEXC_API_SECRET:-}" ]; then
    HAVE_MEXC_KEYS=true
fi

echo "NonKYC API keys: $HAVE_NONKYC_KEYS"
echo "MEXC API keys:   $HAVE_MEXC_KEYS"
echo ""

# ── Counters & tracking ─────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
SECTION_COUNT=0
declare -a FAILED_SECTIONS=()
SCRIPT_START=$(date +%s)

# Disable set -e so test failures don't abort the script
set +e

run_test() {
    local label="$1"
    shift
    local cmd="$*"

    SECTION_COUNT=$((SECTION_COUNT + 1))
    echo "================================================================"
    echo "[$SECTION_COUNT] $label"
    echo "================================================================"
    local start=$(date +%s)

    eval "$cmd" 2>&1
    local rc=$?

    local end=$(date +%s)
    local elapsed=$((end - start))
    echo ""
    if [ $rc -eq 0 ]; then
        echo "  >> PASSED  ($elapsed s)"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "  >> FAILED  (exit $rc, $elapsed s)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        FAILED_SECTIONS+=("$label")
    fi
    echo ""
}

run_skip() {
    local label="$1"
    local reason="$2"
    SECTION_COUNT=$((SECTION_COUNT + 1))
    echo "================================================================"
    echo "[$SECTION_COUNT] $label"
    echo "  >> SKIPPED: $reason"
    echo "================================================================"
    SKIP_COUNT=$((SKIP_COUNT + 1))
    echo ""
}

# ── Path shorthands ──────────────────────────────────────────────────────────
NONKYC="test/hummingbot/connector/exchange/nonkyc"
MEXC="test/hummingbot/connector/exchange/mexc"
NONKYC_CANDLES="test/hummingbot/data_feed/candles_feed/nonkyc_spot_candles"
MEXC_SPOT_CANDLES="test/hummingbot/data_feed/candles_feed/mexc_spot_candles"
MEXC_PERP_CANDLES="test/hummingbot/data_feed/candles_feed/mexc_perpetual_candles"
CANDLES_FEED="test/hummingbot/data_feed/candles_feed"
EXECUTORS="test/hummingbot/strategy_v2/executors"
CONTROLLERS="test/hummingbot/strategy_v2/controllers"

PYTEST="python -m pytest"
PYTEST_V="$PYTEST -v --tb=short --timeout=60"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2: NONKYC CONNECTOR — UNIT TESTS
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 2: NONKYC CONNECTOR — UNIT TESTS                   ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "NonKYC: test_nonkyc_auth.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_auth.py"

run_test "NonKYC: test_nonkyc_utils.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_utils.py"

run_test "NonKYC: test_nonkyc_web_utils.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_web_utils.py"

run_test "NonKYC: test_nonkyc_order_book.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_order_book.py"

run_test "NonKYC: test_nonkyc_api_order_book_data_source.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_api_order_book_data_source.py"

run_test "NonKYC: test_nonkyc_api_user_stream_data_source.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_api_user_stream_data_source.py"

run_test "NonKYC: test_nonkyc_exchange.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_exchange.py"

run_test "NonKYC: test_nonkyc_bugfixes.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_bugfixes.py"

run_test "NonKYC: test_nonkyc_expert_review_fixes.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_expert_review_fixes.py"

run_test "NonKYC: test_nonkyc_time_sync_integration.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_time_sync_integration.py"

run_test "NonKYC: test_nonkyc_phase2_fixes.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase2_fixes.py"

run_test "NonKYC: test_nonkyc_phase3_optimization.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase3_optimization.py"

run_test "NonKYC: test_nonkyc_phase5a.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase5a.py"

run_test "NonKYC: test_nonkyc_phase5c.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase5c.py"

run_test "NonKYC: test_nonkyc_phase5d.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase5d.py"

run_test "NonKYC: test_nonkyc_phase6.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase6.py"

run_test "NonKYC: test_nonkyc_phase7a.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase7a.py"

run_test "NonKYC: test_nonkyc_phase7b.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase7b.py"

run_test "NonKYC: test_nonkyc_phase7c.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_phase7c.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3: NONKYC DATA FEED & RATE ORACLE
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 3: NONKYC DATA FEED & SHARED                       ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "NonKYC: test_nonkyc_spot_candles.py" \
    "$PYTEST_V $NONKYC_CANDLES/test_nonkyc_spot_candles.py"

run_test "Shared: test_candles_factory.py" \
    "$PYTEST_V $CANDLES_FEED/test_candles_factory.py"

run_test "Shared: test_market_data_provider.py" \
    "$PYTEST_V test/hummingbot/data_feed/test_market_data_provider.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4: NONKYC CONNECTOR — AGGREGATE PASS
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 4: NONKYC CONNECTOR — AGGREGATE PASS               ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "NonKYC: aggregate directory pass (excluding live tests)" \
    "$PYTEST $NONKYC/ --ignore=$NONKYC/test_nonkyc_live_api.py --ignore=$NONKYC/test_nonkyc_live_connector_smoke.py --ignore=$NONKYC/nonkyc_auth_test.py -v --tb=short --timeout=60"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5: NONKYC LIVE API TESTS (conditional)
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 5: NONKYC LIVE API TESTS                           ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

if [ "$HAVE_NONKYC_KEYS" = true ]; then
    run_test "NonKYC Live: test_nonkyc_live_api.py" \
        "$PYTEST_V $NONKYC/test_nonkyc_live_api.py"

    run_test "NonKYC Live: test_nonkyc_live_connector_smoke.py" \
        "$PYTEST_V $NONKYC/test_nonkyc_live_connector_smoke.py"

    run_test "NonKYC Live: nonkyc_auth_test.py (standalone)" \
        "python $NONKYC/nonkyc_auth_test.py"
else
    run_skip "NonKYC Live: test_nonkyc_live_api.py" \
        "Set NONKYC_API_KEY and NONKYC_API_SECRET to enable"
    run_skip "NonKYC Live: test_nonkyc_live_connector_smoke.py" \
        "Set NONKYC_API_KEY and NONKYC_API_SECRET to enable"
    run_skip "NonKYC Live: nonkyc_auth_test.py (standalone)" \
        "Set NONKYC_API_KEY and NONKYC_API_SECRET to enable"
fi

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6: MEXC CONNECTOR — UNIT TESTS
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 6: MEXC CONNECTOR — UNIT TESTS                     ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "MEXC: test_mexc_auth.py" \
    "$PYTEST_V $MEXC/test_mexc_auth.py"

run_test "MEXC: test_mexc_utils.py" \
    "$PYTEST_V $MEXC/test_mexc_utils.py"

run_test "MEXC: test_mexc_web_utils.py" \
    "$PYTEST_V $MEXC/test_mexc_web_utils.py"

run_test "MEXC: test_mexc_order_book.py" \
    "$PYTEST_V $MEXC/test_mexc_order_book.py"

run_test "MEXC: test_mexc_post_processor.py" \
    "$PYTEST_V $MEXC/test_mexc_post_processor.py"

run_test "MEXC: test_mexc_api_order_book_data_source.py" \
    "$PYTEST_V $MEXC/test_mexc_api_order_book_data_source.py"

run_test "MEXC: test_mexc_user_stream_data_source.py" \
    "$PYTEST_V $MEXC/test_mexc_user_stream_data_source.py"

run_test "MEXC: test_mexc_exchange.py" \
    "$PYTEST_V $MEXC/test_mexc_exchange.py"

run_test "MEXC: test_mexc_expert_review_fixes.py" \
    "$PYTEST_V $MEXC/test_mexc_expert_review_fixes.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7: MEXC DATA FEED & RATE ORACLE
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 7: MEXC DATA FEED & RATE ORACLE                    ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "MEXC: test_mexc_spot_candles.py" \
    "$PYTEST_V $MEXC_SPOT_CANDLES/test_mexc_spot_candles.py"

run_test "MEXC: test_mexc_perpetual_candles.py" \
    "$PYTEST_V $MEXC_PERP_CANDLES/test_mexc_perpetual_candles.py"

run_test "MEXC: test_mexc_rate_source.py" \
    "$PYTEST_V test/hummingbot/core/rate_oracle/sources/test_mexc_rate_source.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8: MEXC CONNECTOR — AGGREGATE PASS
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 8: MEXC CONNECTOR — AGGREGATE PASS                 ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "MEXC: aggregate directory pass (excluding live tests)" \
    "$PYTEST $MEXC/ --ignore=$MEXC/test_mexc_live_api.py -v --tb=short --timeout=60 -m 'not live_api'"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9: MEXC LIVE / AUTHENTICATED TESTS (conditional)
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 9: MEXC LIVE API TESTS                             ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

# 9a. MEXC public API tests (no auth required — always run)
run_test "MEXC Live: test_mexc_live_api.py (public tests)" \
    "$PYTEST_V $MEXC/test_mexc_live_api.py -k 'not auth'"

# 9b. MEXC authenticated API tests (only if keys are set)
if [ "$HAVE_MEXC_KEYS" = true ]; then
    run_test "MEXC Live: test_mexc_live_api.py (authenticated tests)" \
        "$PYTEST_V $MEXC/test_mexc_live_api.py -k 'auth'"
else
    run_skip "MEXC Live: authenticated tests" \
        "Set MEXC_API_KEY and MEXC_API_SECRET to enable"
fi

# ═════════════════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9b: MEXC PUBLIC CONTRACT SMOKE (always runs, no auth needed)
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 9b: MEXC PUBLIC CONTRACT SMOKE                     ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "MEXC Public: test_mexc_public_contract_smoke.py" \
    "$PYTEST_V $MEXC/test_mexc_public_contract_smoke.py"

# SECTION 10: STRATEGY & EXECUTOR TESTS
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 10: STRATEGY & EXECUTOR TESTS                      ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "Strategy: test_executor_base.py" \
    "$PYTEST_V $EXECUTORS/test_executor_base.py"

run_test "Strategy: test_executor_orchestrator.py" \
    "$PYTEST_V $EXECUTORS/test_executor_orchestrator.py"

run_test "Strategy: test_position_executor.py" \
    "$PYTEST_V $EXECUTORS/position_executor/test_position_executor.py"

run_test "Strategy: test_market_making_controller_base.py" \
    "$PYTEST_V $CONTROLLERS/test_market_making_controller_base.py"

run_test "Strategy: test_pmm_dynamic.py" \
    "$PYTEST_V $CONTROLLERS/test_pmm_dynamic.py"

run_test "Strategy: test_wallet_balance_seeding.py" \
    "$PYTEST_V $CONTROLLERS/test_wallet_balance_seeding.py"

run_test "Connector: test_budget_checker.py" \
    "$PYTEST_V test/hummingbot/connector/test_budget_checker.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 11: CORE INFRASTRUCTURE TESTS
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 11: CORE INFRASTRUCTURE TESTS                      ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "Core: test_order_book_tracker.py" \
    "$PYTEST_V test/hummingbot/core/data_type/test_order_book_tracker.py"

run_test "Core: test_retry_backoff_consistency.py" \
    "$PYTEST_V test/hummingbot/core/data_type/test_retry_backoff_consistency.py"

run_test "Core: test_kill_switch.py" \
    "$PYTEST_V test/hummingbot/core/utils/test_kill_switch.py"

run_test "Core: test_trading_core.py" \
    "$PYTEST_V test/hummingbot/core/test_trading_core.py"

run_test "Core: test_connector_manager.py" \
    "$PYTEST_V test/hummingbot/core/test_connector_manager.py"

run_test "Core: test_performance.py" \
    "$PYTEST_V test/hummingbot/client/test_performance.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 12: BROAD REGRESSION SWEEP
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  SECTION 12: BROAD REGRESSION SWEEP                         ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "Regression sweep (all connector + strategy + core)" \
    "$PYTEST \
        $NONKYC/ \
        $MEXC/ \
        $NONKYC_CANDLES/ \
        $MEXC_SPOT_CANDLES/ \
        $MEXC_PERP_CANDLES/ \
        $EXECUTORS/ \
        $CONTROLLERS/ \
        test/hummingbot/core/data_type/ \
        --ignore=$NONKYC/test_nonkyc_live_api.py \
        --ignore=$NONKYC/test_nonkyc_live_connector_smoke.py \
        --ignore=$NONKYC/nonkyc_auth_test.py \
        -m 'not quarantined and not live_api' \
        --tb=line -q --timeout=60"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 13: FINAL SUMMARY
# ═════════════════════════════════════════════════════════════════════════════
SCRIPT_END=$(date +%s)
TOTAL_ELAPSED=$((SCRIPT_END - SCRIPT_START))

echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  FINAL SUMMARY                                              ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Total sections run:  $SECTION_COUNT"
echo "  Passed:              $PASS_COUNT"
echo "  Failed:              $FAIL_COUNT"
echo "  Skipped:             $SKIP_COUNT"
echo "  Total time:          ${TOTAL_ELAPSED}s"
echo ""

if [ ${#FAILED_SECTIONS[@]} -gt 0 ]; then
    echo "  FAILED SECTIONS:"
    for fs in "${FAILED_SECTIONS[@]}"; do
        echo "    - $fs"
    done
    echo ""
fi

echo "  NonKYC live tests: $([ "$HAVE_NONKYC_KEYS" = true ] && echo 'RAN' || echo 'SKIPPED')"
echo "  MEXC live tests:   $([ "$HAVE_MEXC_KEYS" = true ] && echo 'RAN' || echo 'SKIPPED (no live tests exist yet)')"
echo ""
echo "  Completed at: $(date)"
echo ""
echo "  Full log saved to: $(pwd)/$LOG_FILE"
echo "  You can paste this file into Claude for analysis."
echo "================================================================"

if [ $FAIL_COUNT -gt 0 ]; then
    exit 1
fi
exit 0
