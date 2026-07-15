<#
  run_csf_batch.ps1 -- unattended 12-phase runner for
  HUMMINGBOT_CONNECTOR_STRATEGY_FIXES_V1_claude_code_prompt.md

  Each phase runs as its OWN `claude -p` invocation = a fresh context window
  (equivalent to /clear between phases) with zero human intervention. Git carries
  state across phases; a phase that cannot pass its tests writes BATCH_HALT_phase_N.md
  and the batch stops rather than building the next phase on a broken base.

  USAGE-LIMIT RESILIENCE: every phase is started with a pre-assigned --session-id.
  If the account usage limit (or a crash) interrupts a phase, the runner waits,
  probing availability every $ProbeIntervalMinutes, and then RESUMES THE SAME SESSION
  (--resume) so the agent continues mid-phase with its context intact. Falls back to
  a fresh attempt if the session cannot be resumed. Bounded by $MaxAttemptsPerPhase
  and $MaxLimitWaitHours.

  Kick off before bed (from the repo root):
      powershell -ExecutionPolicy Bypass -File .\run_csf_batch.ps1

  FIRST TIME: run the smoke test once (verifies auth, headless mode, the
  --dangerously-skip-permissions acceptance, model + effort flags, git state):
      powershell -ExecutionPolicy Bypass -File .\run_csf_batch.ps1 -SmokeTest

  Review in the morning:  batch_logs\  +  REPORT_csf_v1.md  +  any BATCH_HALT_*.md

  NOTE: this file is intentionally pure ASCII. Windows PowerShell 5.x reads .ps1 as
  ANSI and mangles non-ASCII (em-dashes etc.), which breaks parsing. Do not add Unicode.
#>

param([switch]$SmokeTest)

# IMPORTANT: must be Continue, not Stop. PS 5.1 turns redirected native stderr (2>&1 / *>&1)
# into error records; git prints "Switched to branch ..." on stderr and claude --verbose logs
# to stderr, so EAP=Stop would falsely terminate the script. Failures are handled explicitly
# via $LASTEXITCODE gates below.
$ErrorActionPreference = "Continue"

# ---------------- config ----------------
$Repo         = "E:\tradingsoftware\hummingbot"
$PromptFile   = "HUMMINGBOT_CONNECTOR_STRATEGY_FIXES_V1_claude_code_prompt.md"
$Model        = "claude-fable-5"     # Fable 5. Change if your CLI expects a different alias.

# Per-phase reasoning effort passed as `--effort <level>` (valid: low, medium, high, xhigh, max).
# Values map the EFFORT labels declared in each phase of the prompt (MEDIUM-HIGH rounded up).
# Bump P10/P11 to 'xhigh' for extra depth on the riskiest edits (classic MM Cython + ARB-1).
$PhaseEffort  = @{
    1='high'; 2='high'; 3='high'; 4='medium'; 5='medium'; 6='low'
    7='medium'; 8='high'; 9='high'; 10='high'; 11='high'; 12='low'
}

# Per-phase model. Mechanical phases run on Sonnet 4.6 (a fraction of the usage-limit weight);
# every money-path phase stays on Fable. P6 = logging template edit; P12 = run tests/merge/report.
$SonnetModel = "claude-sonnet-4-6"
$PhaseModel  = @{
    1=$Model; 2=$Model; 3=$Model; 4=$Model; 5=$Model; 6=$SonnetModel
    7=$Model; 8=$Model; 9=$Model; 10=$Model; 11=$Model; 12=$SonnetModel
}

$PushToOrigin         = $true    # $true = Phase 12 pushes dev+nonkyc (hands-off; push only updates
                                 # the remote, nothing auto-deploys). $false = stop at dev.
$MaxAttemptsPerPhase  = 5        # total attempts per phase (first run + retries/resumes)
$ProbeIntervalMinutes = 20       # how often to probe for restored usage limits
$MaxLimitWaitHours    = 12       # give up waiting for limits after this long (writes halt file)
$RetryPauseSeconds    = 120      # pause before a non-limit retry
# If a phase's model is CAPPED but this fallback model IS available (different limit pool), run
# that phase on the fallback instead of parking until the cap resets. "" disables it (park then
# halt after $MaxLimitWaitHours -- the original behavior). Fable draws its own 50%-of-weekly cap
# plus the shared pool; Opus is a separate pool, so it can keep the batch moving when only Fable
# is capped. TRADEOFF: affected phases then run on the fallback's cost/behavior, not Fable, and
# Opus has its own weekly cap. Only fires when the phase's model != the fallback.
$FableCapFallback     = "claude-opus-4-8"
# ----------------------------------------

