# new_tests.ps1 — Runs ONLY the tests created or modified by the expert review fix set
# Run from repo root: powershell -ExecutionPolicy Bypass -File new_tests.ps1

$ErrorActionPreference = "Continue"

$script:PassCount = 0
$script:FailCount = 0
$script:SkipCount = 0
$script:SectionCount = 0
$script:FailedSections = @()
$ScriptStart = Get-Date

function Run-Test {
    param([string]$Label, [string]$Command)
    $script:SectionCount++
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "[$script:SectionCount] $Label" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    $start = Get-Date
    Invoke-Expression $Command 2>&1 | ForEach-Object { Write-Host $_ }
    $rc = $LASTEXITCODE
    $elapsed = [math]::Round(((Get-Date) - $start).TotalSeconds)
    Write-Host ""
    if ($rc -eq 0) {
        Write-Host "  >> PASSED  ($elapsed s)" -ForegroundColor Green
        $script:PassCount++
    } else {
        Write-Host "  >> FAILED  (exit $rc, $elapsed s)" -ForegroundColor Red
        $script:FailCount++
        $script:FailedSections += $Label
    }
    Write-Host ""
}

function Run-Skip {
    param([string]$Label, [string]$Reason)
    $script:SectionCount++
    Write-Host "================================================================" -ForegroundColor Yellow
    Write-Host "[$script:SectionCount] $Label  >> SKIPPED: $Reason" -ForegroundColor Yellow
    Write-Host "================================================================" -ForegroundColor Yellow
    $script:SkipCount++
    Write-Host ""
}

$PYTEST_V = "python -m pytest -v --tb=short --timeout=60"
$MEXC = "test/hummingbot/connector/exchange/mexc"
$NONKYC = "test/hummingbot/connector/exchange/nonkyc"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1: ENVIRONMENT CHECK
# ═════════════════════════════════════════════════════════════════════════════
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  NEW TESTS — Expert Review Fix Validation" -ForegroundColor Magenta
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "Date:    $(Get-Date)"
Write-Host "Python:  $(python --version 2>&1)"

$HaveNonkycKeys = $false
if ($env:NONKYC_API_KEY -and $env:NONKYC_API_SECRET) { $HaveNonkycKeys = $true }
Write-Host "NonKYC keys: $HaveNonkycKeys"
Write-Host ""

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2: MEXC Auth Fix Tests (Fix 1 + Fix 2)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "MEXC Auth tests (Content-Type fix)" `
    "$PYTEST_V $MEXC/test_mexc_auth.py"

Run-Test "MEXC Expert Review fixes (POST body + Content-Type)" `
    "$PYTEST_V $MEXC/test_mexc_expert_review_fixes.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3: MEXC Full Connector Tests (regression check)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "MEXC Exchange tests (regression)" `
    "$PYTEST_V $MEXC/test_mexc_exchange.py"

Run-Test "MEXC User Stream tests (regression)" `
    "$PYTEST_V $MEXC/test_mexc_user_stream_data_source.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4: NonKYC Fee Estimation Tests (Fix 5)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "NonKYC Phase5C (dynamic fees + alt-fee handling)" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase5c.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5: NonKYC Cancel-All Safety Tests (Fix 4)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "NonKYC Phase5D (cancel_all safety)" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase5d.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6: NonKYC Full Connector Tests (regression check)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "NonKYC Exchange tests (regression)" `
    "$PYTEST_V $NONKYC/test_nonkyc_exchange.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7: NonKYC Live Connector Smoke (Fix 6 — conditional)
# ═════════════════════════════════════════════════════════════════════════════
if ($HaveNonkycKeys) {
    Run-Test "NonKYC Live Connector Smoke (schema + event loop fix)" `
        "$PYTEST_V $NONKYC/test_nonkyc_live_connector_smoke.py"
} else {
    Run-Skip "NonKYC Live Connector Smoke" `
        "Set NONKYC_API_KEY and NONKYC_API_SECRET to enable"
}

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8: Cross-connector regression sweep
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "Regression sweep (MEXC + NonKYC unit tests)" `
    "python -m pytest $MEXC/ $NONKYC/ --ignore=$MEXC/test_mexc_live_api.py --ignore=$NONKYC/test_nonkyc_live_api.py --ignore=$NONKYC/test_nonkyc_live_connector_smoke.py --ignore=$NONKYC/nonkyc_auth_test.py -m 'not live_api and not quarantined' --tb=line -q --timeout=60"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 10: MEXC Listen Key Content-Type Fix (RCA Fix 1)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "MEXC Listen Key CT fix (omit Content-Type on empty body)" `
    "$PYTEST_V $MEXC/test_mexc_listen_key_fix.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 11: PositionExecutor TP Retry Fix (RCA Fix 2)
# ═════════════════════════════════════════════════════════════════════════════
$PE = "test/hummingbot/strategy_v2/executors/position_executor"
Run-Test "PositionExecutor TP retry increment fix" `
    "$PYTEST_V $PE/test_tp_retry_fix.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 12: PositionExecutor Deferred Close Fix (RCA Fix 3)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "PositionExecutor deferred close after cancel fix" `
    "$PYTEST_V $PE/test_deferred_close_fix.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 13: Controller Cross-Order Prevention (RCA Fix 4)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "Controller cross-order prevention + under-seeded warning" `
    "$PYTEST_V test/hummingbot/strategy_v2/controllers/test_cross_order_prevention.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 14: Error Classification Fix (RCA Fix 5)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "exchange_py_base error classification" `
    "$PYTEST_V test/hummingbot/connector/test_exchange_py_base_error_classification.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 15: PositionExecutor + Controller Regression (RCA)
# ═════════════════════════════════════════════════════════════════════════════
Run-Test "PositionExecutor full regression" `
    "$PYTEST_V $PE/test_position_executor.py"

Run-Test "MarketMakingControllerBase full regression" `
    "$PYTEST_V test/hummingbot/strategy_v2/controllers/test_market_making_controller_base.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 16: SUMMARY
# ═════════════════════════════════════════════════════════════════════════════
$TotalElapsed = [math]::Round(((Get-Date) - $ScriptStart).TotalSeconds)

Write-Host ""
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  SUMMARY" -ForegroundColor Magenta
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  Sections: $script:SectionCount  |  Passed: $script:PassCount  |  Failed: $script:FailCount  |  Skipped: $script:SkipCount  |  Time: ${TotalElapsed}s"

if ($script:FailedSections.Count -gt 0) {
    Write-Host "  FAILED:" -ForegroundColor Red
    foreach ($fs in $script:FailedSections) { Write-Host "    - $fs" -ForegroundColor Red }
}
Write-Host "================================================================"

if ($script:FailCount -gt 0) { exit 1 }
exit 0
