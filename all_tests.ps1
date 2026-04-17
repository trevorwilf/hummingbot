# all_tests.ps1 — Comprehensive test runner for NonKYC + MEXC connectors
# Run from the repo root: powershell -ExecutionPolicy Bypass -File all_tests.ps1
#
# All output is logged to test_logs/all_tests_YYYYMMDD_HHMMSS.log

$ErrorActionPreference = "Continue"

$StartTime = Get-Date
# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1: ENVIRONMENT CHECK & LOG SETUP
# ═════════════════════════════════════════════════════════════════════════════
$LogDir = "test_logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("all_tests_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
Start-Transcript -Path $LogFile -Append

Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "SECTION 1: ENVIRONMENT CHECK" -ForegroundColor Magenta
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "Date:           $(Get-Date)"
Write-Host "Python:         $(python --version 2>&1)"
Write-Host "Pytest:         $(python -m pytest --version 2>&1 | Select-Object -First 1)"
Write-Host "Log file:       $(Resolve-Path $LogFile)"
Write-Host ""

$HaveNonkycKeys = $false
if ($env:NONKYC_API_KEY -and $env:NONKYC_API_SECRET) {
    $HaveNonkycKeys = $true
}

$HaveMexcKeys = $false
if ($env:MEXC_API_KEY -and $env:MEXC_API_SECRET) {
    $HaveMexcKeys = $true
}

Write-Host "NonKYC API keys: $HaveNonkycKeys"
Write-Host "MEXC API keys:   $HaveMexcKeys"
Write-Host ""

# ── Counters & tracking ─────────────────────────────────────────────────────
$script:PassCount = 0
$script:FailCount = 0
$script:SkipCount = 0
$script:SectionCount = 0
$script:FailedSections = @()
$ScriptStart = Get-Date

function Run-Test {
    param(
        [string]$Label,
        [string]$Command
    )
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
    param(
        [string]$Label,
        [string]$Reason
    )
    $script:SectionCount++
    Write-Host "================================================================" -ForegroundColor Yellow
    Write-Host "[$script:SectionCount] $Label" -ForegroundColor Yellow
    Write-Host "  >> SKIPPED: $Reason" -ForegroundColor Yellow
    Write-Host "================================================================" -ForegroundColor Yellow
    $script:SkipCount++
    Write-Host ""
}

# ── Path shorthands ──────────────────────────────────────────────────────────
$NONKYC         = "test/hummingbot/connector/exchange/nonkyc"
$MEXC           = "test/hummingbot/connector/exchange/mexc"
$NONKYC_CANDLES = "test/hummingbot/data_feed/candles_feed/nonkyc_spot_candles"
$MEXC_SPOT_CANDLES = "test/hummingbot/data_feed/candles_feed/mexc_spot_candles"
$MEXC_PERP_CANDLES = "test/hummingbot/data_feed/candles_feed/mexc_perpetual_candles"
$CANDLES_FEED   = "test/hummingbot/data_feed/candles_feed"
$EXECUTORS      = "test/hummingbot/strategy_v2/executors"
$CONTROLLERS    = "test/hummingbot/strategy_v2/controllers"

$PYTEST   = "python -m pytest"
$PYTEST_V = "$PYTEST -v --tb=short --timeout=60"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2: NONKYC CONNECTOR — UNIT TESTS
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 2: NONKYC CONNECTOR - UNIT TESTS" -ForegroundColor Magenta
Write-Host ""

Run-Test "NonKYC: test_nonkyc_auth.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_auth.py"

Run-Test "NonKYC: test_nonkyc_auth_nonce.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_auth_nonce.py"

Run-Test "NonKYC: test_nonkyc_fee_schema.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_fee_schema.py"

Run-Test "NonKYC: test_nonkyc_utils.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_utils.py"

Run-Test "NonKYC: test_nonkyc_web_utils.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_web_utils.py"

