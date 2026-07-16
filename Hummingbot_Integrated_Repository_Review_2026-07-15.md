# Integrated Repository Review: Custom Hummingbot Fork and Hummingbot API

**Review date:** 2026-07-15  
**Review posture:** STOP-SHIP by default  
**Primary objective:** determine whether the supplied control plane, trading runtime, connectors, controllers, persistence, backtesting path, and deployment process are safe and reproducible enough for paper or live trading.

## Reviewed artifacts

| Artifact | SHA-256 | Extracted source root |
|---|---|---|
| `hummingbot_API(1).zip` | `a075a4ddab753e2884e1455421b9028491497b8c1c36defc950cbf747378b147` | `tradingsoftware/hummingbot-api/` |
| `hummingbot(1).zip` | `9ba570e1d1747a5ad480bcb845e15b18e3d7bc7e779b961451c6cfed14cf4b81` | `tradingsoftware/hummingbot/` |

The archives were inspected for unsafe ZIP paths before extraction. No path-traversal or symlink entries were found. Neither archive contains a `.git` directory, so commit history, branch ancestry, provenance, and an exact upstream diff cannot be independently reconstructed from the supplied artifacts. The fork package metadata reports Hummingbot version `20260126` in `hummingbot/hummingbot.egg-info/PKG-INFO:1-4`.

---

# 1. Executive summary

## 1.1 Decision

**Overall decision: STOP-SHIP. The supplied system is not suitable for live trading and is not yet suitable for unattended paper trading.**

This conclusion does not depend on strategy profitability. It is driven by confirmed control-plane, deployment, lifecycle, persistence, and simulation defects that can produce unauthorized host control, orphan live orders, corrupted order/fill records, unreproducible artifacts, and misleading backtest metrics.

The strongest parts of the codebase are the custom ladder controller's defensive runtime checks, the API startup order reconciliation logic, and the copy-forward/resume guard system. Those are meaningful engineering positives. They do not compensate for the current deployment and persistence blockers.

## 1.2 Stage classification

| Stage | Classification | Basis |
|---|---|---|
| Source-code research | **Usable with caution** | The source compiles and contains several strong defensive components, but provenance is incomplete and important tests are not reliable gates. |
| Backtesting / research inference | **Research only; performance claims not trustworthy** | The backtester has shared mutable state, unrealistic fills, current-rule lookups for historical runs, optimistic same-candle tie handling, and a mislabeled Sharpe calculation. |
| Controlled local paper trading | **Blocked** | Critical credential, network-exposure, lifecycle, Docker isolation, and fill-idempotency issues must be fixed first. |
| Unattended paper trading | **Blocked** | No verified end-to-end recovery, stop/cancel postcondition, deterministic deployment, or reconciled accounting acceptance record. |
| Live deployment | **Not suitable** | Confirmed paths exist from default credentials to API control, Docker control, arbitrary code execution, and orphan-order risk. |
| Candidate live deployment after remediation | **Possible only after explicit acceptance gates** | Requires architectural security changes, persistence fixes, deterministic builds, exchange-chaos tests, and an extended reconciled paper-trading period. |

## 1.3 Most important blockers

1. **Normal setup silently leaves the API at `admin/admin` and connector encryption password `a`.** The setup script writes bare environment names, while the application reads `HBOT_API_*` names. This was reproduced directly and is contradicted by a failing included test.
2. **The Tailscale deployment does not isolate the services.** The base Compose file still publishes the API, PostgreSQL, MQTT, WebSocket, management, and dashboard ports on all host interfaces; the overlay merely adds a Tailscale sidecar.
3. **A single Basic Auth credential controls a root API process with a writable Docker socket and authenticated arbitrary Python upload/import.** That is effectively a host administration and code-execution boundary, not a normal trading REST API.
4. **Stop-and-archive defaults to skipping order cancellation, then stops/removes the bot after a fixed delay without proving the exchange has zero open orders.** This can leave orphan live orders on the venue.
5. **Duplicate fill events mutate order aggregates before trade-ID deduplication.** A replayed fill can double filled quantity and fees and mark an order filled even though the duplicate trade row is rejected.
6. **The backtester cannot support deployment decisions.** Its execution assumptions and metrics are not realistic, and concurrent jobs share a mutable engine.
7. **The supplied archive contains encrypted exchange credential material, password-verification files, instance snapshots, and live logs.** Because the API may have used the trivial encryption default, affected exchange keys should be rotated as an incident precaution.

## 1.4 Immediate operator action

Until the critical blockers are remediated:

- Do not expose the supplied API Compose stack to any public or untrusted network.
- Do not rely on Tailscale alone with the supplied Compose files; remove or loopback-bind every published service port.
- Rotate the Kraken, MEXC, and NonKYC API keys represented in the supplied instance bundles. Use trade-only permissions, no withdrawal rights, IP restrictions where supported, and dedicated keys per bot or control plane.
- Do not use `stop-and-archive-bot` with its current defaults on a live account.
- Do not treat API database order/fill totals or ladder managed-fund PnL as reconciled accounting.
- Do not promote any parameter set based on the supplied backtesting engine's Sharpe, profit factor, drawdown, or equity curve.

---

# 2. Scope, methodology, and limitations

## 2.1 System inventory reviewed

The review followed the principal paths through the integrated system:

```text
Operator / dashboard / MCP client
        |
        | HTTP Basic Auth / WebSocket credentials
        v
Hummingbot API process
  |-- AccountsService / TradingService --> exchange connectors --> CEX APIs
  |-- GatewayService --------------------> Gateway / DEX paths
  |-- DockerService ---------------------> /var/run/docker.sock --> bot containers
  |-- BotsOrchestrator ------------------> EMQX MQTT -----------> Hummingbot instances
  |-- controller/script APIs ------------> shared Python files --> dynamic imports / bot mounts
  |-- OrdersRecorder / repositories -----> PostgreSQL
  |-- BacktestingService ----------------> Hummingbot backtesting engine / online candle fetches
        |
        v
Custom Hummingbot fork
  |-- NonKYC connector
  |-- range_inventory_ladder controller
  |-- EMA regime-hold and BB/RSI mean-reversion controllers
  |-- restart persistence and structured diagnostics
```

Reviewed areas included:

- API authentication, WebSocket authentication, CORS, rate limiting, setup, Docker deployment, Tailscale overlay, MQTT lifecycle control, controller/script storage, dynamic imports, account/trading endpoints, persistence models/repositories, startup reconciliation, Gateway configuration, background backtests, and bot stop/archive workflows.
- Hummingbot connector changes, especially NonKYC order/fill/reconnect behavior.
- Custom controller logic and supplied live configuration/runtime artifacts.
- Copy-forward/resume logic and deterministic deployment/build behavior.
- CI workflows, dependency declarations, static compilation, selected tests, test collection, and Bandit results.

## 2.2 Verification performed

- Safe ZIP-entry validation and extraction.
- SHA-256 hashing of both archives.
- Repository/file inventory and size analysis.
- Python source compilation with `compileall`, excluding generated/build/runtime cache directories: **passed for both source trees**.
- Direct settings-model probe of bare versus `HBOT_API_*` variables: **confirmed the credential configuration defect**.
- `pytest -q tests/test_config.py`: **1 failed, 3 passed**; the failing test is the bare credential-variable assertion.
- API test collection excluding the live API test: **86 tests collected, 8 modules failed collection** because the review environment did not contain the custom Hummingbot package on the import path. This is an environment limitation, but it also demonstrates that the API repo is not independently hermetic.
- Bandit static scan:
  - API: one high-severity warning and 29 low-severity warnings. The high warning is MD5 used for change detection in `services/executor_ws_manager.py`, not password/cryptographic security; it is low practical risk in that context.
  - Custom Hummingbot subset: no high or medium findings, 18 low findings.
- Direct comparison of controller trees:
  - Across all Python files under the two controller trees, API contains 35 files and Hummingbot contains 37; 33 paths are common and 18 of those common files differ.
  - Excluding `__init__.py`, examples, and the livestream example, the API has 21 controller implementations and Hummingbot has 22; 19 paths are common and only four are byte-identical.

## 2.3 Limitations

- No `.git` metadata was supplied. Findings describe the archive contents, not necessarily the current state of a remote branch or deployed host.
- The system was not connected to real exchanges, a Docker daemon, EMQX, PostgreSQL, or Gateway during this review. No order was placed or canceled.
- Encrypted credentials were not decrypted or displayed.
- Full integration tests could not be executed in a clean, pinned environment because no lockfile or complete hermetic build manifest was supplied.
- Upstream compatibility was not inferred from repository names or comments. Exact upstream divergence requires commit SHAs and remote repository access.

