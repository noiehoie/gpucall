# gpucall Conformance Governance — Design Specification

## 0. Document Control

| Field | Value |
|---|---|
| Title | gpucall Conformance Governance |
| Status | DRAFT (requirements + architecture grounded in source; parts of the implementation plan carry unverified assumptions — see §7) |
| Version | 0.1.0 |
| Scope | Mechanically guaranteeing that catalog declarations match worker real capabilities, so gpucall's fail-closed promise is real rather than declared. |
| Source basis | Verified by first-hand reading of `gpucall/domain.py`, `gpucall/config.py`, `gpucall/routing.py`, `gpucall/config_fingerprint.py`, `gpucall/validation_evidence.py`. Everything sourced from these is stated as fact with file:line. Everything not yet read is isolated in §7 as an assumption. |

### 0.1 How to read this document

- §1 Requirements — what must be true, independent of implementation.
- §2 Architecture — the existing gpucall structure this design attaches to, plus the new governance layer, with module boundaries and I/O.
- §3 Data definitions — the exact types crossing module boundaries.
- §4 Module specifications — per module: responsibility, inputs, outputs, invariants.
- §5 Implementation plan — phased, per change: what file, what function, what test.
- §6 Verification & acceptance.
- §7 Assumptions requiring verification before implementation — the honest boundary of what is confirmed.
- §8 Update policy & revision history.

### 0.2 Update policy

This document is the single source of truth for the conformance-governance change. It is updated **before** the code, not after. Any implementer who discovers that an §7 assumption is false MUST update §2–§5 to match the code and move the item out of §7 in the same change. Each revision appends a row to §8 with date, author, and the sections touched. The document is versioned in-repo at `docs/design/conformance-governance.md`; its history is the git history of that file.

---

## 1. Requirements

### 1.1 Problem statement

gpucall routes each request deterministically by trusting catalog declarations: a model's `capabilities`, a tuple's `input_contracts`, and (proposed) a model's input requirements. The router (`routing.catalog_route_rejection_reason`, routing.py:364-369) admits a tuple when `recipe.required_model_capabilities ⊆ model.capabilities` — a **declaration-vs-declaration** check. Nothing verifies that the declaration matches what the worker actually does. Consequently:

- A model may declare a capability it does not implement, and be selected for a recipe that requires it.
- The validation mechanism uses a fixed synthetic input, so a model whose input requirements differ (e.g. a minimum image dimension) fails validation structurally and is never provable, yet remains in the catalog.
- A tuple that has never passed validation can still be selected as a route primary, and real traffic flows to it.

These surface only when a real caller happens to hit them. The fail-closed guarantee is therefore **"trust the declaration, unverified"** — not a real guarantee.

### 1.2 Functional requirements

- **FR-1 (input-requirement declaration).** The model catalog SHALL be able to declare per-model input requirements (at minimum: minimum image dimension; image delivery mode = embedded-in-chat vs separate-ref; accepted mime prefixes).
- **FR-2 (derived synthetic input).** The validation synthetic-input builder SHALL derive its input from (tuple input contracts) × (model input requirements) × (recipe task), not from fixed constants. The synthetic input SHALL be of the same shape the real caller's request takes for that tuple.
- **FR-3 (static capability truthfulness).** At config-load time, the system SHALL detect any model that declares a capability/vision support absent from the set of models the worker will actually accept, and reject/flag that model so it cannot be silently routed.
- **FR-4 (behavioural capability check).** For capability claims not decidable statically (e.g. instruction-following), validation SHALL exercise the claim (send an instruction the model cannot satisfy by accident, assert it is followed) and fail the tuple's validation if the claim does not hold.
- **FR-5 (minimal invalidation granularity).** A change to one catalog section SHALL invalidate only the validation evidence that depends on that section.
- **FR-6 (dynamic validation budget).** Automatic re-validation SHALL size its budget to the tuple's cost estimate, so large models are not permanently unvalidatable under a fixed low budget.
- **FR-7 (failure classification & quarantine).** Automatic re-validation SHALL classify failures: temporary (provider-origin) failures are retried; structural (deterministic, permanent) failures, after N consecutive occurrences, quarantine the tuple (exclude from routing + escalate). Temporary failures SHALL NOT quarantine.

### 1.3 Non-functional requirements