Set-Location $Repo
$LogDir    = Join-Path $Repo "batch_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$runStamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$masterLog = Join-Path $LogDir "batch_$runStamp.log"

# Signatures (case-insensitive) that mean "retry later", not "code failure".
$LimitPatterns = @('usage limit', 'limit will reset', 'rate limit', 'credit balance',
                   'insufficient credit', 'quota', 'overloaded', 'try again later',
                   'HTTP 429', 'off-peak')

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $masterLog -Value $line
}

function Halted($n) {
    return (Test-Path (Join-Path $Repo ("BATCH_HALT_phase_{0}.md" -f $n)))
}

function Test-LimitSignature($logPath) {
    if (-not (Test-Path $logPath)) { return $false }
    foreach ($p in $LimitPatterns) {
        if (Select-String -Path $logPath -Pattern ([regex]::Escape($p)) -Quiet) { return $true }
    }
    return $false
}

function Test-NoConversation($logPath) {
    if (-not (Test-Path $logPath)) { return $false }
    return (Select-String -Path $logPath -Pattern 'No conversation found' -Quiet)
}

# Run claude with an argument array; tee output to $logPath; return the exit code.
function Invoke-Claude($argList, $logPath) {
    & claude @argList *>&1 | Tee-Object -FilePath $logPath | Out-Null
    return $LASTEXITCODE
}