These limitations reduce positive assurance. They do not weaken the confirmed defects that can be established directly from code and included runtime artifacts.

---

# 3. Strongest evidence of sound engineering

The system contains substantive defensive work. These items should be retained while the blockers are remediated.

## 3.1 API startup order reconciliation is intentionally fail-closed

`hummingbot-api/main.py:260-277` initializes trading connectors, reconciles persisted active orders, cleans orphaned executors, and recovers positions before normal account service operation.

`hummingbot-api/services/unified_connector_service.py:970-1063` has a sensible core rule: terminal state is persisted only when the exchange confirms it or confirms the order is absent. Transient/unverifiable orders are left untouched rather than falsely marked canceled. Still-open orders remain tracked and cancelable. Per-order savepoints prevent one persistence error from poisoning the entire connector reconciliation transaction.

This is the correct direction for restart safety. Its assurance is weakened by the global fill aggregate bug, global client-order-ID uniqueness, a 1,000-order retrieval cap, and incomplete perpetual metadata reconstruction.

## 3.2 Copy-forward/resume logic is unusually defensive

`hummingbot-api/services/resume_service.py` implements a narrow, configuration-derived copy set rather than broad filesystem copying. It performs source/destination guards, ledger and graceful-shutdown checks, containment checks, manifest hashing, drift reporting, and cleanup of a partial destination on failure. The Docker deployment hook in `services/docker_service.py:301-315` runs before container start and fails closed if resume seeding fails.

This is one of the strongest components in the repos. It still depends on truthful bot-run lifecycle state. The current stop-and-archive workflow can mark a run stopped before exchange shutdown is actually complete, undermining the meaning of a “graceful” source.

## 3.3 The ladder controller contains many production-minded safeguards

`hummingbot/controllers/market_making/range_inventory_ladder.py` includes:

- constrained state paths and a sidecar ownership marker;
- atomic state writes with file and parent-directory `fsync` around `os.replace` (`:2020-2035` and following);
- malformed-state quarantine and schema migration logic;
- default blocking of initialization when requested funds are not actually available;
- trading-rule, quantization, minimum-size, and minimum-notional checks;
- market-data hard pause with cancellation behavior;
- session expiry and wind-down controls;
- wallet floors and wallet-truth clamps;
- extensive structured diagnostics and reconciliation fields.

These controls demonstrably reduced immediate damage in the included live artifacts. For example, when the XMR ladder ledger over-claimed owned base, the controller clamped the sell budget to wallet truth rather than placing an unsupported sell.

## 3.4 NonKYC has persistent trade-ID deduplication and reconnect diagnostics

`hummingbot/connector/exchange/nonkyc/nonkyc_exchange.py:188-215` persists a bounded set of processed exchange trade IDs alongside tracking state. Reconnect logic identifies exchange orphans and locally missing orders (`:232-299`), and the connector tracks an explicit post-reconnect balance-settling phase. Controlled cancellation of exchange orphans is opt-in and defaults off (`nonkyc_utils.py:57-63`), which avoids deleting unrelated manual orders by default.

The remaining connector risks are described later; these positives are worth preserving.

---

# 4. Ranked critical issues and live blockers

| ID | Severity | Confirmed issue | Primary failure mode | Release gate |
|---|---|---|---|---|
| F-01 | **Critical** | Setup writes wrong credential variable names | API remains `admin/admin`; credentials encrypted with `a` | Block all deployment |
| F-02 | **Critical** | Tailscale overlay leaves service ports globally published | Public API/DB/MQTT exposure despite private-network claim | Block all remote deployment |
| F-03 | **Critical** | One Basic Auth principal controls root + Docker socket + Python upload/import | Host-level code execution and arbitrary container control | Architectural blocker |
| F-04 | **Critical** | Stop/archive skips cancellation by default and does not verify exchange postcondition | Orphan live orders after bot/container removal | Block live lifecycle operations |
| F-05 | **Critical** | Fill aggregate mutation occurs before trade deduplication | Doubled fills/fees, premature FILLED status, corrupt PnL | Block accounting and recovery |
| F-06 | **High** | MQTT start/stop success means publish accepted, not bot/exchange postcondition | False lifecycle success and spoofable state | Block unattended operation |
| F-07 | **High** | Startup uses ad hoc migrations and drops named tables | Data loss/schema drift at startup | Block shared/production DB |
| F-08 | **High** | Direct trading endpoints lack portfolio-level risk and idempotency controls | Duplicate/excess/stale-price orders | Block direct live trading |
| F-09 | **High** | Builds and controller copy-forward are not reproducible | Deployed code cannot be reconstructed; stale scripts persist | Block release promotion |
| F-10 | **High** | Sensitive encrypted credentials, verifiers, instances, and logs are in archive | Offline attack/privacy/operational leakage | Rotate and sanitize |
| F-11 | **High** | Ladder ownership conflict is warn-only; fee/accounting gaps are unresolved | Concurrent ledger corruption or false managed-fund PnL | Block live accounting claims |
| F-12 | **High** | Directional controllers can trade on arbitrarily stale closed candles/quotes | Stale signals and unbounded live-vs-research mismatch | Research only |
| F-13 | **Critical for inference** | Backtesting engine has shared mutable state and unrealistic execution/metrics | False strategy selection and overfit promotion | Block parameter promotion |
| F-14 | **High** | NonKYC recovery and cancellation can mark uncertain operations successful | Unknown live orders/fills after network failures | Block unattended live use |
| F-15 | **High** | Basic Auth/HTTP/WebSocket query credentials and weak process-local rate limiting | Credential leakage and brute-force/resource abuse | Block untrusted network access |
| F-16 | **Medium/High** | Recovery reconstructs incomplete order metadata and caps active orders | Perpetual/order-state mismatch after restart | Block scale/perpetual recovery |
| F-17 | **Positive dependency risk** | Resume guards are strong but trust an unsafe lifecycle state | Invalid source accepted as graceful | Fix lifecycle before relying on resume |

---

# 5. Detailed findings and remediation

## F-01 — Critical: setup credential names do not match the application settings model

### Confirmed evidence

`hummingbot-api/config.py:75-97` defines `SecuritySettings` with:

- default username `admin`;
- default password `admin`;
- default connector-config encryption password `a`;
- `env_prefix="HBOT_API_"`.

Therefore the effective environment names are:

```text
HBOT_API_USERNAME
HBOT_API_PASSWORD
HBOT_API_CONFIG_PASSWORD
HBOT_API_DEBUG_MODE
```

The setup script instead writes:

```text
USERNAME
PASSWORD
CONFIG_PASSWORD
DEBUG_MODE
```

at `hummingbot-api/setup.sh:439-465`. The README documents the same bare names at `README.md:133-150`. `config.py:105-120` logs a critical warning when defaults are in use, but startup continues.

A direct settings probe confirmed:

- setting the bare variables leaves the effective values `admin`, `admin`, `a`, and `False`;
- setting the `HBOT_API_*` variables changes the effective settings.

The included test `hummingbot-api/tests/test_config.py:10-22` says bare variables “should still configure auth,” but `pytest -q tests/test_config.py` fails with `settings.username == "admin"` instead of the expected `testuser`.

### Impact

A normal operator can follow the installer, enter strong values, and receive a success message while the API silently remains accessible with `admin/admin`. More importantly, connector files created under that deployment are encrypted with the trivial password `a`. The supplied deployment also exposes the API port and mounts the Docker socket, so this is a host-compromise path, not merely an API configuration mistake.

Changing only `CONFIG_PASSWORD` after credential files already exist can make those files unreadable. A remediation must include credential migration/re-encryption, not just an environment rename.

### Additional setup weaknesses

- Credential prompts use ordinary shell `read`, not a non-echoing secret input (`setup.sh:23-60`, calls at `:402-404`).
- The generated `.env` is not explicitly restricted with `chmod 600`.
- Values are interpolated into a shell/env file without robust escaping; spaces, `#`, `$`, backticks, and newlines can corrupt or alter the file.
- The warning tells users to set bare names even though the settings class reads prefixed names (`config.py:112-118`).

### Required remediation

