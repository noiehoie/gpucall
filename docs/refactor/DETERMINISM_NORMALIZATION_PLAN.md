# Determinism Normalization Plan

Author: architect (Fable). Implementer: Sonnet.
Goal: make gpucall's control logic match its own invariant —
"same input + same catalog + same evidence => same decision" — and remove
duplicated / fragile control logic, WITHOUT changing what any healthy request
routes to today.

This plan is derived from a 6-way audit of the control path. Every item below
is either (a) a real determinism defect, or (b) a normalization/duplication
smell that could cause silent divergence under future edits. Nothing here
changes the *meaning* of routing for a request whose inputs, catalog, and
evidence are unchanged.

## HARD CONSTRAINTS (do not violate)

1. **No behavior change for healthy requests.** A request that routes to tuple
   X today must still route to X after every item, given the same catalog and
   the same evidence files. The only permitted observable change is: (a) ties
   that were resolved nondeterministically now resolve deterministically, and
   (b) float-fragile gates now resolve on the safe side. If you think an item
   changes routing for a normal request, STOP and leave that item; note it.
2. **No inference in control.** Do not add any LLM/heuristic/classification.
   These are pure refactors of deterministic rule code.
3. **Preserve fail-closed.** Never make an unknown/stale/unvalidated/over-budget
   state pass. If unsure whether a change weakens a gate, keep the stricter side.
4. **Tests stay green.** `uv run pytest` must pass after every item. If an item
   legitimately changes a test's expectation (only for the tie/FP cases above),
   update the test IN THE SAME COMMIT and explain why in the commit body.
5. **Minimal change.** Extract helpers and add tiebreak keys; do not restructure
   modules, rename public functions, or add abstraction beyond what each item
   names. Match surrounding style.
6. **Scope = this repo's code + tests only.** Do NOT bump the version, tag,
   build wheels, deploy, or touch any gateway host. Stop at "code normalized,
   full suite green." Release is the operator's separate step.

## WORK ORDER

Do items in this order. Each item = one focused commit (message shape below).
Run the item's focused tests after writing it; run the full suite before moving
to the next item only if the item touched shared control code (I1, I2, I6, I7).

Commit message shape (end every commit body with):
```
Refs: docs/refactor/DETERMINISM_NORMALIZATION_PLAN.md item <ID>
Co-Authored-By: Claude Sonnet <noreply@anthropic.com>
```

---

## I1 — [VIOLATION] Deterministic tiebreak for evidence file selection

**Problem.** Route-validation evidence is selected by scanning `*.json` sorted
by mtime descending, first-wins per (tuple, recipe, mode) key. When two
artifacts for the same key have equal mtime, which one wins is decided by
filesystem glob order — nondeterministic across runs/hosts. If the two differ
in `passed` / `config_hash` / `official_contract_hash`, a route's accept/reject
gate can flip between runs. This directly breaks the invariant.

**Sites (verify each; the pattern is `sorted(candidates, key=lambda item: item[0], reverse=True)` over `(mtime, path)`):**
- `gpucall/validation_evidence.py` — `load_route_validation_evidence` (~L66-99)
- `gpucall/validation_evidence.py` — `load_route_validation_statuses` (~L114-152)
- `gpucall/execution_catalog.py` — `_validation_summary` (~L954-965): `sorted(rows, key=<ended_at/started_at str>)` then `rows[-1]`
- `gpucall/tuple_promotion.py` (~L437): glob-by-mtime match selection

**Fix.** Add the filename as a stable secondary sort key so equal-primary-key
records resolve identically every run, preserving "newest wins":
- validation_evidence.py both functions: change the candidate sort key from
  `item[0]` (mtime) to `(item[0], item[1].name)`, keeping `reverse=True`.
  Note: with reverse=True this makes the filename tiebreak descending too;
  that is fine — it just has to be *stable and total*. (Filenames are
  timestamped `YYYYMMDDTHHMMSSZ-...json`, so descending filename == newest.)
- execution_catalog.py `_validation_summary`: sort key becomes
  `(<the existing timestamp string>, <path string>)` before taking `rows[-1]`.
  Use the artifact's path/filename that is already available on each row.
- tuple_promotion.py match: apply the same `(mtime, name)` secondary key.

**Behavior.** Changes only the equal-mtime tie (previously nondeterministic).
Normal single-artifact and distinct-mtime cases are unchanged.

**Tests.** Add `tests/test_validation_evidence.py::test_equal_mtime_tiebreak_is_deterministic`:
write two artifacts for the SAME (tuple, recipe, mode) with the SAME mtime
(use `os.utime` to force equal mtimes) but different `passed`/`config_hash`;
assert `load_route_validation_evidence` and `load_route_validation_statuses`
return the SAME record across repeated calls, and specifically the one whose
filename sorts last (newest by timestamped name). Do not weaken any existing
validation_evidence test.

---

## I2 — [DUPLICATION] Single stream-required-fields predicate

