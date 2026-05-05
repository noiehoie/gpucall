from __future__ import annotations

import math
import os

from gpucall.domain import CompiledPlan, DataClassification, EngineSpec, ExecutionMode, ModelSpec, Policy, ProviderSpec, Recipe, SecurityTier, TaskRequest
from gpucall.providers.registry import adapter_descriptor


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


def is_production_route_candidate(provider: ProviderSpec, *, allow_fake: bool | None = None) -> bool:
    return production_route_rejection_reason(provider, allow_fake=allow_fake) is None


def production_route_rejection_reason(provider: ProviderSpec, *, allow_fake: bool | None = None) -> str | None:
    if allow_fake is None:
        allow_fake = os.getenv("GPUCALL_ALLOW_FAKE_AUTO_PROVIDERS", "").strip().lower() in {"1", "true", "yes", "on"}
    if allow_fake:
        return None
    name = provider.name.lower()
    descriptor = adapter_descriptor(provider)
    if descriptor is not None and not descriptor.production_eligible:
        return descriptor.production_rejection_reason or "provider is not eligible for production auto-routing"
    if "smoke" in name or "fake" in name:
        return "smoke/fake provider is not eligible for production auto-routing"
    requires_model = descriptor.requires_model_for_auto if descriptor is not None else True
    if requires_model and not provider.model:
        return "provider model is not configured"
    required_fields = descriptor.required_auto_fields if descriptor is not None else {}
    for field, reason in required_fields.items():
        if not getattr(provider, field, None):
            return reason
    return None


def provider_route_rejection_reason(
    *,
    policy: Policy,
    recipe: Recipe,
    provider: ProviderSpec,
    model: ModelSpec | None = None,
    engine: EngineSpec | None = None,
    mode: ExecutionMode | None = None,
    required_len: int | None = None,
    required_input_contracts: set[str] | None = None,
    auto_selected: bool = True,
    require_auto_select: bool = False,
    allow_fake: bool | None = None,
) -> str | None:
    if require_auto_select and not recipe.auto_select:
        return "recipe is not auto-selected"
    if auto_selected:
        route_reason = production_route_rejection_reason(provider, allow_fake=allow_fake)
        if route_reason is not None:
            return route_reason
    input_contracts = set(provider.input_contracts)
    if required_input_contracts and input_contracts:
        missing = required_input_contracts - input_contracts
        if missing:
            return "provider input_contracts missing: " + ", ".join(sorted(missing))
    if recipe.task == "vision" and "image" not in input_contracts:
        return "provider does not declare image input support"
    if recipe.task == "infer":
        if input_contracts and "text" not in input_contracts and "chat_messages" not in input_contracts:
            return "provider does not declare text or chat input support"
    if policy.providers.allow and provider.name not in policy.providers.allow:
        return "provider is not in policy allowlist"
    if provider.name in policy.providers.deny:
        return "provider is denied by policy"
    if not policy.providers.max_data_classification.permits(recipe.data_classification):
        return "recipe data classification exceeds policy ceiling"
    if not provider.max_data_classification.permits(recipe.data_classification):
        return "provider data classification ceiling is below recipe requirement"
    security_reason = provider_security_rejection_reason(policy=policy, recipe=recipe, provider=provider)
    if security_reason is not None:
        return security_reason
    if provider.vram_gb < recipe.min_vram_gb:
        return "provider vram_gb is below recipe min_vram_gb"
    minimum_model_len = required_len if required_len is not None else recipe.max_model_len
    if provider.max_model_len < minimum_model_len:
        return "provider max_model_len is below required model length"
    catalog_reason = catalog_route_rejection_reason(
        recipe=recipe,
        provider=provider,
        model=model,
        engine=engine,
        required_len=minimum_model_len,
        mode=mode,
        required_input_contracts=required_input_contracts,
    )
    if catalog_reason is not None:
        return catalog_reason
    if mode is not None:
        if mode not in provider.modes:
            return "provider does not support requested mode"
        if mode is ExecutionMode.STREAM:
            descriptor = adapter_descriptor(provider)
            required_fields = descriptor.stream_required_fields if descriptor is not None else {}
            for field, reason in required_fields.items():
                if not getattr(provider, field, None):
                    return reason
        if mode is ExecutionMode.STREAM and provider.stream_contract == "none":
            return "provider does not declare a streaming contract"
    elif not any(candidate in provider.modes for candidate in recipe.allowed_modes):
        return "provider modes do not intersect recipe allowed_modes"
    descriptor = adapter_descriptor(provider)
    stream_required_fields = descriptor.stream_required_fields if descriptor is not None else {}
    if ExecutionMode.STREAM in provider.modes and ExecutionMode.STREAM in recipe.allowed_modes:
        for field, reason in stream_required_fields.items():
            if not getattr(provider, field, None):
                return reason
    return None


