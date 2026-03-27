#!/usr/bin/env bash
# connectors_only_tests.sh — Connector-only test runner for NonKYC + MEXC
# Produces a clean connector-only report separate from the broad repo sweep.
# Usage: bash connectors_only_tests.sh
# Output: test_logs/connectors_YYYYMMDD_HHMMSS.log

set -uo pipefail

LOG_DIR="test_logs"
mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${LOG_DIR}/connectors_${TIMESTAMP}.log"
exec > >(tee -a "$LOG_FILE") 2>&1

GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

echo "================================================================"
echo "  CONNECTOR-ONLY TEST REPORT"
echo "================================================================"
echo "Date:     $(date)"
echo "Git SHA:  $GIT_SHA"
echo "Branch:   $BRANCH"
echo "Python:   $(python --version 2>&1)"
echo "Pytest:   $(python -m pytest --version 2>&1 | head -1)"
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

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
SECTION_COUNT=0
declare -a FAILED_SECTIONS=()
SCRIPT_START=$(date +%s)
set +e

NONKYC="test/hummingbot/connector/exchange/nonkyc"
MEXC="test/hummingbot/connector/exchange/mexc"
PYTEST="python -m pytest"
PYTEST_V="$PYTEST -v --tb=short --timeout=60"

run_test() {
    local label="$1"; local command="$2"
    SECTION_COUNT=$((SECTION_COUNT + 1))
    echo "================================================================"
    echo "[$SECTION_COUNT] $label"
    echo "================================================================"
    local start=$(date +%s)
    eval "$command" 2>&1
    local rc=$?
    local elapsed=$(( $(date +%s) - start ))
    if [ $rc -eq 0 ]; then
        echo "  >> PASSED  ($elapsed s)"; PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "  >> FAILED  (exit $rc, $elapsed s)"; FAIL_COUNT=$((FAIL_COUNT + 1))
        FAILED_SECTIONS+=("$label")
    fi
    echo ""
}

run_skip() {
    local label="$1"; local reason="$2"
    SECTION_COUNT=$((SECTION_COUNT + 1))
    echo "[$SECTION_COUNT] $label — SKIPPED ($reason)"
    SKIP_COUNT=$((SKIP_COUNT + 1)); echo ""
}

# ═══ LANE A: DETERMINISTIC UNIT TESTS ═══════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  LANE A: DETERMINISTIC UNIT TESTS                           ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "NonKYC Unit: all unit tests" \
    "$PYTEST_V $NONKYC/ --ignore=$NONKYC/test_nonkyc_live_api.py --ignore=$NONKYC/test_nonkyc_live_connector_smoke.py --ignore=$NONKYC/test_nonkyc_public_connector_smoke.py --ignore=$NONKYC/test_nonkyc_private_connector_smoke.py --ignore=$NONKYC/nonkyc_auth_test.py -m 'not live_api'"

run_test "MEXC Unit: all unit tests" \
    "$PYTEST_V $MEXC/ --ignore=$MEXC/test_mexc_live_api.py --ignore=$MEXC/test_mexc_live_connector_smoke.py --ignore=$MEXC/test_mexc_public_contract_smoke.py -m 'not live_api'"

run_test "MEXC Contract: rate-limit weight verification" \
    "$PYTEST_V $MEXC/test_mexc_rate_limit_contract.py"

# ═══ LANE B: PUBLIC LIVE SMOKE ══════════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  LANE B: PUBLIC LIVE SMOKE                                  ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

run_test "NonKYC Public Live: test_nonkyc_live_api.py (public)" \
    "$PYTEST_V $NONKYC/test_nonkyc_live_api.py -k 'not auth'"

run_test "NonKYC Public Live: test_nonkyc_public_connector_smoke.py" \
    "$PYTEST_V $NONKYC/test_nonkyc_public_connector_smoke.py"

run_test "MEXC Public Live: test_mexc_live_api.py (public)" \
    "$PYTEST_V $MEXC/test_mexc_live_api.py -k 'not auth'"

run_test "MEXC Public Live: test_mexc_live_connector_smoke.py" \
    "$PYTEST_V $MEXC/test_mexc_live_connector_smoke.py"

run_test "MEXC Public Live: test_mexc_public_contract_smoke.py" \
    "$PYTEST_V $MEXC/test_mexc_public_contract_smoke.py"

# ═══ LANE C: AUTHENTICATED SMOKE ═══════════════════════════════════════
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  LANE C: AUTHENTICATED SMOKE                                ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

if [ "$HAVE_NONKYC_KEYS" = "true" ]; then
    run_test "NonKYC Auth: test_nonkyc_private_connector_smoke.py" \
        "$PYTEST_V $NONKYC/test_nonkyc_private_connector_smoke.py"
    run_test "NonKYC Auth: test_nonkyc_live_api.py (authenticated)" \
        "$PYTEST_V $NONKYC/test_nonkyc_live_api.py -k 'auth'"
    run_test "NonKYC Auth: nonkyc_auth_test.py (standalone)" \
        "python $NONKYC/nonkyc_auth_test.py"
else
    run_skip "NonKYC Auth: all authenticated tests" \
        "Set NONKYC_API_KEY and NONKYC_API_SECRET to enable"
fi

if [ "$HAVE_MEXC_KEYS" = "true" ]; then
    run_test "MEXC Auth: test_mexc_live_api.py (authenticated)" \
        "$PYTEST_V $MEXC/test_mexc_live_api.py -k 'auth'"
else
    run_skip "MEXC Auth: authenticated tests" \
        "N/A — MEXC has no sandbox; auth testing intentionally excluded"
fi

# ═══ LANE D: REPORT ════════════════════════════════════════════════════
ELAPSED=$(( $(date +%s) - SCRIPT_START ))
echo ""
echo "================================================================"
echo "  CONNECTOR-ONLY RELEASE REPORT"
echo "================================================================"
echo "  Git SHA:    $GIT_SHA"
echo "  Branch:     $BRANCH"
echo "  Date:       $(date)"
echo "  Sections:   $SECTION_COUNT"
echo "  Passed:     $PASS_COUNT"
echo "  Failed:     $FAIL_COUNT"
echo "  Skipped:    $SKIP_COUNT"
echo "  Time:       ${ELAPSED}s"
echo ""
if [ ${#FAILED_SECTIONS[@]} -gt 0 ]; then
    echo "  FAILED SECTIONS:"
    for s in "${FAILED_SECTIONS[@]}"; do echo "    - $s"; done
else
    echo "  ALL SECTIONS PASSED"
fi
echo ""
echo "  NonKYC keys: $HAVE_NONKYC_KEYS"
echo "  MEXC keys:   $HAVE_MEXC_KEYS"
echo "  Log: $(pwd)/$LOG_FILE"
echo "================================================================"

if [ $FAIL_COUNT -gt 0 ]; then exit 1; fi
exit 0
