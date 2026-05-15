# gpucall Senior Code Audit

Date: 2026-05-15 JST
Scope: `/Users/tamotsu/Projects/gpucall` current worktree on `main`
Verdict: **P1 runtime safety issues patched in this pass; remaining production hardening is tracked as P2/P3 backlog**

This is a replacement for the first draft. The first draft was too broad and included weakly classified findings. This version keeps only findings with a concrete failure mode, code citation, and remediation path.

## 2026-05-15 Current Status Matrix

The table below is authoritative for current status. The original finding table is retained below as historical evidence and should not be read as the live backlog.

| ID | Severity | Status | Fix / Evidence |
|---|---|---|---|
| P1-01 | P1 | Fixed | Lazy ASGI app; `tests/test_p1_audit_regressions.py::test_importing_cli_does_not_create_runtime_state` verifies `import gpucall.cli` creates no state. |
| P1-02 | P1 | Fixed | Idempotency store opens in lifespan, not app import/construction. Covered by CLI import regression and `tests/test_app.py` startup tests. |
| P1-03 | P1 | Fixed | `gpucall/worker_contracts/io.py` validates declared `sha256` after S3/HTTP fetch. Regression: `test_s3_dataref_bytes_verify_declared_sha`. |
| P1-04 | P1 | Fixed | Artifact paths use the same hardened DataRef byte fetch. S3 mismatch regression covers the shared boundary. |
| P1-05 | P1 | Fixed | Generic/local HTTP(S) DataRefs require gateway-presigned refs, reject userinfo/redirect/private targets, and require explicit object host allowlists. Regressions in `tests/test_p1_audit_regressions.py` and `tests/test_providers.py`. |
| P1-06 | P1 | Fixed | Local DataRef worker fails closed when the worker API key is empty. Regression: `test_local_dataref_worker_requires_api_key`. |
| P1-07 | P1 | Fixed | SQLite budget reservation uses atomic check+reserve; Postgres ledger uses transaction-scoped advisory lock before insert. Regressions: `test_tenant_budget_reservation_is_atomic`, `test_postgres_tenant_budget_uses_transaction_lock_before_insert`. |
| P1-08 | P1 | Fixed | `GPUCALL_DATABASE_URL` now selects `PostgresTenantUsageLedger`. Regression: `tests/test_app.py::test_database_url_selects_postgres_stores`. |
| P1-09 | P1 | Fixed | SQLite/Postgres idempotency stores now have pending/completed state, atomic first-writer reserve, hash conflict handling, and pending release on terminal failure. Regressions: idempotency store tests plus `tests/test_app.py` idempotency suite. |
| P1-10 | P1 | Fixed | `GPUCALL_DATABASE_URL` now selects `PostgresArtifactRegistry`. Regression: `tests/test_app.py::test_database_url_selects_postgres_stores`. |
| P1-11 | P1 | Fixed | SQLite/Postgres artifact latest pointer uses real CAS semantics. Regression: `test_artifact_latest_compare_and_set_rejects_stale_expected_version`. |
| P1-12 | P1 | Fixed | `tests/test_app.py` copies `tests/fixtures/config` instead of mutable operator `config/`; fixture README documents intent. `tests/test_app.py` now passes with dirty repo config. |
| P2-08 | P2 | Backlog | `validate-config` still emits full tuple names. Current command remains valid; bounded summary should be a follow-up CLI contract change. |
| P2-09 | P2 | Fixed | FastAPI app version now reads `gpucall.__version__`, aligned with `pyproject.toml` at `2.0.9`. |
| P2-17 | P2 | Partially Fixed | Python SDK is bumped to `2.0.16`, docs/install examples now reference `gpucall_sdk-2.0.16`, and SDK tests cover cold-start-safe timeout/idempotency. Remaining backlog: formal release-process check and TS SDK parity. |
| P2-21 | P2 | Accepted Risk | Local DataRef worker now requires gateway-presigned refs and a host allowlist and rejects private/reserved resolved IPs. The local worker still does not pin the actual httpx connection to the validated IP; full DNS-rebinding pinning is backlog because it needs a custom transport without breaking TLS/SNI. |
| P3-04 | P3 | Fixed | `.gitignore` now ignores `.state/` and `.cache/`. |
| RB-01 | Release blocker | Fixed | `tests/test_config.py` no longer depends on dirty active `config/` recipe `auto_select` for long-route assertions; temp copies explicitly enable the required fixture recipes. `tests/test_worker_io.py` was updated for hardened DataRef fetch mocks. Full pytest: `522 passed, 1 skipped`. |
| RB-02 | Release blocker | Fixed | `doctor --live-tuple-catalog` now returns bounded failure without provider credentials instead of entering live provider catalog/network paths. Regression: `test_doctor_supports_live_tuple_catalog_flag_without_credentials`; full pytest: `522 passed, 1 skipped`. |
| SDK-01 | Release blocker | Fixed | Python SDK `2.0.16` forwards `idempotency_key` through sync/async infer, vision, and chat completions; default request timeout is now 600s and callers can override per request with `request_timeout`. Regressions: `tests/test_sdk.py` and `sdk/python/tests/test_client.py`. |

## 2026-05-15 Remediation Update

Patched and regression-tested in this pass:

