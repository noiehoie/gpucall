from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from math import ceil

from gpucall.domain import (
    CompileArtifact,
    CompiledPlan,
    DataClassification,
    CostPolicy,
    EngineSpec,
    ExecutionMode,
    KeyReleaseRequirement,
    ModelSpec,
    Policy,
    ExecutionTupleSpec,
    Recipe,
    RecipeQualityFloor,
    SecurityTier,
    TaskRequest,
    recipe_requirements,
)
from gpucall.domain import ChatMessage, ResponseFormatType
from gpucall.execution.contracts import account_ref_for_spec
from gpucall.price_freshness import tuple_configured_price_freshness
from gpucall.registry import ObservedRegistry
from gpucall.routing import classification_rank, is_production_route_candidate, requested_output_contract, requires_openai_chat_contract, tuple_route_rejection_reason, required_model_len, token_budget
from gpucall.targeting import is_configured_target


class GovernanceError(ValueError):
    def __init__(self, message: str, *, code: str = "GOVERNANCE_ERROR", context: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.context = context or {}


def _message_content_as_text(content: str | list[dict[str, object]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class GovernanceCompiler:
    SUPPORTED_TASKS = {"infer", "vision", "transcribe", "convert", "train", "fine-tune", "split-infer"}
    DEFAULT_AUTO_INTENTS = {
        "short_text_inference",
        "standard_text_inference",
        "large_context_text_inference",
        "extra_large_context_text_inference",
        "ultralong_text_inference",
        "understand_image",
    }
    def __init__(
        self,
        *,
        policy: Policy,
        recipes: dict[str, Recipe],
        tuples: dict[str, ExecutionTupleSpec],
        registry: ObservedRegistry,
        models: dict[str, ModelSpec] | None = None,
        engines: dict[str, EngineSpec] | None = None,
    ) -> None:
        self.policy = policy
        self.recipes = recipes
        self.tuples = tuples
        self.models = models or {}
        self.engines = engines or {}
        self.registry = registry

    def compile(self, request: TaskRequest) -> CompiledPlan:
        recipe = self._recipe_for(request)
        self._validate_request_against_recipe(request, recipe)
        tuple_chain = self._tuple_chain(request, recipe, auto_selected=request.recipe is None)

        compiled_token_budget = self._token_budget(request)
        compiled_required_model_len = self._required_model_len(request, recipe)
        requirements = recipe_requirements(recipe)
        if compiled_required_model_len > requirements.context_budget_tokens:
            raise GovernanceError(
                f"required model length {compiled_required_model_len} exceeds recipe context budget {requirements.context_budget_tokens}",
                code="REQUEST_EXCEEDS_RECIPE_CONTEXT",
                context={
                    "required_model_len": compiled_required_model_len,
                    "recipe_context_budget_tokens": requirements.context_budget_tokens,
                    "recipe": recipe.name,
                    "task": request.task,
                },
            )

        timeout = min(request.timeout_seconds or recipe.timeout_seconds, self.policy.max_timeout_seconds)
        ttl = min(
            request.lease_ttl_seconds or recipe.lease_ttl_seconds or self.policy.default_lease_ttl_seconds,
            self.policy.max_lease_ttl_seconds,
        )
        if timeout > ttl:
            raise GovernanceError("timeout_seconds must not exceed lease_ttl_seconds")

        structured = request.response_format is not None and request.response_format.type is not ResponseFormatType.TEXT
        compiled_temperature = request.temperature
        if compiled_temperature is None:
            compiled_temperature = recipe.structured_temperature if structured else recipe.default_temperature
        effective_system_prompt = recipe.structured_system_prompt if structured and recipe.structured_system_prompt else recipe.system_prompt
        compiled_messages = self._compiled_messages(request, effective_system_prompt)

        compiled_stop_tokens = list(recipe.stop_tokens)
        if isinstance(request.stop, str):
            compiled_stop_tokens.append(request.stop)
        elif isinstance(request.stop, list):
            compiled_stop_tokens.extend(str(s) for s in request.stop)

        plan = CompiledPlan(
            policy_version=self.policy.version,
            recipe_name=recipe.name,
            task=recipe.task,
            mode=request.mode,
            data_classification=recipe.data_classification,
            tuple_chain=tuple_chain,
            timeout_seconds=timeout,
            lease_ttl_seconds=ttl,
            token_estimation_profile=recipe.token_estimation_profile,
            token_budget=compiled_token_budget,
            max_tokens=request.max_tokens,
            temperature=compiled_temperature,
            top_p=request.top_p,
            seed=request.seed,
            presence_penalty=request.presence_penalty,
            frequency_penalty=request.frequency_penalty,
            tools=request.tools,
            tool_choice=request.tool_choice,
            functions=request.functions,
            function_call=request.function_call,
            stream_options=request.stream_options,
            n=request.n,
            input_refs=request.input_refs,
            inline_inputs=request.inline_inputs,
            messages=compiled_messages,
            response_format=request.response_format,
            metadata=dict(request.metadata),
            artifact_export=request.artifact_export,
            split_learning=request.split_learning,
            system_prompt=effective_system_prompt,
            stop_tokens=compiled_stop_tokens,
            repetition_penalty=recipe.repetition_penalty,
            guided_decoding=recipe.guided_decoding,
            output_validation_attempts=recipe.output_validation_attempts,
            attestations={
                "recipe_snapshot": self._recipe_snapshot(recipe),
                "system_prompt_transform": self._system_prompt_transform(recipe, effective_system_prompt, structured),
            },
        )
        plan.attestations["context_estimate"] = {
            "method": "utf8_bytes_times_policy_safety_multiplier_plus_output_budget",
            "required_model_len": compiled_required_model_len,
            "token_budget": compiled_token_budget,
        }
        selected_spec = self.tuples[tuple_chain[0]]
        selected_tuple = self._execution_tuple(recipe=recipe, tuple_spec=selected_spec)
        plan.attestations["selected_execution_tuple"] = selected_tuple
        selected_model = self.models.get(selected_spec.model_ref) if selected_spec.model_ref else None
        plan.attestations["model_trust_policy"] = {
            "model_ref": selected_spec.model_ref,
            "trust_remote_code": bool(selected_model.trust_remote_code) if selected_model is not None else False,
        }
        plan.attestations["cost_estimate"] = self._cost_estimate(selected_spec, request, recipe, timeout)
        plan.attestations["security_gate"] = self._security_gate(recipe, selected_spec)
        if request.artifact_export is not None or recipe.requires_key_release:
            plan.attestations["key_release_requirement"] = self._key_release_requirement(
                request=request,
                recipe=recipe,
                tuple=selected_spec,
            ).model_dump(mode="json")
        governance_hash = self._stable_hash(self._governance_material(plan))
        plan.attestations["governance_hash"] = governance_hash
        plan.attestations["compile_artifact"] = self._compile_artifact(
            request=request,
            recipe=recipe,
            tuple_chain=tuple_chain,
            selected_tuple=selected_tuple,
            governance_hash=governance_hash,
        ).model_dump(mode="json")
        return plan

    def _recipe_for(self, request: TaskRequest) -> Recipe:
        if request.task not in self.SUPPORTED_TASKS:
            raise GovernanceError(f"unsupported task: {request.task}")
        if request.recipe is None:
            return self._select_recipe(request)
        recipe = self.recipes.get(request.recipe)
        if recipe is None:
            raise GovernanceError(f"unknown recipe: {request.recipe}")
        if recipe.task != request.task:
            raise GovernanceError(f"request task {request.task!r} does not match recipe task {recipe.task!r}")
        return recipe

    def _select_recipe(self, request: TaskRequest) -> Recipe:
        candidates: list[Recipe] = []
        rejected: list[str] = []
        for recipe in self.recipes.values():
            reason = self._recipe_rejection_reason(request, recipe)
            if reason is None:
                candidates.append(recipe)
            else:
                rejected.append(f"{recipe.name}: {reason}")
        if not candidates:
            detail = "; ".join(sorted(rejected)) if rejected else "no recipes are configured"
            context: dict[str, object] = {
                "task": request.task,
                "mode": request.mode.value,
                "required_model_len": self._largest_required_model_len(request),
                "largest_auto_recipe_model_len": self._largest_auto_recipe_model_len(request.task),
                "rejections": sorted(rejected),
            }
            raise GovernanceError(
                f"no auto-selectable recipe for task {request.task!r}: {detail}",
                code="NO_AUTO_SELECTABLE_RECIPE",
                context=context,
            )
        return sorted(candidates, key=lambda recipe: self._recipe_selection_key(recipe, request))[0]

    def _recipe_rejection_reason(self, request: TaskRequest, recipe: Recipe) -> str | None:
        if not recipe.auto_select:
            return "auto_select is false"
        if recipe.task != request.task:
            return f"task is {recipe.task!r}"
        if request.intent is None and recipe.intent is not None and recipe.intent not in self.DEFAULT_AUTO_INTENTS:
            return f"intent-specific recipe {recipe.intent!r} requires request intent"
        if request.intent is not None and recipe.intent != request.intent:
            return f"intent is {recipe.intent!r}"
        if request.mode not in recipe.allowed_modes:
            return f"mode {request.mode} is not allowed"
        if request.max_tokens is not None:
            token_budget = self._token_budget(request)
            ceiling = recipe_requirements(recipe).context_budget_tokens
            if token_budget is not None and token_budget > ceiling:
                return f"token budget {token_budget} exceeds recipe context budget {ceiling}"
        required_model_len = self._required_model_len(request, recipe)
        ceiling = recipe_requirements(recipe).context_budget_tokens
        if required_model_len > ceiling:
            return f"required model length {required_model_len} exceeds recipe context budget {ceiling}"
        for ref in request.input_refs:
            if recipe.max_input_bytes is not None and ref.bytes is not None and ref.bytes > recipe.max_input_bytes:
                return f"input data_ref exceeds max_input_bytes {recipe.max_input_bytes}"
            if recipe.allowed_mime_prefixes and ref.content_type:
                if not any(ref.content_type.startswith(prefix) for prefix in recipe.allowed_mime_prefixes):
                    return f"content_type {ref.content_type!r} is not allowed"
        inline_types = [item.content_type for item in request.inline_inputs.values() if item.content_type]
        inline_allowed = self._inline_mime_prefixes(recipe)
        if inline_allowed and inline_types:
            for content_type in inline_types:
                if not any(content_type.startswith(prefix) for prefix in inline_allowed):
                    return f"inline content_type {content_type!r} is not allowed"
        return None

    def _recipe_selection_key(self, recipe: Recipe, request: TaskRequest) -> tuple[int, int, int, int, str]:
        # For intentless requests, do not silently pick draft/smoke recipes just
        # because they are smaller. A caller that supplies an intent still gets
        # the exact deterministic recipe match for that intent.
        requirements = recipe_requirements(recipe)
        quality_rank = 0 if request.intent is not None else _quality_selection_rank(recipe.quality_floor)
        return (quality_rank, classification_rank(recipe.data_classification), requirements.minimum_vram_gb, requirements.context_budget_tokens, recipe.name)

    def _validate_request_against_recipe(self, request: TaskRequest, recipe: Recipe) -> None:
        if request.mode not in recipe.allowed_modes:
            raise GovernanceError(f"mode {request.mode} is not allowed for recipe {recipe.name}")
        if request.messages and (request.inline_inputs or request.input_refs):
            raise GovernanceError("messages cannot be combined with inline_inputs or input_refs in v2.0")
        if recipe.task == "vision" and not _has_image_ref(request):
            raise GovernanceError("vision requires an image data_ref")
        if recipe.task == "transcribe":
            if not request.input_refs:
                raise GovernanceError("transcribe requires audio data_ref inputs")
            if request.inline_inputs or request.messages:
                raise GovernanceError("transcribe accepts DataRef inputs only")
        if recipe.task == "convert":
            if not request.input_refs:
                raise GovernanceError("convert requires document data_ref inputs")
            if request.inline_inputs or request.messages:
                raise GovernanceError("convert accepts DataRef inputs only")
        if recipe.task in {"train", "fine-tune"}:
            if not request.input_refs:
                raise GovernanceError(f"{recipe.task} requires data_ref training inputs")
            if request.inline_inputs or request.messages:
                raise GovernanceError(f"{recipe.task} accepts DataRef inputs only")
            if request.artifact_export is None:
                raise GovernanceError(f"{recipe.task} requires artifact_export")
        if recipe.task == "split-infer":
            if request.split_learning is None:
                raise GovernanceError("split-infer requires split_learning")
            if request.inline_inputs or request.messages:
                raise GovernanceError("split-infer accepts activation DataRef inputs only")
            if request.split_learning.activation_ref.sha256 is None:
                raise GovernanceError("split_learning activation_ref requires sha256")
            if request.split_learning.irreversibility_claim != "not_claimed" and request.split_learning.dp_epsilon is None:
                raise GovernanceError("split_learning empirical irreversibility claims require dp_epsilon")
        if self.policy.security.require_data_ref_sha256:
            for ref in request.input_refs:
                if ref.sha256 is None:
                    raise GovernanceError("data_ref sha256 is required by security policy")
        inline_bytes = sum(len(item.value.encode("utf-8")) for item in request.inline_inputs.values())
        if inline_bytes > self.policy.inline_bytes_limit:
            raise GovernanceError("inline input exceeds policy limit; use signed object references")
        inline_allowed = self._inline_mime_prefixes(recipe)
        if inline_allowed:
            for item in request.inline_inputs.values():
                if item.content_type and not any(item.content_type.startswith(prefix) for prefix in inline_allowed):
                    raise GovernanceError(f"inline content_type {item.content_type!r} is not allowed for recipe {recipe.name}")
        for ref in request.input_refs:
            if ref.expires_at is not None and ref.expires_at <= datetime.now(timezone.utc):
                raise GovernanceError("input data_ref is expired")
            if recipe.max_input_bytes is not None and ref.bytes is not None and ref.bytes > recipe.max_input_bytes:
                raise GovernanceError(f"input data_ref exceeds recipe max_input_bytes {recipe.max_input_bytes}")
            if recipe.allowed_mime_prefixes and ref.content_type:
                if not any(ref.content_type.startswith(prefix) for prefix in recipe.allowed_mime_prefixes):
                    raise GovernanceError(f"input content_type {ref.content_type!r} is not allowed for recipe {recipe.name}")

    def _tuple_chain(self, request: TaskRequest, recipe: Recipe, *, auto_selected: bool) -> list[str]:
        if request.requested_tuple:
            allowed = self._eligible_tuples([request.requested_tuple], request, recipe, auto_selected=False)
            if request.requested_tuple not in allowed:
                raise GovernanceError(f"requested tuple {request.requested_tuple!r} is not eligible for recipe {recipe.name}")
            if not request.bypass_circuit_for_validation and not self.registry.is_available(request.requested_tuple):
                raise GovernanceError(f"requested tuple {request.requested_tuple!r} is unavailable due to circuit breaker")
            return [request.requested_tuple]

        eligible = self._eligible_tuples(sorted(self.tuples), request, recipe, auto_selected=True)
        ranked = self._rank_by_fit_then_observations(eligible, request, recipe)
        if not ranked:
            raise GovernanceError(
                "no eligible tuple after policy, recipe, and circuit constraints",
                code="NO_ELIGIBLE_TUPLE",
                context={
                    "task": request.task,
                    "mode": request.mode.value,
                    "recipe": recipe.name,
                    "required_model_len": self._required_model_len(request, recipe),
                    "tuple_rejections": self._tuple_rejections(request, recipe, auto_selected=True),
                },
            )
        return ranked

    def _rank_by_fit_then_observations(
        self, tuples: list[str], request: TaskRequest, recipe: Recipe
    ) -> list[str]:
        ordered = self._fit_ordered_tuples(tuples, request, recipe)
        ranked: list[str] = []
        current_key: tuple[int, int, int, float] | None = None
        current_group: list[str] = []
        for tuple in ordered:
            fit_key = self._tuple_fit_key(tuple, request, recipe)
            if current_key is not None and fit_key != current_key:
                ranked.extend(self.registry.rank(current_group))
                current_group = []
            current_key = fit_key
            current_group.append(tuple)
        if current_group:
            ranked.extend(self.registry.rank(current_group))
        return ranked

    def _fit_ordered_tuples(self, tuples: list[str], request: TaskRequest, recipe: Recipe) -> list[str]:
        return sorted(tuples, key=lambda name: (*self._tuple_fit_key(name, request, recipe), name))

    def _tuple_fit_key(self, name: str, request: TaskRequest, recipe: Recipe) -> tuple[int, int, int, int, int, float]:
        compiled_required_model_len = self._required_model_len(request, recipe)
        spec = self.tuples[name]
        local_preference = 0 if spec.execution_surface and spec.execution_surface.value == "local_runtime" else 1
        return (
            local_preference,
            self._route_quality_penalty(name, request, recipe),
            self._observed_reliability_tier(name),
            spec.vram_gb,
            spec.max_model_len - compiled_required_model_len,
            float(spec.cost_per_second),
        )

    def _route_quality_penalty(self, name: str, request: TaskRequest, recipe: Recipe) -> int:
        if not (
            request.response_format is not None
            and request.response_format.type is ResponseFormatType.JSON_SCHEMA
            and request.response_format.strict
        ):
            return 0
        return min(
            self.registry.quality_failure_count(
                name,
                recipe=recipe.name,
                task=request.task,
                mode=request.mode.value,
                code="MALFORMED_OUTPUT",
            ),
            99,
        )

    def _observed_reliability_tier(self, name: str) -> int:
        score = self.registry.score(name)
        if not score.samples:
            return 1
        if score.success_rate >= 0.5:
            return 0
        return 2

    def _eligible_tuples(
        self,
        candidates: list[str],
        request: TaskRequest,
        recipe: Recipe,
        *,
        auto_selected: bool,
    ) -> list[str]:
        seen: set[str] = set()
        eligible: list[str] = []
        for name in candidates:
            if not name or name in seen:
                continue
            seen.add(name)
            spec = self.tuples.get(name)
            if spec is None:
                continue
            reason = tuple_route_rejection_reason(
                policy=self.policy,
                recipe=recipe,
                tuple=spec,
                model=self.models.get(spec.model_ref) if spec.model_ref else None,
                engine=self.engines.get(spec.engine_ref) if spec.engine_ref else None,
                mode=request.mode,
                required_len=self._required_model_len(request, recipe),
                required_input_contracts=self._required_input_contracts(request),
                required_output_contract=requested_output_contract(request, recipe),
                require_openai_chat_contract=requires_openai_chat_contract(request),
                auto_selected=auto_selected,
            )
            if reason is not None:
                continue
            cost_reason = self._tuple_cost_rejection_reason(
                tuple=spec,
                request=request,
                recipe=recipe,
                auto_selected=auto_selected,
            )
            if cost_reason is not None:
                continue
            eligible.append(name)
        return eligible

    def _tuple_rejections(self, request: TaskRequest, recipe: Recipe, *, auto_selected: bool) -> dict[str, str]:
        rejected: dict[str, str] = {}
        for name in sorted(self.tuples):
            spec = self.tuples[name]
            reason = tuple_route_rejection_reason(
                policy=self.policy,
                recipe=recipe,
                tuple=spec,
                model=self.models.get(spec.model_ref) if spec.model_ref else None,
                engine=self.engines.get(spec.engine_ref) if spec.engine_ref else None,
                mode=request.mode,
                required_len=self._required_model_len(request, recipe),
                required_input_contracts=self._required_input_contracts(request),
                required_output_contract=requested_output_contract(request, recipe),
                require_openai_chat_contract=requires_openai_chat_contract(request),
                auto_selected=auto_selected,
            )
            if reason is None and not self.registry.is_available(name):
                reason = "tuple is unavailable due to circuit breaker"
            if reason is None:
                reason = self._tuple_cost_rejection_reason(
                    tuple=spec,
                    request=request,
                    recipe=recipe,
                    auto_selected=auto_selected,
                )
            if reason is not None:
                rejected[name] = reason
        return rejected

    def _is_production_route_candidate(self, spec: ExecutionTupleSpec) -> bool:
        return is_production_route_candidate(spec)

    def _token_budget(self, request: TaskRequest) -> int | None:
        return token_budget(request, self.policy)

    def _required_model_len(self, request: TaskRequest, recipe: Recipe) -> int:
        return required_model_len(request, recipe, self.policy)

    def _tuple_cost_rejection_reason(
        self,
        *,
        tuple: ExecutionTupleSpec,
        request: TaskRequest,
        recipe: Recipe,
        auto_selected: bool,
    ) -> str | None:
        if not auto_selected:
            return None
        timeout = min(request.timeout_seconds or recipe.timeout_seconds, self.policy.max_timeout_seconds)
        estimate = self._cost_estimate(tuple, request, recipe, timeout)
        policy = self._effective_cost_policy(recipe)
        explicit_budget = any(
            value is not None
            for value in (
                policy.max_estimated_cost_usd,
                policy.max_cold_start_cost_usd,
                policy.max_idle_cost_usd,
            )
        )
        if policy.max_cold_start_cost_usd is not None and estimate["cold_start_cost_usd"] > float(policy.max_cold_start_cost_usd):
            return (
                f"estimated cold start cost {estimate['cold_start_cost_usd']:.4f} exceeds "
                f"max_cold_start_cost_usd {float(policy.max_cold_start_cost_usd):.4f}"
            )
        if policy.max_idle_cost_usd is not None and estimate["idle_cost_usd"] > float(policy.max_idle_cost_usd):
            return (
                f"estimated idle cost {estimate['idle_cost_usd']:.4f} exceeds "
                f"max_idle_cost_usd {float(policy.max_idle_cost_usd):.4f}"
            )
        if policy.max_estimated_cost_usd is not None and estimate["estimated_cost_usd"] > float(policy.max_estimated_cost_usd):
            return (
                f"estimated cost {estimate['estimated_cost_usd']:.4f} exceeds "
                f"max_estimated_cost_usd {float(policy.max_estimated_cost_usd):.4f}"
            )
        require_budget = policy.require_budget_for_high_cost_tuple
        if require_budget is None:
            require_budget = True
        threshold = policy.high_cost_threshold_usd
        if threshold is None:
            threshold = 5.0
        if require_budget and not explicit_budget and estimate["estimated_cost_usd"] > float(threshold):
            return (
                f"estimated cost {estimate['estimated_cost_usd']:.4f} exceeds high_cost_threshold_usd "
                f"{float(threshold):.4f} without explicit budget"
            )
        if policy.require_fresh_price_for_budget and estimate.get("price_freshness") != "fresh":
            return f"tuple price is {estimate.get('price_freshness')} under strict budget policy"
        return None

    def _cost_estimate(
        self,
        tuple: ExecutionTupleSpec,
        request: TaskRequest,
        recipe: Recipe,
        timeout_seconds: int,
    ) -> dict[str, float | int | str]:
        cold_start_seconds = float(tuple.expected_cold_start_seconds or recipe.expected_cold_start_seconds or 0)
        idle_seconds = float(tuple.scaledown_window_seconds or 0)
        runtime_seconds = float(timeout_seconds)
        standing_cost_seconds = float(tuple.standing_cost_window_seconds or 0)
        endpoint_cost_seconds = float(tuple.endpoint_cost_window_seconds or 0)
        billable_seconds = cold_start_seconds + runtime_seconds + idle_seconds
        if tuple.min_billable_seconds is not None:
            billable_seconds = max(billable_seconds, float(tuple.min_billable_seconds))
        if tuple.billing_granularity_seconds:
            granularity = float(tuple.billing_granularity_seconds)
            billable_seconds = ceil(billable_seconds / granularity) * granularity
        cost_per_second = float(tuple.cost_per_second)
        price_freshness = tuple_configured_price_freshness(tuple)
        execution_cost_usd = cost_per_second * billable_seconds
        standing_cost_per_second = float(tuple.standing_cost_per_second or 0)
        endpoint_cost_per_second = float(tuple.endpoint_cost_per_second or 0)
        standing_cost_usd = standing_cost_per_second * standing_cost_seconds
        endpoint_cost_usd = endpoint_cost_per_second * endpoint_cost_seconds
        return {
            "method": "cost_per_second_times_cold_start_runtime_idle_and_standing_estimate",
            "tuple": tuple.name,
            "cost_per_second": cost_per_second,
            "configured_price_source": tuple.configured_price_source or "",
            "configured_price_observed_at": tuple.configured_price_observed_at or "",
            "configured_price_ttl_seconds": float(tuple.configured_price_ttl_seconds or 0),
            "price_freshness": price_freshness.value,
            "cold_start_seconds": cold_start_seconds,
            "runtime_seconds": runtime_seconds,
            "idle_seconds": idle_seconds,
            "billable_seconds": billable_seconds,
            "standing_cost_per_second": standing_cost_per_second,
            "standing_cost_seconds": standing_cost_seconds,
            "endpoint_cost_per_second": endpoint_cost_per_second,
            "endpoint_cost_seconds": endpoint_cost_seconds,
            "cold_start_cost_usd": cost_per_second * cold_start_seconds,
            "idle_cost_usd": cost_per_second * idle_seconds,
            "standing_cost_usd": standing_cost_usd,
            "endpoint_cost_usd": endpoint_cost_usd,
            "execution_cost_usd": execution_cost_usd,
            "estimated_cost_usd": execution_cost_usd + standing_cost_usd + endpoint_cost_usd,
        }

    def _effective_cost_policy(self, recipe: Recipe) -> CostPolicy:
        base = self.policy.cost_policy
        override = recipe.cost_policy
        if override is None:
            return base
        return CostPolicy(
            max_estimated_cost_usd=override.max_estimated_cost_usd
            if override.max_estimated_cost_usd is not None
            else base.max_estimated_cost_usd,
            max_cold_start_cost_usd=override.max_cold_start_cost_usd
            if override.max_cold_start_cost_usd is not None
            else base.max_cold_start_cost_usd,
            max_idle_cost_usd=override.max_idle_cost_usd
            if override.max_idle_cost_usd is not None
            else base.max_idle_cost_usd,
            require_budget_for_high_cost_tuple=override.require_budget_for_high_cost_tuple
            if override.require_budget_for_high_cost_tuple is not None
            else base.require_budget_for_high_cost_tuple,
            high_cost_threshold_usd=override.high_cost_threshold_usd
            if override.high_cost_threshold_usd is not None
            else base.high_cost_threshold_usd,
            require_fresh_price_for_budget=override.require_fresh_price_for_budget
            if override.require_fresh_price_for_budget is not None
            else base.require_fresh_price_for_budget,
        )

    @staticmethod
    def _inline_mime_prefixes(recipe: Recipe) -> list[str]:
        return recipe.allowed_inline_mime_prefixes or recipe.allowed_mime_prefixes

    def _largest_required_model_len(self, request: TaskRequest) -> int:
        matching = [recipe for recipe in self.recipes.values() if recipe.task == request.task]
        if not matching:
            return required_model_len(request, self._synthetic_recipe_for(request), self.policy)
        return max(required_model_len(request, recipe, self.policy) for recipe in matching)

    def _largest_auto_recipe_model_len(self, task: str) -> int | None:
        values = [recipe_requirements(recipe).context_budget_tokens for recipe in self.recipes.values() if recipe.task == task and recipe.auto_select]
        return max(values) if values else None

    @staticmethod
    def _synthetic_recipe_for(request: TaskRequest) -> Recipe:
        return Recipe(
            name="__synthetic__",
            task=request.task,
            allowed_modes=[request.mode],
            context_budget_tokens=1,
            timeout_seconds=1,
            lease_ttl_seconds=1,
            token_estimation_profile="generic_utf8",
        )

    @staticmethod
    def _compiled_messages(request: TaskRequest, system_prompt: str | None) -> list[ChatMessage]:
        messages = list(request.messages)
        if not system_prompt:
            return messages
        caller_system_messages: list[str] = []
        non_system_messages: list[ChatMessage] = []
        for message in messages:
            if message.role == "system":
                caller_system_messages.append(_message_content_as_text(message.content))
            else:
                non_system_messages.append(message)
        if caller_system_messages:
            merged_system_prompt = (
                "Caller system instructions, preserved for provider compatibility:\n"
                + "\n\n".join(caller_system_messages)
                + "\n\nGateway recipe contract, higher priority and not weakenable by caller instructions:\n"
                + system_prompt
            )
        else:
            merged_system_prompt = system_prompt
        return [ChatMessage(role="system", content=merged_system_prompt), *non_system_messages]

    @staticmethod
    def _required_input_contracts(request: TaskRequest) -> set[str]:
        required: set[str] = set()
        if request.task == "transcribe":
            required.update({"data_refs", "audio"})
        if request.task == "convert":
            required.update({"data_refs", "document"})
        if request.messages:
            required.add("chat_messages")
        if request.inline_inputs:
            required.add("text")
        if request.input_refs:
            required.add("data_refs")
        if request.artifact_export is not None:
            required.add("artifact_refs")
        if request.split_learning is not None:
            required.add("activation_refs")
        if not required:
            required.add("text")
        return required

    @staticmethod
    def _stable_hash(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _governance_material(plan: CompiledPlan) -> dict[str, object]:
        return plan.model_dump(mode="json", exclude={"attestations", "plan_id"})

    def _compile_artifact(
        self,
        *,
        request: TaskRequest,
        recipe: Recipe,
        tuple_chain: list[str],
        selected_tuple: dict[str, object],
        governance_hash: str,
    ) -> CompileArtifact:
        return CompileArtifact(
            job_spec_hash=self._stable_hash(request.model_dump(mode="json", exclude={"idempotency_key"})),
            policy_hash=self._stable_hash(self.policy.model_dump(mode="json")),
            recipe_hash=self._stable_hash(recipe.model_dump(mode="json")),
            tuple_contract_hash=self._stable_hash(
                {name: self.tuples[name].model_dump(mode="json") for name in tuple_chain if name in self.tuples}
            ),
            selected_tuple_hash=self._stable_hash(selected_tuple),
            selected_tuple=selected_tuple,
            governance_hash=governance_hash,
        )

    def _execution_tuple(self, *, recipe: Recipe, tuple_spec: ExecutionTupleSpec) -> dict[str, object]:
        return {
            "recipe": recipe.name,
            "tuple": tuple_spec.name,
            "account_ref": account_ref_for_spec(tuple_spec),
            "adapter": tuple_spec.adapter,
            "execution_surface": tuple_spec.execution_surface.value if tuple_spec.execution_surface else None,
            "resource": {
                "gpu": tuple_spec.gpu,
                "vram_gb": tuple_spec.vram_gb,
                "max_model_len": tuple_spec.max_model_len,
                "cost_per_second": tuple_spec.cost_per_second,
                "region": tuple_spec.region,
                "zone": tuple_spec.zone,
            },
            "worker": {
                "model_ref": tuple_spec.model_ref,
                "engine_ref": tuple_spec.engine_ref,
                "modes": [mode.value for mode in tuple_spec.modes],
                "input_contracts": list(tuple_spec.input_contracts),
                "output_contract": tuple_spec.output_contract,
                "stream_contract": tuple_spec.stream_contract,
                "target_configured": is_configured_target(tuple_spec.target),
            },
            "contract": {
                "data_classification": recipe.data_classification.value,
                "context_budget_tokens": recipe.context_budget_tokens,
                "required_context_tokens": recipe_requirements(recipe).context_budget_tokens,
                "minimum_vram_gb": recipe_requirements(recipe).minimum_vram_gb,
                "output_contract": recipe.output_contract,
            },
        }

    def _security_gate(self, recipe: Recipe, tuple: ExecutionTupleSpec) -> dict[str, object]:
        profile = tuple.trust_profile
        attestation_required = bool(profile.requires_attestation or profile.security_tier is SecurityTier.CONFIDENTIAL_TEE)
        return {
            "tuple": tuple.name,
            "data_classification": recipe.data_classification.value,
            "security_tier": profile.security_tier.value,
            "dedicated_gpu": profile.dedicated_gpu,
            "attestation_required": attestation_required,
            "key_release_supported": profile.supports_key_release,
        }

    def _key_release_requirement(
        self,
        *,
        request: TaskRequest,
        recipe: Recipe,
        tuple: ExecutionTupleSpec,
    ) -> KeyReleaseRequirement:
        if request.artifact_export is None:
            raise GovernanceError(f"recipe {recipe.name} requires artifact_export key_id")
        if tuple.trust_profile.security_tier is SecurityTier.CONFIDENTIAL_TEE and not tuple.trust_profile.supports_key_release:
            raise GovernanceError("confidential TEE artifact export requires tuple key-release support")
        policy_hash = self._stable_hash(self.policy.model_dump(mode="json"))
        return KeyReleaseRequirement(
            key_id=request.artifact_export.key_id,
            policy_hash=policy_hash,
            attestation_required=recipe.data_classification is DataClassification.RESTRICTED,
            gateway_may_generate_dek=False,
        )

    @staticmethod
    def _recipe_snapshot(recipe: Recipe) -> dict[str, object]:
        data = recipe.model_dump(mode="json")
        for key in ("system_prompt", "structured_system_prompt"):
            value = data.get(key)
            data[key] = _text_snapshot(value) if isinstance(value, str) else None
        return data

    @classmethod
    def _system_prompt_transform(cls, recipe: Recipe, system_prompt: str | None, structured: bool) -> dict[str, object]:
        if not system_prompt:
            return {"applied": False, "recipe": recipe.name}
        return {
            "applied": True,
            "recipe": recipe.name,
            "source": "structured_system_prompt" if structured and recipe.structured_system_prompt else "system_prompt",
            "sha256": cls._stable_hash(system_prompt),
            "bytes": len(system_prompt.encode("utf-8")),
        }


def _has_image_ref(request: TaskRequest) -> bool:
    for ref in request.input_refs:
        content_type = (ref.content_type or "").lower()
        if content_type.startswith("image/"):
            return True
    return False


def _quality_selection_rank(quality: RecipeQualityFloor) -> int:
    if quality in {RecipeQualityFloor.STANDARD, RecipeQualityFloor.HIGH, RecipeQualityFloor.LOSSLESS}:
        return 0
    if quality is RecipeQualityFloor.DRAFT:
        return 1
    return 2


def _text_snapshot(value: str) -> dict[str, object]:
    return {
        "redacted": True,
        "bytes": len(value.encode("utf-8")),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }
