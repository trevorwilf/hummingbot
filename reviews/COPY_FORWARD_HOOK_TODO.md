# Copy-Forward Hook — Hardening TODO (from adversarial review)

**Created:** 2026-07-15 · **Source:** adversarial review of the copy-forward hook (2 fresh Opus reviewers).
**Target repo (where all fixes land):** `E:\tradingsoftware\hummingbot-api` (branch `nonkyc`; feature base `0639c7a`).
**Full review reference:** `E:\tradingsoftware\hummingbot-api\review\COPY_FORWARD_HOOK_REVIEW.md`.

## Status / framing (read first)

- ~~The hook is **ship-ready for the current operator config** (engine-on-Postgres, string controller ids, flat `state_file_name`s, no SQLite WAL). **None of the findings below trigger in that config**, and all but **#3** fail *closed* (resume aborts with HTTP 409) rather than silently re-seeding.~~
- This is a **non-blocking hardening pass** — latent-bug cleanup, mostly one shared root cause (falsy/type sloppiness). Do it before broadening usage to numeric ids, SQLite deploys, or `resume_extra_paths`.
- **Suggested delivery:** one branch `fix/copyforward-hardening`, tests + baseline-diff gate, merge to `dev` then `nonkyc`. ~30 LOC + regression tests.

> **[CORRECTION 2026-07-16 — hb_predeploy adversarial review + scope triage]** — the struck bullet above is refuted on both of its claims.
>
> **"Ship-ready" — REFUTED** (correction 5). Two independent review engines (Codex `gpt-5.6-sol`, Claude) cross-confirmed two ship blockers that this TODO does not list at all:
> - **CDX-001 / CDX-M04 (High):** a failed resume `shutil.rmtree`s a **pre-existing** target instance directory — the `DEST_NOT_EMPTY` guard that exists to protect a non-empty destination is what triggers the delete. `_cleanup_failed_instance` has no `created_by_this_attempt` flag, so it deletes what it did not create. Data loss, not a latent nit.
> - **CDX-002 (Critical):** the deployment bind-mounts a stale host `docker_service.py` **over** the image's copy, so a rebuilt hook does not run in the container. The stack's init writes the new upstream checksum *before* the retain decision, permanently silencing the "upstream changed" warning after one restart. **You can rebuild the API image with the hook in it and the running container will not have the hook.**
>
> Sources: `scope_triage.md` (Batch 0 + Batch 1); `REPORT_hb_predeploy.md` §1.1, §1.2, §1.4, §4.1, §4.3. CDX-002 is fixed in the engine repo (batch phase 1, `fix/hbtriage-p1-bindmount-cutover`); CDX-001 lands in the API repo (Run B).
>
> **"All but #3 fail closed" — REFUTED** (see correction 2 at #3 below, and correction 1 in "NOT bugs"). There are **two** reachable fail-open paths, and #3 names the wrong trigger for one of them:
> 1. empty / whitespace-only `id:` (CDX-008 / CLA-002) — **not** `id: 0`/`id: False`;
> 2. absolute `state_file_name` (CDX-007 / CLA-004) — the very item this TODO's "NOT bugs" section tells you not to fix.
>
> Do not use this document as a ship gate.

## Definition of done

- [ ] Every P1 item below fixed with a regression test encoding its exact trigger.
- [ ] `C:/anaconda3/envs/hummingbot/python.exe -m pytest tests/ --ignore=tests/test_nonkyc_live_api.py -q --tb=short` → **no NEW failures** vs `test_logs/copyforward_baseline_failures.txt` (14 frozen), and all new tests pass.
- [ ] No behavior change when `resume_mode: off` (unchanged deploy path).

---

## P1 — do these (cheap, real latent bugs)

### [ ] #1+#4 — id/`.owner` type-and-falsy comparison (shared root cause)
- **Where:** `services/resume_service.py:714` (`owner_id != controller_id`), `:664` (`if not owner_id:`), and the `decisions` / `deployed_ids` key handling in `compute_copy_plan`.
- **Bug:** engine writes `.owner` `controller_id` as a **str** (`id: str` coerced by Pydantic); hook reads raw YAML `id` via `config.get("id")` (could be int). `"123" != 123` → **false OWNER_MISMATCH** on every numeric-id resume (#1). Separately, a falsy-but-present `controller_id` (`0`/`""`/`false`) trips `if not owner_id:` → false OWNER_MISMATCH (#4).
- **Fix:** ~~compare as `str(owner_id) != str(controller_id)`~~; change `if not owner_id:` → `if owner_id is None:` (treat empty string as absent explicitly); normalize `decisions`/`deployed_ids` keys to `str()` so int/str ids don't split into two buckets.
- **Test:** ~~YAML `id: 123` (unquoted) + engine-style `.owner` `{"controller_id":"123"}` → resolves as `copied`, not abort.~~ `.owner` with `controller_id: 0` → treated as present.

> **[CORRECTION 2026-07-16 — hb_predeploy adversarial review + scope triage]** (correction 3) — **the struck `str(owner_id) != str(controller_id)` coercion is UNSOUND. Do not apply it.**
>
> Both review engines reached this independently: the coercion **widens the API's accepted domain past the engine's contract**. The engine rejects a numeric `id:` outright (`id: str` under Pydantic 2.12.5 — see correction 2), so `str()`-coercing both sides makes the hook happily resolve `id: 123` as `copied` and start a container that then **crashes on engine-side config validation**. It converts a clean, actionable 409 into a started-then-dead bot. The premise of the struck test — that `id: 123` "should resolve as `copied`, not abort" — is exactly backwards: aborting is correct, because the engine will not accept that config.
>
> The sound direction is the one the batch took: fix the **contract**, not the comparison. Engine half (**live**, batch phase 2, `controller_base.py`): `id: str = Field(..., min_length=1)` + a strip validator rejecting empty-after-strip; canonical id = the stripped value. API half (Run B): reject `""`/whitespace and **abort** the deploy rather than `continue`. Both halves are independently fail-closed.
>
> The rest of this item (`if not owner_id:` → `is None`, and `str()`-normalizing `decisions`/`deployed_ids` **keys**) is unaffected by this correction — key normalization is a bucketing concern, not an identity-acceptance decision.
>
> Sources: `scope_triage.md` Batch 1 CDX-008 ("⚠ **Do NOT apply the TODO's P1 `str(owner_id) != str(controller_id)`** — both engines call it unsound") and "Doc corrections owed"; `REPORT_hb_predeploy.md` §1.2, §4.7.

### [ ] #3 — falsy controller id silently dropped (only fail-OPEN path)
- **Where:** `services/resume_service.py:874` (`if not controller_id:`) in `compute_copy_plan`.
- **Bug:** ~~`id: 0` / `id: False`~~ → controller skipped as "no id" → engine re-seeds from wallet (the insufficient-funds bug this hook exists to prevent). ~~Only fail-open finding.~~
- **Fix:** `if controller_id is None:` (and reject empty string explicitly), not the falsy `not` test.
- **Test:** ~~staged ladder YAML `id: 0` with a matching source ledger → planned `copied`, not `fresh_seed`/`skipped`.~~

> **[CORRECTION 2026-07-16 — hb_predeploy adversarial review + scope triage]** (correction 2) — **right bug, wrong trigger, and not the only fail-open.**
>
> **Verified by direct probe under the installed Pydantic 2.12.5** (this doc reasons from Pydantic **v1** coercion semantics, which no longer apply):
>
> | staged `id:` | engine `ControllerConfigBase` @ review revision `47c714a19` | reachable fail-open? |
> |---|---|---|
> | `0`, `False`, `123` | **REJECTED** — `ValidationError: Input should be a valid string` | **No** — the engine never accepts these, so a container with such an id cannot run |
> | `""`, `"   "` | **ACCEPTED** (`id: str = Field(...)`, no `min_length`) | **YES — this is the real trigger** |
>
> So the struck test is unrunnable-by-construction: `id: 0` never reaches a running engine. The reachable trigger is an **empty or whitespace-only `id:`** — but note the two halves do **not** share a mechanism:
>
> | staged `id:` | `not controller_id` @ `:874`? | historical path | outcome |
> |---|---|---|---|
> | `""` (also bare `id:` → `None`) | **True** — falsy | missing-id branch: one WARNING + `continue` | dropped → no `range_inventory_ladder_.json` → **re-seeds from wallet** (the insufficient-funds bug this hook exists to prevent) |
> | `"   "` (quoted) | **False** — a non-empty str is **truthy** | **bypasses** the guard; ledger name built from the **raw** id at `:617-618`, matching the pre-C2 engine's own raw derivation (`range_inventory_ladder.py:1624`) | historically **copied** — contract-invalid, but `:874` never drops it |
>
> Verified: `bool("   ") is True`; nothing in `:873` → `:617` strips. So the `if controller_id is None:` fix line above is **not sufficient** — it addresses only the `""` half.
>
> **The whitespace half is a strip asymmetry, and the engine half now activates it.** With batch phase 2 live the engine canonicalizes `id: " abc "` → `"abc"` (writing `range_inventory_ladder_abc.json`), while the unstripped hook looks for `range_inventory_ladder_ abc .json` → no match → `fresh_seed` → **wallet re-seed**. A padded `id` in an existing config is therefore a *newly reachable* fail-open until the API half lands — which is why the API must canonicalize with the same `.strip()` and **abort**, not merely swap the falsy test. (Second-order: two empty-id controllers collide on the same ledger filename.)
>
> This is CDX-008 / CLA-002 (**High**, cross-confirmed). Fix status: engine half **live** (batch phase 2 — `min_length=1` + strip validator, so empty/whitespace `id` is now rejected at engine startup); API half in Run B (reject **and abort**, never `continue`). See correction 3 for the fix that must **not** be applied here.
>
> Note the deliberate break: with the engine half live, an existing config carrying an empty/whitespace `id` now **fails at startup** instead of silently re-seeding. That is intended and desirable — check live conf trees before deploying.
>
> Sources: `scope_triage.md` "Doc corrections owed" + Batch 1 CDX-008; `REPORT_hb_predeploy.md` §1.2, §1.4, §4.7.

### [ ] #2 — `resume_extra_paths: ["."]` / `[""]` copies the entire `data/`
- **Where:** `services/resume_service.py:818-833` (`_plan_extra_paths`); also the P1 model validator in `models/bot_orchestration.py`.
- **Bug:** `source.data_dir / "." == data_dir` → `is_relative_to(root)` True → whole `data/` planned as a dir `copytree` (double-copies ledgers, pulls in diagnostics / other controllers / excluded files). Defeats the "config-derived, never glob" invariant. ~~`""` behaves the same.~~
- **Fix:** reject `rel in ("", ".")` and require a **strict subpath** of source `data/` (reject `resolved == data_root`; forbid directory-valued extras equal to the root). Prefer rejecting at the model validator too.
- **Test:** `resume_extra_paths=["."]` ~~and `[""]`~~ → validation/plan error, not a whole-dir copy.

> **[CORRECTION 2026-07-16 — hb_predeploy adversarial review + scope triage]** (correction 4) — **`""` is already rejected; `"."` / `"./"` is the real trigger.**
>
> **`""` never reaches `_plan_extra_paths`.** The model layer already rejects it with HTTP **422**: `models/bot_orchestration.py:124-125` — `if not p: raise ValueError(f"Empty path in {label}")` (verified at `hummingbot-api` HEAD `b3aad36`). Treating `""` as an open path here is wrong, and "also fix `""` in `_plan_extra_paths`" is redundant.
>
> **The reachable trigger is `"."` — and `"./"`, which this doc and the REVIEW both miss.** `models/bot_orchestration.py:119-136` rejects empty, absolute, and `..`, but neither `.` nor `./`; `source.data_dir / "."` collapses to `data_dir`, and `resolved.is_relative_to(data_root)` is True for a path against itself → the whole tree is `copytree`d.
>
> **Status: OUT OF SCOPE / accepted risk — this item is NOT queued for a fix.** The scope triage classifies CDX-014 / CLA-005 (Medium) as accepted: `"."`/`"./"` is not present in this operator's configs, and `resume_extra_paths` is an advanced field this operator does not set. This correction exists **only** so the doc states the correct trigger; it is not an instruction to implement the "Fix" line above. Do not fix it as part of the current batch.
>
> If it is ever addressed, note the predicate: the correct test is **"normalizes to the data root"** (any entry whose `PurePosixPath` normalizes to `.` or empty) plus a **strict** descendant check (`resolved != data_root`) — *not* a literal `("", ".")` denylist, which `"./"`, `"a/.."`, and friends walk straight through.
>
> Sources: `scope_triage.md` OUT OF SCOPE (CDX-014 / CLA-005) + "Doc corrections owed"; `REPORT_hb_predeploy.md` §1.2, §4.11.

### [ ] #7 — `db_engine` case/whitespace mis-detects SQLite
- **Where:** `services/resume_service.py:600` (`_is_sqlite_deployment`).
- **Bug:** exact `== "sqlite"` → `db_engine: SQLite` or `" sqlite"` mis-classed as non-sqlite → sqlite deploy's `*.sqlite` trade DB silently not carried forward. (Postgres operator unaffected, but this is the sqlite-user footgun.)
- **Fix:** `str(engine).strip().lower() == "sqlite"`.
- **Test:** `conf_client.yml` with `db_engine: SQLite` and ` sqlite` → sqlite files planned; `postgresql+psycopg2` → none.

### [ ] #5 — subdir `state_file_name` misplaces the `.owner` sidecar
- **Where:** `services/resume_service.py:721` (owner dst = `new_data_dir / src_owner.name`).
- **Bug:** `state_file_name: sub/ledger.json` → ledger dst keeps `sub/` but owner dst flattens to `data/ledger.json.owner` (should be `data/sub/ledger.json.owner`); copied owner is orphaned. (Cosmetic — resume still works — but latent.)
- **Fix:** owner dst = `new_data_dir / f"{ledger_name}.owner"`.
- **Test:** subdir `state_file_name` → owner lands beside its ledger under `data/sub/`.

---

## P2 — optional (defense-in-depth / narrow triggers)

### [ ] #6 — plan-time containment + transactional owner/ledger copy
- **Where:** `services/resume_service.py:673` (`_plan_controller`), `:1267` (`_execute_copy_plan`).
- **Bug:** relative-traversal `state_file_name` (`../../evil.json`) isn't containment-checked at plan time; execute-time abort can leave an orphan owner in `data/`. **Already mitigated** by `_cleanup_failed_instance` (rmtrees the instance dir on `ResumeError`); content is NOT exfiltrated.
- **Fix:** enforce `dst.resolve().is_relative_to(new_data_root)` in `_plan_controller` (fail before any copy); keep owner/ledger dst derivation consistent (pairs with #5).

### [ ] P2 — Docker `APIError` → 409 instead of opaque 500
- **Where:** `services/resume_service.py:1018-1029` (`_guard_source_container`; only catches `NotFound`).
- **Bug:** daemon-down/unreachable raises `docker.errors.APIError` → router `except Exception` → HTTP 500 with raw error (both real deploy and preview).
- **Fix:** wrap `.get()` in `try/except APIError` → raise `ResumeError` (a "cannot verify source container state" reason) so it maps to 409 with an actionable detail.

### [ ] #10 — abort on duplicate staged controller `id:`
- **Where:** `services/resume_service.py:870-881` (`compute_copy_plan`).
- **Bug:** two staged YAMLs with the same `id:` → duplicate copy items + clobbered `decisions` (idempotent today, but ambiguous). Violates fleet-unique-id constraint (design C1).
- **Fix:** detect duplicate ids across staged YAMLs → abort (ambiguous deploy) or dedup `(src,dst)`.

### [ ] #9 — reject symlinked source ledger
- **Where:** `services/resume_service.py:628` (`_validate_ledger`) / `:696` (`_plan_controller`).
- **Bug:** default-named ledger that is a symlink out of `data/` → external content copied in (asymmetric vs `extra_paths`, which uses `resolve()`). Needs attacker write to source `data/`.
- **Fix:** reject `src_ledger.is_symlink()` or check `src_ledger.resolve().is_relative_to(source.data_dir.resolve())`.

### [ ] P1 (preview) — document/limit the `DEST_NOT_EMPTY` truthfulness gap
- **Where:** `services/resume_service.py:1620-1626` (preview points the guard at a fresh temp `data/`).
- **Bug:** preview can report `would_succeed: true` while the real deploy 409s on `DEST_NOT_EMPTY` (same-second redeploy collides on the `%Y%m%d-%H%M%S` suffix, or a leftover instance dir). Real deploy still fails **closed**; only preview is optimistic.
- **Fix:** either document that `DEST_NOT_EMPTY` is not preview-evaluable, or have preview scan the prospective real `bots/instances/<timestamped-name>/data` when it already exists (stays read-only).

### [ ] #8 — SQLite WAL sidecars (only if engine ever uses WAL)
- **Where:** `services/resume_service.py:799-804` (`_plan_sqlite`).
- **Bug:** `.sqlite-wal` / `.sqlite-shm` not copied; a `.sqlite` without its live `-wal` loses un-checkpointed txns. Moot for Postgres / rollback-journal mode.
- **Fix:** include `-wal`/`-shm` suffixes, or checkpoint before copy. Skip unless a SQLite+WAL deploy is actually in scope.

---

## Related (not code — pre-production validation)

- [ ] **Live `resume-preview` dry-run** against a real stopped instance before ever setting `resume_mode` on. Endpoint: `POST /bot-orchestration/deploy-v2-controllers/resume-preview`. Read-only (Docker touched only for the container-state check). This closes the mock-vs-reality gap that no amount of static review can — all hook tests are mocked.
- [ ] **Confirm the upgrade image ships `psycopg2`** (engine-on-Postgres needs the driver; stock images lack it) — runbook step, not enforced by the hook (design §10.1).

## NOT bugs (verified safe — do not "fix")

- ~~Absolute `state_file_name` (POSIX/backslash/drive-letter) → correctly `absolute_skipped`.~~

  > **[CORRECTION 2026-07-16 — hb_predeploy adversarial review + scope triage]** (correction 1) — **REFUTED, unanimously and with high confidence. Absolute `state_file_name` IS a fail-open (CDX-007 / CLA-004, High), not a safe skip.** Both review engines independently rated this the **most consequential line in this document**, because it instructs a future engineer *not to fix a reachable fail-open path*.
  >
  > `absolute_skipped` is not "correct" — it is the hook resolving an ambiguity by **proceeding**, in direct contradiction of its own stated containment invariant. The full mechanism, each step verified:
  > 1. **Hook skips and proceeds.** `resume_service.py:684-693` appends an `absolute_skipped` item, records `plan.decisions[controller_id] = "skipped"`, warns, and `return None` — no abort. Its stated rationale is "shared-mount scheme assumed".
  > 2. **That assumption is false.** `docker_service.py:327-341` builds the `volumes` dict from a **fixed** set — `conf/`, `conf/connectors`, `conf/scripts`, `conf/controllers`, `data/`, `logs/`, shared `scripts`/`controllers`, and (SEC-048) `certs` read-only. **No arbitrary absolute host path is ever mounted.** The hook assumes a mount that the code that does the mounting does not implement.
  > 3. **The engine honours the absolute path.** `range_inventory_ladder.py` composes the state path as `Path("data") / file_name`; pathlib **drops the left operand** when the right is absolute, so `/tmp/x.json` escapes `data/` (and `../conf/x.yml` resolves outside it).
  > 4. **Result:** the ledger lands in the container's **ephemeral layer** — carried forward by nobody, surviving nothing — and the resumed bot **fresh-initializes from wallet balances**. That is the insufficient-funds re-seed this hook exists to prevent, reached silently, with `would_succeed: true`.
  >
  > Fix status: engine half **live** (batch phase 3, CONTRACT C1 — a lexical validator rejecting absolute/drive/UNC/root/`..`/`.` under **both** `PurePosixPath` and `PureWindowsPath`, an explicit `allow_absolute_state_file_name: bool = False` opt-out for genuine shared-mount operators, plus a runtime resolve-and-containment assertion at the use-site that raises rather than proceeds). API half in Run B: enforce the same accept/reject set in CFH planning and **remove the absolute-skip success path**. Each half is independently fail-closed.
  >
  > Sources: `scope_triage.md` "Doc corrections owed" (first item) + Batch 1 CDX-007 / CLA-004; `REPORT_hb_predeploy.md` §1.2, §1.4, §4.6.

- Empty-but-valid JSON ledger (`{}`/`[]`/`null`) → copied by design (engine quarantine handles structural validity; rejecting would false-positive on legit fresh states).
- Preview read-only guarantees (temp-dir staging + cleanup on all exit paths, no `_execute_copy_plan`, only `containers.get()`), error-mapping order, copy-set source parity, `db_manager=None` degradation — all confirmed correct.