- Import-time side effect: `gpucall.app:app` is now a lazy ASGI wrapper, and idempotency storage is opened in FastAPI lifespan rather than during `import gpucall.cli` or app construction.
- DataRef integrity: `worker_contracts/io.py` now verifies declared `sha256` after every supported fetch path, including `s3://`.
- Local DataRef worker: worker API key is required by default; arbitrary fetches now require gateway-presigned metadata, reject URI userinfo, and block private/loopback/link-local/reserved IP targets unless an explicit host allowlist is configured.
- Local DataRef worker fetch behavior: DataRef downloads are streamed with a byte cap; DataRef/OpenAI-compatible 429 responses are preserved as retryable 429; request-layer DataRef failures are converted to `TupleError`.
- Generic worker HTTP(S) DataRefs: URI userinfo is rejected, redirects are disabled, private/loopback/link-local/reserved IP targets are blocked, and negative declared `bytes` values are rejected before fetch.
- Tenant budget: SQLite tenant reservations now perform check-and-insert under `BEGIN IMMEDIATE`.
- Idempotency overwrite: SQLite and Postgres idempotency stores no longer overwrite an existing key on conflict.
- Artifact latest pointer: SQLite latest compare-and-set now updates under `BEGIN IMMEDIATE` and succeeds only when the expected version matches.
- Test fixture fragility: `tests/test_app.py` now copies a dedicated minimal fixture under `tests/fixtures/config` instead of mutable operator `config/`.
- Production DB parity: `GPUCALL_DATABASE_URL` selects Postgres implementations for tenant usage and artifact registry as well as jobs/idempotency/admission.
- Idempotency pending reservation: SQLite/Postgres stores now reserve pending keys before execution and reject duplicate concurrent starts.
- P3 cleanup: `.state/` and `.cache/` are ignored; FastAPI app version is aligned with package version.

## 2026-05-15 Release Blocker Update

Release blocker fixed:

- `tests/test_config.py::test_standard_config_routes_news_sized_prompts_to_long_recipes` now enables `text-infer-large`, `text-infer-exlarge`, and `text-infer-ultralong` only in the copied temp config fixture. The active repo `config/` dirty `auto_select: false` changes were not reverted.
- Related stale catalog expectations in `tests/test_config.py` were updated to current deterministic tuple chains without mutating operator config.
- `tests/test_validator_plan.py` now verifies candidate validation planning without requiring the active catalog to contain missing endpoint candidates.
- `tests/test_worker_io.py` now mocks the pinned opener and allowlisted DNS path required by hardened HTTP(S) DataRef fetch.

Verification:

```text
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_config.py::test_standard_config_routes_news_sized_prompts_to_long_recipes -q
.                                                                        [100%]
1 passed in 4.20s
```

```text
XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
EXIT:0
```

```text
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/fulltest uv run pytest -q -x
522 passed, 1 skipped in 549.39s (0:09:09)
EXIT:0
```

## 2026-05-16 Doctor Live Catalog Hang Fix

Release blocker fixed:

- Reproduced the hang with `timeout 30s env XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_config.py::test_doctor_supports_live_tuple_catalog_flag_without_credentials -q`, which exited `124`.
- `gpucall doctor --live-tuple-catalog` now detects an empty credentials set and emits a deterministic `live_tuple_catalog.ok=false` finding without invoking provider live catalog validators.
- The regression test now uses `subprocess.run(..., timeout=10)` and asserts the bounded skip message, so future hangs fail quickly.

Verification:

```text
timeout 30s env XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_config.py::test_doctor_supports_live_tuple_catalog_flag_without_credentials -q
EXIT:124
```

```text
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_config.py::test_doctor_supports_live_tuple_catalog_flag_without_credentials -q
.                                                                        [100%]
1 passed in 5.04s
EXIT:0
```

```text
XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
EXIT:0
```

```text
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/fulltest uv run pytest -q -x
522 passed, 1 skipped in 507.17s (0:08:27)
EXIT:0
```

## 2026-05-16 SDK Canary Contract Update

news-system canary reported `GPUCallColdStartTimeout` after 121.1s before the
gateway returned a response. The SDK source now closes that caller-side gap:

- Python SDK version: `2.0.16`.
- `idempotency_key` is forwarded by sync/async `infer`, `vision`, and chat completions.
- Default SDK request timeout is `600s`; per-request override is available as `request_timeout`.
- Public install examples now reference `gpucall_sdk-2.0.16-py3-none-any.whl`.

Verification:

```text
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_sdk.py -q
29 passed in 0.10s
EXIT:0
```

```text
cd sdk/python && XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache uv run pytest tests/test_client.py -q
30 passed in 0.03s
EXIT:0
```

```text
XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
EXIT:0
```

```text
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/fulltest uv run pytest -q -x
524 passed, 1 skipped in 510.02s (0:08:30)
EXIT:0
```

```text
cd sdk/python && XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache uv build --wheel
Successfully built dist/gpucall_sdk-2.0.16-py3-none-any.whl
SHA256: 22a5812d9079d7cf05bf2c9bb808dda7ddcfd3ff4b51b6b96fe78212d2991569
EXIT:0
```

Verification output from this pass:

```text
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_p1_audit_regressions.py -q
..................                                                       [100%]
18 passed in 0.54s
```

```text
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/test uv run pytest tests/test_app.py -q --maxfail=10
94 passed in 2.43s
```

```text
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_p1_audit_regressions.py tests/test_providers.py -q --maxfail=10
77 passed in 1.23s
```

```text
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/test uv run pytest tests/test_app.py -q -k idempotency --maxfail=10
8 passed, 86 deselected in 5.98s
```

```text
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/validate uv run gpucall validate-config --config-dir config
valid True
recipes 27
tuples 3233
```

Residual backlog after this pass:

- `validate-config` bounded summary remains P2 backlog; current command is valid but noisy for CI/operator output.
- SDK/docs/version matrix remains P2 backlog: gateway/Python SDK/TypeScript SDK versions intentionally differ but need an automated compatibility check.
- Local DataRef worker DNS pinning remains P2 accepted risk/backlog; mandatory host allowlist plus private/reserved-IP rejection closes the default SSRF path, but httpx still performs its own connection resolution.
- CLI/app/admin file-size split remains P3 backlog; no broad refactor was attempted in this correctness pass.

