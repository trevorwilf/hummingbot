# Ladder + NonKYC Connector Hardening — Decisions

Hardening pass implemented 2026-07-06 from the completed code audit
(`hummingbot_LADDER_HARDENING_claude_code_prompt.md`). All nine phases implemented,
each on its own `hardening/phase-N-*` branch, merged into `nonkyc` with `--no-ff`.

## Question-round decision table

| # | Question | Decision |
|---|----------|----------|
| 1 | Fee headroom config source | Reuse the existing `fee_rate` field (no new config) |
| 2 | Fee headroom method | Divide: `buy_budget_quote / (1 + fee_rate)` (exact) |
| 3 | Python pin vs asyncio.timeout | BOTH: pin `python>=3.11` in `setup/environment.yml` AND replace both `asyncio.timeout` usages with `async_timeout.timeout` |
| 4 | Understatement threshold / persistence | Reuse `ledger_reconcile_threshold_quote` for magnitude; 1800 s persistence (class constant `LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS`) |
| 5 | JSONL rotation | 50 MB (`diagnostic_log_max_bytes` = 52 428 800), keep 3 (`diagnostic_log_backup_count`) |
| 6 | Test command | `C:/anaconda3/envs/hummingbot/python.exe -m pytest <dir> -q --tb=short`; the 4 live/smoke files excluded from directory runs (event-loop poisoning) and run standalone; live tests limited to read-only (< $5 risk); keys from `.env` |
| 7 | Push policy | Leave local — no push performed |
| 8 | Phase 9 | Included (all of 9a–9e) |
| 9 | State schema | Confirmed: no `STATE_SCHEMA_VERSION` bump anywhere in this pass (none turned out to be needed) |

## Judgment calls made mid-run

1. **Phase 1 — existing test harnesses pin `fee_rate=0`.** The haircut (with the default
   `fee_rate=0.002`) changes exact budget values asserted by the pre-existing
   budget-*sourcing* suites (`ledger_funded_budgets`, `self_balance`, `v12`, `v13`).
   Those suites test where budgets come from, not fee treatment, so their harness
   defaults now pin `fee_rate=Decimal("0")` (haircut is a no-op at 0) and the haircut has
   its own dedicated suite (`test_range_inventory_ladder_fee_headroom.py`, 11 tests).
   One v13 test that *specifically* exercises the fee-rate booking fallback sets
   `fee_rate=0.002` explicitly.

2. **Phase 1 — planner parity formula.** `_side_rebuild_budget_quote` now returns
   `free + reserved / (1 + fee_rate)`: `free` is already haircut, and the reserved
   notional returned by cancelling resting buys is re-haircut before redeployment. This is
   exact in the ledger-bound steady state (the case the audit targeted) and slightly
   conservative (~`fee_rate × reserved`) when the wallet floor binds — conservative means
   it plans marginally less, never more, so it can never over-deploy.

3. **Phase 1 — headroom measured pre-throttle.** `buy_fee_headroom_quote` is the amount
   withheld at the point of the haircut (after clamp + quota, before the deploy-ceiling
   throttle), matching the prompt's ordering. The wallet-floor diagnostic
   (`_note_wallet_floor`) still reports the pre-haircut clamped budget, unchanged.

4. **Phase 4 — deferral event latched with the warning.** Both the warning log and the
   `range_ladder_reseed_deferred_active_orders` event fire once per reseed token (same
   latch), not per cycle, mirroring the once-per-token idempotency of the re-seed itself.

5. **Phase 5 — persistence is a class constant, not config.** The prompt added no config
   field for the 1800 s persistence, so it lives as
   `RangeInventoryLadderController.LEDGER_UNDERSTATEMENT_PERSISTENCE_SECONDS` (patchable
   in tests, no config surface change).

6. **Phase 6 — directory fsync is best-effort everywhere.** `os.open(dir, O_RDONLY)`
   raises on Windows; the whole directory-fsync block is wrapped in
   `try/except OSError: pass` so non-POSIX/test environments never break. The
   file-content fsync is the load-bearing part and is unconditional.

7. **Phase 7 — compression defer tests re-bound the real helper.** The pre-existing
   `TestDetermineExecutorActionsDefersCreates` tests drive `determine_executor_actions`
   on a spec-mock and mock `_find_executor_by_id`/`_executor_side`; they now also bind
   the real `_executor_side_by_id` so the new lookup routes through the same mock seam
   (test intent unchanged).

8. **Phase 8 — rotation check placement.** The throttled size check runs inside
   `_write_diagnostic_event`'s existing try/except (never-crash contract) before the
   append, and the rotation itself has its own inner exception swallow so a failed
   rotation still appends to the current file.

9. **Phase 9e — contention marker is claimed by the newest controller.** After warning
   about a foreign `<state>.owner` marker, the controller overwrites the marker with its
   own identity (each starter claims; the displaced controller warns on ITS next first
   save/restart). The check runs once per process, warn-only, and marker I/O failures
   never block a state save.

10. **Phase 9c — floor uses the time synchronizer.** The first-poll `since` floor is
    computed from `self._time_synchronizer.time()` (exchange-aligned), the same clock the
    subsequent-poll `query_time` bookkeeping uses.

## Test-lane notes

- Directory runs exclude the four live/smoke files
  (`test_nonkyc_live_api.py`, `test_nonkyc_live_connector_smoke.py`,
  `test_nonkyc_private_connector_smoke.py`, `test_nonkyc_public_connector_smoke.py`) —
  they poison the shared event loop in aggregate runs and are executed standalone instead.
- Live tests executed in this pass are read-only (balances/markets/auth signing); nothing
  that can place, cancel, or move funds. The `createorder` references inside
  `test_nonkyc_live_api.py` are local request-signing tests that are never sent.
- MEXC and candles suites were not run: no MEXC or data-feed code was touched in this pass.
