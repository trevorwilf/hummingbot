# hbtriage_engine — batch prompt (Run A: engine repo)

Repo: `E:/tradingsoftware/hummingbot` · Base branch: **`nonkyc`** · 6 implementation phases + finalization (phase 7).
Scope authority: `E:/claude/advesarial_reviews/reports/scope_triage.md` (**SOLE authority** — 14 IN SCOPE / 9 OUT OF SCOPE; where any other report disagrees, the triage wins).
Evidence (read-only detail): `REPORT_hb_predeploy.md` (synthesis), `CODEX_FINDINGS_*`, `CLAUDE_FINDINGS_*`, `CODEX_VS_CLAUDE_*`, `CLAUDE_VS_CODEX_*` in the same folder.
This run implements the ENGINE-repo share of the triage: CDX-002 (Batch 0, gates everything), the engine halves of CDX-008 and CDX-007, CDX-009, CDX-010, and the 5 doc corrections. The API-repo share runs separately (Run B) AFTER a human docker-rebuild gate.

## Batch execution mode

- Each step is a separate headless invocation with a FRESH context. You are told ONE step of ONE phase; do ONLY that.
- Re-read this prompt and `scope_triage.md` from disk at the start of every step — prior conversation is gone.
- Implementation phases commit on their own branch; they do NOT push. Merging happens only in step C (adjudication), `--no-ff` into `nonkyc`.
- The final phase (7) is finalization: test the base branch, write the report. No review step.
- On unfixable failure (after the 5-attempt protocol) your FINAL MESSAGE must begin `BATCH_HALT phase=<N> reason=` and you must stop. Do not write halt files yourself; the runner owns them. Do not "pause to notify".
- Runner-owned artifacts are NOT project files: `batch_logs/`, `batch_reviews/`, `BATCH_HALT_*.md`, this prompt, the runner `.ps1`, and `REPORT_hbtriage_engine.md` are gitignored and must NEVER be committed. Never use `git add -A` or `git add .` — stage exactly the files your phase changed.

## Absolute prohibitions (binding on every step)

1. **NO DOCKER, in any form** — no `docker`/`docker compose` `build|config|run|pull|exec|stop|rm|up|down|images|inspect` or any docker-adjacent command. This is a live trading system; the build scripts stop/remove running bot containers with no order cancellation. All docker work is the human's.
2. **Never run the build scripts** (`build_hummingbot_nonkyc.sh`, `Build_hummingbot_api_nonkyc.sh`) — not even "partially" or "to test arg parsing". They purge live containers. `bash -n` (syntax check only) is the ONLY permitted execution.
3. **Do NOT implement any OUT OF SCOPE finding**: CDX-003, CDX-011/CDX-M01, CDX-012, CDX-014/CLA-005, CLA-006, CDX-004, CLA-001, CLA-003, CLA-007. If a phase notices one, record it in your final message; never fix it. In particular: while editing the stack files, do NOT touch `${DEV_VAR:-true}` (CDX-003 is accepted risk).
4. **Do NOT apply the TODO's P1 fix `str(owner_id) != str(controller_id)`** — both review engines independently called it unsound; the triage prohibits it. It is a trap left in the doc.
5. **Do NOT touch `E:/tradingsoftware/dockerscripts/`** or any path outside `E:/tradingsoftware/hummingbot` (writes) and `E:/tradingsoftware/hummingbot-api` + `E:/claude/advesarial_reviews/reports/` (reads only).
6. **This phase's repo is `E:/tradingsoftware/hummingbot` ONLY.** You may READ the api repo for cross-repo verification; you may never write to it.
7. Never cancel, modify, or place an order. Never touch a running container. Never read or print secrets: `.env`, keys, tokens, connector credentials. `DOCKER_BOT_NETWORK_MODE` and `DEV_VAR` live-value reads are the human's.
8. **Fail closed on uncertainty.** Several in-scope fixes exist because code chose fail-open (CDX-007, CDX-008). Never weaken a fail-closed default to make a test pass. Existing `STOPPED` rows are UNVERIFIED (CDX-005) — never treat them as evidence of clean retirement.

## Shared cross-repo contracts (verbatim in both run prompts — implement EXACTLY this)

Two findings span both repos and cannot ship atomically. Each side must be independently fail-closed so whichever lands first is still safe.

### CONTRACT C1 — `state_file_name` path contract (CDX-007 / CLA-004)

- **ACCEPT:** unset/None; or a `str` whose stripped value is non-empty and, parsed as BOTH `PurePosixPath` and `PureWindowsPath`: `is_absolute()` is False, has no drive and no root/anchor, contains no `..` component, is not `.`, and its POSIX normalization remains a strict descendant of `data/` when joined (lexical check — no filesystem access needed at validation time).
- **REJECT (fail-closed):** absolute POSIX or Windows paths, drive letters, UNC paths, any `..` component, `.` — UNLESS an explicit opt-out boolean (`allow_absolute_state_file_name`, default False) is set, which permits ABSOLUTE paths only, never traversal.
- **Canonical value:** the stripped string. Empty-after-strip maps to unset/None (default behavior), which is not fail-open: None selects the default state file name.
- Engine half (this run, phase 3): enforce at controller-config validation in `range_inventory_ladder.py`, plus a runtime resolve-and-containment assertion where the path is used. API half (Run B): enforce the same accept/reject set in CFH planning and remove the absolute-skip success path.

### CONTRACT C2 — controller `id` contract (CDX-008 / CLA-002)

- **ACCEPT:** a `str` whose stripped length is >= 1. Canonical id = the stripped value; all identity derivations (ledger filename, `.owner` match) use the canonical id.
- **REJECT:** non-`str` (int, bool, None — Pydantic v2 already rejects these engine-side; preserve that), `""`, whitespace-only.
- Engine half (this run, phase 2): `ControllerConfigBase.id` gets `min_length=1` plus a strip validator that rejects empty-after-strip. API half (Run B): `resume_service.py` ABORTS the deploy (never `continue`s) on any staged range-ladder controller violating C2.
- **PROHIBITED on both sides:** the `str(owner_id) != str(controller_id)` comparison fix.

## Comprehensive testing (every phase; exact commands)

Run from the repo root (`E:/tradingsoftware/hummingbot`). The interpreter is `C:/anaconda3/envs/hummingbot/python.exe` (verified: resolves `hummingbot` to this repo tree, pydantic 2.12.5, PyYAML present).

```bash
mkdir -p batch_logs
C:/anaconda3/envs/hummingbot/python.exe -m pytest test/hummingbot/strategy_v2/controllers/ -q > batch_logs/<phase>_<step>_pytest.log 2>&1
if [ -d test/stack_files ]; then C:/anaconda3/envs/hummingbot/python.exe -m pytest test/stack_files/ -q >> batch_logs/<phase>_<step>_pytest.log 2>&1; fi
bash -n build_hummingbot_nonkyc.sh
bash -n Build_hummingbot_api_nonkyc.sh
C:/anaconda3/envs/hummingbot/python.exe -c "import yaml; yaml.safe_load(open('docker_files/hummingbot stack - vpn', encoding='utf-8')); yaml.safe_load(open('docker_files/hummingbot stack - no vpn', encoding='utf-8')); print('YAML OK')"
```

- **Baseline protocol:** immediately after creating your phase branch and BEFORE any edit, run the full block once into `batch_logs/<phase>_pre.log`. "Green" for this phase = no test that passed pre-change now fails, every test this phase added passes, and the `bash -n` + YAML checks pass. A pre-existing failure recorded in the pre-log is NOT your regression — do not "fix" it and never weaken anything to route around it.
- **Token discipline (mandatory):** never stream full test/build output into the conversation. Redirect to `batch_logs/` and read back only `tail -30` plus `grep -E "FAILED|ERROR|passed|failed"`; pull individual tracebacks with targeted `grep -B2 -A15`.
- **Failure protocol:** on failure, ultrathink; up to 5 fix attempts. Iterate cheaply inside an attempt with `pytest --lf` or explicit node ids; an attempt is judged ONLY by re-running the full block. After 5 failed attempts, stop: FINAL MESSAGE begins `BATCH_HALT phase=<N> reason=`.