# Cheap availability probe (~1 tiny call) at the MODEL the phase will use (limit pools can be
# model-weighted, so a cheap-model probe must not green-light an expensive-model phase).
# Deliberately WITHOUT --dangerously-skip-permissions so an unaccepted flag can never
# make the probe false-negative.
function Test-ClaudeReady($model) {
    $log = Join-Path $LogDir ("probe_{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    $code = Invoke-Claude @('-p', 'Reply with exactly: OK', '--model', $model, '--effort', 'low') $log
    return ($code -eq 0)
}

# Decide which model to run THIS attempt on. Prefers the phase's own model; if that is capped
# but $FableCapFallback is configured and available, returns the fallback so the batch keeps
# moving instead of parking. Returns $null if nothing usable within the wait budget.
function Resolve-UsableModel($model) {
    $deadline = (Get-Date).AddHours($MaxLimitWaitHours)
    while ($true) {
        if (Test-ClaudeReady $model) { return $model }
        if ($FableCapFallback -ne "" -and $FableCapFallback -ne $model -and (Test-ClaudeReady $FableCapFallback)) {
            Log ("    {0} is capped but fallback {1} is available; running this attempt on {1}." -f $model, $FableCapFallback)
            return $FableCapFallback
        }
        if ((Get-Date) -gt $deadline) { return $null }
        Log ("Claude unavailable (probably usage limit); no fallback usable. Probing again in {0} min (until {1})." -f `
             $ProbeIntervalMinutes, $deadline.ToString("HH:mm"))
        Start-Sleep -Seconds ($ProbeIntervalMinutes * 60)
    }
}

# The retry engine. Returns 'OK', 'HALT', or 'FAILED'.
#   $n       phase number       $taskText  full first-attempt prompt
#   $effort  --effort level     $model     --model for this phase
#   $verify  scriptblock returning $true when the phase is complete
function Run-Phase($n, $taskText, $effort, $model, $verify) {
    $sid = [guid]::NewGuid().ToString()
    $sessionStarted = $false

    $resumeText = "AUTOMATED BATCH RESUME - Phase $n. Your previous session was interrupted (usage limit or crash). Continue exactly where you left off and complete Phase $n of $PromptFile per its rules, including the 'Batch execution mode' section. Finish implementation, run COMPREHENSIVE TESTING, and end with the phase's required merge. If prior partial work is unusable, reset from dev and redo the phase. If testing cannot go green after the 5-attempt protocol, write BATCH_HALT_phase_$n.md and stop."
    $retryPrefix = "RETRY NOTE: a previous interrupted attempt may have left branch fix/csf-p$n-* and/or uncommitted changes in the working tree. Inspect first; either continue that work or reset the branch from dev, then proceed. "

    for ($a = 1; $a -le $MaxAttemptsPerPhase; $a++) {
        $useModel = Resolve-UsableModel $model
        if ($null -eq $useModel) {
            Log ("HALT: waited {0}h for usage limits on phase {1}; still unavailable (no fallback usable)." -f $MaxLimitWaitHours, $n)
            Set-Content (Join-Path $Repo "BATCH_HALT_phase_$n.md") `
                "Phase ${n}: claude unavailable for over $MaxLimitWaitHours hours (usage limit not restored). Re-run the batch to continue; completed phases are already merged to dev."
            return 'HALT'
        }

        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $log = Join-Path $LogDir ("phase_{0:D2}_attempt{1}_{2}.log" -f $n, $a, $stamp)

        if (-not $sessionStarted) {
            $text = $taskText
            if ($a -gt 1) { $text = $retryPrefix + $taskText }
            Log ("=== Phase {0} attempt {1}/{2} FRESH (model={3}, effort={4}, session={5}) -> {6}" -f `
                 $n, $a, $MaxAttemptsPerPhase, $useModel, $effort, $sid.Substring(0,8), $log)
            $code = Invoke-Claude @('-p', $text, '--model', $useModel, '--effort', $effort,
                                    '--session-id', $sid, '--dangerously-skip-permissions',
                                    '--verbose') $log
            $sessionStarted = $true
        }
        else {
            Log ("=== Phase {0} attempt {1}/{2} RESUME (model={3}, effort={4}, session={5}) -> {6}" -f `
                 $n, $a, $MaxAttemptsPerPhase, $useModel, $effort, $sid.Substring(0,8), $log)
            $code = Invoke-Claude @('-p', $resumeText, '--resume', $sid, '--model', $useModel,
                                    '--effort', $effort, '--dangerously-skip-permissions',
                                    '--verbose') $log
            if ($code -ne 0 -and (Test-NoConversation $log)) {
                # Session evaporated (e.g. failed before creation). Restart fresh within this attempt.
                $sid = [guid]::NewGuid().ToString()
                Log ("    resume impossible (no conversation); restarting FRESH (new session={0})" -f $sid.Substring(0,8))
                $log = Join-Path $LogDir ("phase_{0:D2}_attempt{1}b_{2}.log" -f $n, $a, $stamp)
                $code = Invoke-Claude @('-p', ($retryPrefix + $taskText), '--model', $useModel,
                                        '--effort', $effort, '--session-id', $sid,
                                        '--dangerously-skip-permissions', '--verbose') $log
            }
        }
        Log ("=== Phase {0} attempt {1} returned (CLI exit {2})" -f $n, $a, $code)

        if (Halted $n)   { return 'HALT' }
        if (& $verify)   { return 'OK' }

        if (Test-LimitSignature $log) {
            Log ("    attempt {0} hit a usage/rate limit; will wait for availability, then RESUME session." -f $a)
            # Wait happens at the top of the next attempt via Resolve-UsableModel.
        }
        else {
            Log ("    attempt {0} failed without a limit signature (exit {1}); pausing {2}s before retry." -f `
                 $a, $code, $RetryPauseSeconds)
            Start-Sleep -Seconds $RetryPauseSeconds
        }
    }

    Log ("HALT: phase {0} exhausted {1} attempts." -f $n, $MaxAttemptsPerPhase)
    Set-Content (Join-Path $Repo "BATCH_HALT_phase_$n.md") `
        "Phase $n did not complete after $MaxAttemptsPerPhase attempts (no merge, no agent halt file). See batch_logs/phase_$('{0:D2}' -f $n)_attempt*.log."
    return 'FAILED'
}

# ---- preflight ----
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Log "ABORT: 'claude' not on PATH."; exit 1 }
if (-not (Test-Path (Join-Path $Repo $PromptFile)))          { Log "ABORT: prompt file missing."; exit 1 }

# ---- smoke test mode (runs BEFORE the hard git gates; reports git state as warnings) ----
if ($SmokeTest) {
    Log "SMOKE TEST: verifying the exact production invocation path (auth, headless, permissions flag, model, effort)..."
    $log = Join-Path $LogDir "smoketest_$runStamp.log"
    $code = Invoke-Claude @('-p', 'Reply with exactly: OK', '--model', $Model, '--effort', 'low',
                            '--dangerously-skip-permissions') $log
    if ($code -eq 0) {
        Log "SMOKE TEST PASSED: claude headless invocation works with the production flags."
    } else {
        Log ("SMOKE TEST FAILED (exit {0}). Log tail:" -f $code)
        Get-Content $log -Tail 15 | ForEach-Object { Log ("    " + $_) }
        Log "Most likely causes: --dangerously-skip-permissions never accepted (run once interactively: claude --dangerously-skip-permissions), auth expired (run: claude login), or usage limit active."
    }
    $smokeDirty = (& git status --porcelain --untracked-files=no)
    if ($smokeDirty) { Log "WARNING: tracked files are modified; the real run will ABORT until committed/stashed:"; $smokeDirty | ForEach-Object { Log "    $_" } }
    $smokeHalts = Get-ChildItem -Path $Repo -Filter "BATCH_HALT_phase_*.md" -ErrorAction SilentlyContinue
    if ($smokeHalts) { Log "WARNING: stale BATCH_HALT files present; the real run will ABORT until deleted:"; $smokeHalts | ForEach-Object { Log ("    " + $_.Name) } }
    exit $code
}

$staleHalts = Get-ChildItem -Path $Repo -Filter "BATCH_HALT_phase_*.md" -ErrorAction SilentlyContinue
if ($staleHalts) {
    Log "ABORT: stale halt file(s) from a previous run exist. Read them, then delete to proceed:"
    $staleHalts | ForEach-Object { Log ("    " + $_.Name) }
    exit 1
}

# Refuse a dirty working tree: modified TRACKED files would ride into every phase branch.
# (Untracked files -- the prompt, findings docs, this script, batch_logs -- are fine.)
$dirty = (& git status --porcelain --untracked-files=no)
if ($dirty) {
    Log "ABORT: working tree has modified tracked files. Commit or stash them first, then rerun:"
    $dirty | ForEach-Object { Log "    $_" }
    exit 1
}

& git check-ignore -q batch_logs 2>$null
if ($LASTEXITCODE -ne 0) {
    Log "NOTE: consider adding 'batch_logs/', 'BATCH_HALT_*.md' and 'REPORT_csf_v1.md' to .gitignore so phase commits stay clean."
}

Log ("Batch start. Model={0}  PushToOrigin={1}  MaxAttempts={2}  repo={3}" -f $Model, $PushToOrigin, $MaxAttemptsPerPhase, $Repo)

# ---- one-time: sync dev from nonkyc (prompt's first binding action) ----
Log "Syncing dev from nonkyc (fast-forward)..."
& git checkout dev 2>&1 | Out-Null
& git merge --ff-only nonkyc 2>&1 | Tee-Object -FilePath (Join-Path $LogDir "devsync_$runStamp.log") | Out-Null
if ($LASTEXITCODE -ne 0) { Log "ABORT: dev sync is not a fast-forward. Resolve manually."; exit 1 }

# ---- phases 1..11: implement + test + merge to dev ----
for ($n = 1; $n -le 11; $n++) {
    $devBefore = (& git rev-parse dev) | Select-Object -First 1
    $task = @"
AUTOMATED BATCH - single phase. Read $PromptFile in the repo root and execute ONLY Phase $n, following every binding rule (branch off dev, implement, COMPREHENSIVE TESTING, the 5-attempt ultrathink failure protocol, money-safety rules, MEXC strictly off-limits, no live API calls, no ladder-YAML retuning, no orphan-order cancellation). See the 'Batch execution mode' section of that document. The phase MUST end with its fix/csf-p$n-* branch merged --no-ff into dev. Do NOT push to origin. If comprehensive testing cannot be made green after 5 attempts, do NOT merge - write BATCH_HALT_phase_$n.md in the repo root with a dense failure summary, then stop.
"@
    $verify = { ((& git rev-parse dev) | Select-Object -First 1) -ne $devBefore }.GetNewClosure()

    $result = Run-Phase $n $task $PhaseEffort[$n] $PhaseModel[$n] $verify
    if ($result -ne 'OK') { Log ("Batch stopped at phase {0} ({1})." -f $n, $result); exit 2 }
    Log ("=== Phase {0} merged to dev ({1})." -f $n, ((& git rev-parse dev) | Select-Object -First 1))
}

# ---- phase 12: finalization (dev -> nonkyc, optional push) ----
if ($PushToOrigin) { $pushClause = "then push dev and nonkyc to origin" }
else               { $pushClause = "do NOT push to origin (leave for manual review in the morning)" }
$task12 = @"
AUTOMATED BATCH - finalization (Phase 12). Read $PromptFile. Run full COMPREHENSIVE TESTING on dev plus the remote_iface suite. If green, merge dev into nonkyc and $pushClause. Write the final dense per-phase report to REPORT_csf_v1.md in the repo root (branches, findings fixed, files changed, tests added, suite counts, DEFERRED/NOT-A-BUG/SKIP lists, and the note that Docker images must be rebuilt for fixes to reach the pods). If testing fails, write BATCH_HALT_phase_12.md and stop WITHOUT merging to nonkyc.
"@
$verify12 = { & git merge-base --is-ancestor dev nonkyc 2>$null; $LASTEXITCODE -eq 0 }

$result12 = Run-Phase 12 $task12 $PhaseEffort[12] $PhaseModel[12] $verify12
if ($result12 -ne 'OK') { Log ("Batch stopped at phase 12 ({0})." -f $result12); exit 2 }
if (-not (Test-Path (Join-Path $Repo "REPORT_csf_v1.md"))) { Log "WARNING: REPORT_csf_v1.md missing despite successful finalization." }

Log "=== BATCH COMPLETE. Review batch_logs\, REPORT_csf_v1.md, and any BATCH_HALT_*.md."
exit 0
