from __future__ import annotations

import math
import os

from gpucall.domain import (
    CompiledPlan,
    DataClassification,
    EngineSpec,
    ExecutionMode,
    ExecutionSurface,
    ModelSpec,
    Policy,
    ExecutionTupleSpec,
    Recipe,
    ResponseFormatType,
    SecurityTier,
    TaskRequest,
    recipe_requirements,
)
from gpucall.execution.registry import adapter_descriptor
from gpucall.targeting import is_configured_cidr, is_configured_target


def classification_rank(value: DataClassification) -> int:
    return {
        DataClassification.PUBLIC: 0,
        DataClassification.INTERNAL: 1,
        DataClassification.CONFIDENTIAL: 2,
        DataClassification.RESTRICTED: 3,
    }[value]


def token_budget(request: TaskRequest, policy: Policy) -> int | None:
    if request.max_tokens is None:
        return None
    return int(request.max_tokens * float(policy.tokenizer_safety_multiplier))


def required_model_len(request: TaskRequest, recipe: Recipe, policy: Policy) -> int:
    output_budget = token_budget(request, policy) or 0
    inline_bytes = sum(len(item.value.encode("utf-8")) for item in request.inline_inputs.values())
    message_bytes = sum(len(message.content.encode("utf-8")) for message in request.messages)
    if recipe.task == "vision":
        image_refs = [ref for ref in request.input_refs if str(ref.content_type or "").startswith("image/")]
        non_image_bytes = sum(int(ref.bytes or 0) for ref in request.input_refs if ref not in image_refs)
        estimated_input_tokens = math.ceil((inline_bytes + message_bytes + non_image_bytes) * float(policy.tokenizer_safety_multiplier))
        return max(1, estimated_input_tokens + (256 * len(image_refs)) + output_budget)
    ref_bytes = sum(int(ref.bytes or 0) for ref in request.input_refs)
    estimated_input_tokens = math.ceil((inline_bytes + message_bytes + ref_bytes) * float(policy.tokenizer_safety_multiplier))
    return max(1, estimated_input_tokens + output_budget)


def requested_output_contract(request: TaskRequest, recipe: Recipe) -> str | None:
    if request.response_format is None or request.response_format.type is ResponseFormatType.TEXT:
        return recipe.output_contract
    if request.response_format.type is ResponseFormatType.JSON_OBJECT:
        return "json_object"
    if request.response_format.type is ResponseFormatType.JSON_SCHEMA:
        return "json_schema"
    return recipe.output_contract


def is_production_route_candidate(
    tuple: ExecutionTupleSpec, *, allow_fake: bool | None = None, require_configured_runtime: bool = True
) -> bool:
    return production_route_rejection_reason(tuple, allow_fake=allow_fake, require_configured_runtime=require_configured_runtime) is None


def production_route_rejection_reason(
    tuple: ExecutionTupleSpec, *, allow_fake: bool | None = None, require_configured_runtime: bool = True
) -> str | None:
    if allow_fake is None:
        allow_fake = os.getenv("GPUCALL_ALLOW_FAKE_AUTO_TUPLES", "").strip().lower() in {"1", "true", "yes", "on"}
    if allow_fake:
        return None
    name = tuple.name.lower()
    descriptor = adapter_descriptor(tuple)
    if descriptor is not None and not descriptor.production_eligible:
        return descriptor.production_rejection_reason or "tuple is not eligible for production auto-routing"
    if "smoke" in name or "fake" in name:
        return "smoke/fake tuple is not eligible for production auto-routing"
    requires_model = descriptor.requires_model_for_auto if descriptor is not None else True
    if requires_model and not tuple.model:
        return "tuple model is not configured"
    if (
        require_configured_runtime
        and descriptor is not None
        and descriptor.execution_surface is ExecutionSurface.IAAS_VM
        and tuple.ssh_remote_cidr is not None
        and not is_configured_cidr(tuple.ssh_remote_cidr)
    ):
        return "IaaS ssh_remote_cidr is not configured for live execution"
    required_fields = descriptor.required_auto_fields if descriptor is not None else {}
    for field, reason in required_fields.items():
        value = getattr(tuple, field, None)
        if field in {"target", "stream_target"}:
            if not is_configured_target(value):
                return reason
        elif not value:
            return reason
    return None