## Cross-family review (how a phase actually lands)

Every implementation phase runs three fresh-context calls:
- **Step A — AUTHOR (Claude family, write):** implement the phase on `fix/hbtriage-p<N>-<slug>` off `nonkyc`, tests green, COMMIT, do not merge, do not push.
- **Step B — REVIEWER (GPT-5.6 sol, codex family, STRICTLY READ-ONLY):** interrogates the branch diff (inlined by the runner) using the Review schema and TEST INTEGRITY AUDIT below. An opposing-family engine WILL interrogate your diff — write code and tests expecting hostile reading. Reviewer: you may not modify anything; a git tripwire aborts the run if the tree moves.
- **Step C — AUTHOR adjudicates (write):** answer EVERY finding with the Adjudication schema below; REJECT only with file:line evidence; run every named mutation experiment; re-run the full suite green on unmutated code; merge `--no-ff` into `nonkyc`.

**Bias note (applies to adjudication and the final report):** the reviewer family (codex) AUTHORED most of these findings (`CDX-*`). It may defend its own finding rather than judge the fix on its merits. Adjudicate on evidence, not deference — and every REJECTED `CDX-*` finding must be flagged for the human in the final report.

## Review schema (step B — use verbatim)

The reviewer judges ONLY this phase's branch diff plus read-only context. Each finding is a block:
- `ID` — `CDX-R01`, `CDX-R02`, ... (codex reviewer prefix, zero-padded).
- `Title` — one line.
- `Category` — correctness | concurrency | security | data-loss | api-contract | logic | resource-leak | performance | config | error-handling | edge-case | test-theater | test-gap | spec-conformance | other.
- `Severity` — Critical | High | Medium | Low | Info. · `Confidence` — High | Medium | Low.
- `Location` — `file:line` (+ symbol).
- `Symptoms / observable behavior` — what an engineer would SEE. Concrete.
- `Root-cause analysis` — mechanism + trigger conditions.
- `Validation / reproduction steps` — exact steps: inputs/state, command or code path, expected-vs-actual, reproducible by the author without you.
- `Proposed resolution` — fix, trade-offs, regression risk.
- `Evidence` — minimal quoted line(s) with `file:line`.
Then two sections: `Spec conformance` (does the diff implement phase N as specified here, including the shared contracts C1/C2 where relevant?) and `Test adequacy` (verdict per added/changed test — see audit below).
Rules: default to skepticism but do NOT invent findings — an empty report is valid ("no findings above the bar"). Medium+ only, unless a Low is a genuine latent bug. Never pure style. Every claim checkable at a cited `file:line`. You may NOT modify anything.

## TEST INTEGRITY AUDIT (step B — mandatory, highest-value job)

The phase arrived with passing tests. **Passing tests are evidence the tests ran, not that the code works.** For EVERY added or changed test, answer: *"If I broke the exact behavior this test claims to verify, would THIS test fail?"* If no — or you cannot tell — file a `test-theater` finding and name the **exact single-line mutation** to the IMPLEMENTATION (`file:line` — flip a comparison, delete a guard, return a constant, skip a persist) that the test should catch. You are read-only: reason statically and hand the author a precise, executable experiment. "Break it somehow" is useless.

Hunt these patterns explicitly: asserts nothing (passes if nothing raises) · tests the mock, not the code (unit under test patched; asserts only on `mock.return_value`/`assert_called_with`) · over-mocked so no real path executes · cannot-fail assertions (`is not None` on always-object, `len>=0`, self-comparison) · vacuous input (empty fixture never exercises the changed branch) · golden-output change-detector (expected values captured by RUNNING the implementation, not derived from the SPEC — check every expected value against the spec) · assertion weaker than the spec · disabled in place (skip/xfail/early return/commented asserts/`try/except: pass`) · wrong subject · accidental pass (ordering/timing/shared state) · happy path only (spec defines error/fail-closed behavior; only success tested — that is `test-gap`).

**Known context for this codebase:** the hb_predeploy review found every existing CFH hook test in the API repo is mocked at the unit-under-test level (`tests/test_copyforward_e2e.py:176-182`, `tests/test_copyforward_hook.py:138-143` in `hummingbot-api`). That pattern is precisely what this audit exists to catch — do not let it into THIS repo's new tests. Severity follows the un-covered code: a vacuous test over money, data-loss, or fail-closed logic is High or Critical, never Low. In `Test adequacy`, give a verdict for EVERY added/changed test: PASS (would fail under the named mutation) or FAIL (test-theater), with the mutation stated.

## Adjudication schema (step C — answer every finding)

Per finding: `Ref` · `Decision` (exactly one):
- `ACCEPTED-FIXED` — you agree; fixed properly (not a band-aid), with covering test. State commit + change.
- `REJECTED` — evidence-based rationale with `file:line` PROVING the reviewer wrong. "I prefer my version" is not a rationale. Do not cave to confident tone; do not dismiss what you cannot refute.
- `DEFERRED-OUT-OF-SCOPE` — real, but in code this phase did not touch. Do NOT fix here; record for the final report.
Plus `Rationale`, `Change` (for fixes), `Mutation evidence`, `Test evidence`.
**Mutation evidence is REQUIRED for every `test-theater`/`test-gap` finding — run the experiment, never argue:** apply the reviewer's named single-line mutation, run that test, record the outcome, REVERT. Test FAILED under mutation → the test is real; you may REJECT quoting the failure line. Test PASSED under mutation → reviewer proven right; you may NOT reject; fix the test so it fails under that mutation, revert, confirm green. A mutation is temporary scaffolding: revert it, confirm `git diff` shows zero residue; the FULL suite must be green on UNMUTATED code before merging. Never commit a mutation.
End with `Summary counts` (accepted/rejected/deferred by severity) and `Merge statement` (full suite green, merged `--no-ff` into `nonkyc`).

---

# PHASES

Sequencing is binding: phase 1 (CDX-002) gates everything — until the bind-mount is gone, rebuilt hook code does not run in the container and every other fix is unobservable. Phases 2–6 are independent of each other but run in order.

## Phase 1 — CDX-002: remove the source bind-mount (Critical; Batch 0) — branch `fix/hbtriage-p1-bindmount-cutover`

**Files:** `docker_files/hummingbot stack - vpn` (patch machinery ~:180–341, mount :714), `docker_files/hummingbot stack - no vpn` (patch machinery ~:94–230, mount :459). Filenames contain spaces — quote them.

**Step A — implement:**
1. **Fail-closed equivalence gate (do this FIRST):** read the `api-init` docker_service patch machinery in BOTH stack files. Enumerate EVERY behavior the embedded `PATCH_SCRIPT` injects into `docker_service.py` (helper methods, call-site rewires, labels, anything else). For each, verify an equivalent exists in `E:/tradingsoftware/hummingbot-api/services/docker_service.py` at current HEAD (READ-ONLY — pre-verified: `_get_bot_network_mode` at :46, `_get_compose_labels` at :56, wired at :364-377; you must verify the rest yourself). If ANY injected behavior has no equivalent in api source, STOP: `BATCH_HALT phase=1 reason=patch behavior <X> not present in api source` — you cannot edit the api repo from this run, and removing the mount would change networking.
2. Remove the host-patch bind-mount over `/hummingbot-api/services/docker_service.py` from BOTH stacks (vpn :714 `- /mnt/sharedrive/apps/hummingbot/patches/docker_service.py:...:ro`; no-vpn :459 the `hummingbot_us` twin).
3. Remove the docker_service patch machinery from `api-init` in BOTH stacks: the upstream-ref copy + checksum write (the bug: checksum bumped at ~:214-215 BEFORE the retain decision, which permanently silences the ~:322-341 "upstream changed" warning), the `PATCH_SCRIPT` heredoc, and the warning banner. **Preserve api-init's unrelated duties** (e.g. "Version-aware seed: controllers" and anything else it does) — surgical removal only.
4. Add boot provenance in BOTH stacks: api-init (which runs the api image) logs a `[provenance]` line with the image's `services/docker_service.py` sha256/md5 and `grep -c "def seed_resume_state"` count, so runtime-source-vs-image verification is a log read.
5. Do NOT touch: `${DEV_VAR...}` lines (CDX-003, out of scope), gateway config (phase 4), any other service.
6. Tests: create `test/stack_files/__init__.py` + `test/stack_files/test_stack_invariants.py` — pure file-content tests, no docker: both stack files YAML-parse; NEITHER contains `patches/docker_service.py`; neither contains the `PATCH_SCRIPT` marker; both contain the `[provenance]` emission. Derive expectations from THIS SPEC.

