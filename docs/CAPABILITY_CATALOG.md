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
  recipes/*.yml
  models/*.yml
  engines/*.yml
  providers/*.yml
  provider_candidates/*.yml
```

Recipes contain abstract requirements:

- `required_model_capabilities`
- `max_model_len`
- `min_vram_gb`
- input/output contracts
- classification and allowed modes

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

Providers bind a real provider/GPU to a model and engine:

- `model_ref`
- `engine_ref`
- GPU / VRAM / region / trust profile
- official endpoint and lifecycle contract

Provider candidates are not production routing entries. They are a queue of plausible
provider/model/engine tuples that need endpoint credentials, official-adapter
conformance review, and billable live validation before they can be promoted into
`providers/*.yml`.

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

## Provider Tuple Audit

Use the provider tuple audit before promoting provider candidates or changing routing:

```bash
gpucall provider-audit --config-dir config
gpucall provider-audit --config-dir config --recipe text-infer-standard --live
```

The audit treats the recipe as the authority. It evaluates every active provider
tuple and every `provider_candidates/*.yml` tuple against recipe requirements,
model catalog declarations, engine catalog guarantees, provider/GPU metadata,
official adapter contracts, endpoint configuration, and exact live validation
artifacts. Candidate tuples that fit the recipe remain outside production routing
until they have endpoint/lifecycle configuration and billable validation evidence.

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
4. `gpucall-recipe-admin promote` creates an isolated promotion workspace containing the generated recipe and candidate provider YAML.
5. An administrator or automation fills provider-specific endpoint credentials and runs billable validation.
6. Only after validation evidence exists can the provider candidate be copied into `providers/*.yml` and made eligible for production routing.

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