Run-Test "NonKYC: test_nonkyc_order_book.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_order_book.py"

Run-Test "NonKYC: test_nonkyc_api_order_book_data_source.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_api_order_book_data_source.py"

Run-Test "NonKYC: test_nonkyc_api_user_stream_data_source.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_api_user_stream_data_source.py"

Run-Test "NonKYC: test_nonkyc_exchange.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_exchange.py"

Run-Test "NonKYC: test_nonkyc_bugfixes.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_bugfixes.py"

Run-Test "NonKYC: test_nonkyc_expert_review_fixes.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_expert_review_fixes.py"

Run-Test "NonKYC: test_nonkyc_time_sync_integration.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_time_sync_integration.py"

Run-Test "NonKYC: test_nonkyc_phase2_fixes.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase2_fixes.py"

Run-Test "NonKYC: test_nonkyc_phase3_optimization.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase3_optimization.py"

Run-Test "NonKYC: test_nonkyc_phase5a.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase5a.py"

Run-Test "NonKYC: test_nonkyc_phase5c.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase5c.py"

Run-Test "NonKYC: test_nonkyc_phase5d.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase5d.py"

Run-Test "NonKYC: test_nonkyc_phase6.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase6.py"

Run-Test "NonKYC: test_nonkyc_phase7a.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase7a.py"

Run-Test "NonKYC: test_nonkyc_phase7b.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase7b.py"

Run-Test "NonKYC: test_nonkyc_phase7c.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase7c.py"

Run-Test "NonKYC: test_nonkyc_phase2_connector_hardening.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_phase2_connector_hardening.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3: NONKYC DATA FEED & RATE ORACLE
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 3: NONKYC DATA FEED & SHARED" -ForegroundColor Magenta
Write-Host ""

Run-Test "NonKYC: test_nonkyc_spot_candles.py" `
    "$PYTEST_V $NONKYC_CANDLES/test_nonkyc_spot_candles.py"

Run-Test "Shared: test_candles_factory.py" `
    "$PYTEST_V $CANDLES_FEED/test_candles_factory.py"

Run-Test "Shared: test_market_data_provider.py" `
    "$PYTEST_V test/hummingbot/data_feed/test_market_data_provider.py"

Run-Test "Phase 1: test_phase1_wiring.py (controller deployment wiring)" `
    "$PYTEST_V tests/phase1/test_phase1_wiring.py"

Run-Test "Phase 2: test_mean_reversion.py (MR controller correctness)" `
    "$PYTEST_V tests/phase2/test_mean_reversion.py"

Run-Test "Phase 3: test_ema_regime_hold.py (EMA controller correctness)" `
    "$PYTEST_V tests/phase3/test_ema_regime_hold.py"

Run-Test "Phase 4: test_mexc_spot_candles.py (MEXC adapter)" `
    "$PYTEST_V tests/phase4/test_mexc_spot_candles.py"

Run-Test "Phase 4: test_nonkyc_validator.py (NonKYC validator)" `
    "$PYTEST_V tests/phase4/test_nonkyc_validator.py"

Run-Test "Phase 4: test_rsi_wilder.py (shared TA utility)" `
    "$PYTEST_V tests/phase4/test_rsi_wilder.py"

Run-Test "Phase 5: test_loader_to_factory_route.py (integration routing)" `
    "$PYTEST_V tests/phase5/test_loader_to_factory_route.py"

Run-Test "Phase 5: test_candle_feed_reuse.py (feed reuse contract)" `
    "$PYTEST_V tests/phase5/test_candle_feed_reuse.py"

Run-Test "Phase 5: test_decision_trace.py (MR/EMA trace emission)" `
    "$PYTEST_V tests/phase5/test_decision_trace.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4: NONKYC CONNECTOR — AGGREGATE PASS
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 4: NONKYC CONNECTOR - AGGREGATE PASS" -ForegroundColor Magenta
Write-Host ""

Run-Test "NonKYC: aggregate directory pass (excluding live tests)" `
    "$PYTEST $NONKYC/ --ignore=$NONKYC/test_nonkyc_live_api.py --ignore=$NONKYC/test_nonkyc_live_connector_smoke.py --ignore=$NONKYC/nonkyc_auth_test.py -m 'not live_api' -v --tb=short --timeout=60"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5: NONKYC LIVE API TESTS (conditional)
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 5: NONKYC LIVE API TESTS" -ForegroundColor Magenta
Write-Host ""

# Always run public NonKYC live tests (Tier 1 + Tier 5 don't need keys)
Run-Test "NonKYC Live: test_nonkyc_live_api.py (public tests)" `
    "$PYTEST_V $NONKYC/test_nonkyc_live_api.py -k 'not auth'"

if ($HaveNonkycKeys) {
    Run-Test "NonKYC Live: test_nonkyc_live_api.py (authenticated tests)" `
        "$PYTEST_V $NONKYC/test_nonkyc_live_api.py -k 'auth'"

    Run-Test "NonKYC Live: test_nonkyc_live_connector_smoke.py" `
        "$PYTEST_V $NONKYC/test_nonkyc_live_connector_smoke.py"

    Run-Test "NonKYC Live: nonkyc_auth_test.py (standalone)" `
        "python $NONKYC/nonkyc_auth_test.py"
} else {
    Run-Skip "NonKYC Live: authenticated tests" `
        "Set NONKYC_API_KEY and NONKYC_API_SECRET to enable"
    Run-Skip "NonKYC Live: test_nonkyc_live_connector_smoke.py" `
        "Set NONKYC_API_KEY and NONKYC_API_SECRET to enable"
    Run-Skip "NonKYC Live: nonkyc_auth_test.py (standalone)" `
        "Set NONKYC_API_KEY and NONKYC_API_SECRET to enable"
}

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6: MEXC CONNECTOR — UNIT TESTS
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 6: MEXC CONNECTOR - UNIT TESTS" -ForegroundColor Magenta
Write-Host ""

Run-Test "MEXC: test_mexc_auth.py" `
    "$PYTEST_V $MEXC/test_mexc_auth.py"

Run-Test "MEXC: test_mexc_utils.py" `
    "$PYTEST_V $MEXC/test_mexc_utils.py"

Run-Test "MEXC: test_mexc_web_utils.py" `
    "$PYTEST_V $MEXC/test_mexc_web_utils.py"

Run-Test "MEXC: test_mexc_order_book.py" `
    "$PYTEST_V $MEXC/test_mexc_order_book.py"

Run-Test "MEXC: test_mexc_post_processor.py" `
    "$PYTEST_V $MEXC/test_mexc_post_processor.py"

Run-Test "MEXC: test_mexc_api_order_book_data_source.py" `
    "$PYTEST_V $MEXC/test_mexc_api_order_book_data_source.py"

Run-Test "MEXC: test_mexc_user_stream_data_source.py" `
    "$PYTEST_V $MEXC/test_mexc_user_stream_data_source.py"

Run-Test "MEXC: test_mexc_exchange.py" `
    "$PYTEST_V $MEXC/test_mexc_exchange.py"

Run-Test "MEXC: test_mexc_expert_review_fixes.py" `
    "$PYTEST_V $MEXC/test_mexc_expert_review_fixes.py"

Run-Test "MEXC: test_mexc_fee_schema.py" `
    "$PYTEST_V $MEXC/test_mexc_fee_schema.py"

Run-Test "MEXC: test_mexc_balance_preadjust.py" `
    "$PYTEST_V $MEXC/test_mexc_balance_preadjust.py"

Run-Test "MEXC: test_mexc_fill_recreation_fee.py" `
    "$PYTEST_V $MEXC/test_mexc_fill_recreation_fee.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7: MEXC DATA FEED & RATE ORACLE
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 7: MEXC DATA FEED & RATE ORACLE" -ForegroundColor Magenta
Write-Host ""

