# gpucall Capability Catalog

gpucall routing is not allowed to infer model ability from prompt text. Unknown caller workloads become routable only when four catalogs line up:

1. Recipe: what the workload requires.
2. Model: what a model declares it can do.
3. Engine: what the runtime can guarantee.
4. Execution tuple: account + surface + resource + worker + model + engine.

The gateway then requires live validation evidence before an administrator treats a new tuple as production-ready.

## Config Layout

```text
config/
  accounts/*.yml
  recipes/*.yml
  models/*.yml
  engines/*.yml
  surfaces/*.yml
  workers/*.yml
  candidate_sources/*.yml
  tuple_candidates/*.yml
  tuples/*.yml.example
```

Recipes use the caller-facing v3 DSL and contain workload requirements:

- `recipe_schema_version: 3`
- `intent`
- `context_budget_tokens`
- `resource_class`
- `latency_class`
- `quality_floor`
- `required_model_capabilities`
- input/output contracts
- classification and allowed modes

Recipes must not declare provider resource fields such as `gpu`,
`min_vram_gb`, or `max_model_len`. The loader derives the internal tuple
contract from the v3 workload fields.

Models contain semantic and resource declarations:

- `capabilities`
- `max_model_len`
- `min_vram_gb`
- `supported_engines`
- `input_contracts`
- `output_contracts`
- vision/guided-decoding/streaming support

Engines contain runtime guarantees:

- DataRef support
- multimodal support
- guided decoding support
- streaming support
- input/output contracts

Provider accounts, execution surfaces, and workers are split so one API account
can own multiple execution surfaces:

- `accounts/*.yml`: credential reference, API base, billing scope, provider family.
- `surfaces/*.yml`: execution surface, GPU / VRAM / region, price, stock state,
  trust profile, lifecycle fields, and endpoint or VM shape.
- `workers/*.yml`: model, engine, input/output/stream contracts, modes, target
  function or endpoint contract.

The loader joins `surfaces/*.yml` to `workers/*.yml` by explicit `worker_ref`.
The loader fails closed if a surface and worker are missing a counterpart or
disagree on account, adapter, or execution surface. The joined tuple is the
active execution surface eligible for production routing after policy,
validation, circuit, and cleanup checks.

Tuple candidates are not production routing entries. They are plausible
account/surface/resource/worker/model/engine combinations that need endpoint
credentials, official-contract review, and billable live validation before they
can be promoted into active `surfaces/*.yml` and `workers/*.yml`.

Candidate sources such as `candidate_sources/*.yml` are deterministic generators
for broad surfaces. For example, RunPod Serverless candidates are generated from
GPU, model, and worker-family matrices instead of maintaining one YAML file per
possible tuple. `tuple_candidates/*.yml` remains available for small explicit
candidate definitions and for compatibility with existing catalog tooling.

## Catalog DB

Build the SQLite catalog from config:

```bash
gpucall catalog build --config-dir config
```

Default path:

```text
$XDG_STATE_HOME/gpucall/capability-catalog.db
```

The DB is a deterministic materialization of YAML config. YAML remains the source
of truth; the DB is for review, inspection, and operational tooling. Active
tuples and candidate tuples are stored separately so unvalidated candidates
cannot enter production auto-routing.

## Execution Catalog

The execution catalog is the provider-independent view used for tuple generation.
It separates source facts by lifecycle so routing does not confuse primary data,
runtime observations, and derived decisions:

- Account: provider family, credential reference, billing scope, and API base. It never stores resolved secret values.
- Hardware catalog: normalized GPU SKU facts such as VRAM, architecture, and memory bandwidth.
- Execution surface: lifecycle, isolation, cleanup, network exposure, and cold-start class.
- Provider offering: account + execution surface + GPU SKU + region/network topology.
- Worker contract: input/output/stream contracts, model, engine, modes, and endpoint/function configuration.
- Capability claim: the compatibility assertion joining a resource and worker contract.
  It also carries the tuple's security tier, sovereignty boundary, dedicated GPU
  claim, TEE boot capability, attestation requirement, and key-release support.
- Pricing rule: account + resource billing terms such as fallback price,
  source, observation time, TTL, granularity, and minimum billable seconds.
- Live status overlay: TTL-scoped stock, price, endpoint, credential, health, and contract observations.
- Validation evidence: redacted billable artifact summary, artifact hash,
  pass/fail counts, observed latency, and optional attestation evidence hash.
- Tuple candidate: a derived account + surface + resource + worker + model + engine plan pinned to a snapshot id.
  Its `tuple_ref` includes the recipe-specific `recipe_fit` view when a recipe is supplied, so it is a candidate-view id, not a stable physical resource id.

Generate the current snapshot and deterministic tuple drafts:

```bash
gpucall execution-catalog snapshot --config-dir config
gpucall execution-catalog candidates --config-dir config --recipe text-infer-standard
gpucall execution-catalog snapshot --config-dir config --live
gpucall execution-catalog candidates --config-dir config --recipe text-infer-standard --live
```

The snapshot is a candidate-selection cache, not live truth. `routing_decision`
and `ExecutionPlan` are derived views calculated from the catalog and runtime
signals; they are not primary catalog entities. Production execution still
requires endpoint/lifecycle configuration, policy checks, billable live
validation for the exact tuple, and cleanup guarantees.

With `--live`, active tuples are revalidated against provider catalog APIs where
gpucall has an official validator. Snapshot rows and generated candidates carry
`live_catalog_status` so routing governance can distinguish a merely configured
tuple from a tuple blocked by today's catalog or stock evidence.