- **NFR-1 (no false negatives on healthy routes).** No change may cause a currently-valid healthy route to be rejected. Quarantine must never trigger on a purely temporary provider fault.
- **NFR-2 (backward-compatible catalog).** Adding declaration fields must not break existing catalog YAML. Note: model/tuple specs are pydantic models with `extra="forbid"` (domain.py:615, 703) — unknown keys are rejected — so new fields MUST be added to the schema with defaults, never left as loose YAML keys.
- **NFR-3 (no unnecessary spend).** Static checks (FR-3) must not call providers. Behavioural checks (FR-4) run only within already-billed validation smokes.
- **NFR-4 (determinism preserved).** No change may introduce inference into a control decision, or make the same (input, catalog, evidence) produce a different routing decision.
- **NFR-5 (build on existing mechanisms).** Where gpucall already has a mechanism (config-load static validation, provider error taxonomy, routing-exclusion remediation), the design extends it rather than building a parallel one.

### 1.4 Constraints

- The router already performs declaration-vs-declaration matching (routing.py:340-402); this design adds the truthfulness layer *beneath* that trust, not inside the matcher.
- Validation evidence keying is `(tuple_name, recipe_name, mode)` (validation_evidence.py:17) and is coupled to both a config fingerprint and a code fingerprint (git commit) (validation_evidence.py:219-225). Only the config-fingerprint coupling is in scope for FR-5.

### 1.5 Acceptance criteria

A implementation is accepted when the tests in §6 pass, specifically: a model with a false capability is detected (FR-3/FR-4); a model with a non-default input requirement gets a conforming synthetic input (FR-2); a one-file catalog change does not invalidate unrelated routes (FR-5); a structural failure quarantines while a simulated temporary failure does not (FR-7); and the full existing suite stays green (NFR-1/NFR-4).

---

## 2. Architecture

### 2.1 System context — the existing pipeline (verified)

Request → **compile** → **route selection** → **execution** → **result**, governed by policy and gated by validation evidence.

```
TaskRequest ──▶ compiler.compile ──▶ CompiledPlan(tuple_chain) ──▶ dispatcher/worker ──▶ TupleResult
                     │                        ▲
                     │ uses                   │ gated by
                     ▼                        │
             routing.tuple_route_rejection_reason        validation_evidence (per tuple×recipe×mode)
                     │ consults                              ▲ keyed/invalidated by
                     ▼                                       │
        Config{policy, recipes, tuples, models, engines} ── config_fingerprint (single SHA256 of all yml)
```

- **Config assembly (verified, config.py).** `load_config` (config.py:231) builds `GpucallConfig{policy, recipes, tuples, models, engines, object_store, tenants, admin_automation, runtimes}` (config.py:32-41). Tuples are **not** stored whole: `_load_split_tuple_payloads` (config.py:132-176) joins a `surfaces/*.yml` and a `workers/*.yml` by `worker_ref` into one `ExecutionTupleSpec` (`payload = {**surface, **worker}`, name = `surface_ref`). This is why `config/tuples/` is empty. `input_contracts` therefore lives on the assembled tuple.
- **Static config validation (verified, config.py:293-399).** `validate_config` already enforces cross-references: `tuple.model` vs `model.provider_model_id` (343-347), `tuple.max_model_len ≤ model.max_model_len` (348-352), engine support, adapter-descriptor contract agreement, and a per-adapter `config_validator` hook (396-399). It does **not** check declared capability vs worker-accepted model set — that is exactly the gap FR-3 fills, and it belongs here.
- **Route selection (verified, routing.py).** `tuple_route_rejection_reason` (routing.py:231-331) is the deterministic gate; `catalog_route_rejection_reason` (340-402) does the capability/contract matching against `model.capabilities`, `model.input_contracts`, `model.supports_vision`, etc. All declaration-vs-declaration. `_missing_input_contracts` (405-409) treats `chat_messages` as satisfying a `text` requirement.
- **Validation evidence (verified, validation_evidence.py).** Evidence JSONs live under `<state>/tuple-validation/`, keyed `(tuple, recipe, mode)` (17). `route_validation_rejection_reason` (196-254) accepts an evidence only if required fields present, `commit` matches (219-220), `config_hash` matches under strict mode (221-225, default on: 257-261), `passed is True` (228 — else `latest_route_validation_failed:{error.code}`), and the official contract hashes match.
- **Config fingerprint (verified, config_fingerprint.py).** `route_validation_config_hash` (17-31) is a **single SHA256 over every `*.yml`** under the config dir (excluding `credentials.yml`, `tenants/`). Monolithic: any one-file change changes the whole hash → all evidence invalidated.

### 2.2 The new layer

The conformance-governance layer is not a new pipeline; it is three insertions into existing stages:

1. **Declaration layer (model catalog):** add input requirements as first-class schema (FR-1).
2. **Static truthfulness (config-load validation):** extend `validate_config` with capability-vs-worker-accepted-set matching (FR-3).
3. **Validation-time truthfulness & operations:** derive synthetic input (FR-2), behavioural probe (FR-4), section-scoped fingerprint (FR-5), dynamic budget (FR-6), failure classification & quarantine (FR-7).

### 2.3 Module decomposition (I/O summarised; full spec in §4)

| Module | Stage | Input | Output |
|---|---|---|---|
| M1 InputRequirements (schema) | declaration | model YAML | `ModelSpec.input_requirements` |
| M2 WorkerAcceptedModels (pure data) | static | — | frozenset of provider_model_ids the worker accepts |
| M3 CapabilityConformance | config-load | Config (models + M2) | list of findings (declared-but-unaccepted) |
| M4 SyntheticInputBuilder | validation | tuple, model(M1), recipe | TaskRequest (contract- & requirement-correct) |
| M5 BehaviouralProbe | validation | model, capability claim | pass/fail per claim |
| M6 SectionFingerprint | invalidation | config dir | per-section hashes + evidence dependency map |
| M7 FailureClassifier | operations | validation result / exception | {temporary, structural} |
| M8 QuarantineBinding | operations | structural-failure count | routing-exclusion input |

---

## 3. Data definitions (verified types crossing boundaries)

Exact as in domain.py; new fields marked (NEW).

- **ModelSpec** (domain.py:614) pydantic, `extra="forbid"`: `name; provider_model_id; capabilities: list[str]; max_model_len; min_vram_gb; supported_engines; input_contracts: list[InputContract]; output_contracts; supports_vision; supports_guided_decoding; supports_streaming; ...`. **(NEW) `input_requirements: InputRequirements | None = None`** (M1).
- **InputContract** (domain.py:611) = `Literal["text","chat_messages","data_refs","image","audio","document","activation_refs","artifact_refs"]`.
- **ExecutionTupleSpec** (domain.py:702): assembled tuple; carries `input_contracts`, `model_ref`, `engine_ref`, `adapter`, `endpoint_contract`, `output_contract`, `supports_vision`, `max_model_len`, cost fields.
- **Recipe** (domain.py:522): `required_model_capabilities: list[str]`, `task`, `guided_decoding`, `output_contract`, `allowed_mime_prefixes`, derived timeout/lease/context-budget.
- **RouteValidationKey** (validation_evidence.py:17) = `(tuple_name, recipe_name, mode)`.
- **ProviderErrorCode** (domain.py:76-102): 23 temporary provider failure codes — the canonical "temporary" set for M7.
- **TupleError** (domain.py:888): `retryable: bool; code; status_code; raw_output` — per-execution failure carrier for M7.
- **(NEW) InputRequirements**: `{ image: { min_dimension_px: int; delivery: Literal["chat_content","data_ref"]; mime_prefixes: list[str] } | None }` — optional, defaulted; consumed by M4.

---

## 4. Module specifications

Each module: Responsibility / Inputs / Outputs / Invariants. (Function-level targets that depend on unread files are cross-referenced to §7.)

### M1 InputRequirements schema
- **Responsibility:** declare per-model input requirements.
- **Inputs:** model YAML.
- **Outputs:** `ModelSpec.input_requirements`.
- **Invariants:** optional with a safe default (≥64px image, delivery inferred from tuple input_contracts) so absent declarations never break loading (NFR-2). Added as a real schema field, not a loose key.