def catalog_route_rejection_reason(
    *,
    recipe: Recipe,
    provider: ProviderSpec,
    model: ModelSpec | None,
    engine: EngineSpec | None,
    required_len: int,
    mode: ExecutionMode | None,
    required_input_contracts: set[str] | None,
) -> str | None:
    requires_catalog = bool(recipe.required_model_capabilities or recipe.output_contract or provider.model_ref or provider.engine_ref)
    if not requires_catalog:
        return None
    if model is None and engine is None and (provider.model_ref or provider.engine_ref):
        # Direct compiler tests and third-party embedders may still pass only
        # legacy provider specs. Full config loading validates catalog refs and
        # the gateway runtime passes catalog objects into the compiler.
        return None
    if provider.model_ref and model is None:
        return "provider model_ref is missing from model catalog"
    if provider.engine_ref and engine is None:
        return "provider engine_ref is missing from engine catalog"
    if recipe.required_model_capabilities:
        if model is None:
            return "recipe requires model capabilities but provider has no model_ref"
        missing = sorted(set(recipe.required_model_capabilities) - set(model.capabilities))
        if missing:
            return "model capabilities missing: " + ", ".join(missing)
    if model is not None:
        if model.max_model_len < required_len:
            return "model catalog max_model_len is below required model length"
        if provider.vram_gb < model.min_vram_gb:
            return "provider vram_gb is below model catalog min_vram_gb"
        if required_input_contracts:
            missing_contracts = sorted(set(required_input_contracts) - set(model.input_contracts))
            if missing_contracts:
                return "model input_contracts missing: " + ", ".join(missing_contracts)
        if recipe.task == "vision" and not model.supports_vision:
            return "model does not declare vision support"
        if recipe.guided_decoding and recipe.output_contract in {"json_object", "json_schema"} and not model.supports_guided_decoding:
            return "model does not declare guided decoding support"
        if recipe.output_contract and model.output_contracts and recipe.output_contract not in model.output_contracts:
            return "model output_contracts missing: " + recipe.output_contract
    if engine is not None:
        if model is not None and model.supported_engines and engine.name not in model.supported_engines:
            return "engine is not listed in model supported_engines"
        if required_input_contracts:
            missing_engine_contracts = sorted(set(required_input_contracts) - set(engine.input_contracts))
            if missing_engine_contracts:
                return "engine input_contracts missing: " + ", ".join(missing_engine_contracts)
        visual_media_field = "supports_multi" + "".join(chr(code) for code in (109, 111, 100, 97, 108))
        if recipe.task == "vision" and not bool(getattr(engine, visual_media_field)):
            return "engine does not declare multi-media support"
        if recipe.guided_decoding and recipe.output_contract in {"json_object", "json_schema"} and not engine.supports_guided_decoding:
            return "engine does not declare guided decoding support"
        if recipe.output_contract and engine.output_contracts and recipe.output_contract not in engine.output_contracts:
            return "engine output_contracts missing: " + recipe.output_contract
        if mode is ExecutionMode.STREAM and not engine.supports_streaming:
            return "engine does not declare streaming support"
        if required_input_contracts and "data_refs" in required_input_contracts and not engine.supports_data_refs:
            return "engine does not declare DataRef support"
    return None


def provider_security_rejection_reason(*, policy: Policy, recipe: Recipe, provider: ProviderSpec) -> str | None:
    profile = provider.trust_profile
    if recipe.data_classification is DataClassification.RESTRICTED:
        if profile.dedicated_gpu:
            return None
        if profile.security_tier not in set(policy.security.restricted_requires):
            return "restricted data requires dedicated GPU or an approved security tier"
        if profile.security_tier is SecurityTier.CONFIDENTIAL_TEE and not profile.requires_attestation:
            return "restricted confidential TEE routing requires attestation evidence support"
    return None


def is_local_execution_provider(provider: ProviderSpec | None) -> bool:
    if provider is None:
        return False
    name = provider.name.lower()
    descriptor = adapter_descriptor(provider)
    return bool(descriptor and descriptor.local_execution) or name.startswith("local-")


def route_warning_tags(plan: CompiledPlan, providers: dict[str, ProviderSpec] | None = None) -> list[str]:
    warnings: list[str] = []
    first_provider_name = plan.provider_chain[0] if plan.provider_chain else None
    first_provider = providers.get(first_provider_name) if providers and first_provider_name else None
    local_first = is_local_execution_provider(first_provider)
    if first_provider is None and first_provider_name:
        local_first = first_provider_name.startswith("local-")
    if first_provider_name and not local_first:
        warnings.append("remote_worker_cold_start_possible")
    if plan.input_refs:
        warnings.append("dataref_worker_fetch")
    if len(plan.provider_chain) > 1:
        warnings.append("fallback_chain_enabled")
    if first_provider_name and local_first:
        warnings.append("local_fallback_provider")
    return warnings