1. Standardize on one naming contract. Prefer the existing `HBOT_API_*` names.
2. Update setup, README, Compose, tests, environment examples, and operational runbooks atomically.
3. Add legacy aliases only for a bounded migration period, with a warning identifying which variable was used.
4. Make startup fail with nonzero exit when username, API password, or config password equals an insecure default outside an explicit local-test mode.
5. Reject weak config passwords and require sufficient length/entropy.
6. Use non-echoing prompts and safely quote generated values.
7. Write `.env` atomically with owner-only permissions.
8. Add a migration command that decrypts with the old password and re-encrypts with the new password without printing plaintext.
9. Rotate exchange credentials after migration; do not merely re-encrypt already exposed keys.

### Acceptance criteria

- A clean installer run with random credentials results in effective values matching the entered credentials.
- Bare variables either fail with a precise message or work only through explicit, tested compatibility aliases.
- Startup exits nonzero when any default credential is effective in deployment mode.
- Automated tests start a real settings process from the generated `.env`, not only instantiate a mocked model.
- Existing credential files are successfully migrated in a test fixture, and the old config password no longer decrypts them.

---

## F-02 — Critical: the supplied Tailscale deployment does not isolate any published service

### Confirmed evidence

The base Compose file publishes:

- API `8000:8000` (`hummingbot-api/docker-compose.yml:5-6`);
- MQTT and management ports `1883`, `8883`, `8083`, `8084`, `8081`, `18083`, and `61613` (`:59-65`);
- PostgreSQL `5432:5432` (`:91-92`).

Docker publishes an unqualified mapping on all host interfaces by default. The Tailscale overlay at `docker-compose.tailscale.yml:11-27` only adds a host-network Tailscale sidecar. It does not remove or replace the base `ports` mappings.

The README states that port 8000 is not exposed publicly when Tailscale is enabled (`README.md:46-53`, `:186-192`). That statement is false for the supplied Compose merge.

The API Dockerfile launches plain HTTP on `0.0.0.0` (`Dockerfile:55-56`). PostgreSQL uses a fixed password `hummingbot-api` in the connection string and server configuration (`docker-compose.yml:27`, `:80-84`). Broker settings default to `admin/password` (`config.py:8-17`; `setup.sh:446-450`). The supplied Compose file does not provision explicit per-client EMQX authentication or topic ACLs; actual broker access therefore depends on external/manual configuration or image defaults not represented in the repository.

### Impact

Operators can reasonably believe the stack is private while the API, database, broker, broker dashboard, and management interfaces remain reachable from any network interface allowed by the host firewall. Combined with F-01 and F-03, this creates a credible remote path to host control and exchange trading authority.

Even if a firewall currently blocks access, the repository does not make the security property true. A later firewall or cloud-security-group change can silently expose the stack.

### Required remediation

- Remove public host port mappings for PostgreSQL and EMQX entirely unless a narrowly justified local binding is required.
- Bind the API only to `127.0.0.1` and expose it to the tailnet through an authenticated Tailscale serve/reverse-proxy layer, or run the service in a network namespace that is reachable only through Tailscale.
- Create a dedicated Compose profile for local development and a separate production Compose file with no wildcard port publication.
- Configure MQTT TLS, unique per-bot credentials, deny-by-default topic ACLs, and no unauthenticated dashboard/management access.
- Generate unique database credentials and keep PostgreSQL internal to the Compose network.
- Pin Tailscale and other images by digest rather than `latest`.
- Add deployment-time assertions that inspect effective Compose output and host listeners.

### Acceptance criteria

From a non-tailnet machine on the same public network, all of the following must be unreachable: 8000, 1883, 8883, 8083, 8084, 8081, 18083, 61613, and 5432. From an authorized tailnet device, only the intended API/proxy endpoint should be reachable. CI must render `docker compose config` and fail if a production service contains an unapproved wildcard port mapping.

---

## F-03 — Critical: one Basic Auth principal has host-level Docker and code-execution authority

### Confirmed evidence

The API container receives a writable host Docker socket at `hummingbot-api/docker-compose.yml:7-10`. The API Dockerfile has no `USER` directive, so the direct image runs as root.

Every HTTP router shares the same Basic Auth dependency (`hummingbot-api/main.py:427-480`). There are no roles, scopes, read-only principals, per-account policies, step-up authentication, or two-person controls.

The Docker surface can:

- list all active/exited containers and images;
- start or stop arbitrary named containers (`routers/docker.py:134-161`);
- prune all exited containers on the host (`services/docker_service.py:149-153`);
- pull arbitrary image names (`routers/docker.py:164-178`);
- remove containers;
- launch a caller-selected bot image (`models/bot_orchestration.py` image field; `services/docker_service.py:371-382`).

Bot containers are launched with caller-selected images, writable host mounts, the config password in their environment, and the configured network mode. The run call does not set read-only root filesystem, user, capability drops, `no-new-privileges`, PID limits, CPU/memory limits, seccomp profile, or image-digest allowlist (`services/docker_service.py:317-382`).

The API also accepts arbitrary controller and script Python text:

- controller write: `routers/controllers.py:200-236`;
- script write: `routers/scripts.py:151-168`.

Controller loading/validation dynamically imports or reloads modules in the API process through `utils/file_system.py:273-336` and controller endpoints around `routers/controllers.py:278-335`. This is intentional code deployment. In the current root/Docker-socket context, an authenticated malicious controller can execute host-administration operations.

### Impact

A stolen, guessed, logged, or default API password does not merely permit a bad order. It permits arbitrary Python execution in the control plane and arbitrary Docker image execution through the host daemon. Access to the Docker socket is generally equivalent to root control of the Docker host.

The API therefore has a much larger blast radius than the README's description suggests. It must be designed as a privileged deployment control plane, not a conventional trading API.

### Required remediation

1. Remove the raw Docker socket from the API container.
2. Introduce a narrow deployment worker with a minimal, authenticated command schema and an allowlist of signed image digests, mount roots, environment keys, and resource limits.
3. Separate identities and permissions at minimum into read-only monitoring, trading, credential administration, controller deployment, and host/container administration.
4. Require step-up authentication and an auditable approval for code/image deployment and destructive lifecycle actions.
5. Do not import uploaded code in the API process. Validate it in an ephemeral, network-disabled, unprivileged sandbox with no secrets or Docker socket.
6. Require signed controller packages with content hash, source commit, reviewer identity, and immutable promotion metadata.
7. Launch bot containers as a non-root UID with read-only rootfs, capability drops, `no-new-privileges`, PID/CPU/memory limits, constrained network, and only the minimum required mounts.
8. Stop placing the config password in general container environment inspection paths; use a narrowly scoped secret mount or runtime secret service.

### Acceptance criteria

- Compromising an API read-only or trading token cannot access Docker, write Python, read connector secrets, or deploy images.
- The API process has no Docker socket and runs non-root.
- A malicious uploaded controller cannot perform filesystem, network, process, or Docker operations during validation.
- Only pre-approved, digest-pinned images can be launched.
- A security test demonstrates that path traversal, symlink redirection, arbitrary image names, arbitrary mounts, and shell/code injection are rejected.

---

## F-04 — Critical: stop-and-archive can deliberately leave live exchange orders orphaned

### Confirmed evidence

`StopAndArchiveRequest.skip_order_cancellation` defaults to `True` at `hummingbot-api/models/bot_orchestration.py:102-108`. The actual route also defaults the query parameter to `True` at `routers/bot_orchestration.py:394-460`.

The workflow:

1. captures final status;
2. marks the bot run stopped before proving shutdown (`services/bots_orchestrator.py:641-647`);
3. publishes an asynchronous stop command (`:649-656`);
4. treats MQTT publish success as stop success;
5. sleeps a fixed 15 seconds (`:663-665`);
6. stops the container (`:667-687`);
7. archives and removes it, even continuing after archive failure (`:689-710`);
8. removes the bot from active tracking in `finally`, including failed paths (`:752-761`).

Hummingbot cancels outstanding orders only when `skip_order_cancellation` is false (`hummingbot/client/command/stop_command.py:38-40`). The asynchronous MQTT handler schedules the stop and returns before the stop loop necessarily completes (`hummingbot/remote_iface/mqtt.py:193-214`; `stop_command.py:12-18`).

No step proves:

- no new orders are being created;
- every cancellation was acknowledged by the exchange;
- the exchange's open-order endpoint returns zero owned orders;
- all final fills were received and persisted;
- the bot's connector state is reconciled before the container is killed.

### Impact

The default behavior can intentionally leave open orders on the exchange and then destroy the process responsible for managing them. A later fill can occur with no local owner, no stop-loss logic, and no reliable persistence. This is a direct live-loss scenario.