**Problem.** `gpucall/routing.py` `tuple_route_rejection_reason` contains the
stream-required-fields loop TWICE, verbatim: once in the explicit
`mode is ExecutionMode.STREAM` branch (~L307-315) and once in the
`mode is None` + tuple-declares-STREAM branch (~L320-329). Same loop body over
`descriptor.stream_required_fields` with the same `is_configured_target`
target-field special-case. If the check changes, one copy will be updated and
the other won't — the same tuple could be accepted on the no-explicit-mode path
and rejected on the explicit-STREAM path.

**Fix.** Extract a module-private helper in routing.py:
```python
def _stream_required_field_rejection(descriptor, tuple) -> str | None:
    required_fields = descriptor.stream_required_fields if descriptor is not None else {}
    for field, reason in required_fields.items():
        value = getattr(tuple, field, None)
        if field in {"target", "stream_target"}:
            if not is_configured_target(value):
                return reason
        elif not value:
            return reason
    return None
```
Call it from both branches (fetch `descriptor = adapter_descriptor(tuple)` once
per branch as today, or reuse). Keep the surrounding branch conditions and the
separate `stream_contract == "none"` / mode-intersection checks exactly as they
are — only the duplicated loop body moves into the helper.

**Behavior.** Byte-for-byte identical logic; pure extraction. No routing change.

**Tests.** Existing routing/config tests must stay green. Add
`tests/test_routing.py` (or extend existing) case asserting a tuple missing a
required stream field is rejected identically whether `mode=STREAM` is passed
explicitly or inferred from `mode=None` with STREAM in both tuple.modes and
recipe.allowed_modes.

---

## I3 — [DUPLICATION] Single effective-timeout helper

**Problem.** `min(request.timeout_seconds or recipe.timeout_seconds, self.policy.max_timeout_seconds)`
appears verbatim 3× in `gpucall/compiler.py` (L181, L490, L645). A policy change
must be made in all three or decisions diverge.

**Fix.** Add a private method on the compiler:
```python
def _effective_timeout(self, request, recipe) -> int:
    return min(request.timeout_seconds or recipe.timeout_seconds, self.policy.max_timeout_seconds)
```
Replace all three inline expressions with `self._effective_timeout(request, recipe)`.
Mirror the existing `self._required_model_len(...)` extraction pattern.

**Behavior.** Identical value; pure extraction.

**Tests.** Full compiler suite stays green (no new test needed, but confirm
`tests/test_compiler.py` passes).

---

## I4 — [DUPLICATION] Single MIME-prefix predicate

**Problem.** `gpucall/compiler.py` checks `allowed_mime_prefixes` /
`allowed_inline_mime_prefixes` in two places with re-written (not shared) logic:
`_recipe_rejection_reason` (~L345-357, returns a reason string) and
`_validate_request_against_recipe` (~L409-420, raises GovernanceError). A change
to prefix matching (e.g. case handling) must be made in both.

**Fix.** Extract the shared matching into module-level helper(s) that return
`str | None` (the rejection reason, or None if allowed), e.g.
`_mime_prefix_rejection(request, recipe) -> str | None` and, if the inline vs
input-ref distinction is separate, `_inline_mime_prefix_rejection(...)`. Have
`_recipe_rejection_reason` return the reason and
`_validate_request_against_recipe` raise `GovernanceError(reason)` from the same
helper. Preserve the EXACT current matching semantics (same prefixes, same
startswith behavior, same fields) — this is extraction, not a rule change.

**Behavior.** Identical accept/reject; only the code location is unified.

**Tests.** Compiler suite green. Add one case if none exists: a request whose
content_type violates `allowed_mime_prefixes` is rejected both at recipe
selection and at validation with the same reason.

---

## I5 — [DUPLICATION] Billing-model classification single source

**Problem.** `gpucall/costing.py` `_billing_model` (~L179-192) classifies
adapters/surfaces into billing models with its own `if adapter == ... /
startswith("runpod") / surface is ...` ladder, independent of the adapter
descriptors in `gpucall/execution/registry.py`. A new adapter can register a
descriptor correctly yet silently fall through to `generic_request_runtime`
billing, giving wrong cost estimates and wrong cost-policy gates, with no error.

**Fix (minimal, no descriptor schema change).** Keep `_billing_model` as the
single classifier but make its coverage *enforced by test* so drift is caught:
1. Leave `_billing_model` as the one source of truth (do not duplicate its
   logic anywhere else; if any other module re-derives a billing model, route
   it through `_billing_model`).
2. Add `tests/test_costing.py::test_every_registered_adapter_has_explicit_billing_model`:
   iterate `registered_adapter_names()` (from execution.registry), build a
   minimal `ExecutionTupleSpec` per adapter with its descriptor's
   `execution_surface`, call `_billing_model`, and assert the result is NOT the
   `generic_request_runtime` fallback for any adapter that is `production_eligible`
   in its descriptor. This freezes the mapping: adding a production adapter
   without teaching `_billing_model` fails CI.
3. Add a one-line comment above `_billing_model` pointing to that test.

Do NOT add a `billing_model` field to the descriptor in this pass (larger
change, touches every adapter registration) — the test-enforced single source
is sufficient and lower-risk.

**Behavior.** No runtime change; adds a guard test.

---