## 2026-05-15 Multi-AI Review Disposition

| Finding | Disposition | Action |
|---|---|---|
| SQLite legacy idempotency table with `NOT NULL` response columns breaks pending insert | Adopted | Added SQLite table rebuild migration and regression `test_sqlite_idempotency_migrates_legacy_not_null_schema`. |
| SQLite pending reservation takeover after 300s can duplicate long-running execution | Adopted | Removed takeover path; `reserve()` now uses `INSERT OR IGNORE` first-writer semantics. |
| Postgres tenant ledger `hashlib` import missing | Rejected as stale | Current `gpucall/tenant.py` imports `hashlib`; compileall passes. |
| Postgres idempotency migration can fail on old column layout | Adopted | Added `ADD COLUMN IF NOT EXISTS` and legacy column copy for `status_code`/`response_json`/`headers_json`. |
| Idempotency pending rows not released on non-`TupleError`/budget failures | Adopted | Added release on `GovernanceError`, `ValueError`, and unhandled exception paths after reservation. |
| Local worker DNS rebinding still possible between validation and httpx fetch | Accepted Risk / Backlog | Default path now requires gateway-presigned refs, explicit host allowlist, and public resolved IPs. Full connection pinning remains P2 backlog. |
| OpenAI facade lacks idempotency reservation | Backlog | The v2 task endpoints now have DB-backed pending reservation. OpenAI-compatible facade idempotency-by-header remains a separate P2 contract change. |

## Corrections From First Draft

- Active `config/` does **not** have zero infer auto-select coverage. It has 19 infer recipes, 2 with `auto_select: true`: `infer-summarize-text-light` and `text-infer-light`.
- The direct active-config sync request first hits auth unless `GPUCALL_ALLOW_UNAUTHENTICATED=1`. With unauth enabled it reaches execution and returned `503 PROVIDER_CAPACITY_UNAVAILABLE`, not `422`.
- The reproducible `422` is in `tests/test_app.py`: its `copy_config()` fixture deletes all recipes except `smoke-text-small.yml` and `text-infer-standard.yml`; the dirty `text-infer-standard.yml` currently has `auto_select: false`.
- A more serious issue found in the redo: generic worker `s3://` DataRef byte fetch returns bytes without validating the declared `sha256`.

## Evidence Snapshot

```text
git status --short
 M config/recipes/infer-author-recipe-draft.yml
 M config/recipes/infer-extract-json-draft.yml
 M config/recipes/infer-extract-json-efficient.yml
 M config/recipes/infer-rank-text-items-draft.yml
 M config/recipes/infer-rank-text-items-standard.yml
 M config/recipes/infer-summarize-text-draft.yml
 M config/recipes/infer-summarize-text-standard.yml
 M config/recipes/infer-translate-text-draft.yml
 M config/recipes/text-infer-exlarge.yml
 M config/recipes/text-infer-large.yml
 M config/recipes/text-infer-standard.yml
 M config/recipes/text-infer-ultralong.yml
 M config/surfaces/runpod-vllm-ampere48-qwen2-5-vl-7b-instruct.yml
 M config/surfaces/runpod-vllm-ampere80-qwen2-5-vl-7b-instruct.yml
 M config/workers/runpod-vllm-ampere48-qwen2-5-vl-7b-instruct.yml
 M config/workers/runpod-vllm-ampere80-qwen2-5-vl-7b-instruct.yml
?? .state/
?? TECH_DEBT_AUDIT.md
```

```text
import gpucall.cli side effect reproduction
before_exists False
before_entries []
after_exists True
after_entries ['idempotency.db', 'idempotency.db-shm', 'idempotency.db-wal']
```

```text
active config infer coverage
infer_recipes 19
auto_select_true 2
auto_select_false 17
true_names ['infer-summarize-text-light', 'text-infer-light']
```

```text
generic worker s3 DataRef integrity reproduction
returned b'tampered'
```

```text
pytest tests/test_app.py -q --maxfail=5
5 failed, 1 passed in 6.58s
FAILED test_sync_endpoint_returns_200: assert 422 == 200
FAILED test_sync_endpoint_auto_selects_recipe: assert 422 == 200
FAILED test_sync_endpoint_accepts_intent_without_caller_routing: assert 422 == 200
FAILED test_batch_endpoint_executes_sync_requests: assert 207 == 200
FAILED test_sync_endpoint_returns_structured_context_overflow: TypeError comparing int and None
```

```text
validate-config --config-dir config | tail -n 3
  ],
  "valid": true
}
```

```text
launch-check --profile static --config-dir config
gpucall launch-check: GO
blockers: 0
static_config_valid: True
tuple_live_validation: required=16 missing=16 gateway_live=0
```

## Historical P1 Findings

