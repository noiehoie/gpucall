# gpucall Capability Catalog

gpucall routing is not allowed to infer model ability from prompt text. Unknown caller workloads become routable only when four catalogs line up:

1. Recipe: what the workload requires.
2. Model: what a model declares it can do.
3. Engine: what the runtime can guarantee.
4. Provider/GPU: where the engine/model tuple can run.

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
  provider_candidates/*.yml
  providers/*.yml.example
```

Recipes use the caller-facing v2 DSL and contain workload requirements:

- `recipe_schema_version: 2`
- `intent`
- `context_budget_tokens`
- `resource_class`
- `latency_class`
- `quality_floor`
- `required_model_capabilities`
- input/output contracts
- classification and allowed modes

Recipes must not declare provider resource fields such as `gpu`,
`min_vram_gb`, or `max_model_len`. The loader derives the internal provider
contract from the v2 workload fields.

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

The loader joins `surfaces/*.yml` and `workers/*.yml` by `provider_name` and
fails closed if a surface and worker are missing a counterpart or disagree on
account, adapter, or execution surface. The joined tuple is the active provider
surface eligible for production routing after policy, validation, circuit, and
cleanup checks.

Provider candidates are not production routing entries. They are a queue of plausible
provider/model/engine tuples that need endpoint credentials, official-adapter
conformance review, and billable live validation before they can be promoted into
active `surfaces/*.yml` and `workers/*.yml`.

## Catalog DB

Build the SQLite catalog from config:

```bash
gpucall catalog build --config-dir config
```

Default path:

```text
$XDG_STATE_HOME/gpucall/capability-catalog.db
```

The DB is a deterministic materialization of YAML config. YAML remains the source of truth; the DB is for review, inspection, and operational tooling. Active providers and candidate providers are stored separately so unvalidated candidates cannot enter production auto-routing.

## Execution Catalog

The execution catalog is the provider-independent view used for tuple generation.
It separates provider accounts, execution surfaces, and workers:

- Provider account: credential and billing scope such as `runpod`, `modal`, or `hyperstack`.
- Resource snapshot: GPU, region, VRAM, price, and configured/candidate stock state.
- Worker contract: input/output/stream contracts, model, engine, modes, and endpoint/function configuration.
- Tuple candidate: account + surface + resource + worker + model + engine, pinned to a snapshot id.

Generate the current snapshot and deterministic tuple drafts:

```bash
gpucall execution-catalog snapshot --config-dir config
gpucall execution-catalog candidates --config-dir config --recipe text-infer-standard
```

The snapshot is a candidate-selection cache, not live truth. Production execution
still requires endpoint/lifecycle configuration, policy checks, billable live
validation for the exact tuple, and cleanup guarantees.

## Provider Tuple Audit

Use the provider tuple audit before promoting provider candidates or changing routing:

```bash
gpucall provider-audit --config-dir config
gpucall provider-audit --config-dir config --recipe text-infer-standard --live
```

The audit treats the recipe as the authority. It evaluates every active joined
surface/worker tuple and every `provider_candidates/*.yml` tuple against recipe requirements,
model catalog declarations, engine catalog guarantees, provider/GPU metadata,
official adapter contracts, endpoint configuration, and exact live validation
artifacts. Candidate tuples that fit the recipe remain outside production routing
until they have endpoint/lifecycle configuration and billable validation evidence.
The audit also reports surface distribution so Modal function-runtime candidates,
RunPod managed endpoints, and Hyperstack VM routes are compared as different
official execution surfaces instead of pretending they are equivalent provider
wrappers.

## Admin Review

Caller submissions are reviewed against recipes, models, engines, providers, policy, and live validation artifacts:

```bash
gpucall-recipe-admin review \
  --input /opt/gpucall/state/recipe_requests/inbox/rr-....json \
  --config-dir config
```

If active providers are insufficient, the report includes `required_provider_contract`.
If `provider_candidates/*.yml` contains tuples that satisfy that contract, the same
report includes `provider_candidate_matches`. These matches are promotion plans,
not routing entries.

Each candidate match carries:

- candidate name and source path
- model/engine/provider tuple
- fit rank
- promotion actions

The automated path is:

1. Caller submission reaches the admin inbox.
2. `gpucall-recipe-admin review` writes a recipe candidate and computes `required_provider_contract`.
3. The reviewer matches that contract against `provider_candidates`.
4. `gpucall-recipe-admin promote` creates an isolated promotion workspace containing the generated recipe, candidate provider YAML, and split surface/worker YAML.
5. An administrator or automation fills provider-specific endpoint credentials and runs billable validation.
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

- `READY_FOR_ENDPOINT_CONFIGURATION`: generated provider YAML exists, but provider-specific required fields such as endpoint id or Modal target are still missing.
- `READY_FOR_BILLABLE_VALIDATION`: generated config validates, but no matching live validation artifact exists.
- `VALIDATION_FAILED`: `--run-validation` was requested and the billable smoke failed.
- `VALIDATED_READY_TO_ACTIVATE`: matching validation exists and the provider can be activated.
- `ACTIVATED`: validated recipe/provider were copied into the active config directory.

Activation is refused unless the exact generated recipe/provider/model/engine tuple has a matching live validation artifact.

Possible decisions:

- `REJECT`: unsafe or malformed submission.
- `CANDIDATE_ONLY`: recipe can be drafted, but provider/model/engine/live evidence is missing.
- `READY_FOR_VALIDATION`: catalog has a plausible tuple, but live validation is missing.
- `READY_FOR_PRODUCTION`: catalog and live validation are present.
- `AUTO_SELECT_SAFE`: production-ready and low routing-shadowing risk.

The admin reviewer must not claim business quality from catalog metadata alone. Quality claims require live validation artifacts for the recipe/model/engine/provider/GPU tuple.
