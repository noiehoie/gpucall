from __future__ import annotations

import math
import os

from gpucall.domain import CompiledPlan, DataClassification, ExecutionMode, Policy, ProviderSpec, Recipe, SecurityTier, TaskRequest
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