The early database STOPPED status also contaminates the copy-forward/resume trust contract: a run may look gracefully stopped even though exchange orders remain live.

### Required remediation

Implement a durable lifecycle state machine:

```text
RUNNING
  -> QUIESCING_NEW_ORDERS
  -> CANCELLING_OWNED_ORDERS
  -> VERIFYING_EXCHANGE_ZERO_OPEN
  -> DRAINING_FINAL_FILLS
  -> PERSISTING_FINAL_STATE
  -> BOT_STOPPED
  -> CONTAINER_STOPPED
  -> ARCHIVED
```

- Default cancellation must be enabled.
- The bot must stop creating new orders before cancellation begins.
- Use synchronous/correlated MQTT RPC with an operation ID and timeout.
- Query the exchange directly after cancellation and prove zero owned open orders, preferably in two consecutive snapshots separated by a short interval.
- Persist final fills and balances before marking the run stopped.
- If exchange state cannot be verified, fail closed and keep the bot/container available for intervention.
- An emergency “detach with open orders” operation may exist only as an elevated, explicit, two-step action with order inventory shown to the operator and a permanent incident record.
- Do not remove active tracking in `finally` when the operation fails.

### Acceptance criteria

Chaos tests must cover dropped MQTT replies, delayed bot stop, partial cancellation, cancel accepted but not effected, final fill arriving during stop, exchange 5xx, broker reconnect, and API restart midway through the state machine. The operation may report success only after the exchange postcondition is satisfied and persisted.

---

## F-05 — Critical: duplicate fill events corrupt order aggregates before deduplication

### Confirmed evidence

The `orders` table defines `client_order_id` as globally unique (`hummingbot-api/database/models.py:33-70`), and `trades.trade_id` as globally unique (`:73-90`). Neither key is scoped by account and connector.

On every fill event, `OrderRepository.update_order_fill`:

- adds the event amount to existing filled quantity;
- overwrites `average_fill_price` with the latest fill price rather than calculating VWAP;
- adds the fee;
- may mark the order FILLED (`database/repositories/order_repository.py:40-72`).

`OrdersRecorder` calls that aggregate update before constructing and inserting the deduplicated trade row (`services/orders_recorder.py:239-285`). If a duplicate event arrives, the aggregate is mutated first; `TradeRepository.create_trade` then returns `None` for the duplicate, but does not undo the order mutation.

The fallback trade ID uses client order ID, event timestamp, and amount (`orders_recorder.py:264-270`). That identity is not stable if a replay changes timestamp representation or event normalization. Timestamps use `datetime.fromtimestamp`, which is local-time dependent and creates a naive datetime at `:275`.

`TradeRepository.create_trade` performs a check-then-insert and catches `IntegrityError` by rolling back the session (`database/repositories/trade_repository.py:15-36`). The aggregate and trade insert are not expressed as one conditional, idempotent database operation.

### Impact

Reconnects, WebSocket/REST overlap, broker replay, event reordering, or duplicate exchange notifications can:

- double filled amount;
- double fees;
- prematurely mark an order FILLED;
- create a false average fill price;
- corrupt position recovery and PnL;
- cause later cancellation/reconciliation logic to act on false state.

This is a production accounting and control defect, not merely a reporting issue.

### Required remediation

- Define a database unique key on `(account_name, connector_name, exchange_trade_id)`; include exchange order ID where needed for venues with only per-order trade IDs.
- Persist the raw fill first with `INSERT ... ON CONFLICT DO NOTHING RETURNING ...`.
- Update order aggregates only if the insert actually wins.
- Perform insert and aggregate update in one database transaction.
- Calculate weighted average fill price from cumulative quote/base, not latest price.
- Keep all monetary values as `Decimal`/`NUMERIC`; do not convert to float for persistence.
- Store UTC-aware exchange timestamps plus receipt timestamp and source channel.
- Scope client order identity by account and connector.
- Preserve raw event payload hash and sequence/source metadata for audit and replay.

### Acceptance criteria

Replaying the same fill 1, 10, and 1,000 times must result in exactly one trade row and unchanged order aggregates after the first insertion. Tests must include concurrent duplicate inserts, reordered partial fills, REST/WS duplicates, missing exchange trade IDs, process restart, and fee assets different from the quote asset.

---

## F-06 — High: MQTT command publication is mistaken for lifecycle completion

### Confirmed evidence

The API subscribes to wildcard bot topics and auto-discovers bot IDs from messages (`hummingbot-api/utils/mqtt_manager.py:50-60`, `:112-159`). Basic start/stop uses `publish_command`, which returns `True` when the broker accepts the publish (`:426-465`). `BotsOrchestrator.start_bot` and `stop_bot` return that boolean as operation success (`services/bots_orchestrator.py:173-215`).

A separate `publish_command_and_wait` path exists, but response topics and message IDs are based on millisecond timestamps (`mqtt_manager.py:337-417`), and the ordinary start/stop path does not use it.

The Hummingbot stop handler has an asynchronous mode that schedules stop and returns immediately (`hummingbot/remote_iface/mqtt.py:193-214`). Thus even a valid RPC response would need a second state transition/postcondition check.

No repository-defined EMQX ACL restricts which clients can publish heartbeats, status, performance, or commands. Do not assume anonymous broker access without runtime verification, but the repository does not establish a deny-by-default security property.

### Impact

The API can report a bot started/stopped when only a broker publish succeeded. A malicious or misconfigured MQTT client may spoof a bot heartbeat/status, collide with an instance ID, or inject misleading performance data if broker policies permit it. Lifecycle races propagate into archiving, persistence, and resume source selection.

### Required remediation

- Unique per-bot broker identity and certificate.
- TLS/mTLS transport.
- Topic ACL: a bot may publish only its own telemetry and subscribe only to its own command topics; API may use only explicitly permitted control topics.
- Cryptographically random UUID operation IDs, not millisecond timestamps.
- Durable command request/ack/result state in PostgreSQL.
- Idempotent commands with expected prior state and monotonically increasing generation.
- Success only after verified bot heartbeat state and, for stop, verified exchange order postcondition.
- Broker-side retained-message and replay behavior explicitly configured and tested.

---

## F-07 — High: startup migrations are ad hoc and drop tables by name

### Confirmed evidence

`hummingbot-api/database/connection.py:42-57` calls `create_all`, then a small list of hand-written migrations, then drops Hummingbot-named tables on every startup. The migrations cover only three columns (`:59-98`), and unexpected migration errors are logged as warnings rather than always aborting. `_drop_hummingbot_tables` executes `DROP TABLE IF EXISTS` for three names (`:99-112`). No Alembic/versioned migration framework is present.

### Impact

- Schema state is not reproducible or auditable.
- A failed partial migration can leave an unknown schema while startup continues.
- Any shared database or future table-name reuse can lose data.
- Rollback and point-in-time recovery are not defined.
- Release compatibility cannot be proven from a schema version.

### Required remediation

Use versioned, transactional migrations with a schema-version table, preflight checks, backup policy, rollback/forward-fix plan, and explicit ownership of every table. Remove destructive table drops from normal application startup. Unknown or failed schema state must block startup.

### Acceptance criteria

Upgrade tests must cover every supported prior schema, production-sized data, rollback/restore, interrupted migration, and repeated startup. A restore drill must prove recovery from a failed release. No startup path may issue an unreviewed `DROP TABLE`.

---

## F-08 — High: direct API trading lacks portfolio-level risk, idempotency, and confirmed cancellation

### Strong behavior

`AccountsService.place_trade` validates account/connector availability, trading rules, supported order types, amount quantization, minimum order size, and minimum notional (`hummingbot-api/services/accounts_service.py:859-922`). Those are necessary checks.

### Confirmed gaps

- No client idempotency key or request replay protection.
- No maximum order notional, daily order count/value, total account exposure, symbol exposure, inventory cap, leverage cap, daily loss, drawdown, or kill switch.
- No balance reservation across concurrent API requests.
- Market-order notional validation can use a current price without explicit age/depth/slippage bounds (`:908-915`).
- The code calculates `quantized_price` for validation but submits the original `price` (`:903-943`).
- Order placement returns the connector's submitted client order ID before exchange acceptance/finality (`:926-947`).
- Cancellation calls `connector.cancel(trading_pair="NA", ...)` and reports initiation, not exchange-confirmed cancellation (`:989-1013`).
- Error responses may include raw exception text, which can leak internal details.

### Required remediation