**Step B — review focus:** the equivalence gate's evidence (did the author actually verify every injected behavior against api source, or assert it?); surgical-removal collateral (did unrelated api-init duties survive byte-exact?); YAML validity; test integrity (would `test_stack_invariants` fail if someone re-added the mount line? Name the mutation: re-insert the mount line at vpn :714).

**Step C — adjudicate**, run named mutations, full suite, merge.

Note for the report: post-merge verification (docker rebuild; confirm runtime file == image file; one restart; confirm networking unchanged via `_get_bot_network_mode`/labels behavior) is the HUMAN's, by hand, and gates Run B.

## Phase 2 — CDX-008 engine half: controller `id` contract (High) — branch `fix/hbtriage-p2-controller-id`

**File:** `hummingbot/strategy_v2/controllers/controller_base.py:68`.

**Step A:** implement CONTRACT C2 exactly: `id: str = Field(..., min_length=1, ...)` plus a `field_validator` (mode `before` or `after`, your judgment) that strips whitespace and rejects empty-after-strip; the canonical stored id is the stripped value. Preserve Pydantic v2's existing rejection of non-str (`0`/`False`/`123` — verified rejected under pydantic 2.12.5). Tests: new `test/hummingbot/strategy_v2/controllers/test_controller_base_id_contract.py`, expectations derived from C2, not from running the code: `""` rejected; `"   "` rejected; `"\t\n"` rejected; `0`, `False`, `123` rejected; `"abc"` accepted; `" abc "` accepted with `.id == "abc"`.

**Deliberate break (runbook obligation):** this rejects existing configs with empty/whitespace `id` at engine startup — intended and desirable per the triage. Your final message must state it so finalization carries it into the runbook section.

**Step B — review focus:** does the validator EXACTLY implement C2 (no wider, no narrower)? Test integrity: name mutations (e.g. drop `min_length=1` at :68; make the validator return the raw value) and check each rejection test actually fails under them.

## Phase 3 — CDX-007 engine half: `state_file_name` path contract (High) — branch `fix/hbtriage-p3-state-file-path`

**File:** `controllers/market_making/range_inventory_ladder.py` (validator ~:753-758 currently only maps `""`→None; use-site ~:1471 `Path("data") / file_name` honours an absolute right operand).

**Step A:** implement CONTRACT C1 exactly: replace the validator with the full C1 lexical check (both PurePosixPath and PureWindowsPath; no absolute/drive/root/`..`/`.`; empty-after-strip → None). Add config field `allow_absolute_state_file_name: bool = False`; when True, absolute paths (only — never `..`) are permitted. At the use-site(s) where the state path is composed (~:1471 and any sibling), add a runtime belt-and-braces check: resolve the composed path and assert containment within the data dir (or the absolute path when opted out); on violation raise — never proceed (this converts the silent escape into fail-closed). Tests (spec-derived, new file under `test/hummingbot/strategy_v2/controllers/` or beside existing ladder tests): `/tmp/x.json` rejected; `../conf/x.yml` rejected; `C:\\x.json` rejected; `\\\\share\\x` rejected; `.` rejected; `sub/dir/x.json` accepted; `x.json` accepted; `""` → None; opt-out True + `/abs/x.json` accepted, but opt-out + `../x` still rejected.

**Step B — review focus:** C1 conformance both directions (accept set not narrowed, reject set not widened); the Windows-path cases; whether the runtime containment check can actually fire (name a mutation: revert the validator to the old `""`→None-only body and check which tests fail).

## Phase 4 — CDX-009: gateway transport contract (Med-High) — branch `fix/hbtriage-p4-gateway-mtls`