Run-Test "MEXC: test_mexc_spot_candles.py" `
    "$PYTEST_V $MEXC_SPOT_CANDLES/test_mexc_spot_candles.py"

Run-Test "MEXC: test_mexc_perpetual_candles.py" `
    "$PYTEST_V $MEXC_PERP_CANDLES/test_mexc_perpetual_candles.py"

Run-Test "MEXC: test_mexc_rate_source.py" `
    "$PYTEST_V test/hummingbot/core/rate_oracle/sources/test_mexc_rate_source.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8: MEXC CONNECTOR — AGGREGATE PASS
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 8: MEXC CONNECTOR - AGGREGATE PASS" -ForegroundColor Magenta
Write-Host ""

Run-Test "MEXC: aggregate directory pass (excluding live tests)" `
    "$PYTEST $MEXC/ --ignore=$MEXC/test_mexc_live_api.py -v --tb=short --timeout=60 -m 'not live_api'"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9: MEXC LIVE / AUTHENTICATED TESTS (conditional)
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 9: MEXC LIVE API TESTS" -ForegroundColor Magenta
Write-Host ""

# 9a. MEXC public API tests (no auth required — always run)
Run-Test "MEXC Live: test_mexc_live_api.py (public tests)" `
    "$PYTEST_V $MEXC/test_mexc_live_api.py -k 'not auth'"

# 9b. MEXC authenticated API tests (only if keys are set)
if ($HaveMexcKeys) {
    Run-Test "MEXC Live: test_mexc_live_api.py (authenticated tests)" `
        "$PYTEST_V $MEXC/test_mexc_live_api.py -k 'auth'"
} else {
    Run-Skip "MEXC Live: authenticated tests" `
        "Set MEXC_API_KEY and MEXC_API_SECRET to enable"
}

# ═════════════════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9b: MEXC PUBLIC CONTRACT SMOKE (always runs, no auth needed)
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 9b: MEXC PUBLIC CONTRACT SMOKE" -ForegroundColor Magenta
Write-Host ""

Run-Test "MEXC Public: test_mexc_public_contract_smoke.py" `
    "$PYTEST_V $MEXC/test_mexc_public_contract_smoke.py"

Run-Test "MEXC Public: test_mexc_live_connector_smoke.py" `
    "$PYTEST_V $MEXC/test_mexc_live_connector_smoke.py"

Run-Test "MEXC Contract: test_mexc_rate_limit_contract.py" `
    "$PYTEST_V $MEXC/test_mexc_rate_limit_contract.py"

# Always run NonKYC public connector smoke (no keys needed)
Run-Test "NonKYC Public: test_nonkyc_public_connector_smoke.py" `
    "$PYTEST_V $NONKYC/test_nonkyc_public_connector_smoke.py"

# SECTION 10: STRATEGY & EXECUTOR TESTS
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 10: STRATEGY & EXECUTOR TESTS" -ForegroundColor Magenta
Write-Host ""

Run-Test "Strategy: test_executor_base.py" `
    "$PYTEST_V $EXECUTORS/test_executor_base.py"

Run-Test "Strategy: test_executor_orchestrator.py" `
    "$PYTEST_V $EXECUTORS/test_executor_orchestrator.py"

Run-Test "Strategy: test_position_executor.py" `
    "$PYTEST_V $EXECUTORS/position_executor/test_position_executor.py"

Run-Test "Strategy: test_terminal_error_patterns.py" `
    "$PYTEST_V $EXECUTORS/position_executor/test_terminal_error_patterns.py"

Run-Test "Strategy: test_market_making_controller_base.py" `
    "$PYTEST_V $CONTROLLERS/test_market_making_controller_base.py"

Run-Test "Strategy: test_pmm_dynamic.py" `
    "$PYTEST_V $CONTROLLERS/test_pmm_dynamic.py"

Run-Test "Strategy: test_wallet_balance_seeding.py" `
    "$PYTEST_V $CONTROLLERS/test_wallet_balance_seeding.py"

Run-Test "Strategy: test_range_inventory_ladder_budget.py" `
    "$PYTEST_V $CONTROLLERS/test_range_inventory_ladder_budget.py"

Run-Test "Strategy: test_range_inventory_ladder_compression.py" `
    "$PYTEST_V $CONTROLLERS/test_range_inventory_ladder_compression.py"

Run-Test "Strategy: test_range_inventory_ladder_startup.py" `
    "$PYTEST_V $CONTROLLERS/test_range_inventory_ladder_startup.py"

Run-Test "Strategy: test_range_inventory_ladder_observability.py" `
    "$PYTEST_V $CONTROLLERS/test_range_inventory_ladder_observability.py"

Run-Test "Strategy: test_range_inventory_ladder_shutdown_aware.py" `
    "$PYTEST_V $CONTROLLERS/test_range_inventory_ladder_shutdown_aware.py"

Run-Test "Strategy: test_range_inventory_ladder_quote_quota.py" `
    "$PYTEST_V $CONTROLLERS/test_range_inventory_ladder_quote_quota.py"

Run-Test "Strategy: test_range_inventory_ladder_weight_denominator.py" `
    "$PYTEST_V $CONTROLLERS/test_range_inventory_ladder_weight_denominator.py"

Run-Test "Strategy: test_range_inventory_ladder_post_refresh_settle.py" `
    "$PYTEST_V $CONTROLLERS/test_range_inventory_ladder_post_refresh_settle.py"

Run-Test "Connector: test_budget_checker.py" `
    "$PYTEST_V test/hummingbot/connector/test_budget_checker.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 10b: PROVENANCE & LIFECYCLE TESTS
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 10b: PROVENANCE & LIFECYCLE TESTS" -ForegroundColor Magenta
Write-Host ""

Run-Test "Logger: test_structured_event_logger.py" `
    "$PYTEST_V test/hummingbot/logger/test_structured_event_logger.py"

Run-Test "Model: test_provenance_columns.py" `
    "$PYTEST_V test/hummingbot/model/test_provenance_columns.py"

Run-Test "Model: test_bot_run.py" `
    "$PYTEST_V test/hummingbot/model/test_bot_run.py"

Run-Test "Model: test_order_lifecycle_event.py" `
    "$PYTEST_V test/hummingbot/model/test_order_lifecycle_event.py"

Run-Test "Persistence: test_lifecycle_event.py" `
    "$PYTEST_V test/hummingbot/persistence/test_lifecycle_event.py"

Run-Test "Persistence: test_lifecycle_writer.py" `
    "$PYTEST_V test/hummingbot/persistence/test_lifecycle_writer.py"

Run-Test "Connector: test_markets_recorder_provenance.py" `
    "$PYTEST_V test/hummingbot/connector/test_markets_recorder_provenance.py"

Run-Test "Connector: test_id_propagation.py" `
    "$PYTEST_V test/hummingbot/connector/test_id_propagation.py"

Run-Test "Connector: test_markets_recorder_botrun.py" `
    "$PYTEST_V test/hummingbot/connector/test_markets_recorder_botrun.py"

Run-Test "Persistence: test_replay_tool.py" `
    "$PYTEST_V test/hummingbot/persistence/test_replay_tool.py"

Run-Test "Connector: test_lifecycle_integration.py" `
    "$PYTEST_V test/hummingbot/connector/test_lifecycle_integration.py"

Run-Test "Connector: test_executor_lineage_comprehensive.py" `
    "$PYTEST_V test/hummingbot/connector/test_executor_lineage_comprehensive.py"

Run-Test "Logger: test_structured_event_logger_strict_jsonl.py" `
    "$PYTEST_V test/hummingbot/logger/test_structured_event_logger_strict_jsonl.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 11: CORE INFRASTRUCTURE TESTS
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 11: CORE INFRASTRUCTURE TESTS" -ForegroundColor Magenta
Write-Host ""

Run-Test "Core: test_order_book_tracker.py" `
    "$PYTEST_V test/hummingbot/core/data_type/test_order_book_tracker.py"

Run-Test "Core: test_retry_backoff_consistency.py" `
    "$PYTEST_V test/hummingbot/core/data_type/test_retry_backoff_consistency.py"

Run-Test "Core: test_kill_switch.py" `
    "$PYTEST_V test/hummingbot/core/utils/test_kill_switch.py"

Run-Test "Core: test_trading_core.py" `
    "$PYTEST_V test/hummingbot/core/test_trading_core.py"

Run-Test "Core: test_connector_manager.py" `
    "$PYTEST_V test/hummingbot/core/test_connector_manager.py"

Run-Test "Core: test_performance.py" `
    "$PYTEST_V test/hummingbot/client/test_performance.py"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 12: BROAD REGRESSION SWEEP
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  SECTION 12: BROAD REGRESSION SWEEP" -ForegroundColor Magenta
Write-Host ""

Run-Test "Regression sweep (all connector + strategy + core)" `
    "$PYTEST $NONKYC/ $MEXC/ $NONKYC_CANDLES/ $MEXC_SPOT_CANDLES/ $MEXC_PERP_CANDLES/ $EXECUTORS/ $CONTROLLERS/ test/hummingbot/core/data_type/ --ignore=$NONKYC/test_nonkyc_live_api.py --ignore=$NONKYC/test_nonkyc_live_connector_smoke.py --ignore=$NONKYC/nonkyc_auth_test.py -m 'not quarantined and not live_api' --tb=line -q --timeout=60"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 13: FINAL SUMMARY
# ═════════════════════════════════════════════════════════════════════════════
$TotalElapsed = [math]::Round(((Get-Date) - $ScriptStart).TotalSeconds)

Write-Host ""
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  FINAL SUMMARY" -ForegroundColor Magenta
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host ""
Write-Host "  Total sections run:  $script:SectionCount"

if ($script:PassCount -gt 0) {
    Write-Host "  Passed:              $script:PassCount" -ForegroundColor Green
}
if ($script:FailCount -gt 0) {
    Write-Host "  Failed:              $script:FailCount" -ForegroundColor Red
} else {
    Write-Host "  Failed:              0"
}
if ($script:SkipCount -gt 0) {
    Write-Host "  Skipped:             $script:SkipCount" -ForegroundColor Yellow
}
Write-Host "  Total time:          ${TotalElapsed}s"
Write-Host ""

if ($script:FailedSections.Count -gt 0) {
    Write-Host "  FAILED SECTIONS:" -ForegroundColor Red
    foreach ($fs in $script:FailedSections) {
        Write-Host "    - $fs" -ForegroundColor Red
    }
    Write-Host ""
}

$endTime = Get-Date
$duration = $endTime - $startTime

$nonkycStatus = if ($HaveNonkycKeys) { "RAN (public + authenticated)" } else { "RAN (public only)" }
$mexcStatus = if ($HaveMexcKeys) { "RAN (public + authenticated)" } else { "RAN (public only)" }
Write-Host "  NonKYC live tests: $nonkycStatus"
Write-Host "  MEXC live tests:   $mexcStatus"
Write-Host ""
Write-Host "  Completed at: $(Get-Date)"
Write-Host "  Total duration: $($duration.TotalSeconds) seconds"
Write-Host "  Total duration: $($duration.TotalMinutes) minutes"
Write-Host ""
Write-Host "  Full log saved to: $(Resolve-Path $LogFile)"
Write-Host "  You can paste this file into Claude for analysis."
Write-Host "================================================================"

Stop-Transcript

if ($script:FailCount -gt 0) {
    exit 1
}
exit 0
