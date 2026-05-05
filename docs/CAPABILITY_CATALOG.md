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

## Admin Review

Caller submissions are reviewed against recipes, models, engines, providers, policy, and live validation artifacts:

```bash
gpucall-recipe-admin review \
  --input /opt/gpucall/state/recipe_requests/inbox/rr-....json \
  --config-dir config
```

Possible decisions:

- `REJECT`: unsafe or malformed submission.
- `CANDIDATE_ONLY`: recipe can be drafted, but provider/model/engine/live evidence is missing.
- `READY_FOR_VALIDATION`: catalog has a plausible tuple, but live validation is missing.
- `READY_FOR_PRODUCTION`: catalog and live validation are present.
- `AUTO_SELECT_SAFE`: production-ready and low routing-shadowing risk.

The admin reviewer must not claim business quality from catalog metadata alone. Quality claims require live validation artifacts for the recipe/model/engine/provider/GPU tuple.