| ID | Component | Location | Finding | Impact | Fix |
|---|---|---|---|---|---|
| P1-01 | CLI/API construction | `gpucall/cli.py:26`, `gpucall/app.py:321-323`, `gpucall/app.py:1050` | Importing `gpucall.cli` imports `gpucall.app`, and module import constructs `app = create_app()`. `create_app()` opens idempotency SQLite immediately. | Config-only commands and pytest collection depend on writable runtime state. This produced `unable to open database file` and import-side DB creation. | Remove module-level stateful app. Put ASGI app in a thin `gpucall.asgi` module or create app lazily only for `serve`. |
| P1-02 | App lifecycle | `gpucall/app.py:295-323`, `gpucall/sqlite_store.py:81-99` | `create_app()` opens idempotency storage before lifespan. Runtime stores are split between pre-lifespan and lifespan-managed state. | Startup/shutdown ownership is unclear; `create_app().openapi()` and CLI reports mutate runtime state. | Move idempotency store into `build_runtime()` or app lifespan. OpenAPI generation must be side-effect-free. |
| P1-03 | DataRef integrity | `gpucall/worker_contracts/io.py:31-53`, `gpucall/worker_contracts/io.py:56-85` | Generic `fetch_data_ref_bytes()` verifies sha for HTTP(S), but returns immediately for `s3://` without sha verification. | Artifact/split-learning workers can consume tampered S3 bytes if ambient S3 credentials are enabled. Reproduced with fake boto3 returning `b'tampered'` despite mismatched `sha256`. | Always hash-check after scheme-specific fetch. Move the expected hash check after the `s3://` branch, as Modal worker already does at `gpucall/worker_contracts/modal.py:273-278`. |
| P1-04 | Artifact workflows | `gpucall/worker_contracts/artifacts.py:23-27`, `gpucall/worker_contracts/artifacts.py:76-84` | Artifact and split-learning paths call `fetch_data_ref_bytes()` directly. Because P1-03 skips S3 sha validation, these high-trust workflows are exposed. | The artifact manifest can record actual bytes after accepting tampered input, but it does not enforce the caller/gateway declared `sha256`. | Fix P1-03 and add artifact tests for S3 `sha256` mismatch. |
| P1-05 | Local DataRef worker | `gpucall/local_dataref_worker.py:110-118` | Local worker fetches arbitrary HTTP(S) URI without requiring `gateway_presigned`, host allowlist, or private-IP blocking. | SSRF path. Reproduced with a mocked fetch to `http://169.254.169.254/latest/meta-data` returning inline result. | Require gateway-issued DataRefs or explicit allowlist. Block loopback/link-local/RFC1918 by default. |
| P1-06 | Local worker auth | `gpucall/local_dataref_worker.py:37`, `gpucall/local_dataref_worker.py:44-47`, `gpucall/execution_surfaces/local_runtime.py:393-423` | Worker API key is optional. Empty `GPUCALL_LOCAL_DATAREF_WORKER_API_KEY` means no auth header required, and the adapter also sends no auth when env is empty. | If this worker is bound beyond loopback, anyone reaching it can trigger fetch+OpenAI-compatible calls. | Fail startup unless worker key exists, except explicit `GPUCALL_LOCAL_DATAREF_DEV_INSECURE=1` and loopback bind. |
| P1-07 | Tenant budget | `gpucall/tenant.py:170-180`, `gpucall/tenant.py:66-74` | Budget enforcement reads spend, compares, then inserts reservation in separate calls. | Concurrent requests can overspend daily/monthly budget. | Implement atomic reservation in one DB transaction. SQLite: `BEGIN IMMEDIATE`; Postgres: row/advisory lock or aggregate counter. |
| P1-08 | Production DB consistency | `gpucall/app.py:122-145`, `gpucall/app.py:276-290` | Jobs/idempotency/admission switch to Postgres, but tenant usage remains SQLite. | Multi-container production shares jobs/admission but not quota state. Tenant budget can be bypassed per container. | Add Postgres tenant ledger and select it with `GPUCALL_DATABASE_URL`. |
| P1-09 | Idempotency semantics | `gpucall/app_helpers.py:406-427`, `gpucall/app_helpers.py:429-469`, `gpucall/postgres_store.py:176-209` | Execution lock is process-local. Store `set()` overwrites on conflict. Cross-process same idempotency key can execute twice; different-body conflict can be lost if both miss lookup first. | Idempotency is not an enterprise guarantee under multi-worker deployment. | Add DB-backed idempotency reservation state: pending/completed with request hash checked in an atomic insert. |
| P1-10 | Artifact registry | `gpucall/app.py:255`, `gpucall/artifacts.py:11-23`, `gpucall/artifacts.py:86-114` | Artifact registry is always SQLite even when the gateway uses Postgres for jobs/idempotency/admission. | Artifact lineage/latest pointers are local to one container/host. | Add Postgres artifact registry for production DB mode. |
| P1-11 | Artifact compare-and-set | `gpucall/artifacts.py:38-56` | `compare_and_set_latest()` reads current version then performs unconditional upsert. Two concurrent writers that both observed the expected version can both succeed logically, with last writer winning. | Artifact latest pointer is not a real CAS. This matters for train/fine-tune/split-infer chains. | Use `UPDATE ... WHERE artifact_chain_id=? AND version=?` or serializable transaction. Return success only if one row changed. |
| P1-12 | Test hermeticity | `tests/test_app.py:55-83`, `config/recipes/text-infer-standard.yml:1-5` | Tests derive fixtures from mutable repo `config/` and then delete most recipes. Current dirty config makes the basic app suite fail. | CI and local tests are coupled to operator config changes. This hides product regressions and creates false failures. | Move canonical test config to `tests/fixtures/` or construct minimal config in code. Do not copy mutable production config. |

## P2 Findings

| ID | Component | Location | Finding | Impact | Fix |
|---|---|---|---|---|---|
| P2-01 | Config validation | `gpucall/config.py:311-329`, `gpucall/compiler.py:201-214` | `validate_config()` only validates recipes that already have `auto_select: true`. A config with no auto-selectable recipe for a production task can still be valid. | Operators can ship a syntactically valid but unroutable public task surface. | Add route coverage checks for declared production scope: `infer`, `vision`, required modes/intents, and expected profile. |
| P2-02 | Error context | `gpucall/compiler.py:201-214`, `gpucall/compiler.py:616-618`, `tests/test_app.py:186-206` | When no auto-select recipes exist, `largest_auto_recipe_model_len` is `None`. Tests compare it with `int` and fail with `TypeError`. | Failure artifacts are awkward for deterministic callers and tests. | Return `0` or explicit `null` contract and update tests; do not mix numeric comparison semantics with `None`. |
| P2-03 | Rate limit | `gpucall/app.py:326-331`, `gpucall/app.py:417-430` | Rate limiting is process-local memory. | Effective rate limit multiplies by worker/container count. | Use shared admission/Postgres counters, or explicitly document one-process rate enforcement. |
| P2-04 | Credentials permissions | `gpucall/credentials.py:22-40` | `load_credentials()` reads credentials without enforcing mode `0600` or owner sanity. | Violates the project’s own secret-file rule; world-readable credentials would still be accepted. | Reject or warn loudly on group/world-readable credentials in production mode. |
| P2-05 | Credentials parse errors | `gpucall/credentials.py:25-36` | YAML parse/load errors are swallowed with a warning and execution continues. | A corrupted credentials file can silently degrade provider/auth behavior. | For runtime commands, make credential parse failure fatal unless command is an explicit diagnostic. |
| P2-06 | Credentials lost update | `gpucall/credentials.py:43-73`, `gpucall/app.py:218-227` | `save_credentials()` read-modify-writes without a file lock. Trusted bootstrap can lose concurrent tenant keys. | Concurrent onboarding can overwrite another tenant key. | Add `flock`/lockfile around load-modify-replace, or move tenant keys to DB/secret manager. |
| P2-07 | Trusted bootstrap boundary | `gpucall/app.py:403-404`, `gpucall/app.py:563-571` | `/v2/bootstrap/tenant-key` bypasses auth and relies on `request.client.host` allowlist. Behind a reverse proxy, the client may be the proxy address. | Misconfigured proxy can turn trusted bootstrap into broad key minting. | Require explicit proxy trust config; otherwise reject bootstrap when `client.host` is a proxy/load balancer address. |
| P2-08 | CLI output contract | `gpucall/cli.py:488-503` | `validate-config` prints full recipes/runtimes/tuples list. Current output is 3274 lines. | Not automation-friendly; makes CI logs and operator diagnosis noisy. | Default to summary counts plus `valid`; provide `--json --verbose` for full lists. |
| P2-09 | Release metadata | `pyproject.toml:6-7`, `gpucall/app.py:321` | Package version is `2.0.9`, FastAPI app version is hardcoded `2.0.1`. | `/openapi.json` and runtime metadata can mislead clients/operators. | Source app version from package metadata. |
| P2-10 | Middleware fragility | `gpucall/app.py:352-397` | Oversize middleware consumes body then mutates private Starlette fields `_receive` and `_stream_consumed`. | Framework upgrade risk; private fields are not stable API. | Replace with ASGI receive wrapper middleware. |
| P2-11 | Dispatcher retry classification | `gpucall/dispatcher.py:244-262` | Unexpected adapter exceptions are converted to retryable `PROVIDER_ERROR`. | Local programming/config bugs can be retried as provider-temporary failures. | Treat unexpected local exceptions as non-retryable unless adapter marks them retryable. |
| P2-12 | JSON schema errors | `gpucall/dispatcher.py:689-694` | Strict JSON schema validation catches every `Exception` and maps it to retryable malformed output. | Bad schema/config/import errors can look like model output failures. | Catch `jsonschema.ValidationError` separately; treat `SchemaError` and unexpected errors as operator errors. |
| P2-13 | Request contract looseness | `gpucall/domain.py:387-391`, `gpucall/domain.py:400` | Tools/functions/stream options/metadata are broad dicts. | OpenAI-compatible contract drift can enter the core domain instead of staying at facade normalization. | Add typed bounded models or validate unknown fields at the facade boundary. |
| P2-14 | Provider params | `gpucall/domain.py:653` | `provider_params` is `dict[str, Any]` for all adapters. | Provider-specific security/cost knobs have no schema-level validation. | Add adapter-specific parameter schemas during config load. |
| P2-15 | Python SDK upload memory | `sdk/python/gpucall_sdk/client.py:210-224` | `upload_file()` reads entire file into memory, then `upload_bytes()` uploads it. | Large DataRef workflows can double memory pressure. | Stream hash and upload, or spool bounded chunks with deterministic sha/size verification. |
| P2-16 | Python SDK transport | `sdk/python/gpucall_sdk/client.py:197-224` | Presign POST uses injected `httpx.Client`; object PUT uses global `httpx.put`. | Tests and production hooks/proxies cannot consistently control transport. | Use the same client or an explicit upload transport. |
| P2-17 | SDK version drift | `pyproject.toml:6-7`, `sdk/python/pyproject.toml:6-7`, `sdk/typescript/package.json:1-8`, `README.md:223`, `docs/SDK_DISTRIBUTION.md:41-59` | Python SDK source/docs are aligned at `2.0.16`; TypeScript SDK remains `2.0.8` and a formal release-process compatibility check is still missing. | Release consumers can still miss TS/Python parity drift without CI. | Add a version matrix and a docs check that fails stale wheel URLs. |
| P2-18 | TypeScript SDK contract | `sdk/typescript/src/index.ts:16`, `sdk/typescript/src/index.ts:85-100`, `sdk/typescript/src/index.ts:128-145` | TS SDK supports only string chat content, limited roles, no timeout/abort option, generic errors. | It is behind the Python SDK and OpenAI-compatible facade. | Mark source-only/non-production or generate it from the same contract as Python. |
| P2-19 | Source SDK shim | `gpucall_sdk/client.py:7-19`, `docs/SDK_DISTRIBUTION.md:91` | Root source tree has a dynamic `gpucall_sdk` shim even though gateway and SDK are separate artifacts. | Local tests can pass while packaged gateway correctly lacks SDK. | Remove shim from default import path or test installed artifacts separately. |
| P2-20 | Python audit tooling | dependency audit command output | `pip-audit` was not reproducible: uv venv has no pip; external pip-audit runner crashed in `ensurepip`. | CVE audit cannot be claimed green. | Commit a deterministic `uv export` + known runner audit script and run it in CI. |