Add a durable order-intent layer with idempotency keys, authorization scope, account/symbol risk limits, fresh market-data requirement, price collars, expected slippage bound, balance reservation, and operation state. Submit quantized values only. Treat place/cancel as asynchronous operations with exchange-confirmed state and reconciliation deadlines.

### Acceptance criteria

Concurrent duplicate requests with one idempotency key create one exchange order. Requests beyond configured account/symbol/notional/loss limits fail before reaching the connector. Market orders fail closed when price or order-book data is stale. Cancel success is reported only after exchange confirmation or an explicit terminal-not-found result.

---

## F-09 — High: build and controller deployment are not reproducible

### Confirmed evidence

`hummingbot-api/environment.yml:5-37` leaves most dependencies unpinned and installs public `hummingbot` without a version at `:22-24`. Base images and application images use mutable tags such as `continuumio/miniconda3` and `latest`.

The custom build script:

- shallow-clones API branch HEAD and records only its short SHA (`hummingbot/Build_hummingbot_api_nonkyc.sh:390-399`);
- separately shallow-clones the Hummingbot/controller branch HEAD (`:279-297`);
- syncs controllers add/update-only and explicitly never deletes stale destination scripts (`:299-357`);
- labels API SHA and Hummingbot repo/branch, but not the Hummingbot commit SHA in the final image (`:553-562`);
- performs positive import checks but no complete test suite (`:532-606`);
- retags the artifact as mutable `hummingbot/hummingbot-api:latest` (`:608-610`).

Because API and Hummingbot/controller branch HEADs are fetched at different times, the branch can move between fetches. Stale destination controllers survive a removal upstream. The final deployment cannot be reconstructed from labels alone.

The controller trees are materially divergent. Across all Python files, 18 common paths differ. Among controller implementations after excluding examples and package initializers, only four of 19 common paths are byte-identical. The API lacks the Hummingbot fork's `range_inventory_ladder`, EMA regime-hold, and BB/RSI mean-reversion controllers; the API has two PMM variants absent from the fork.

CI concerns:

- API Docker workflow builds and pushes merged code but has no preceding test, dependency, secret, container, or provenance gate (`hummingbot-api/.github/workflows/docker_buildx_workflow.yml:1-43`).
- Hummingbot collection installation is allowed to fail, and the collection smoke job is nonblocking (`hummingbot/.github/workflows/ci-test.yml:19-25`).
- The scheduled NonKYC live contract test ends with `|| true` (`ci-live-contract.yml:22-26`).

### Required remediation

- A single immutable release manifest containing API full SHA, Hummingbot full SHA, controller-tree hash, dependency lock hash, base-image digests, schema version, build toolchain, and test result IDs.
- Pin all Python/Conda dependencies and container images; generate an SBOM and sign the artifact/provenance.
- Build API and bot runtime from exact SHAs in one pipeline.
- Make controller sync mirror an explicit allowlisted manifest and delete or quarantine stale files.
- Deploy only immutable image digests; never use `latest` in production.
- Gate build/push on unit, integration, migration, security, and deterministic-build checks.

### Acceptance criteria

Two clean builds of the same manifest produce equivalent source/controller hashes and a documented reproducibility result. The running container exposes the exact release manifest. A rollback selects a previous digest and matching database/controller schema, not a mutable tag.

---

## F-10 — High: sensitive operational artifacts are included in the archive

### Confirmed evidence

The Hummingbot archive contains:

- generated build output (~196 MB);
- instance bundles (~16 MB);
- live logs (~7 MB);
- caches, test logs, structured events, state files, and archived instances;
- `.password_verification` files;
- encrypted Kraken, MEXC, and NonKYC connector YAML values;
- controller state and diagnostic files containing balances, order reservations, pair names, bot IDs, and operational timestamps.

The connector values appear encrypted rather than plaintext. They remain sensitive because the password verifier is present and F-01 may have caused use of the trivial password `a`. The presence of these files in the supplied archive does not prove they were committed to Git because `.git` metadata was omitted.

### Required remediation

- Rotate all represented exchange credentials as an incident precaution.
- Confirm withdrawal permission is disabled; revoke any key with broader scope.
- Use dedicated keys per account/bot and IP restrictions where supported.
- Remove runtime, credential, state, logs, builds, archives, `.env`, verifiers, and caches from source distributions.
- If these artifacts ever existed in Git history, release archives, CI artifacts, object storage, or shared backups, scrub those locations and rotate again.
- Add automated secret scanning and an allowlist-aware packaging manifest.
- Build release source archives from Git-tracked files rather than zipping a working directory.

### Acceptance criteria

A clean source archive contains no connector YAML with secret fields, no password-verification file, no `.env`, no instance state, no live log, no build object, and no generated cache. Secret scanning runs on every commit and release artifact.

---

## F-11 — High: ladder accounting is defensive but not yet a reconciled fund ledger

### Strong behavior

The controller uses atomic state writes, wallet floors, reservation accounting, market-data pause, session limits, quantization/rule checks, and rich reconciliation diagnostics. Those controls likely prevented immediate oversell/overspend in the captured runs.

### Confirmed gaps

#### State ownership contention is warn-only

`range_inventory_ladder.py:1974-2018` states that two controllers sharing one state file corrupt each other's ledger, but deliberately logs and continues. It then overwrites the owner marker. Marker errors are swallowed.

This is incompatible with fail-closed live accounting. A duplicate controller, copied YAML, stale process, or wrong state path can create concurrent writers and irreconcilable managed-fund history.

#### Non-quote fees are not applied to the two-asset ledger

`range_inventory_ladder.py:2337-2392` records base-asset or third-asset fees in `alt_fees` but explicitly does not fold them into owned base/quote. The event is visible, but managed inventory and PnL remain overstated unless an external reconciliation corrects it.

#### Production-risky configuration overrides

The controller default blocks initialization with unavailable funds, but included live YAMLs enable `allow_initialize_with_unavailable_wallet_funds: true`. Included instance YAMLs also set `max_session_duration_hours: 66000`, approximately 7.5 years, while validation only enforces a positive value. This effectively disables session expiry despite the safety feature.

#### Passivity is not guaranteed on NonKYC

Where a venue lacks a strict post-only/LIMIT_MAKER type, downgrade to ordinary LIMIT makes maker-only behavior best-effort. On a thin or moving book, an apparently passive level can cross and take liquidity.

### Included runtime evidence

The captured NonKYC XMR log records an over-claim and wallet clamp:

- `instances/extracted/nonkyc/.../logs/logs_NONKYC_LADDER...log:3742` reports managed owned base above wallet total and an over-claim of approximately `10.206905` quote-equivalent.
- `:3743` reports that the sell budget was clamped to wallet availability.

Final diagnostic heartbeat reconciliation gaps were:

| Venue / pair | Diagnostic source line | Reconciliation gap in quote currency |
|---|---|---:|
| Kraken XPL-USD | `.../k_range_inventory_ladder_xpl_usd_auto_20260713_diagnostic_20260713-050303.jsonl:508` | `+1.2891339850` USD |
| Kraken XMR-USD | `.../range_inventory_ladder_xmr_usd_diagnostic_20260713-050303.jsonl:832` | `-6.883990533336` USD |
| NonKYC BELLS-USDT | `.../range_inventory_ladder_bells_usdt_auto_20260713_diagnostic_20260713-045806.jsonl:632` | approximately `0` USDT |
| NonKYC DASH-USDT | `.../range_inventory_ladder_dash_usdt_diagnostic_20260713-045806.jsonl:625` | `-1.38817782186` USDT |
| NonKYC SUN-USDT | `.../range_inventory_ladder_sun_usdt_diagnostic_20260713-045806.jsonl:515` | `-3.00326026` USDT |
| NonKYC XMR-USDT | `.../range_inventory_ladder_xmr_usdt_diagnostic_20260713-045806.jsonl:1055` | `-5.42508079` USDT |

These values do not by themselves prove a loss; they prove that the managed ledger and reconciliation reference did not close to zero at the captured endpoint. Therefore managed-fund growth and inventory PnL cannot be treated as audited performance.

### Required remediation

- Make owner contention a hard startup failure. Use a real interprocess lock/lease with generation and heartbeat, not only a sidecar marker.
- Do not overwrite an existing live owner marker.
- Include exchange account, connector, pair, controller ID, release hash, and instance generation in state identity.
- Book fees in every asset. For base fees, reduce owned base. For third-asset fees, maintain an explicit multi-asset fee ledger and value it using a timestamped, quality-checked conversion rate.
- Production validation must reject unavailable-fund initialization and cap session duration to a reviewed maximum.
- Require explicit post-only capability for a maker-only deployment; otherwise incorporate taker probability and taker fees into risk limits.
- Tie every ledger booking to a unique exchange trade ID and reconcile to exchange trade history, balances, deposits/withdrawals, manual trades, and external order holds.

