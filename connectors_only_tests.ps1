# connectors_only_tests.ps1 — Connector-only test runner for NonKYC + MEXC
# Usage: powershell -ExecutionPolicy Bypass -File connectors_only_tests.ps1

$ErrorActionPreference = "Continue"

$LogDir = "test_logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("connectors_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
Start-Transcript -Path $LogFile -Append

$script:PassCount = 0; $script:FailCount = 0; $script:SkipCount = 0
$script:SectionCount = 0; $script:FailedSections = @()
$ScriptStart = Get-Date

function Run-Test { param([string]$Label, [string]$Command)
    $script:SectionCount++
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "[$script:SectionCount] $Label" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    $start = Get-Date
    Invoke-Expression $Command 2>&1 | ForEach-Object { Write-Host $_ }
    $rc = $LASTEXITCODE
    $elapsed = [math]::Round(((Get-Date) - $start).TotalSeconds)
    if ($rc -eq 0) { Write-Host "  >> PASSED  ($elapsed s)" -ForegroundColor Green; $script:PassCount++ }
    else { Write-Host "  >> FAILED  (exit $rc, $elapsed s)" -ForegroundColor Red; $script:FailCount++; $script:FailedSections += $Label }
    Write-Host ""
}
function Run-Skip { param([string]$Label, [string]$Reason)
    $script:SectionCount++
    Write-Host "[$script:SectionCount] $Label — SKIPPED ($Reason)" -ForegroundColor Yellow
    $script:SkipCount++; Write-Host ""
}

$NONKYC = "test/hummingbot/connector/exchange/nonkyc"
$MEXC = "test/hummingbot/connector/exchange/mexc"
$PYTEST_V = "python -m pytest -v --tb=short --timeout=60"

$HaveNonkycKeys = $false; if ($env:NONKYC_API_KEY -and $env:NONKYC_API_SECRET) { $HaveNonkycKeys = $true }
$HaveMexcKeys = $false; if ($env:MEXC_API_KEY -and $env:MEXC_API_SECRET) { $HaveMexcKeys = $true }

Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  CONNECTOR-ONLY TEST REPORT" -ForegroundColor Magenta
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "NonKYC keys: $HaveNonkycKeys | MEXC keys: $HaveMexcKeys"
Write-Host ""

# LANE A
Write-Host "  LANE A: DETERMINISTIC UNIT TESTS" -ForegroundColor Magenta
Run-Test "NonKYC Unit" "$PYTEST_V $NONKYC/ --ignore=$NONKYC/test_nonkyc_live_api.py --ignore=$NONKYC/test_nonkyc_live_connector_smoke.py --ignore=$NONKYC/test_nonkyc_public_connector_smoke.py --ignore=$NONKYC/test_nonkyc_private_connector_smoke.py --ignore=$NONKYC/nonkyc_auth_test.py -m 'not live_api'"
Run-Test "MEXC Unit" "$PYTEST_V $MEXC/ --ignore=$MEXC/test_mexc_live_api.py --ignore=$MEXC/test_mexc_live_connector_smoke.py --ignore=$MEXC/test_mexc_public_contract_smoke.py -m 'not live_api'"
Run-Test "MEXC Rate-Limit Contract" "$PYTEST_V $MEXC/test_mexc_rate_limit_contract.py"

# LANE B
Write-Host "  LANE B: PUBLIC LIVE SMOKE" -ForegroundColor Magenta
Run-Test "NonKYC Public Live API" "$PYTEST_V $NONKYC/test_nonkyc_live_api.py -k 'not auth'"
Run-Test "NonKYC Public Connector Smoke" "$PYTEST_V $NONKYC/test_nonkyc_public_connector_smoke.py"
Run-Test "MEXC Public Live API" "$PYTEST_V $MEXC/test_mexc_live_api.py -k 'not auth'"
Run-Test "MEXC Public Connector Smoke" "$PYTEST_V $MEXC/test_mexc_live_connector_smoke.py"
Run-Test "MEXC Public Contract Smoke" "$PYTEST_V $MEXC/test_mexc_public_contract_smoke.py"

# LANE C
Write-Host "  LANE C: AUTHENTICATED SMOKE" -ForegroundColor Magenta
if ($HaveNonkycKeys) {
    Run-Test "NonKYC Private Smoke" "$PYTEST_V $NONKYC/test_nonkyc_private_connector_smoke.py"
    Run-Test "NonKYC Auth Live API" "$PYTEST_V $NONKYC/test_nonkyc_live_api.py -k 'auth'"
    Run-Test "NonKYC Auth Standalone" "python $NONKYC/nonkyc_auth_test.py"
} else { Run-Skip "NonKYC Auth" "Set NONKYC_API_KEY and NONKYC_API_SECRET" }
if ($HaveMexcKeys) { Run-Test "MEXC Auth Live API" "$PYTEST_V $MEXC/test_mexc_live_api.py -k 'auth'" }
else { Run-Skip "MEXC Auth" "N/A — no sandbox" }

# SUMMARY
$Elapsed = [math]::Round(((Get-Date) - $ScriptStart).TotalSeconds)
Write-Host ""
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  CONNECTOR-ONLY RELEASE REPORT" -ForegroundColor Magenta
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  Sections: $script:SectionCount | Passed: $script:PassCount | Failed: $script:FailCount | Skipped: $script:SkipCount | Time: ${Elapsed}s"
if ($script:FailedSections.Count -gt 0) { Write-Host "  FAILED:" -ForegroundColor Red; foreach ($f in $script:FailedSections) { Write-Host "    - $f" -ForegroundColor Red } }
else { Write-Host "  ALL SECTIONS PASSED" -ForegroundColor Green }
Write-Host "================================================================"

Stop-Transcript
if ($script:FailCount -gt 0) { exit 1 }; exit 0
