# Ladder + NonKYC Connector Hardening — Known Issues

**None.** No fix in this pass survived the 5-attempt loop unresolved; every phase's test
gate passed before merge, and the final integration gate passed on `nonkyc`:

- Controllers suite: 479 passed (baseline before this pass: 429)
- NonKYC connector suite (non-live): 415 passed (baseline: 393)
- Standalone live API test: exit 0, all lanes `[OK]`
- Standalone auth verification: AUTH SUCCESS (both GET lanes)
- Smoke tests (public / private / live), each in its own process: 5 + 2 + 6 passed

Pre-existing caveat (documented before this pass, unchanged by it): the live/smoke test
files poison the shared event loop when run together in one pytest process — running all
three smoke files in a single invocation produces `Event loop is closed` failures. Run
them standalone (one pytest process per file), as `all_tests.ps1` does.