## I6 — [FP] Safe zero comparison in price freshness

**Problem.** `gpucall/price_freshness.py` (L18, L33) uses float `== 0.0` to
detect the "local-free" (zero-cost) branch that yields `PriceFreshness.FRESH`.
A computed value that is epsilon-off zero would miss the branch. (It falls to
UNKNOWN, i.e. the safe direction, so this is fragile-but-safe.)

**Fix.** Replace `float(x) == 0.0` with `float(x) <= 0.0` at both sites (cost is
never legitimately negative; `<= 0.0` cleanly captures zero and any FP-underflow
toward zero). Keep the rest of the freshness logic identical.

**Behavior.** For any real config (`cost_per_second: 0` / positive prices),
identical. Only an epsilon-negative artifact changes, and toward the correct
"free" classification.

**Tests.** Add `tests/test_price_freshness.py` case: a tuple with
`cost_per_second: 0.0` is FRESH (local-free); a tiny positive price is NOT
treated as free. Keep existing price tests green.

---

## I7 — [FP] Deterministic ranking group boundary

**Problem.** `gpucall/compiler.py` `_rank_by_fit_then_observations` (~L456)
groups tuples by `fit_key != current_key`, where `fit_key`'s 6th element is a
float (`budget_reservation_usd`). Two tuples whose reservations differ only by
FP rounding land in separate groups; coincidental bit-equality merges them. The
group boundary is FP-fragile.

**Fix.** Quantize the float key element so grouping is stable. In
`_tuple_fit_key`, round the budget-reservation element to a fixed precision
before returning it, e.g. `round(self._tuple_budget_reservation(...), 6)`
(6 decimals = sub-micro-dollar; finer than any real price distinction). This
keeps the SAME ordering for genuinely different costs and makes near-equal
costs group deterministically. Do NOT change the key's *position* or the
*meaning* (still budget_reservation, still ascending = cheaper first).

**Behavior.** Ordering for distinct costs unchanged; only near-FP-equal costs
now group/tiebreak deterministically (falling through to `registry.rank` then
name, as designed).

**Tests.** Add `tests/test_compiler.py` case: two tuples with identical fit
attributes and budget reservations equal to 6 decimals are ranked in a stable,
name-deterministic order across repeated `compile()` calls.

---

## I8 — [MAGIC] Name the control constants

**Problem.** `gpucall/compiler.py` hard-codes control thresholds inline:
`success_rate >= 0.5` (reliability-tier boundary, ~L515 / in
`_observed_reliability_tier`) and `min(quality_failure_count, 99)` (penalty cap,
~L508 / in `_route_quality_penalty`). These are deterministic but unnamed.

**Fix.** Promote to named module-level constants at the top of compiler.py with
a short comment each, e.g.
`RELIABILITY_SUCCESS_RATE_THRESHOLD = 0.5` and
`ROUTE_QUALITY_PENALTY_CAP = 99`, and reference them. Do NOT move them into
policy YAML in this pass (that is a schema change with wider blast radius); a
named constant satisfies "named source" without altering behavior.

**Behavior.** Identical values; readability/normalization only.

**Tests.** Compiler suite green (no behavior change).

---

## I9 — [DOC] Align the ranking spec with the implementation

**Problem.** `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` documents the ranking
tiebreak chain as `... cost_per_second, tuple name`, but the implementation is
`local_preference, route_quality_penalty, reliability_tier, vram,
max_model_len - required, budget_reservation_usd` then observed telemetry
(success_rate, latency, cost_per_success) then `tuple name` (via
`registry.rank`'s `input_order`). The implementation is the intended one
(budget_reservation reflects real billable cost; telemetry only orders within
exact fit-ties and always terminates on name). The DOC is stale.

**Fix.** Update the ranking-order description in
`docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` (and any duplicate in README /
docs) to state the ACTUAL, now-normalized chain, explicitly noting: (a) the 6th
fit key is `budget_reservation_usd` (cost_per_second × billable_seconds), (b)
observed telemetry (success_rate, p50_latency, cost_per_success) orders only
tuples with an identical fit key, and (c) `tuple name` is the final total-order
tiebreaker guaranteeing determinism. Also add a one-line code comment above
`_rank_by_fit_then_observations` summarizing the full key chain.

**Behavior.** Documentation only.

---

## FINAL GATE (before declaring done)

Run and paste real output for each:
1. `uv run pytest -q` — must be all green (>= prior count; new tests added).
2. `(cd sdk/python && uv run --with-editable . pytest -q)` — green.
3. `uv run gpucall validate-config --config-dir config` — valid.
4. `uv run gpucall security scan-secrets` — 0 findings.
5. `uv run gpucall provider-conformance` — passed.
6. `uv run gpucall launch-check --profile static --config-dir config` — static_config_valid True.
7. Determinism spot-check: run `uv run gpucall explain-config <a real infer recipe>`
   (or a compile smoke) twice and confirm identical tuple_chain output.

Report: per-item commit hash, what changed, test evidence. If any item was
skipped because it would change healthy-request behavior, say which and why.
Do not deploy or release — hand back to the operator (Fable) for that.