def tuple_route_rejection_reason(
    *,
    policy: Policy,
    recipe: Recipe,
    tuple: ExecutionTupleSpec,
    model: ModelSpec | None = None,
    engine: EngineSpec | None = None,
    mode: ExecutionMode | None = None,
    required_len: int | None = None,
    required_input_contracts: set[str] | None = None,
    required_output_contract: str | None = None,
    auto_selected: bool = True,
    require_auto_select: bool = False,
    allow_fake: bool | None = None,
    require_configured_runtime: bool = True,
) -> str | None:
    if require_auto_select and not recipe.auto_select:
        return "recipe is not auto-selected"
    if auto_selected:
        route_reason = production_route_rejection_reason(tuple, allow_fake=allow_fake, require_configured_runtime=require_configured_runtime)
        if route_reason is not None:
            return route_reason
    input_contracts = set(tuple.input_contracts)
    if required_input_contracts and input_contracts:
        missing = required_input_contracts - input_contracts
        if missing:
            return "tuple input_contracts missing: " + ", ".join(sorted(missing))
    if recipe.task == "vision" and "image" not in input_contracts:
        return "tuple does not declare image input support"
    if recipe.task == "infer":
        if tuple.supports_vision or "image" in input_contracts:
            return "vision tuple is not eligible for infer task"
        if input_contracts and "text" not in input_contracts and "chat_messages" not in input_contracts:
            return "tuple does not declare text or chat input support"
    if policy.tuples.allow and tuple.name not in policy.tuples.allow:
        return "tuple is not in policy allowlist"
    if tuple.name in policy.tuples.deny:
        return "tuple is denied by policy"
    if not policy.tuples.max_data_classification.permits(recipe.data_classification):
        return "recipe data classification exceeds policy ceiling"
    if not tuple.max_data_classification.permits(recipe.data_classification):
        return "tuple data classification ceiling is below recipe requirement"
    security_reason = tuple_security_rejection_reason(policy=policy, recipe=recipe, tuple=tuple)
    if security_reason is not None:
        return security_reason
    requirements = recipe_requirements(recipe)
    if tuple.vram_gb < requirements.minimum_vram_gb:
        return "tuple vram_gb is below derived recipe requirement"
    minimum_model_len = required_len if required_len is not None else requirements.context_budget_tokens
    if tuple.max_model_len < minimum_model_len:
        return "tuple max_model_len is below required model length"
    catalog_reason = catalog_route_rejection_reason(
        recipe=recipe,
        tuple=tuple,
        model=model,
        engine=engine,
        required_len=minimum_model_len,
        mode=mode,
        required_input_contracts=required_input_contracts,
        required_output_contract=required_output_contract,
    )
    if catalog_reason is not None:
        return catalog_reason
    if mode is not None:
        if mode not in tuple.modes:
            return "tuple does not support requested mode"
        if mode is ExecutionMode.STREAM:
            descriptor = adapter_descriptor(tuple)
            required_fields = descriptor.stream_required_fields if descriptor is not None else {}
            for field, reason in required_fields.items():
                value = getattr(tuple, field, None)
                if field in {"target", "stream_target"}:
                    if not is_configured_target(value):
                        return reason
                elif not value:
                    return reason
        if mode is ExecutionMode.STREAM and tuple.stream_contract == "none":
            return "tuple does not declare a streaming contract"
    elif not any(candidate in tuple.modes for candidate in recipe.allowed_modes):
        return "tuple modes do not intersect recipe allowed_modes"
    descriptor = adapter_descriptor(tuple)
    stream_required_fields = descriptor.stream_required_fields if descriptor is not None else {}
    if ExecutionMode.STREAM in tuple.modes and ExecutionMode.STREAM in recipe.allowed_modes:
        for field, reason in stream_required_fields.items():
            value = getattr(tuple, field, None)
            if field in {"target", "stream_target"}:
                if not is_configured_target(value):
                    return reason
            elif not value:
                return reason
    return None