### Acceptance criteria

- A second process using the same state identity cannot start.
- Restart/replay of all fills produces the same state hash and zero duplicate bookings.
- For a dedicated account/key with no external flows, the ledger closes to exchange balances within venue quantization and fully accounted fee tolerances for at least 30 consecutive paper days.
- Any unexplained gap over the configured tolerance hard-pauses new orders and pages an operator.

---

## F-12 — High: directional controllers can trade on stale data and lack validated predictive evidence

### Positive design

The EMA and mean-reversion controllers correctly try to avoid using a still-forming final candle. On sparse feeds they retain the latest bar only if it is already closed:

- EMA: `controllers/directional_trading/ema_regime_hold_v1.py:92-107`;
- mean reversion: `mean_reversion_bb_rsi_v1.py:148-165`.

The EMA controller uses a backward `merge_asof` for slow-regime alignment (`ema_regime_hold_v1.py:140-168`), avoiding forward lookup. The mean-reversion spread gate fails closed when bid/ask is missing or invalid (`mean_reversion_bb_rsi_v1.py:102-133`). Both implement explicit cooldown logic from executor timestamps, and mean reversion has a daily trade cap.

### Confirmed gaps

- Both calculate/log `bar_age_s` but do not enforce a maximum candle age (`EMA:174-195`; MR:206-236`). A days-old closed bar can still produce a signal on a sparse/disconnected feed.
- The spread gate has no bid/ask timestamp freshness, order-book depth, impact, or expected-slippage check.
- EMA configuration does not enforce `regime_ema_fast < regime_ema_slow`.
- Raw EMA slope in the mean-reversion controller is price-unit dependent, so thresholds are not portable across pairs or price regimes.
- Included YAMLs disable the manual kill switch and allocate `total_amount_quote: 300` without a strategy-level daily-loss/drawdown control.
- No walk-forward results, untouched final test, calibration, regime stability, feature ablation, or live shadow comparison artifact was supplied.

### Required remediation

- Add a hard maximum candle age, preferably expressed as a multiple of interval and an absolute ceiling. Signal must be zero and existing risk reduced when exceeded.
- Add quote timestamp/depth freshness and expected-impact checks.
- Normalize slope/volatility features and validate parameter relationships.
- Add strategy-level exposure, loss, drawdown, and stale-data circuit breakers.
- Use temporal cross-validation and walk-forward retraining/evaluation. Never tune on the final test period.
- Report performance by volatility, liquidity, spread, and trend regimes; include turnover, fill probability, latency sensitivity, and cost break-even.

### Classification

**Research only.** There is not enough evidence to justify paper deployment of these predictive controllers, and the current backtesting engine cannot provide that evidence.

---

## F-13 — Critical for inference: the backtesting service and engine are not valid promotion gates

### Shared mutable engine and task isolation

`hummingbot-api/services/backtesting_service.py:54-58` creates one `BacktestingEngineBase` instance for the whole service. Every synchronous and background task uses it (`:93-135`). The Hummingbot engine mutates controller, executors, provider data, and time during a run. Concurrent jobs can therefore contaminate one another.

Task state exists only in memory (`backtesting_service.py:27-37`). `max_tasks` cleans completed tasks but does not reject or constrain more active tasks (`:166-177`). There is no semaphore, worker queue, process isolation, CPU/memory quota, persistence, dataset identifier, controller hash, random seed, or cancellation cleanup contract.

### Data and execution realism

`hummingbot/strategy_v2/backtesting/backtesting_data_provider.py:70-113` fetches historical candles online at run time. Trading rules are loaded from the current connector (`:70-74`), not a historical venue-rule snapshot. Therefore the same nominal run can change as exchange data endpoints or current rules change.

`position_executor_simulator.py`:

- tests limit entry against candle close, then fills at that candle close (`:10-15`, `:37-40`), not at the limit price with queue/volume constraints;
- models no spread, order-book depth, latency, queue position, cancellation delay, partial fill, adverse selection, maker/taker determination, funding, or exchange outage;
- calculates take profit from close-based PnL but stop loss from candle low/high (`:43-70`), an inconsistent intrabar model;
- when take profit and stop loss occur at the same timestamp, checks TP first and labels the trade TAKE_PROFIT (`:71-80`), an optimistic tie break;
- charges a constant round-trip cost unrelated to actual order type/fill path.

### Metrics are mislabeled or biased

`backtesting_engine_base.py:274-315`:

- excludes zero-PnL executors from the position count and metrics;
- calculates cumulative drawdown from the current dataframe order without an explicit close-time sort in the metric block;
- defines “returns” as cumulative PnL divided by cumulative traded volume;
- labels mean/std of that series as `sharpe_ratio` (`:289-294`), which is not a time-series Sharpe ratio;
- sets profit factor to `1` when there are no losses (`:292-294`), rather than infinity/undefined with explicit handling.

The API then fills all processed-data NaNs with zero (`backtesting_service.py:135`), masking indicator warmup and missingness in the returned artifact.

### Impact

This engine can produce attractive, stable-looking metrics from fills that would not occur live. Optimistic intrabar resolution and close-price limit fills are especially dangerous for thin NonKYC markets and tight ladder/mean-reversion strategies. Concurrency makes even the same run potentially non-isolated.

### Required remediation

At minimum:

- one engine/process per job;
- immutable dataset snapshot with content hash, timezone, source, symbol mapping, gap/duplicate report, and exchange-rule snapshot;
- deterministic run manifest including code/controller hashes and seed;
- event-driven or conservative bar execution model with bid/ask spread, order price, high/low path ambiguity, queue/fill volume, partial fills, latency, cancellation delay, maker/taker fees, funding, min notional, tick/lot rules, and stale-market behavior;
- pessimistic or randomized intrabar handling with sensitivity bounds, never unconditional TP-first;
- correct periodic return series and risk metrics, with confidence intervals and regime slices;
- walk-forward evaluation and an untouched final holdout;
- independent paper-fill calibration against venue telemetry.

### Acceptance criteria

- Two identical manifests produce identical outputs and hashes.
- Concurrent jobs produce the same result as isolated jobs.
- Limit fills cannot occur unless market path reaches the price and available modeled volume/queue permits execution.
- Ambiguous same-candle TP/SL outcomes are reported as a range or resolved conservatively.
- Sharpe is based on a documented periodic return series with risk-free and annualization assumptions.
- Simulated fill rate, slippage, maker ratio, and cancellation latency are benchmarked against paper/live observations before using the engine for promotion.

### Classification

**Research sandbox only. No current backtest result should be used as evidence for paper or live readiness.**

---

## F-14 — High: NonKYC connector uncertainty is sometimes converted into success

### Positive behavior

Persistent bounded trade-ID deduplication, alternate fee extraction, external-order-hold accounting, reconnect orphan diagnostics, and opt-in orphan cancellation are all good additions.

### Confirmed risks

- Rate limits are explicitly estimates because the venue does not publish them (`nonkyc_constants.py:107-133`).
- Reconnect reconciliation sets `_orders_reconciled_after_reconnect = True` in `finally`, including when the reconciliation request fails (`nonkyc_exchange.py:232-299`). Downstream logic can therefore interpret failed reconciliation as completed.
- The post-reconnect balance-settling window eventually exits; uncertainty after timeout must not permit unsupported order sizing.
- Market-buy quantity semantics are explicitly marked unverified in code (`nonkyc_exchange.py:729-736`).
- `cancel_all` marks tracked orders successfully canceled when the per-symbol request returns without exception, without re-querying the exchange to prove the orders disappeared (`:1017-1042`).
- Orphan and missing-order handling is diagnostic rather than a durable ownership/recovery protocol.

### Required remediation

Use explicit reconciliation states such as `NOT_STARTED`, `IN_PROGRESS`, `SUCCEEDED`, and `FAILED`, retaining failure details and blocking new trading when state is not `SUCCEEDED`. Verify market-buy semantics with a documented, low-value exchange contract test before enabling market buys. Treat batch cancel response as initiation and poll open orders/order status to terminal state. Add controlled retry/backoff for 429/5xx and nonce/time errors, with unknown-placement reconciliation by client/exchange order identifiers.

### Acceptance criteria

A fault-injection suite must cover:

- order accepted but HTTP response lost;
- duplicate REST and WebSocket fills;
- out-of-order fills/status;
- reconnect with stale balances;
- cancel accepted but order remains open;
- 429, 5xx, timeout, malformed response, clock skew, and nonce rejection;
- API restart during every uncertain state.

No uncertain operation may be reported as confirmed success.

---

## F-15 — High: authentication transport, WebSocket credentials, and rate limiting are insufficient

### Confirmed evidence

- HTTP Basic Auth is used for all HTTP routers (`hummingbot-api/main.py:427-480`). The direct Docker image serves plain HTTP.
- Debug mode can bypass invalid credentials (`main.py:91-96`, `:442-449`).
- WebSocket authentication accepts Basic Auth, a base64 token in the query string, or plaintext username/password query parameters (`routers/websocket.py:23-57`, endpoint documentation at `:80-81` and `:161-162`). Query credentials can leak through browser history, reverse-proxy logs, observability tools, and copied URLs.
- WebSockets are accepted before authentication and have no repository-level connection/rate cap.
- The HTTP limiter is an in-memory, process-local map keyed by IP and path (`main.py:357-396`). It is not distributed, has no bounded key eviction, does not protect WebSockets, and only applies a special limit to paths beginning `/trading/place` or `/trading/cancel`.
- Gateway wallet setup submits a private key through the API (`routers/accounts.py:167-190`; `models/gateway.py:39-43`), increasing the importance of transport and logging guarantees.

### Required remediation

Use TLS/mTLS or an authenticated private reverse proxy; replace reusable Basic Auth with short-lived, scoped tokens or an identity provider; remove all query-string credentials; disable debug bypass in production builds; implement bounded distributed rate limiting and login throttling; audit all request/log instrumentation for secret redaction; isolate wallet key import into a local, one-time secure workflow.

### Acceptance criteria

No credential or private key appears in URLs, access logs, error traces, telemetry, or process arguments. Repeated authentication failure triggers throttling/alerting. WebSocket and HTTP authorization enforce the same scoped policy. Production cannot start with debug auth bypass enabled.

---

## F-16 — Medium/High: recovery metadata and identity constraints are incomplete

### Confirmed evidence

- `orders.client_order_id` is globally unique, not scoped by account/connector (`database/models.py:38`). This may collide across accounts or venues, depending on connector ID generation.
- Startup active-order query is capped at 1,000 (`order_repository.py:108-127`). Orders beyond the cap are not recovered by that path.
- Database order reconstruction hardcodes `leverage=1` and `PositionAction.NIL` (`unified_connector_service.py:1088-1128`). Perpetual order intent is not faithfully recovered.
- The reconstructed in-flight order restores filled base amount but not a full fill ledger, average quote, fee breakdown, or all venue-specific metadata.

### Required remediation

Use composite identities and store full order intent, venue metadata, leverage, position action, reduce-only flag, time-in-force, creation source, and request idempotency key. Paginate recovery until exhaustion. Validate recovered state against exchange positions, orders, and fills before permitting new trading.

---

## F-17 — Positive component with dependency risk: resume cannot be trusted until lifecycle truth is fixed

The copy-forward implementation has strong path, source, ledger, hash, and partial-copy guards. Keep this design. However, its notion of a gracefully stopped source depends on bot-run status and lifecycle records that the current stop-and-archive flow writes before shutdown is verified. Remediation must make the lifecycle state machine authoritative before resume source selection can be considered safe.

---

# 6. Research and validation assessment

## 6.1 Data lineage

The supplied backtesting path does not persist a complete dataset identifier or snapshot. Historical candles are fetched at run time, and current trading rules are fetched from connectors. Required lineage fields are missing from the run artifact:

- exchange/connector code hash;
- exact REST endpoint/query and retrieval timestamp;
- symbol mapping version;
- timezone and timestamp semantics;
- raw/cleaned dataset hash;
- duplicate/missing/out-of-order report;
- resampling and forward-fill rules;
- current versus historical trading-rule source;
- controller code/config hash;
- dependency and engine version;
- random seed and execution-model version.

A research result without these fields cannot be reproduced or audited.

## 6.2 Feature and target validity

The custom directional controllers are deterministic indicator strategies rather than fitted predictive models, so label leakage from a supervised target is not the main concern. The relevant risks are:

- using stale but technically closed candles;
- sparse-market bar construction and zero-volume behavior;
- regime alignment and warmup;
- price-unit-dependent feature thresholds;
- selecting thresholds against the same period used for evaluation;
- unmodeled spread, slippage, and fill probability dominating nominal signal edge.

No evidence was supplied for walk-forward selection, final holdout protection, multiple-testing correction, parameter-region stability, or regime-specific performance.

## 6.3 Microstructure realism

The largest live-vs-sim risks for the reviewed strategies are:

- thin-book spread variation;
- price impact and queue priority;
- post-only rejection/downgrade;
- partial fills and fill fragmentation;
- cancel latency and stale quotes;
- adverse selection around fills;
- maker/taker fee classification;
- alternate fee assets;
- venue rate limits and API uncertainty;
- manual/external orders and shared-account balances;
- order minimums and current/historical rule changes.

The backtester currently models almost none of these. For a ladder strategy, execution quality is the strategy. A candle-close simulator is not an adequate approximation.

## 6.4 Optimization / Optuna readiness

No complete Optuna study artifact or objective implementation was supplied in the reviewed snapshots. Before optimization is added or trusted, the objective should penalize:

- net return after realistic maker/taker fees, slippage, and funding;
- turnover and cancellation intensity;
- drawdown and tail loss;
- inventory concentration and time at risk;
- unfilled/partially filled orders;
- adverse-selection loss after fill;
- instability across walk-forward folds and regimes;
- sensitivity to small parameter perturbations;
- suspiciously strong or low-trade-count outcomes.

The final test period must never feed the sampler, pruner, early stopping, feature selection, or manual parameter revisions. Study metadata must persist sampler, pruner, search space, seed, objective version, dataset hash, code hashes, and every failed/pruned trial reason.

---

# 7. Controller and YAML deployment assessment

## 7.1 Range inventory ladder

**Current classification: paper-trading candidate only after platform blockers and ledger fixes; not live-ready.**

The controller is sophisticated and defensive, but live readiness requires:

- hard state ownership lock;
- exact fill-ID ledger and multi-asset fee accounting;
- reconciliation gap acceptance/stop policy;
- production profile forbidding unavailable-fund initialization;
- reviewed session-duration upper bound;
- verified post-only behavior or conservative taker modeling;
- isolated dedicated account/key with no manual trades;
- restart/replay and exchange-history reconciliation tests.

## 7.2 EMA regime-hold and BB/RSI mean reversion

**Current classification: research only.**

Required before paper deployment:

- hard stale-candle and stale-quote gates;
- walk-forward out-of-sample evidence;
- realistic execution model calibrated to the intended venue/pair;
- parameter sensitivity and regime stability;
- explicit account/symbol risk limits and loss circuit breaker;
- proof that the strategy retains positive expected value after conservative spread/slippage/fill assumptions.

## 7.3 YAML contract controls

A production YAML validation layer should enforce environment-specific constraints rather than only type validity. Examples:

- production forbids `allow_initialize_with_unavailable_wallet_funds: true`;
- production caps `max_session_duration_hours` to an approved value;
- `manual_kill_switch` or equivalent remote kill capability must be enabled;
- strategy amount cannot exceed account/symbol allocation policy;
- controller state path must be unique for `(account, connector, pair, strategy generation)`;
- maker-only strategies require connector post-only capability;
- stale-data thresholds, max exposure, max daily loss, and reconciliation tolerance are mandatory;
- connector, pair, tick/lot, minimum notional, and fee assumptions are snapshotted and validated before start.

Every deployed YAML should be stored with a SHA-256 hash, release manifest, creator/reviewer, account, connector, symbol, and deployment generation in PostgreSQL or an immutable artifact store.

---

# 8. Exact remediation sequence

## Phase 0 — Emergency containment

1. Stop public exposure. Remove/loopback-bind all Compose ports and verify from an external host.
2. Correct credential variable names, fail closed on defaults, and rotate API/config credentials.
3. Rotate all exchange keys represented in the archive; verify no withdrawal permission.
4. Disable arbitrary controller/script upload and arbitrary image pull/run until isolation and RBAC exist.
5. Disable `stop-and-archive-bot` on live accounts or force cancellation plus manual exchange verification.
6. Preserve current database/logs for forensic reconciliation before modifying credentials or state.
7. Compare every active exchange order and position with API/Hummingbot ownership records.

**Exit criterion:** no untrusted network can reach the stack; no default credential is effective; all current exchange orders have an identified owner and operator decision.

## Phase 1 — Control-plane security and lifecycle

1. Remove Docker socket from API; introduce least-privilege deployment worker.
2. Implement scoped identity/RBAC and step-up authorization.
3. Secure MQTT with TLS, per-bot identity, ACLs, durable operation IDs, and acknowledgments.
4. Replace stop/archive with durable quiesce-cancel-verify-drain-stop state machine.
5. Remove WebSocket query credentials and require secure transport.
6. Add tamper-evident audit events for order, credential, code, image, and lifecycle actions.

**Exit criterion:** credential compromise of a read/trade principal cannot produce code execution or host control; lifecycle success implies verified exchange state.

## Phase 2 — Persistence and recovery correctness

1. Fix fill-first idempotent persistence and VWAP.
2. Introduce versioned database migrations and backups.
3. Use composite order/trade identities and full order intent metadata.
4. Paginate recovery; reconcile orders, fills, positions, balances, and fees.
5. Make ladder state ownership exclusive and multi-asset accounting complete.
6. Add replay tooling that rebuilds state from immutable raw events and compares hashes.

**Exit criterion:** duplicate/reordered event chaos tests are idempotent; restart produces the same reconciled state; no unexplained accounting gap exceeds tolerance.

## Phase 3 — Reproducible release engineering

1. Pin API/Hummingbot/controller commits, dependencies, and base-image digests.
2. Produce lockfiles, SBOM, signed provenance, and immutable image digests.
3. Mirror controller manifests exactly and remove stale files.
4. Make CI tests, migrations, security scans, and container checks blocking.
5. Store release manifest with every bot run and YAML.

**Exit criterion:** a clean rebuild and rollback are reproducible from one manifest.

## Phase 4 — Research and simulator repair

1. Isolate backtest jobs and persist manifests/results.
2. Snapshot and validate historical data/rules.
3. Implement realistic and conservative execution.
4. Correct metrics and time-series validation.
5. Calibrate fills/slippage against paper telemetry.
6. Perform walk-forward and untouched-final-test evaluation.

**Exit criterion:** simulation output is deterministic, execution assumptions are empirically calibrated, and results remain stable under conservative stress.

## Phase 5 — Controlled paper trading

Run dedicated paper/shadow accounts with:

- one strategy per account/key where practical;
- low notional and zero withdrawal authority;
- full raw event capture;
- daily exchange/API/ledger reconciliation;
- forced restart, broker outage, API outage, stale feed, cancellation, and kill-switch drills;
- operator-reviewed incident log.

Suggested minimum acceptance window: **30 consecutive calendar days and at least 100 fills per venue/pair/controller**, whichever takes longer. Thin markets may require a longer period. This is a minimum evidence threshold, not a live certificate.

## Phase 6 — Candidate live review

Only after all prior gates pass, perform a separate live-readiness review using the exact immutable release, YAML, accounts, connector permissions, venue rules, monitoring, runbook, rollback, and incident response plan. Start with a deliberately small risk budget and predeclared stop criteria.

---

# 9. Acceptance checklist for engineering handoff

## Security and deployment

- [ ] Production effective Compose has no unapproved wildcard host port mappings.
- [ ] External scan confirms only the intended authenticated endpoint is reachable.
- [ ] Startup fails on default/weak API, config, broker, or database secrets.
- [ ] API runs non-root and has no raw Docker socket.
- [ ] Images are digest-pinned, signed, allowlisted, and resource constrained.
- [ ] Controller/script validation occurs in an unprivileged sandbox; API never imports untrusted source.
- [ ] RBAC separates read, trade, credential, code, and host actions.
- [ ] WebSocket credentials never appear in query strings.
- [ ] MQTT uses TLS, per-bot identities, and deny-by-default ACLs.

## Order lifecycle and persistence

- [ ] Place/cancel/stop operations have durable IDs and idempotency.
- [ ] Stop success requires zero owned open orders verified at the exchange.
- [ ] Duplicate fill replay cannot alter aggregate state after first insertion.
- [ ] Average fill price is true weighted VWAP.
- [ ] Fees in quote, base, and third assets are fully accounted.
- [ ] Active-order recovery paginates to exhaustion and restores complete metadata.
- [ ] Unknown placement/cancel outcomes remain UNKNOWN until reconciled.
- [ ] Database schema changes are versioned, transactional, backed up, and tested.

## Controller accounting

- [ ] State identity is unique and protected by an exclusive lease/lock.
- [ ] Production forbids initialization with unavailable funds.
- [ ] Session duration has a reviewed upper bound.
- [ ] Reconciliation gap beyond tolerance hard-pauses trading.
- [ ] Every ledger booking references a unique exchange trade ID.
- [ ] Exchange balances, fills, fees, deposits/withdrawals, and external orders reconcile daily.

## Research and backtesting

- [ ] Dataset snapshot has provenance, hash, timezone, quality report, and rule snapshot.
- [ ] Controller/config/code/dependency/execution hashes are stored with each run.
- [ ] Concurrent jobs are isolated and deterministic.
- [ ] Execution models spread, queue/fill probability, partial fills, latency, cancellation, and fees.
- [ ] Ambiguous intrabar outcomes are conservative or reported as bounds.
- [ ] Sharpe and drawdown use documented periodic returns and sorted timestamps.
- [ ] Walk-forward evaluation and untouched final holdout are enforced.
- [ ] Performance is stable across volatility, liquidity, spread, and trend regimes.

## Paper-trading promotion

- [ ] At least 30 days and 100 fills per venue/pair/controller with immutable release.
- [ ] Zero orphan orders.
- [ ] Zero duplicate fill aggregate mutations.
- [ ] Zero unowned/concurrent state-file writes.
- [ ] No unresolved reconciliation gap beyond tolerance.
- [ ] Restart, stale-data, broker-loss, exchange-error, and kill-switch drills pass.
- [ ] Simulated and paper fill/slippage distributions are compared and documented.
- [ ] Operator runbook, alerts, rollback, and incident ownership are tested.

---

# 10. Test and static-analysis record

| Check | Result | Interpretation |
|---|---|---|
| ZIP path/symlink safety | Passed | Archives extracted without unsafe entries. |
| Python compileall, API source | Passed | Syntax/import compilation only; not runtime assurance. |
| Python compileall, Hummingbot source | Passed | Syntax/import compilation only; generated Cython/build artifacts excluded. |
| `pytest -q tests/test_config.py` | 1 failed, 3 passed | Confirms credential environment mismatch. |
| API collection excluding live API | 86 collected; 8 module collection errors | Review environment lacked custom Hummingbot package; API is not independently hermetic. |
| Bandit, API | 1 high, 29 low | High is MD5 for change detection, not password security; manual review finds more material architectural risks than Bandit. |
| Bandit, custom HB subset | 0 high/medium, 18 low | Does not validate trading correctness or exchange semantics. |
| Full live connector tests | Not run | Intentionally not run without isolated credentials/account and explicit exchange test plan. |
| Docker/Compose runtime test | Not run | Static Compose behavior is sufficient to confirm retained port mappings; runtime hardening still requires deployment tests. |

---

# 11. Final assessment

The repositories show substantial effort toward restart safety, structured diagnostics, controller accounting, and copy-forward protection. The custom ladder controller in particular is more defensive than most strategy code reviewed in isolation. The system nevertheless combines too many authorities in one weakly authenticated process: exchange credentials, direct trading, arbitrary Python deployment, Docker host control, bot lifecycle, persistence, and wallet/Gateway functions.

The most dangerous defects are not hypothetical strategy-model weaknesses. They are deterministic platform behaviors:

- the installer does not configure the credentials the application reads;
- the private-network overlay does not remove public service bindings;
- a single credential reaches root-equivalent Docker/code execution;
- stop/archive can abandon orders by default;
- duplicate fills corrupt aggregate order state;
- research metrics can overstate performance through unrealistic and non-isolated simulation.

**Suitability:**

- **Research/source inspection:** acceptable with explicit caveats.
- **Backtesting:** research sandbox only; current performance statistics are not decision-grade.
- **Paper trading:** blocked until security, lifecycle, persistence, and reproducibility gates pass.
- **Live trading:** not suitable.
- **Potential future status:** candidate for a separate limited-live review only after all critical/high findings are remediated and a reconciled, fault-injected paper-trading acceptance record is complete.