### M2 WorkerAcceptedModels
- **Responsibility:** expose, as pure data importable without the execution SDK, the set of provider_model_ids the worker will actually accept (text and vision separately).
- **Inputs:** none (constant data mirroring the worker's own allowlist, co-located with the worker so the two cannot drift — see §7-A for the exact current location, unverified).
- **Outputs:** two frozensets.
- **Invariants:** the worker consumes the same constant it exposes (single source of truth).

### M3 CapabilityConformance
- **Responsibility:** at config load, flag any model whose declaration exceeds what the worker accepts.
- **Inputs:** `Config.models`, M2.
- **Outputs:** findings `[{model, provider_model_id, issue}]`; a non-empty result fails `validate_config` (or downgrades the model to non-eligible).
- **Invariants:** pure/static, no provider calls (NFR-3). Extends `validate_config` (config.py:293-399), not a parallel validator (NFR-5).

### M4 SyntheticInputBuilder
- **Responsibility:** build the validation request from contract × requirement × task.
- **Inputs:** tuple (with input_contracts), model (with M1), recipe.
- **Outputs:** a `TaskRequest` whose image size satisfies M1, whose image is embedded in chat content iff the tuple's contract is `chat_messages`, and whose shape equals the real caller's request for that tuple.
- **Invariants:** never emits `messages` + `input_refs` together (the compiler forbids it — see §7-B). Same compile path as production (§7-B).

### M5 BehaviouralProbe
- **Responsibility:** exercise a capability claim during validation.
- **Inputs:** model, the claim under test.
- **Outputs:** pass/fail; fail marks the tuple's validation failed.
- **Invariants:** runs only inside an already-billed smoke (NFR-3); reuses M4.

### M6 SectionFingerprint
- **Responsibility:** replace the monolithic config hash with per-section hashes and an evidence→section dependency map.
- **Inputs:** config dir.
- **Outputs:** `{models_hash, tuples_hash, policy_hash, recipes_hash}`; evidence compares only the hashes of sections it depends on.
- **Invariants:** an evidence for tuple T depends on the section of T's referenced model, T's recipe, and policy — not on unrelated models. Migration: existing single-hash evidence is treated as "all sections changed" once. Out of scope: commit-fingerprint invalidation (validation_evidence.py:219-220) stays as-is.

### M7 FailureClassifier
- **Responsibility:** split a validation failure into temporary vs structural.
- **Inputs:** the validation result (its `error.code`, surfaced already at validation_evidence.py:228-231) or a `TupleError`.
- **Outputs:** `temporary` (code ∈ ProviderErrorCode, or `retryable=True`) → retry; else `structural`.
- **Invariants:** default-safe — an unknown code is treated as temporary, never quarantined, to protect healthy routes (NFR-1). Exact watch-loop insertion point: §7-C.

### M8 QuarantineBinding
- **Responsibility:** after N consecutive structural failures, feed the tuple into the existing routing-exclusion remediation.
- **Inputs:** structural-failure count from M7.
- **Outputs:** an exclusion request to the existing remediation mechanism (`PanopticonRemediationPolicy.exclude_from_routing` exists, domain.py:457 — its invocation path is §7-C).
- **Invariants:** binds to existing remediation, does not invent a new exclusion path (NFR-5).

---

## 5. Implementation plan (phased)

**Phase 1 — stop the bleeding + biggest operational pain**
1. M1 minimal (image.min_dimension_px) + M4 (derive size; embed-in-chat for chat_messages contract). Test: FR-2.
2. M6 (section fingerprint + dependency map + migration). Test: FR-5. *Highest operational value: a catalog edit no longer invalidates unrelated large-model routes.*
3. M7 + M8 (structural-only quarantine on existing remediation). Test: FR-7 both directions.
4. M2 + M3 (static capability↔worker matching in validate_config). Test: FR-3.

**Phase 2 — depth of truthfulness + operational optimisation**
5. M5 (behavioural probe). Test: FR-4.
6. M1 extension (delivery/mime) + M6 refinements + M6? no — **M6 done**; **FR-6 dynamic budget**. Test: FR-6.

Each item lands as one reviewable change with its test; healthy-request behaviour unchanged (NFR-1); no inference in control (NFR-4).

---

## 6. Verification & acceptance

- FR-2: a model declaring min_dimension_px yields a synthetic image ≥ that size; a chat_messages-contract vision tuple gets image-in-content, not input_refs; and the built request equals a captured real caller request in shape (§7-B check).
- FR-3: a model with a fabricated capability absent from M2 is flagged by validate_config; the known catalog is scanned and its real declared-but-unaccepted models are reported.
- FR-4: a task-token-only model that declares instruction-following fails the behavioural probe.
- FR-5: editing one model file changes only that section's hash; an unrelated route's evidence stays accepted.
- FR-7: a GovernanceError-class (structural) repeated failure quarantines; a simulated ProviderErrorCode (temporary) failure does not.
- Regression: full existing suite green.

---

## 7. Assumptions requiring verification before implementation

These are NOT yet confirmed by reading source. Each MUST be verified (and this doc corrected) before the dependent module is built.

- **§7-A (M2 location).** The worker's accepted-model set currently lives inside worker-contract code (not yet fully read). Assumption: it can be lifted to a pure-data module without pulling execution-SDK imports. Verify by reading `gpucall/worker_contracts/*` and `gpucall/execution_surfaces/*`.
- **§7-B (M4 compile path).** Assumption: the synthetic request and the real caller request traverse the same `compiler.compile` path, and the compiler forbids `messages`+`input_refs` together. Verify by reading `gpucall/compiler.py` (compile + request/recipe validation) and the smoke-request builder in `gpucall/cli.py`.
- **§7-C (M7/M8 watch & remediation).** Assumption: the re-validation watch loop runs tuple-smoke as a subprocess and can read `error.code`, and the existing `exclude_from_routing` remediation is callable with a tuple as input. Verify by reading `gpucall/recipe_admin.py` (watch loop) and `gpucall/panopticon*.py` (remediation invocation).
- **§7-D (FR-6 budget).** Assumption: the fixed `recipe_inbox_auto_validation_budget_usd=0.10` (domain.py:121) is the value the watch loop passes to tuple-smoke, and a per-tuple cost estimate is available. Verify by reading the watch loop and the cost/estimate module.

When any assumption is verified, move it into §2–§4 as fact with file:line and delete it here.

---

## 9. Multi-angle review synthesis (2026-07-10)

Five review angles were run (architecture, adversarial, security, source fact-check, requirements/plan completeness). Only findings I can independently confirm against code I have read myself are recorded here; unconfirmable agent claims are omitted (per the project's verification discipline).

### 9.1 Confirmed / high-priority findings

- **The problem statement (§1.1) overclaims and must be narrowed.** "fail-closed is unverified trust" is only partly true. The router DOES gate on validation evidence: when `require_route_validation` is set, a tuple with no `(tuple,recipe,mode)` entry in `validated_routes` is rejected (compiler.py:621-638; evidence gate in validation_evidence.py:196-254). The real, narrower gaps are exactly two: (a) **capability declaration truthfulness** — routing trusts `model.capabilities` verbatim (routing.py:364-369) and never checks it against worker reality; (b) **unvalidatable models** — a fixed synthetic input makes some models fail validation structurally, so they never earn evidence. §1.1 should be rewritten to claim only (a) and (b), not a blanket "unverified."

- **Requirements are incomplete — same defect class, uncovered.** The design covers *capability* truthfulness but routing also trusts, declaration-only: `model.input_contracts` (routing.py:375-378), `model.output_contracts` (383-384), `model.supports_guided_decoding` (381), `engine` capabilities (385-401), and `tuple.max_model_len` vs the worker's actually-loaded context (the H100-sizing bug class). These are the identical "declaration vs reality" gap. They must be added as requirements or explicitly declared out-of-scope. As written the design fixes one facet and silently leaves siblings.

- **System 1 scope crosses a layer boundary.** The confirmed chat_messages×vision defect (compiler.py:383-386) needs a *compiler* change, not just a validation-builder change. "Synthetic input derivation" is therefore not a pure validation-layer concern; §2.2/§5 must show System 1 spans the compiler.

### 9.2 Key risks (targeted for the assumptions still open in §7)

- **Highest-risk assumption — M7 failure classification.** Whether a re-validation failure cleanly maps to temporary(ProviderErrorCode)/structural is unverified until recipe_admin.py is read (§7-C). If it misclassifies, quarantine either falsely excludes healthy large routes (NFR-1 breach) or never fires. Mitigation already in design: default unknown→temporary; but note the tempering fact — traffic only flows to *validated* routes, so a structurally-failing tuple is not routable regardless of quarantine, which lowers (not removes) the severity.

- **Security tradeoff — M6 section fingerprint (must be conservative).** Splitting the monolithic hash (config_fingerprint.py:17-31) is fail-OPEN if the evidence→section dependency map is too narrow: a change in section X that actually affects section Y's routes would leave Y's evidence stale-but-accepted. The dependency map MUST over-approximate (when in doubt, depend on the section) or the invalidation weakening becomes a security hole. This is the central design decision of M6.

### 9.3 Verdict

The design's *direction* is sound and now grounded in six source files, but it is **not ready to implement**: (1) §1.1 must be narrowed to the two real gaps; (2) the uncovered sibling truthfulness gaps must be scoped in or out; (3) System 1's compiler span must be made explicit; (4) the three §7 assumptions (recipe_admin, panopticon, worker_contracts, budget) must be closed by reading before M6/M7/M8 are built. Until then the document remains DRAFT.

## 8. Update policy & revision history

Update policy: see §0.2. This document changes before code; §7 shrinks as source is read.

| Date | Version | Author | Sections | Note |
|---|---|---|---|---|
| 2026-07-05 | 0.1.0 | operator (Claude) | all | Initial draft. Requirements + architecture grounded in domain/config/routing/config_fingerprint/validation_evidence. Implementation-plan gaps isolated in §7. |
| 2026-07-10 | 0.2.0 | operator (Claude) | §4 M4, §7, §9 | compiler.py read: §7-B confirmed, chat_messages×vision compiler constraint added to M4. Multi-angle review synthesized into §9 (narrow §1.1; cover sibling truthfulness gaps; M6 must over-approximate; M7 highest risk). |