**Files:** both stack files. Evidence: REPORT §5.2.

**Step A:**
1. **VPN stack:** replace the TCP-only healthcheck at :621 with an authenticated mTLS readiness probe: a node `https` request to `https://127.0.0.1:15888` presenting the client cert+key (and CA) from the certs volume the stack already mounts for gateway/api (read the stack to locate the exact in-container cert paths; if genuinely ambiguous, pick the path the gateway service itself mounts and state the assumption in your final message for the reviewer), `rejectUnauthorized` true, exit 0 only on an authenticated 2xx/OK response.
2. **VPN stack:** set the canonical endpoint — `GATEWAY_URL` (~:730) becomes `https://127.0.0.1:15888` (the config contract says "Gateway always runs secured (mTLS)"; plaintext contradicts it).
3. **No-VPN stack:** it defines no gateway service and no `GATEWAY_URL`, so the API's `https://localhost:15888` default points at nothing. Make the failure explicit: set `GATEWAY_URL=https://gateway-not-deployed.invalid:15888` with a comment stating the no-VPN stack deliberately ships no gateway and gateway routes fail fast by design (explicit fail beats silent default).
4. Declare the cross-compose network dependency: make the external network the gateway is expected to join an explicit, commented, top-level `networks:` declaration (external) in the stack(s) that rely on it, so the dependency is visible instead of implied.
5. Extend `test/stack_files/test_stack_invariants.py`: vpn `GATEWAY_URL` is https; vpn healthcheck no longer uses bare `net.createConnection` and does present client credentials; no-vpn has an explicit `GATEWAY_URL`.

**Runbook obligation (state in final message):** flipping `GATEWAY_URL` to https requires the human to verify the deployed gateway actually serves TLS/mTLS before next deploy — docker-side verification is prohibited here.

**Step B — review focus:** does the probe actually authenticate (client cert presented, server verified) or is it TLS-theater (e.g. `rejectUnauthorized:false` making it a fancy TCP check)? Are cert paths consistent with the volumes actually mounted in the stack? Spec conformance on the no-VPN explicit-fail choice.

## Phase 5 — CDX-010: controllers destination must be explicit (High) — branch `fix/hbtriage-p5-controllers-dest`

**Files:** `Build_hummingbot_api_nonkyc.sh` (default at :110, sync at ~:298-370), `build_hummingbot_nonkyc.sh` (audit for the same pattern). Triage correction: the real failure is WORSE than "skips silently" — a default no-VPN build **succeeds into the wrong (VPN) tree with no warning**.

**Step A:**
1. Remove the VPN-tree default for `CONTROLLERS_DEST`. When `SYNC_CONTROLLERS=1` and `CONTROLLERS_DEST` is empty/unset: print a clear error naming `--controllers-dest` and the two known trees, and exit non-zero BEFORE any destructive step (before purge, before build). Explicit `--no-controllers-sync` remains a valid way to skip. Also fail if the given destination does not exist as a directory.
2. Audit `build_hummingbot_nonkyc.sh` for equivalent defaulted-destination behavior; apply the same explicit-or-fail rule where present.
3. Manifest + provenance: `sync_controllers()` writes `controllers.manifest.sha256` (sorted `sha256<2 spaces>relative/path` lines over the synced set) into the destination; the script computes the manifest hash and the current hummingbot commit SHA and adds `--label nonkyc.hummingbot_commit=<sha> --label nonkyc.controllers_manifest_sha256=<hash>` to the existing `docker build` invocation **in the script text** (you edit the script; you never run docker).
4. Tests: `bash -n` both scripts; extend `test/stack_files/test_stack_invariants.py` with content locks: no `CONTROLLERS_DEST:-/mnt` default remains in either script; the fail-fast branch exists (grep for the error string); the manifest filename and both label keys appear.

**Step B — review focus:** ordering (does the fail-fast REALLY precede the purge branch in control flow? cite line numbers); does `--controllers-dest` parsing still work; are the content-lock tests capable of failing (mutation: restore the :110 default)? These tests are grep-level — judge whether anything stronger is possible without executing the script (executing is prohibited).

