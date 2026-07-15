# Nightly Batch CC Meta-Prompt (reusable across projects)

PASTE THIS at the start of a "create the CC prompt" session in any project. It instructs you (Claude Code)
to produce TWO artifacts that run together, fully unattended overnight:
  A. the CC prompt  ->  `{project}_{name}_claude_code_prompt.md`  (repo root)
  B. its runner     ->  `run_{project}_{name}_batch.ps1`          (repo root)

The runner launches the CC prompt one phase at a time, each in a fresh `claude -p` context (a real
"/clear" between phases the agent cannot do itself), survives usage-limit interruptions by resuming the
same session, and stops safely on failure. This template is self-contained — do not assume any prior
conversation or project memory.

---

## INSTRUCTION

Create a comprehensive CC prompt that implements the work items / fixes identified in [PART 1 / the named
findings doc — confirm which]. Break it into logical phases. THEN create the matching PowerShell runner.
Keep both dense — no fluff. Ask the clarifying questions below BEFORE generating either artifact.

## RESOLVE BEFORE GENERATING (ask the user anything not already answered)

1. **Source of work**: where are the items to implement (findings doc path / this session's review / a spec)?
2. **Comprehensive test command(s)** — EXACT, copy-pasteable, including the interpreter (e.g.
   `C:/anaconda3/envs/env/python.exe -m pytest <paths> -q --tb=short`, or `npm test`, `cargo test`,
   `make test`). The runner is worthless with a wrong command. Note any suites to EXCLUDE (live/network).
3. **Build/compile step** needed before tests? (Cython `build_ext --inplace`, `tsc`, `cargo build`, etc.)
4. **Git topology**: does `dev` exist? base branch to create it from if not (`main`/`master`)? For fork
   workflows: a branch to fast-forward `dev` from before starting, and the integration branch `dev` merges
   into at finalization. Push to origin overnight (Y/N)?
5. **Model**: primary model for money/logic phases (e.g. `claude-fable-5`), and WHICH phases are mechanical
   enough for a cheaper model (`claude-sonnet-4-6`) to cut usage.
6. **Absolute repo path** (for the runner config).
7. **Domain safety invariants / do-not-touch list** (money/data-loss/security rules that override
   everything; files or dirs that are strictly off-limits).
8. **Verify CC CLI capabilities against the installed version** — run `claude --help` and confirm the flags
   in the Appendix still exist / levels are current. Do NOT trust this template or docs over the binary.

---

## ARTIFACT A — the CC prompt (`{project}_{name}_claude_code_prompt.md`)

Follow these rules (superset of the standard phase rules + batch additions):

**Phase rules**
1. All phases in one CC prompt.
2. Each phase declares an **EFFORT** level (map to `--effort`: low/medium/high/xhigh/max). Default each phase
   to the LOWEST effort that fits; reserve high+ for genuinely hard phases. If any phase needs above `high`,
   CALL IT OUT explicitly to the user before they run.
3. Each phase runs on its own git branch off `dev` (naming: `fix/<slug>-p{N}-<phase-slug>`).
4. If `dev` doesn't exist, create it from `main` (fallback `master`).
5. Each phase creates/updates tests for its changes unless there's a stated good reason.
6. Comprehensive testing runs at the END of every phase.
7. On test failure: `ultrathink`, up to 5 fix attempts, re-running comprehensive testing each attempt.
8. After 5 failed attempts: stop and notify (in batch mode: write the halt file — see below).
9. On green: merge the branch back to `dev`, no approval prompt, continue.

**Model tiering (per phase — the biggest usage lever after token discipline)**
Each phase ALSO declares a MODEL TIER = the lowest-cost model whose capability clears the phase's ACTUAL
difficulty. Usage-pool weight, heaviest -> lightest: **Fable 5 (~2x) > Opus 4.8 (~1x, still elite) >
Sonnet 4.6 (light) > Haiku 4.5 (lightest)**. Assign:
- **Fable 5** — ONLY genuinely open-ended, highest-consequence reasoning (novel algorithm/design, subtle
  concurrency or financial-correctness, the single riskiest 1-2 phases). Not the default.
- **Opus 4.8** — money/logic-critical work that is HEAVILY SPECIFIED (exact algorithms, fixtures,
  fail-closed rules) so it is implementation-against-spec, not open discovery. This is the DEFAULT for
  "important but well-defined" phases: ~half Fable's pool burn at negligible quality risk.
- **Sonnet 4.6** — mechanical edits, config/template changes, run-tests + merge + report.
- **Haiku 4.5** — trivial/rote phases only (optional).
KEY PRINCIPLE: shift a phase to a cheaper tier NOT by instructing a model to "write at a higher model's
level" (impossible — capability is fixed by weights, and that instruction backfires into confident-but-wrong
output), but by SPECIFYING THE PHASE HARDER — move the reasoning into the prompt (algorithms, fixtures,
fail-closed defaults) until a lower tier executes it safely. The more precise the spec, the lower the tier
that clears it. Reserve Fable only for what genuinely cannot be specified down. The declared tiers MUST
match the runner's `$PhaseModel` map (Artifact B).

**General**
- Use the internet as needed. Lowest-effort prompts. Dense, no fluff.
- Filename: `{project}_{name}_claude_code_prompt.md` in the repo root.
- Every code change is covered by tests; comprehensive testing after each change; 5 fix attempts before
  moving on.

**Batch additions (REQUIRED — these make it run unattended)**
- Add a **"Batch execution mode"** section near the top stating: each phase is a separate `claude -p`
  invocation with a FRESH context; the agent is told ONE phase to run and does ONLY that; it must re-read
  the prompt (and any findings doc) from disk since prior conversation is gone; implementation phases end
  by merging their branch to `dev` and do NOT push; the final phase is finalization (test `dev`, optionally
  merge `dev`->integration branch, optionally push, write `REPORT_<name>.md`); on unfixable failure the
  agent writes `BATCH_HALT_phase_{N}.md` (dense summary) and stops INSTEAD of "pausing to notify".
- Add a **"Token discipline"** rule to the testing section: never stream full test/build output into the
  conversation — redirect each run to a log file and read back only `tail -N` + a `grep` of failures;
  pull individual tracebacks with targeted greps. (Test-output reading is the dominant token sink.)
- In the failure protocol, iterate cheaply INSIDE an attempt with last-failed-only runs (`pytest --lf` or
  explicit node ids); an attempt is judged only by then re-running the FULL suite. Targeted re-runs are
  free inner loops, not attempts.
- Number the phases so the LAST phase is finalization. Make phase count, branch prefix, and effort labels
  match what you put in the runner's config block (Artifact B).
- Encode the domain safety invariants and do-not-touch list as absolute prohibitions.

## ARTIFACT B — the runner (`run_{project}_{name}_batch.ps1`)

Reproduce the canonical script below VERBATIM, changing only the marked config block (and the two git
topology lines if the project isn't a simple dev->main flow). It already encodes every hard-won fix:

- Fresh `claude -p` per phase (= per-phase context reset); NO `--continue`/`--resume` for fresh starts.
- **Usage-limit resilience**: each phase gets a pre-assigned `--session-id`; on interruption the runner
  probes availability (tiny call at the PHASE's model, every N min, capped) then `--resume`s the SAME
  session so the agent continues mid-phase; falls back to fresh on "No conversation found".
- Per-phase `--effort` AND `--model` via the tiered `$PhaseModel` map (Fable only for the riskiest phases,
  Opus 4.8 as the default for well-specified money/logic phases at ~half the pool burn, Sonnet for
  mechanical) — see "Model tiering" in Artifact A.
- Halt on: agent halt file, `dev` not advancing, or attempt cap. Preflight guards: `claude` on PATH,
  prompt file present, stale halt files, dirty tracked tree (would contaminate branches).
- `-SmokeTest` switch verifies the exact production invocation (auth + `--dangerously-skip-permissions`
  acceptance + model/effort) with one tiny call before you commit to the overnight run.

**MANDATORY after generating the concrete runner:**
- It MUST be pure ASCII. Windows PowerShell 5.1 reads .ps1 as ANSI and mangles em-dashes/Unicode into a
  byte that reads as `"`, breaking parsing. Replace all Unicode with ASCII.
- Parse-check it and confirm zero non-ASCII bytes:
  ```
  powershell -NoProfile -Command "$e=$null;[System.Management.Automation.Language.Parser]::ParseFile('<path>',[ref]$null,[ref]$e)|Out-Null; if($e){$e|%{'ERR: '+$_.Message}}else{'syntax OK'}; ('non-ascii: '+(([IO.File]::ReadAllBytes('<path>')|?{$_-gt127}).Count))"
  ```
- Then run `-SmokeTest` and confirm it passes before telling the user it's ready.

### Canonical runner (adapt the config block; keep everything else)

```powershell
<#
  run_<project>_<name>_batch.ps1 -- unattended phased runner. Each phase = one `claude -p` (fresh context).
  Survives usage limits via --session-id/--resume. Halts safely on failure. PURE ASCII (PS 5.1 ANSI rule).
  Launch:  powershell -ExecutionPolicy Bypass -File .\run_<project>_<name>_batch.ps1
  Smoke:   powershell -ExecutionPolicy Bypass -File .\run_<project>_<name>_batch.ps1 -SmokeTest
#>
param([switch]$SmokeTest)

# EAP MUST be Continue: PS 5.1 turns redirected native stderr (2>&1/*>&1) into error records; git prints
# "Switched to branch" and claude --verbose logs to stderr, so Stop would falsely kill the script.
$ErrorActionPreference = "Continue"

# ---------------- config (EDIT PER PROJECT) ----------------
$Repo          = "<ABSOLUTE REPO PATH>"
$PromptFile    = "<project>_<name>_claude_code_prompt.md"
$NumImplPhases = 11                       # implementation phases; finalization is phase ($NumImplPhases+1)
$BranchPrefix  = "fix/<slug>-p"           # phase N branch the agent creates = "${BranchPrefix}${N}-*"
$BaseBranch    = "dev"                    # phase branches fork from here
$BaseCreateFrom= "main"                   # create $BaseBranch from here if missing (falls back to master)
$SyncBaseFrom  = ""                       # ff-only sync $BaseBranch from this before start; "" = skip
$IntegrationBranch = "main"               # finalization merges $BaseBranch into this; "" = leave on base
$ReportFile    = "REPORT_<name>.md"       # final report the finalization phase writes
# MODEL TIERS (map each phase to the LOWEST tier that clears its difficulty; see "Model tiering" above).
$Model         = "claude-fable-5"         # TIER 1: peak reasoning; reserve for the riskiest 1-2 phases
$MidModel      = "claude-opus-4-8"        # TIER 2: elite, ~half Fable's pool burn; DEFAULT for well-
                                          #         specified money/logic phases
$LightModel    = "claude-sonnet-4-6"      # TIER 3: mechanical edits, tests/merge/report
# TIER 4 (optional, trivial phases): "claude-haiku-4-5-20251001"
$PhaseEffort   = @{ 1='high'; 2='high'; 3='medium'; 12='low' }        # fill ALL phases 1..($NumImplPhases+1)
$PhaseModel    = @{ 1=$Model; 2=$MidModel; 3=$MidModel; 12=$LightModel } # fill ALL; mirror the declared tiers
$PushToOrigin  = $false
$MaxAttemptsPerPhase  = 5
$ProbeIntervalMinutes = 20
$MaxLimitWaitHours    = 12
$RetryPauseSeconds    = 120
$FableCapFallback = "claude-opus-4-8"     # if a phase's model is CAPPED but this (different
                                          # limit pool) IS available, run that phase on it instead
                                          # of parking. "" disables (park-then-halt). Fires only
                                          # when phase model != fallback. See Appendix note.
# -----------------------------------------------------------

Set-Location $Repo
$LogDir    = Join-Path $Repo "batch_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$runStamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$masterLog = Join-Path $LogDir "batch_$runStamp.log"
$FinalPhase = $NumImplPhases + 1

$LimitPatterns = @('usage limit','limit will reset','rate limit','credit balance','insufficient credit',
                   'quota','overloaded','try again later','HTTP 429','off-peak')

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $masterLog -Value $line
}
function Halted($n) { return (Test-Path (Join-Path $Repo ("BATCH_HALT_phase_{0}.md" -f $n))) }
function Test-LimitSignature($p) {
    if (-not (Test-Path $p)) { return $false }
    foreach ($x in $LimitPatterns) { if (Select-String -Path $p -Pattern ([regex]::Escape($x)) -Quiet) { return $true } }
    return $false
}
function Test-NoConversation($p) { if (-not (Test-Path $p)) { return $false }; return (Select-String -Path $p -Pattern 'No conversation found' -Quiet) }
function Invoke-Claude($argList, $logPath) { & claude @argList *>&1 | Tee-Object -FilePath $logPath | Out-Null; return $LASTEXITCODE }
function Test-ClaudeReady($model) {
    $log = Join-Path $LogDir ("probe_{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    return ((Invoke-Claude @('-p','Reply with exactly: OK','--model',$model,'--effort','low') $log) -eq 0)
}
function Resolve-UsableModel($model) {
    $deadline = (Get-Date).AddHours($MaxLimitWaitHours)
    while ($true) {
        if (Test-ClaudeReady $model) { return $model }
        if ($FableCapFallback -ne "" -and $FableCapFallback -ne $model -and (Test-ClaudeReady $FableCapFallback)) {
            Log ("    {0} is capped but fallback {1} is available; running this attempt on {1}." -f $model, $FableCapFallback); return $FableCapFallback
        }
        if ((Get-Date) -gt $deadline) { return $null }
        Log ("Claude unavailable (probably usage limit); no fallback usable. Probing again in {0} min (until {1})." -f $ProbeIntervalMinutes, $deadline.ToString("HH:mm"))
        Start-Sleep -Seconds ($ProbeIntervalMinutes * 60)
    }
}
# Returns 'OK' | 'HALT' | 'FAILED'
function Run-Phase($n, $taskText, $effort, $model, $verify) {
    $sid = [guid]::NewGuid().ToString(); $sessionStarted = $false
    $resumeText  = "AUTOMATED BATCH RESUME - Phase $n. Your previous session was interrupted (usage limit or crash). Continue exactly where you left off and complete Phase $n of $PromptFile per its rules and its 'Batch execution mode' section: finish implementation, run comprehensive testing, end with the required merge. If prior partial work is unusable, reset from $BaseBranch and redo. If tests cannot go green after the 5-attempt protocol, write BATCH_HALT_phase_$n.md and stop."
    $retryPrefix = "RETRY NOTE: a previous interrupted attempt may have left branch ${BranchPrefix}${n}-* and/or uncommitted changes. Inspect first; continue it or reset the branch from $BaseBranch, then proceed. "
    for ($a = 1; $a -le $MaxAttemptsPerPhase; $a++) {
        $useModel = Resolve-UsableModel $model
        if ($null -eq $useModel) {
            Log ("HALT: waited {0}h for usage limits on phase {1}; still unavailable (no fallback)." -f $MaxLimitWaitHours, $n)
            Set-Content (Join-Path $Repo "BATCH_HALT_phase_$n.md") "Phase ${n}: claude unavailable for over $MaxLimitWaitHours hours. Re-run the batch to continue; completed phases are already merged to $BaseBranch."
            return 'HALT'
        }
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $log = Join-Path $LogDir ("phase_{0:D2}_attempt{1}_{2}.log" -f $n, $a, $stamp)
        if (-not $sessionStarted) {
            $text = $taskText; if ($a -gt 1) { $text = $retryPrefix + $taskText }
            Log ("=== Phase {0} attempt {1}/{2} FRESH (model={3}, effort={4}, session={5})" -f $n,$a,$MaxAttemptsPerPhase,$useModel,$effort,$sid.Substring(0,8))
            $code = Invoke-Claude @('-p',$text,'--model',$useModel,'--effort',$effort,'--session-id',$sid,'--dangerously-skip-permissions','--verbose') $log
            $sessionStarted = $true
        } else {
            Log ("=== Phase {0} attempt {1}/{2} RESUME (model={3}, effort={4}, session={5})" -f $n,$a,$MaxAttemptsPerPhase,$useModel,$effort,$sid.Substring(0,8))
            $code = Invoke-Claude @('-p',$resumeText,'--resume',$sid,'--model',$useModel,'--effort',$effort,'--dangerously-skip-permissions','--verbose') $log
            if ($code -ne 0 -and (Test-NoConversation $log)) {
                $sid = [guid]::NewGuid().ToString()
                Log ("    resume impossible (no conversation); restarting FRESH (session={0})" -f $sid.Substring(0,8))
                $log = Join-Path $LogDir ("phase_{0:D2}_attempt{1}b_{2}.log" -f $n,$a,$stamp)
                $code = Invoke-Claude @('-p',($retryPrefix+$taskText),'--model',$useModel,'--effort',$effort,'--session-id',$sid,'--dangerously-skip-permissions','--verbose') $log
            }
        }
        Log ("=== Phase {0} attempt {1} returned (CLI exit {2})" -f $n,$a,$code)
        if (Halted $n) { return 'HALT' }
        if (& $verify) { return 'OK' }
        if (Test-LimitSignature $log) { Log ("    attempt {0} hit a usage/rate limit; will wait then RESUME." -f $a) }
        else { Log ("    attempt {0} failed (exit {1}); pausing {2}s before retry." -f $a,$code,$RetryPauseSeconds); Start-Sleep -Seconds $RetryPauseSeconds }
    }
    Log ("HALT: phase {0} exhausted {1} attempts." -f $n,$MaxAttemptsPerPhase)
    Set-Content (Join-Path $Repo "BATCH_HALT_phase_$n.md") "Phase $n did not complete after $MaxAttemptsPerPhase attempts. See batch_logs phase_$('{0:D2}' -f $n)_attempt*.log."
    return 'FAILED'
}

# ---- preflight ----
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Log "ABORT: 'claude' not on PATH."; exit 1 }
if (-not (Test-Path (Join-Path $Repo $PromptFile)))          { Log "ABORT: prompt file missing."; exit 1 }
if ($SmokeTest) {
    Log "SMOKE TEST: verifying the production invocation path (auth, headless, permissions flag, model, effort)..."
    $log = Join-Path $LogDir "smoketest_$runStamp.log"
    $code = Invoke-Claude @('-p','Reply with exactly: OK','--model',$Model,'--effort','low','--dangerously-skip-permissions') $log
    if ($code -eq 0) { Log "SMOKE TEST PASSED." } else { Log ("SMOKE TEST FAILED (exit {0}). Log tail:" -f $code); Get-Content $log -Tail 15 | %{ Log ("    "+$_) }; Log "Likely: skip-permissions never accepted (run once interactive: claude --dangerously-skip-permissions), auth expired (claude login), or usage limit." }
    $sd = (& git status --porcelain --untracked-files=no); if ($sd) { Log "WARNING: modified tracked files; real run ABORTS until committed/stashed:"; $sd | %{ Log "    $_" } }
    exit $code
}
$staleHalts = Get-ChildItem -Path $Repo -Filter "BATCH_HALT_phase_*.md" -ErrorAction SilentlyContinue
if ($staleHalts) { Log "ABORT: stale halt file(s) exist. Read then delete to proceed:"; $staleHalts | %{ Log ("    "+$_.Name) }; exit 1 }
$dirty = (& git status --porcelain --untracked-files=no)
if ($dirty) { Log "ABORT: modified tracked files. Commit or stash first:"; $dirty | %{ Log "    $_" }; exit 1 }
Log ("Batch start. Model={0} Light={1} PushToOrigin={2} repo={3}" -f $Model,$LightModel,$PushToOrigin,$Repo)

# ---- ensure base branch exists; optional ff-only sync ----
& git rev-parse --verify $BaseBranch 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $from = $BaseCreateFrom
    & git rev-parse --verify $from 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { $from = "master" }
    Log ("Creating $BaseBranch from $from..."); & git checkout -b $BaseBranch $from 2>&1 | Out-Null
} else { & git checkout $BaseBranch 2>&1 | Out-Null }
if ($SyncBaseFrom -ne "") {
    Log ("Syncing $BaseBranch from $SyncBaseFrom (ff-only)...")
    & git merge --ff-only $SyncBaseFrom 2>&1 | Tee-Object -FilePath (Join-Path $LogDir "sync_$runStamp.log") | Out-Null
    if ($LASTEXITCODE -ne 0) { Log "ABORT: $BaseBranch sync from $SyncBaseFrom is not a fast-forward."; exit 1 }
}

# ---- implementation phases ----
for ($n = 1; $n -le $NumImplPhases; $n++) {
    $devBefore = (& git rev-parse $BaseBranch) | Select-Object -First 1
    $task = @"
AUTOMATED BATCH - single phase. Read $PromptFile in the repo root and execute ONLY Phase $n, following every binding rule and its 'Batch execution mode' section (branch off $BaseBranch, implement, comprehensive testing, the 5-attempt ultrathink protocol, all safety/prohibition rules). The phase MUST end with its ${BranchPrefix}${n}-* branch merged --no-ff into $BaseBranch. Do NOT push. If tests cannot go green after 5 attempts, do NOT merge - write BATCH_HALT_phase_$n.md with a dense summary, then stop.
"@
    $verify = { ((& git rev-parse $BaseBranch) | Select-Object -First 1) -ne $devBefore }.GetNewClosure()
    $r = Run-Phase $n $task $PhaseEffort[$n] $PhaseModel[$n] $verify
    if ($r -ne 'OK') { Log ("Batch stopped at phase {0} ({1})." -f $n,$r); exit 2 }
    Log ("=== Phase {0} merged to {1} ({2})." -f $n,$BaseBranch,((& git rev-parse $BaseBranch) | Select-Object -First 1))
}

# ---- finalization phase ----
if ($IntegrationBranch -ne "") {
    if ($PushToOrigin) { $intClause = "merge $BaseBranch into $IntegrationBranch, then push $BaseBranch and $IntegrationBranch to origin" }
    else               { $intClause = "merge $BaseBranch into $IntegrationBranch (do NOT push)" }
} else {
    if ($PushToOrigin) { $intClause = "push $BaseBranch to origin" } else { $intClause = "leave everything on $BaseBranch (no merge, no push)" }
}
$task12 = @"
AUTOMATED BATCH - finalization (Phase $FinalPhase). Read $PromptFile. Run full comprehensive testing on $BaseBranch. If green, $intClause. Write the final dense per-phase report to $ReportFile in the repo root. If testing fails, write BATCH_HALT_phase_$FinalPhase.md and stop WITHOUT the integration merge.
"@
if ($IntegrationBranch -ne "") { $verifyF = { & git merge-base --is-ancestor $BaseBranch $IntegrationBranch 2>$null; $LASTEXITCODE -eq 0 }.GetNewClosure() }
else                           { $verifyF = { Test-Path (Join-Path $Repo $ReportFile) }.GetNewClosure() }
$rF = Run-Phase $FinalPhase $task12 $PhaseEffort[$FinalPhase] $PhaseModel[$FinalPhase] $verifyF
if ($rF -ne 'OK') { Log ("Batch stopped at finalization ({0})." -f $rF); exit 2 }
Log "=== BATCH COMPLETE. Review batch_logs, REPORT_<name>.md, and any BATCH_HALT_*.md."
exit 0
```

---

## APPENDIX — Claude Code CLI facts this depends on (verify against `claude --help`; version-sensitive)

- `--effort <low|medium|high|xhigh|max>`: per-invocation reasoning effort; INVALID values fall back to
  default SILENTLY (exit 0) — spell exactly. Do NOT also set `CLAUDE_CODE_EFFORT_LEVEL` (env var outranks
  the flag). `ultrathink` in prompt text is a separate per-turn nudge, still honored.
- `--session-id <guid>`: pre-assign so an interrupted phase can be resumed. `--resume <guid>`: continue that
  session with context intact (fresh-start invocations omit it). "No conversation found" => session never
  materialized; restart fresh.
- `--dangerously-skip-permissions`: required for unattended; must have been accepted once interactively.
- `-p`/`--print`: headless; each invocation is a FRESH context (this is the per-phase "/clear").
- `--model <alias-or-id>`, `--max-budget-usd <n>` (optional per-invocation spend cap).
- Auto-compact is token-threshold, NOT phase-aware (fires mid-phase) and the agent cannot self-`/compact`
  or `/clear` — which is the whole reason the runner uses separate processes per phase.

## Usage limits & model fallback (verified 2026-07-14; plan/version-specific — re-verify)
- Limits are LAYERED on a Max plan: one shared pool (5-hour rolling + weekly) burned at
  per-model rates (Fable ~2x Opus), PLUS model-scoped caps — an Opus-only weekly cap, and (promo)
  a Fable cap at 50% of the weekly limit. So exhausting Fable does NOT block Opus; exhausting the
  shared 5h/weekly blocks everything.
- Headless `claude -p` on a usage-limit exhaustion EXITS NON-ZERO — it does NOT silently switch
  models. `--fallback-model` fires only on overload/unavailable (529), NEVER on usage/rate limits.
  The runner's own $FableCapFallback logic (probe the phase model; if capped, probe the fallback;
  use it if available) is what lets a Fable-cap-only situation continue on Opus instead of halting.
- REAL silent Fable->Opus vector, NOT limit-related: Fable runs behind safety classifiers and
  Claude Code by default ("/config -> Switch models when a message is flagged", ON) re-runs a
  flagged request on Opus 4.8 and STAYS on Opus for that session. It can trip on security/auth/
  crypto/HMAC/exchange code. So `--model` pinning is not absolute. To see what actually ran, use
  `-p --output-format json` (has model/modelUsage); to hard-pin, turn the toggle off (then a
  flagged request PAUSES instead — which in headless can fail the phase). Choose per project.
- COST CLIFF: Fable-on-subscription is a promo (as of this writing ending ~2026-07-19); after it,
  Fable draws usage credits ($/MTok), not plan limits. Unattended Fable batches at ~2x burn get
  expensive fast after the cliff. Confirm current status before scheduling nightly Fable runs.

## Usage-reduction defaults already baked in (user's biggest constraint)
- Token discipline (log-and-tail test output) — the largest sink is the model READING test output.
- `--lf` inner loops; full suite only as the attempt gate.
- Tiered per-phase models (Fable only for the riskiest phases; Opus 4.8 as the default elite tier at
  ~half Fable's pool burn; Sonnet for mechanical; Haiku for trivial). The deeper lever: specify a phase
  harder to drop it a tier — you cannot prompt a model up to a better model's capability. Probe
  availability at the phase's own model (limit pools are model-weighted). For fully off-subscription
  batches: set `ANTHROPIC_API_KEY` before launch (bills API credits; pair with `--max-budget-usd`).
