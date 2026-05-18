# Workload Contract Onboarding

`gpucall-migrate` owns the deterministic caller-side onboarding path. It does
not ask an LLM to judge quality. It turns static source observations and
sanitized run traces into a workload contract that declares caller-side success
metrics.

The intended flow is:

```bash
gpucall-migrate assess /path/to/app --source app-name
gpucall-migrate trace /path/to/app --command "uv run python -m app.canary" --backend baseline
gpucall-migrate profile /path/to/app --trace .gpucall-migration/workload-trace.json
gpucall-migrate draft-contract /path/to/app --profile .gpucall-migration/workload-profile.json --write-intake
gpucall-recipe-admin validate-draft --input .gpucall-migration/recipe-intake.json --config-dir config
gpucall-recipe-admin materialize --input .gpucall-migration/recipe-intake.json --config-dir config --output-dir config/recipes --accept-all
```

If a caller already has a representative baseline log or JSON artifact:

```bash
gpucall-migrate onboard /path/to/app --log-file baseline.log --backend baseline
```

For a candidate gpucall run:

```bash
gpucall-migrate trace /path/to/app --command "uv run python -m app.canary" --backend gpucall --output-dir .gpucall-gpucall
gpucall-migrate compare /path/to/app --contract .gpucall-migration/workload-contract.json --trace .gpucall-gpucall/workload-trace.json
```

## Boundary

The gateway must not infer subjective quality. The contract may only contain
deterministic metrics declared by the caller or derived from a baseline trace:

- `min_response_chars`
- `min_topics`
- `min_sources`
- `min_articles`
- `require_schema_success`
- `min_rss_matches`
- `min_rss_match_total`
- `max_no_auto_selectable_recipe`
- `max_http_422`
- `max_json_extract_failures`

The trace artifact stores only counts, lengths, booleans, durations, and a log
hash. It does not forward raw prompts, model outputs, URLs, DataRef URIs, or log
tails.

## Draft Grammar

Caller-side draft generation must overdeclare when uncertain. The caller owns
its business need, not provider selection, so the deterministic draft should
describe the largest honest workload envelope it can prove from code and
baseline traces. For example, RSS semantic matching and pairwise matching are
not generic `infer` workloads; they are explicit `rss_semantic_match` or
`pairwise_match` intents with large context budgets.

Administrator-side materialization then narrows or rejects deterministically.
Draft validation is a separate gate before recipe YAML is written. It must not
promote these draft shapes:

- missing, empty, generic, or task-equal intent such as `infer`
- `unknown_workload_*` intent without operator mapping
- missing context budget
- missing or unsupported output contract
- missing baseline quality metrics
- strict intake whose `draft_grammar.materialization_allowed` is false

This division is intentional. The caller is biased toward "dangerous-side"
overdeclaration, while the gateway administrator is biased toward deterministic
rejection or narrowing before production activation.

File-based inbox processing applies the same gate automatically:

1. submitted JSON lands in `inbox/`
2. `gpucall-recipe-admin validate-draft` semantics run first
3. weak drafts move to `inbox/failed` with a rejection report
4. accepted drafts proceed to admin review and materialization as draft recipe
   YAML

## Purpose

This is the C workstream:

- A is the fixed gpucall runtime plus baseline recipe/catalog defects.
- B is hand-driven external-system adaptation work.
- C compresses B into a deterministic product workflow.

The validation target for C is the pre-gpucall news-system worktree on macmini,
not the already migrated production checkout.