## Phase 6 — Doc corrections: the CFH review docs actively mislead (5 items) — branch `fix/hbtriage-p6-doc-corrections`

**Files (tracked for this purpose):** `reviews/COPY_FORWARD_HOOK_TODO.md`, `reviews/COPY_FORWARD_HOOK_REVIEW.md`. Touch nothing else in `reviews/`.

**Step A:** apply all 5 corrections from the triage's "Doc corrections owed". Style: do NOT silently rewrite history — insert clearly marked correction blocks adjacent to (or striking through) the misleading text, each tagged `[CORRECTION 2026-07-16 — hb_predeploy adversarial review + scope triage]` and citing the verification that settled it:
1. TODO's "NOT a bug — do not fix: absolute `state_file_name`" → it IS a fail-open (CDX-007/CLA-004): `resume_service.py:684-693` records "skipped" and proceeds; `docker_service.py:327-341` mounts no arbitrary absolute path; the engine's `Path("data") / file_name` honours an absolute right operand; the ledger lands in the container's ephemeral layer and the bot fresh-initializes.
2. REVIEW's TL;DR "single fail-open requires `id: 0`/`id: False`" → refuted. Verified under Pydantic 2.12.5: `0`/`False`/`123` are REJECTED; `""` and `"   "` are ACCEPTED — the reachable fail-open is empty/whitespace `id` (CDX-008/CLA-002).
3. TODO's P1 fix `str(owner_id) != str(controller_id)` → unsound; do not apply (both engines independently: it widens the API's accepted domain past the engine contract, converting a clean 409 into a started container that then crashes).
4. Both docs on `resume_extra_paths`: `""` is already rejected 422 at the model layer (`models/bot_orchestration.py:124-125`); the real trigger is `"."`/`"./"` (CDX-014/CLA-005 — accepted risk; the docs must still state the correct trigger).
5. Both docs' "ship-ready" verdict → refuted (CDX-001 pre-existing-target `rmtree` data loss + CDX-002 bind-mount shadowing, both cross-confirmed).
No tests — documentation-only phase (stated good reason). Commit exactly these two files.

**Step B — review focus:** factual accuracy of each correction against `scope_triage.md` (the authority) and REPORT §1.2/§4; that correction 4 does not accidentally instruct fixing the out-of-scope CDX-014; that nothing else in the docs changed.

## Phase 7 — Finalization (no review step)

Run the comprehensive block on `nonkyc`. If green, write `REPORT_hbtriage_engine.md` in the repo root (do NOT commit it), containing:
- **Review ledger** built by reading every record in `batch_reviews/`: per phase — findings raised / accepted+fixed / rejected (verbatim rationale) / deferred.
- **REQUIRES HUMAN ARBITRATION:** every REJECTED Critical/High, every DEFERRED finding, any test-theater finding rejected without mutation evidence (protocol violation), and — per the bias note — every REJECTED `CDX-*` finding flagged explicitly.
- **Test integrity:** every test-theater finding and its mutation-experiment outcome (test failed under mutation = real; passed = was theater, then fixed).
- **Runbook notes:** (1) CDX-008 engine half now rejects configs with empty/whitespace `id` — the human must check live conf trees before deploying; (2) CDX-009: verify the deployed gateway serves TLS/mTLS before deploying the https flip; (3) CDX-002 post-merge human verification steps: rebuild (with `--no-purge`, per the out-of-scope CDX-011 operational rule), confirm runtime `docker_service.py` == image file via the new `[provenance]` log, one restart, confirm networking unchanged.
- **THE HUMAN GATE:** Run B (`hummingbot-api`) must NOT start until the CDX-002 verification above is done by hand.
- **Open obligations:** live `resume-preview` dry-run against a real stopped instance (human, Run B side); docker-side verifications above; cross-repo contract status — state that C1/C2 engine halves are live and the API halves land in Run B, and that each side is independently fail-closed.
- Bias note verbatim: the reviewer family (codex) authored the `CDX-*` findings and may have defended them; rejected `CDX-*` findings deserve extra human scrutiny.
No integration merge. No push. Stay on `nonkyc`.