def catalog_route_rejection_reason(
    *,
    recipe: Recipe,
    tuple: ExecutionTupleSpec,
    model: ModelSpec | None,
    engine: EngineSpec | None,
    required_len: int,
    mode: ExecutionMode | None,
    required_input_contracts: set[str] | None,
    required_output_contract: str | None = None,
) -> str | None:
    output_contract = required_output_contract or recipe.output_contract
    requires_catalog = bool(recipe.required_model_capabilities or output_contract or tuple.model_ref or tuple.engine_ref)
    if not requires_catalog:
        return None
    if model is None and engine is None and (tuple.model_ref or tuple.engine_ref):
        # Direct compiler tests and third-party embedders may still pass only
        # legacy tuple specs. Full config loading validates catalog refs and
        # the gateway runtime passes catalog objects into the compiler.
        return None
    if tuple.model_ref and model is None:
        return "tuple model_ref is missing from model catalog"
    if tuple.engine_ref and engine is None:
        return "tuple engine_ref is missing from engine catalog"
    if recipe.required_model_capabilities:
        if model is None:
            return "recipe requires model capabilities but tuple has no model_ref"
        missing = sorted(set(recipe.required_model_capabilities) - set(model.capabilities))
        if missing:
            return "model capabilities missing: " + ", ".join(missing)
    if model is not None:
        if model.max_model_len < required_len:
            return "model catalog max_model_len is below required model length"
        if tuple.vram_gb < model.min_vram_gb:
            return "tuple vram_gb is below model catalog min_vram_gb"
        if required_input_contracts:
            missing_contracts = sorted(set(required_input_contracts) - set(model.input_contracts))
            if missing_contracts:
                return "model input_contracts missing: " + ", ".join(missing_contracts)
        if recipe.task == "vision" and not model.supports_vision:
            return "model does not declare vision support"
        if recipe.guided_decoding and output_contract in {"json_object", "json_schema"} and not model.supports_guided_decoding:
            return "model does not declare guided decoding support"
        if output_contract and model.output_contracts and output_contract not in model.output_contracts:
            return "model output_contracts missing: " + output_contract
    if engine is not None:
        if model is not None and model.supported_engines and engine.name not in model.supported_engines:
            return "engine is not listed in model supported_engines"
        if required_input_contracts:
            missing_engine_contracts = sorted(set(required_input_contracts) - set(engine.input_contracts))
            if missing_engine_contracts:
                return "engine input_contracts missing: " + ", ".join(missing_engine_contracts)
        if recipe.task == "vision" and not engine.supports_multimedia:
            return "engine does not declare multi-media support"
        if recipe.guided_decoding and output_contract in {"json_object", "json_schema"} and not engine.supports_guided_decoding:
            return "engine does not declare guided decoding support"
        if output_contract and engine.output_contracts and output_contract not in engine.output_contracts:
            return "engine output_contracts missing: " + output_contract
        if mode is ExecutionMode.STREAM and not engine.supports_streaming:
            return "engine does not declare streaming support"
        if required_input_contracts and "data_refs" in required_input_contracts and not engine.supports_data_refs:
            return "engine does not declare DataRef support"
    return None


def tuple_security_rejection_reason(*, policy: Policy, recipe: Recipe, tuple: ExecutionTupleSpec) -> str | None:
    profile = tuple.trust_profile
    if recipe.data_classification is DataClassification.RESTRICTED:
        if profile.dedicated_gpu:
            return None
        if profile.security_tier not in set(policy.security.restricted_requires):
            return "restricted data requires dedicated GPU or an approved security tier"
        if profile.security_tier is SecurityTier.CONFIDENTIAL_TEE and not profile.requires_attestation:
            return "restricted confidential TEE routing requires attestation evidence support"
    return None


def is_local_execution_tuple(tuple: ExecutionTupleSpec | None) -> bool:
    if tuple is None:
        return False
    descriptor = adapter_descriptor(tuple)
    return bool(descriptor and descriptor.local_execution)


def route_warning_tags(plan: CompiledPlan, tuples: dict[str, ExecutionTupleSpec] | None = None) -> list[str]:
    warnings: list[str] = []
    first_tuple_name = plan.tuple_chain[0] if plan.tuple_chain else None
    first_tuple = tuples.get(first_tuple_name) if tuples and first_tuple_name else None
    local_first = is_local_execution_tuple(first_tuple)
    if first_tuple_name and not local_first:
        warnings.append("remote_worker_cold_start_possible")
    if plan.input_refs:
        warnings.append("dataref_worker_fetch")
    if len(plan.tuple_chain) > 1:
        warnings.append("fallback_chain_enabled")
    if first_tuple_name and local_first:
        warnings.append("local_fallback_tuple")
    return warnings