## P3 / Maintainability Findings

| ID | Component | Location | Finding | Fix |
|---|---|---|---|---|
| P3-01 | CLI size | `gpucall/cli.py:73-90`, file length 2928 LOC | Parser and command implementation live in one large module. | Split command groups and keep `gpucall.cli` as dispatch only. |
| P3-02 | API size | `gpucall/app.py:295-330`, file length 1050 LOC | App factory owns lifecycle, middleware, auth, idempotency, rate limit, routes, metrics. | Extract middleware and route modules after P1 lifecycle fix. |
| P3-03 | Admin CLI size | `gpucall/recipe_admin.py:46-70`, file length 1679 LOC | Admin parser and workflows are dense. | Split materialize/review/promote/watch into command modules. |
| P3-04 | Local generated state | `.gitignore:12-14` | Ignores `state/`, `cache/`, `audit/`, but not `.state/`; audit created untracked `.state/`. | Add `.state/` and `.cache/` if local XDG-in-repo usage is supported. |
| P3-05 | Package manager drift | `sdk/typescript/package.json:1-13`, `sdk/typescript/package-lock.json` | Project rules prefer `pnpm`; TS SDK has npm lock. | Either add `pnpm-lock.yaml` or document the TS SDK exception. |
| P3-06 | Worker text fallback | `gpucall/worker_contracts/io.py:94-97`, `gpucall/worker_contracts/modal.py:249-254` | Non-text DataRef bodies can become hex text. | Hard-reject non-text for text contracts unless a recipe explicitly expects hex. |
| P3-07 | Default local worker target | `gpucall/local_dataref_worker.py:34-37` | Defaults to `127.0.0.1:8000/v1`, model `local-model`, API key `local`. | Require explicit base URL/model outside dev mode. |
| P3-08 | Config/template drift | `config/recipes/infer-summarize-text-standard.yml:1-6`, `gpucall/config_templates/recipes/infer-summarize-text-standard.yml:1-6` | Active config and template disagree on `auto_select`. | Add drift report distinguishing operator override from accidental divergence. |

## Priority Fix Sequence

1. Fix DataRef integrity first: `worker_contracts/io.py` must validate `sha256` for all schemes and artifact paths need regression tests.
2. Remove import-time app side effects: no DB open from `import gpucall.cli` or `create_app().openapi()`.
3. Make test fixtures hermetic: stop deriving tests from mutable `config/`.
4. Move tenant budget, artifact registry, and idempotency reservations into shared DB semantics for Postgres mode.
5. Harden local DataRef worker: mandatory auth, gateway-presigned refs, URI allowlist/blocklist.
6. Add release/SDK/version checks after runtime correctness is stable.

## Commands Run

```text
env XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache GPUCALL_STATE_DIR=/private/tmp/gpucall-import-sideeffect-20260515-1816 uv run python -c '... import gpucall.cli ...'
env XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache GPUCALL_STATE_DIR=/Users/tamotsu/Projects/gpucall/.state/audit-route-coverage uv run python -c '... load_config(Path("config")) ...'
env XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache uv run python -c '... fetch_data_ref_bytes(fake s3 ref) ...'
env XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache uv run python -c '... run_dataref_openai_request(mock metadata URL) ...'
env XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache GPUCALL_STATE_DIR=/Users/tamotsu/Projects/gpucall/.state/audit-pytest-maxfail uv run pytest tests/test_app.py -q --maxfail=5
env XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache GPUCALL_STATE_DIR=/Users/tamotsu/Projects/gpucall/.state/final-validate-x uv run gpucall validate-config --config-dir config
env XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache GPUCALL_STATE_DIR=/Users/tamotsu/Projects/gpucall/.state/launch-check-x uv run gpucall launch-check --profile static --config-dir config
env XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache uv run python -m compileall -q gpucall sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
npm run build
npm audit --audit-level=moderate
XDG_CACHE_HOME=/Users/tamotsu/Projects/gpucall/.cache uv export --format requirements-txt --all-extras --no-hashes --output-file /private/tmp/gpucall-audit-requirements.txt
pip-audit -r /private/tmp/gpucall-audit-requirements.txt
```

## What Passed

```text
uv run python -m compileall -q gpucall sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
# no output; exit code 0
```

```text
npm run build
> @gpucall/sdk@2.0.8 build
> tsc -p tsconfig.json
```

```text
npm audit --audit-level=moderate
found 0 vulnerabilities
```

```text
uv run gpucall security scan-secrets --config-dir config
{
  "findings": [],
  "ok": true
}
```

## What Did Not Pass

```text
uv run pytest tests/test_app.py -q --maxfail=5
5 failed, 1 passed in 6.58s
```

```text
pip-audit -r /private/tmp/gpucall-audit-requirements.txt
subprocess.CalledProcessError: Command '['.../bin/python3.13', '-m', 'ensurepip', '--upgrade', '--default-pip']' died with <Signals.SIGABRT: 6>.
```

Full pytest was not completed in this audit. Earlier full run reached roughly one third of the suite with multiple failures and stopped producing output; the pytest process was terminated. No live provider or billable validation was run.

## 2026-05-16 Product RC Test Update

Status: Conditional Go for release-candidate packaging/static gateway validation. No remaining local/static P1 blocker was reproduced after the fixes below. Live provider/object-store canary remains environment-dependent and was not claimed green.

Release blockers fixed in this pass:

| ID | Severity | Status | Finding | Fix | Verification |
|---|---|---|---|---|---|
| RB-03 | P1 | Fixed | `gpucall doctor --live-tuple-catalog --config-dir config` could hang indefinitely when provider credentials existed and the live provider catalog call did not return. | `gpucall/cli.py` now runs live tuple catalog lookup in a bounded worker process with `GPUCALL_LIVE_TUPLE_CATALOG_TIMEOUT_SECONDS` defaulting to 15s, and reports a deterministic failure finding instead of hanging. | `uv run pytest tests/test_config.py::test_doctor_live_tuple_catalog_lookup_is_bounded tests/test_config.py::test_doctor_supports_live_tuple_catalog_flag_without_credentials -q` -> `2 passed`; product command returned exit 0 with `"reason": "live tuple catalog check timed out after 15s; skipped bounded live lookup"`. |
| RB-04 | P1 | Fixed | Source-tree Python SDK import failed because root `gpucall_sdk` shim could not resolve canonical SDK subpackage `gpucall_sdk.openai_contract`. | `gpucall_sdk/__init__.py` extends package search path to the canonical SDK source package before importing the shim client. | `uv run pytest tests/test_sdk.py::test_python_sdk_import_smoke_from_source_tree -q` -> `1 passed`; isolated SDK wheel smoke imported `GPUCallClient`, `AsyncGPUCallClient`, and default timeout `600.0`. |

Product test commands and outcomes:

```text
XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
# exit 0, no output

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/product-full uv run pytest -q -x
# 526 passed, 1 skipped in 508.85s (0:08:28)

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/product-validate uv run gpucall validate-config --config-dir config
# "valid": true

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/product-doctor-live uv run gpucall doctor --config-dir config --live-tuple-catalog
# exit 0; bounded live tuple catalog finding emitted after 15s instead of hanging

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/product-static uv run gpucall launch-check --profile static --config-dir config
# gpucall launch-check: GO; blockers: 0

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/product-tuple uv run gpucall tuple-audit --config-dir config
# exit 0

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/product-cost uv run gpucall cost-audit --config-dir config
# exit 0

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/product-cleanup uv run gpucall cleanup-audit --config-dir config
# "ok": true

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/product-gateway GPUCALL_TENANT_API_KEYS=default:product-test-secret uv run gpucall serve --config-dir config --host 127.0.0.1 --port 18088
# started on 127.0.0.1:18088; /healthz -> {"status":"ok"}; /readyz -> {"status":"ready"}; /openapi.json exposed 17 paths

XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_p1_audit_regressions.py tests/test_worker_io.py -q
# 23 passed in 0.67s

XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_p1_audit_regressions.py -q -k 'postgres or database or idempotency or artifact'
# 8 passed, 10 deselected

GPUCALL_POSTGRES_PASSWORD=product-test XDG_CACHE_HOME=$PWD/.cache docker compose config --quiet
# exit:0

cd sdk/typescript && ./node_modules/.bin/tsc -p tsconfig.json
# exit 0, no output

XDG_CACHE_HOME=$PWD/.cache uv build
# Successfully built dist/gpucall-2.0.9.tar.gz and dist/gpucall-2.0.9-py3-none-any.whl

cd sdk/python && XDG_CACHE_HOME=$PWD/../../.cache uv build --wheel
# Successfully built dist/gpucall_sdk-2.0.16-py3-none-any.whl

XDG_CACHE_HOME=$PWD/.cache uv run gpucall security scan-secrets --config-dir config
# {"findings": [], "ok": true}
```

Observed non-blockers / accepted risks from product smoke:

| Area | Status | Evidence | Classification |
|---|---|---|---|
| Active gateway config | Accepted Risk | `uv run python -m uvicorn gpucall.app:app ...` without `GPUCALL_CONFIG_DIR` read `~/.config/gpucall/policy.yml` and failed validation; `uv run gpucall serve --config-dir config ...` started correctly. | Operator config isolation issue, not release blocker for repo-config RC. |
| Live sync/API execution | Accepted Risk | With active repo config, auto-selected `local-author-ollama`; SDK live smoke returned `GPUCallColdStartTimeout`, async job failed with `PROVIDER_CAPACITY_UNAVAILABLE`, and OpenAI facade returned structured 422 for no auto-selectable recipe when metadata intent did not match. | Execution surface readiness/live provider canary required before production Go. |
| Object store canary | Backlog | DataRef integrity/fetch tests passed; live object-store credentials were not exercised. | Environment-dependent canary. |
| Docker Postgres runtime | Backlog | Docker available and compose config valid when `GPUCALL_POSTGRES_PASSWORD` is supplied; containers were not started in this pass. Postgres/idempotency/artifact focused tests passed with fake/mock semantics. | Real Postgres smoke still required before production deployment. |
| TypeScript package manager | Backlog | `pnpm` was not installed, `pnpm-lock.yaml` absent, `package-lock.json` present; local `./node_modules/.bin/tsc -p tsconfig.json` passed. | Existing P3-05 remains open. |

multi-ai-review result:

```text
Gemini: reported broader CLI risks, including macOS fork risk in bounded live-catalog worker and unrelated existing CLI memory/pagination issues.
Codex: reviewed changed diff only and reported "problemなし".
Decision: no additional patch. Gemini's changed-line fork concern is a portability risk but not reproduced in CLI/tests; current worker is bounded and full pytest/product doctor passed. Broader CLI findings map to existing P3 CLI-size/pagination backlog, not this release-blocker patch.
```

## 2026-05-16 Live Readiness Canary Update

Status: Conditional Go remains. RC checkpoint candidate is `rc-audit-product-static-conditional-go`; no commit was created because the worktree contains mixed audit/product fixes, generated artifacts, and existing dirty operator `config/` changes that must not be staged without explicit file selection.

Static/local checks reconfirmed:

```text
pwd
/Users/tamotsu/Projects/gpucall

git branch --show-current
main

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/live-readiness uv run gpucall validate-config --config-dir config
# "valid": true
```

Readiness and active route facts:

```text
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/live-readiness uv run gpucall readiness --config-dir config --intent infer_text
# recipe_count: 1
# recipe: null; eligible_tuple_count: 0
# classification: negative check only; infer_text is not a canonical recipe intent in this config

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/live-readiness uv run gpucall readiness --config-dir config --intent summarize_text
# recipe_count: 3
# infer-summarize-text-light auto_select: true, eligible_tuple_count: 49, live_ready_tuple_count: 49

XDG_CACHE_HOME=$PWD/.cache uv run python -c '... compile minimal infer and summarize_text ...'
# intent None -> recipe text-infer-light, selected local-author-ollama
# intent summarize_text -> recipe infer-summarize-text-light, selected local-author-ollama

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/live-readiness uv run gpucall doctor --config-dir config --live-tuple-catalog
# live_tuple_catalog.ok: false
# reason: live tuple catalog check timed out after 15s; skipped bounded live lookup
```

Gateway live smoke:

```text
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/live-gateway GPUCALL_TENANT_API_KEYS=default:live-canary-secret uv run gpucall serve --config-dir config --host 127.0.0.1 --port 18088
# /healthz -> {"status":"ok"}
# /readyz -> {"status":"ready"}
# /openapi.json -> gpucall v2.0 2.0.9, paths 17, openai True, readiness True
# /v2/readiness/intents/short_text_inference -> text-infer-light auto_select True; first_live_ready local-author-ollama
```

SDK / API live canaries:

```text
SDK sync infer, request_timeout=20
# elapsed: 20.01
# exception: GPUCallColdStartTimeout
# message: gpucall request timed out; this may be normal cold-start latency and is not a provider circuit-breaker signal

SDK async infer, poll_timeout=35
# submit: ok, state QUEUED, selected_tuple local-author-ollama
# poll: state FAILED
# provider_error_code: PROVIDER_CAPACITY_UNAVAILABLE
# error: tuple execution failed (PROVIDER_CAPACITY_UNAVAILABLE)

POST /v1/chat/completions
# error.code: PROVIDER_CAPACITY_UNAVAILABLE
# gpucall_failure_artifact.failure_kind: provider_temporary_unavailable
# retryable: true
# fallback_eligible: true
# caller_action: retry_later_or_wait_for_gpucall_fallback
```

DataRef / object store:

```text
env presence
# AWS_ACCESS_KEY_ID absent
# AWS_SECRET_ACCESS_KEY absent
# AWS_REGION absent
# GPUCALL_OBJECT_STORE_BUCKET absent

POST /v2/objects/presign-put
# {"detail":"object store is not configured"}

XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_p1_audit_regressions.py tests/test_worker_io.py -q
# 23 passed in 0.76s
```

Docker Postgres smoke:

```text
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
# client=29.4.3 server=29.2.1

GPUCALL_POSTGRES_PASSWORD=product-test docker compose config --quiet
# exit 0

GPUCALL_POSTGRES_PASSWORD=product-test GPUCALL_API_KEYS=live-canary-secret docker compose -p gpucall-product-smoke up -d postgres gpucall
# postgres healthy
# gpucall healthy

curl http://127.0.0.1:18088/healthz
# {"status":"ok"}

curl http://127.0.0.1:18088/readyz
# {"status":"ready"}

curl http://127.0.0.1:18088/openapi.json
# gpucall v2.0 2.0.9 17

POST /v2/tasks/async
# state QUEUED; selected_tuple local-author-ollama

GET /v2/jobs/c7ad27b9a92f432dbe80ae17821bdc36
# state FAILED
# provider_error_code PROVIDER_CAPACITY_UNAVAILABLE

docker compose -p gpucall-product-smoke down
# containers and network removed; volume was not removed
```

RunPod / worker readiness:

```text
credentials presence
# runpod ['api_key', 'endpoint_id']
# aws []
# modal []
# hyperstack ['api_key']

XDG_CACHE_HOME=$PWD/.cache uv run gpucall seed-liveness --help
# --budget-usd BUDGET_USD is required

runpod tuple inventory
# runpod_tuple_count 1689
```

Classification:

| Area | Status | Evidence | Classification |
|---|---|---|---|
| Product code / static gateway | Go | validate-config green; gateway starts; OpenAPI, health, ready pass. | RC checkpointable. |
| Active infer route | Production blocker | Deterministic compile selects `local-author-ollama` first for minimal infer and summarize_text. Runtime fails/does not answer. | Live execution surface readiness, not code import/startup bug. |
| SDK sync | Production blocker | `GPUCallColdStartTimeout` at 20s with no gateway response body. | Caller timeout/readiness issue. |
| SDK async | Production blocker | Submit succeeds, final job fails with `PROVIDER_CAPACITY_UNAVAILABLE`. | Gateway fail-closed correctly; no successful production tuple. |
| OpenAI facade | Production blocker | Structured `PROVIDER_CAPACITY_UNAVAILABLE` with failure artifact, retryable/fallback/caller_action. | Facade contract works; no live tuple success. |
| Object store/DataRef live | Environment-gated blocker | `object_store.yml` absent and AWS/object-store env absent; presign returns `object store is not configured`. | Must configure object store before production traffic. |
| Docker Postgres | Go for startup | Compose project `gpucall-product-smoke` starts Postgres and gateway healthy; API reaches async execution. | DB startup path works; live tuple still fails. |
| RunPod billable canary | Production blocker | Credentials present, but `seed-liveness` requires `--budget-usd`; no budget was specified in this task. | Must run explicit low-budget canary before production Go. |

No code changes were made in this live-readiness pass. The only file updated was this audit log.