Live catalog processing is deterministic. Provider probes may fetch official
API/page data, but they only emit typed observations: `contract`, `endpoint`,
`credential`, `stock`, and `price`. The execution catalog then normalizes those
observations into `live_stock_state`, `configured_price_per_second`,
`live_price_per_second`, and the effective `price_per_second`. If live price is
unavailable, gpucall keeps the configured price and marks the live field null
instead of guessing.
Configured prices are fallback values, not live truth. They carry
`configured_price_source`, `configured_price_observed_at`, and
`configured_price_ttl_seconds`. Fresh live price observations are cached under
the gpucall state directory as TTL-scoped overlay evidence; strict budget policy
fails closed when the effective price is stale or unknown.

Runtime observations are deliberately split by freshness. Static hardware and
surface definitions are config/catalog facts. Slow-changing price and regional
terms enter pricing rules or TTL overlays. Hot capacity, endpoint, credential,
and health observations stay in live overlays and expire quickly. Raw provider
responses and raw validation logs are not embedded in the catalog; only redacted
findings, hashes, counts, timestamps, and latency summaries are exposed.
Each live overlay carries `next_revalidate_after` when it was observed with a
positive TTL. Exact tuple validation evidence carries `expires_at`. The
background validator planner uses those timestamps plus pricing rules to decide
which tuple smokes are due and which fit the operator's explicit validation
budget.

`cold_start_class` on an execution surface is a static lifecycle class, not a
runtime promise. Actual startup behavior is evaluated from validation evidence
latency summaries such as latest, p50, p99, and max observed wall seconds. A
router may use the static class for first-pass filtering, but production
eligibility must prefer fresh evidence when it exists.

`resources` and `workers` in the execution catalog are compatibility views.
They are emitted as immutable derived objects so older CLI paths and tests can
continue to inspect the catalog while the primary schema moves to normalized
accounts, hardware, surfaces, offerings, capability claims, pricing rules,
overlays, and evidence.

## Execution Tuple Audit

Use the execution tuple audit before promoting tuple candidates or changing routing:

```bash
gpucall tuple-audit --config-dir config
gpucall tuple-audit --config-dir config --recipe text-infer-standard --live
```

The audit treats the recipe as the authority. It evaluates every active joined
surface/worker tuple and every tuple candidate, whether explicit or generated
from `candidate_sources/*.yml`, against recipe requirements, model catalog
declarations, engine catalog guarantees, resource catalog metadata, official
execution contracts, endpoint configuration, and exact tuple validation
artifacts. Candidate tuples that fit the recipe remain outside production
routing until they have endpoint/lifecycle configuration and billable validation
evidence.
The audit also reports surface distribution so Modal function-runtime candidates,
RunPod managed endpoints, and Hyperstack VM routes are compared as different
official execution surfaces instead of pretending they are equivalent provider
wrappers.

## Admin Review

Caller submissions are reviewed against recipes, models, engines, execution
tuples, policy, and live validation artifacts:

```bash
gpucall-recipe-admin review \
  --input /opt/gpucall/state/recipe_requests/inbox/rr-....json \
  --config-dir config
```

If active tuples are insufficient, the report includes `required_execution_contract`.
If the candidate catalog contains tuples that satisfy that contract, the same
report includes `tuple_candidate_matches`. These matches are tuple promotion
plans, not routing entries.

Each candidate match carries:

- candidate name and source path
- model/engine/resource/contract tuple
- fit rank
- promotion actions

The automated path is:

1. Caller submission reaches the admin inbox.
2. `gpucall-recipe-admin review` writes a recipe candidate and computes `required_execution_contract`.
3. The reviewer matches that contract against the candidate catalog.
4. `gpucall-recipe-admin promote` creates an isolated promotion workspace containing the generated recipe, candidate tuple YAML, and split surface/worker YAML.
5. An administrator or automation fills execution-surface endpoint credentials and runs billable validation.
6. Only after validation evidence exists can the candidate be copied into active `surfaces/*.yml` and `workers/*.yml` and made eligible for production routing.

Promotion command:

```bash
gpucall-recipe-admin promote \
  --review /path/to/review.json \
  --candidate modal-h100-qwen25-vl-7b \
  --config-dir config \
  --work-dir /tmp/gpucall-promotion
```

Possible promotion decisions:

- `READY_FOR_ENDPOINT_CONFIGURATION`: generated tuple YAML exists, but execution-surface required fields such as endpoint id or Modal target are still missing.
- `READY_FOR_BILLABLE_VALIDATION`: generated config validates, but no matching live validation artifact exists.
- `VALIDATION_FAILED`: `--run-validation` was requested and the billable smoke failed.
- `VALIDATED_READY_TO_ACTIVATE`: matching validation exists and the provider can be activated.
- `ACTIVATED`: validated recipe and production tuple were copied into the active config directory.

Activation is refused unless the exact generated recipe/tuple/model/engine contract has a matching live validation artifact.

Possible decisions:

- `REJECT`: unsafe or malformed submission.
- `CANDIDATE_ONLY`: recipe can be drafted, but provider/model/engine/live evidence is missing.
- `READY_FOR_VALIDATION`: catalog has a plausible tuple, but live validation is missing.
- `READY_FOR_PRODUCTION`: catalog and live validation are present.
- `AUTO_SELECT_SAFE`: production-ready and low routing-shadowing risk.

The admin reviewer must not claim business quality from catalog metadata alone. Quality claims require live validation artifacts for the recipe/model/engine/resource/contract tuple.
