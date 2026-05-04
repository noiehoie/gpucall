from __future__ import annotations

import pytest

from gpucall.compiler import GovernanceCompiler, GovernanceError
from datetime import datetime, timedelta, timezone

from gpucall.domain import DataRef, ExecutionMode, Policy, ProviderPolicy, ProviderSpec, Recipe, TaskRequest
from gpucall.registry import ObservedRegistry
from gpucall.domain import ProviderObservation


def build_compiler() -> GovernanceCompiler:
    policy = Policy(
        version="test",
        inline_bytes_limit=10,
        default_lease_ttl_seconds=30,
        max_lease_ttl_seconds=60,
        max_timeout_seconds=30,
        tokenizer_safety_multiplier=1.25,
        providers=ProviderPolicy(allow=["p1", "p2"], deny=[]),
    )
    recipe = Recipe(
        name="r1",
        task="infer",
        allowed_modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
        min_vram_gb=24,
        max_model_len=100,
        timeout_seconds=10,
        lease_ttl_seconds=20,
        tokenizer_family="qwen",
    )
    providers = {
        "p1": ProviderSpec(
            name="p1",
            adapter="modal",
            gpu="L4",
            vram_gb=24,
            max_model_len=100,
            cost_per_second=1,
            modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
            target="app:fn",
            model="test-model-small",
        ),
        "p2": ProviderSpec(
            name="p2",
            adapter="modal",
            gpu="L4",
            vram_gb=24,
            max_model_len=100,
            cost_per_second=1,
            modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
            target="app:fn",
            model="test-model-large",
        ),
    }
    return GovernanceCompiler(policy=policy, recipes={"r1": recipe}, providers=providers, registry=ObservedRegistry())


def test_compiler_applies_tokenizer_safety_margin() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", max_tokens=80)

    plan = compiler.compile(request)

    assert plan.token_budget == 100


def test_compiler_auto_selects_recipe_when_omitted() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", max_tokens=80)

    plan = compiler.compile(request)

    assert plan.recipe_name == "r1"
    assert plan.provider_chain == ["p1", "p2"]


def test_compiler_preserves_chat_messages_and_recipe_generation_contract() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(
        update={
            "system_prompt": "Answer directly.",
            "structured_system_prompt": "Return JSON only.",
            "default_temperature": 0.7,
            "structured_temperature": 0.0,
            "stop_tokens": ["<stop>"],
            "repetition_penalty": 1.05,
            "guided_decoding": True,
        }
    )

    plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode="sync",
            messages=[{"role": "system", "content": "caller sys"}, {"role": "user", "content": "hello"}],
            response_format={"type": "json_object"},
        )
    )

    assert [message.model_dump() for message in plan.messages] == [
        {"role": "system", "content": "Return JSON only."},
        {"role": "system", "content": "caller sys"},
        {"role": "user", "content": "hello"},
    ]
    assert plan.system_prompt == "Return JSON only."
    assert plan.temperature == 0.0
    assert plan.stop_tokens == ["<stop>"]
    assert plan.repetition_penalty == 1.05
    assert plan.guided_decoding is True
    assert plan.attestations["context_estimate"]["method"] == "utf8_bytes_times_policy_safety_multiplier_plus_output_budget"
    snapshot = plan.attestations["recipe_snapshot"]
    assert snapshot["system_prompt"]["redacted"] is True
    assert snapshot["structured_system_prompt"]["redacted"] is True
    assert "Answer directly." not in str(snapshot)
    assert "Return JSON only." not in str(snapshot)


def test_compiler_auto_selects_smallest_capable_recipe_for_weight() -> None:
    compiler = build_compiler()
    small = compiler.recipes["r1"].model_copy(
        update={
            "name": "small",
            "max_model_len": 100,
            "max_input_bytes": 100,
        }
    )
    large = compiler.recipes["r1"].model_copy(
        update={
            "name": "large",
            "max_model_len": 1000,
            "max_input_bytes": 1000,
        }
    )
    compiler.recipes = {"small": small, "large": large}
    compiler.providers["p2"] = compiler.providers["p2"].model_copy(update={"max_model_len": 1000})

    small_plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode="sync",
            input_refs=[DataRef(uri="s3://bucket/small.txt", sha256="a" * 64, bytes=50, content_type="text/plain")],
        )
    )
    large_plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode="sync",
            input_refs=[DataRef(uri="s3://bucket/large.txt", sha256="b" * 64, bytes=500, content_type="text/plain")],
        )
    )

    assert small_plan.recipe_name == "small"
    assert small_plan.provider_chain == ["p1", "p2"]
    assert large_plan.recipe_name == "large"
    assert large_plan.provider_chain == ["p2"]


def test_compiler_reports_structured_context_when_no_recipe_can_fit() -> None:
    compiler = build_compiler()
    request = TaskRequest(
        task="infer",
        mode="sync",
        input_refs=[DataRef(uri="s3://bucket/too-large.txt", sha256="a" * 64, bytes=500, content_type="text/plain")],
    )

    with pytest.raises(GovernanceError) as exc_info:
        compiler.compile(request)

    assert exc_info.value.code == "NO_AUTO_SELECTABLE_RECIPE"
    assert exc_info.value.context["required_model_len"] > exc_info.value.context["largest_auto_recipe_model_len"]


def test_runpod_provider_without_endpoint_is_not_auto_routed() -> None:
    compiler = build_compiler()
    compiler.policy.providers.allow.append("runpod")
    compiler.providers["runpod"] = ProviderSpec(
        name="runpod",
        adapter="runpod-vllm-serverless",
        gpu="L4",
        vram_gb=24,
        max_model_len=100,
        cost_per_second=0,
        model="Qwen/Qwen2.5-1.5B-Instruct",
        image="runpod/worker-v1-vllm:v2.18.1",
        target="",
    )

    plan = compiler.compile(TaskRequest(task="infer", mode="sync", max_tokens=10))

    assert "runpod" not in plan.provider_chain


def test_modal_stream_requires_explicit_stream_target() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(update={"allowed_modes": [ExecutionMode.STREAM]})
    compiler.providers["p1"] = compiler.providers["p1"].model_copy(update={"modes": [ExecutionMode.STREAM], "stream_target": None})
    compiler.providers["p2"] = compiler.providers["p2"].model_copy(
        update={"modes": [ExecutionMode.STREAM], "stream_target": "app:stream", "stream_contract": "token-incremental"}
    )

    plan = compiler.compile(TaskRequest(task="infer", mode="stream", max_tokens=10))

    assert plan.provider_chain == ["p2"]


def test_compiler_auto_selection_ignores_non_auto_recipes() -> None:
    compiler = build_compiler()
    smoke = compiler.recipes["r1"].model_copy(update={"name": "smoke", "auto_select": False})
    compiler.recipes = {"smoke": smoke}
    request = TaskRequest(task="infer", mode="sync")

    with pytest.raises(GovernanceError, match="no auto-selectable recipe"):
        compiler.compile(request)


def test_compiler_auto_selection_uses_request_content_type() -> None:
    compiler = build_compiler()
    compiler.providers = {
        name: provider.model_copy(update={"input_contracts": ["text", "data_refs", "image"]})
        for name, provider in compiler.providers.items()
    }
    text_recipe = compiler.recipes["r1"].model_copy(update={"name": "text", "allowed_mime_prefixes": ["text/"]})
    image_recipe = compiler.recipes["r1"].model_copy(
        update={"name": "image", "task": "vision", "allowed_mime_prefixes": ["image/"]}
    )
    compiler.recipes = {"text": text_recipe, "image": image_recipe}
    request = TaskRequest(
        task="vision",
        mode="sync",
        input_refs=[DataRef(uri="s3://bucket/image.png", sha256="a" * 64, bytes=10, content_type="image/png")],
    )

    plan = compiler.compile(request)

    assert plan.recipe_name == "image"


def test_vision_recipe_allows_text_prompt_as_inline_companion() -> None:
    compiler = build_compiler()
    compiler.providers = {
        name: provider.model_copy(update={"input_contracts": ["text", "data_refs", "image"]})
        for name, provider in compiler.providers.items()
    }
    vision = compiler.recipes["r1"].model_copy(
        update={
            "name": "vision",
            "task": "vision",
            "allowed_mime_prefixes": ["image/"],
            "allowed_inline_mime_prefixes": ["text/"],
        }
    )
    compiler.recipes = {"vision": vision}
    request = TaskRequest(
        task="vision",
        mode="sync",
        input_refs=[DataRef(uri="s3://bucket/image.png", sha256="a" * 64, bytes=10, content_type="image/png")],
        inline_inputs={"prompt": {"value": "look", "content_type": "text/plain"}},
    )

    plan = compiler.compile(request)

    assert plan.recipe_name == "vision"


def test_vision_recipe_rejects_non_image_refs_even_when_text_prompt_is_allowed() -> None:
    compiler = build_compiler()
    vision = compiler.recipes["r1"].model_copy(
        update={
            "name": "vision",
            "task": "vision",
            "allowed_mime_prefixes": ["image/"],
            "allowed_inline_mime_prefixes": ["text/"],
        }
    )
    compiler.recipes = {"vision": vision}
    request = TaskRequest(
        task="vision",
        mode="sync",
        input_refs=[DataRef(uri="s3://bucket/not-image.txt", sha256="a" * 64, bytes=10, content_type="text/plain")],
        inline_inputs={"prompt": {"value": "look", "content_type": "text/plain"}},
    )

    with pytest.raises(GovernanceError, match="no auto-selectable recipe"):
        compiler.compile(request)


def test_vision_recipe_rejects_missing_image_ref() -> None:
    compiler = build_compiler()
    vision = compiler.recipes["r1"].model_copy(update={"name": "vision", "task": "vision"})
    compiler.recipes = {"vision": vision}

    with pytest.raises(GovernanceError, match="vision requires an image data_ref"):
        compiler.compile(TaskRequest(task="vision", mode="sync", recipe="vision"))


def test_compiler_rejects_messages_mixed_with_inline_inputs() -> None:
    compiler = build_compiler()
    request = TaskRequest(
        task="infer",
        mode="sync",
        recipe="r1",
        messages=[{"role": "user", "content": "hello"}],
        inline_inputs={"prompt": {"value": "also hello", "content_type": "text/plain"}},
    )

    with pytest.raises(GovernanceError, match="messages cannot be combined"):
        compiler.compile(request)


def test_compiler_rejects_margin_over_model_limit() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", max_tokens=81)

    with pytest.raises(GovernanceError):
        compiler.compile(request)


def test_compiler_rejects_large_inline_payload() -> None:
    compiler = build_compiler()
    request = TaskRequest(
        task="infer",
        mode="sync",
        recipe="r1",
        inline_inputs={"prompt": {"value": "this is too long"}},
    )

    with pytest.raises(GovernanceError):
        compiler.compile(request)


def test_compiler_rejects_unsupported_mvp_task() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="train", mode="sync", recipe="r1")

    with pytest.raises(GovernanceError, match="unsupported task"):
        compiler.compile(request)


def test_compiler_carries_response_format_into_plan() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", response_format={"type": "json_object"})

    plan = compiler.compile(request)

    assert plan.response_format.type == "json_object"


def test_compiler_carries_generation_params_into_plan() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", max_tokens=64, temperature=0.0)

    plan = compiler.compile(request)

    assert plan.max_tokens == 64
    assert plan.temperature == 0.0


def test_compiler_rejects_response_format_for_stream_mode() -> None:
    compiler = build_compiler()
    recipe = compiler.recipes["r1"].model_copy(update={"allowed_modes": [ExecutionMode.STREAM]})
    compiler.recipes = {"r1": recipe}
    request = TaskRequest(task="infer", mode="stream", recipe="r1", response_format={"type": "json_object"})

    with pytest.raises(GovernanceError, match="response_format"):
        compiler.compile(request)


def test_compiler_rejects_expired_data_ref() -> None:
    compiler = build_compiler()
    request = TaskRequest(
        task="infer",
        mode="sync",
        recipe="r1",
        input_refs=[DataRef(uri="s3://bucket/key", sha256="a" * 64, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))],
    )

    with pytest.raises(GovernanceError, match="expired"):
        compiler.compile(request)


def test_open_circuit_provider_is_skipped() -> None:
    compiler = build_compiler()
    compiler.registry.breakers["p1"].open = True
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.provider_chain == ["p2"]


def test_observed_registry_ranks_all_eligible_providers() -> None:
    compiler = build_compiler()
    compiler.registry.record(ProviderObservation(provider="p1", latency_ms=1000, success=True, cost=10))
    compiler.registry.record(ProviderObservation(provider="p2", latency_ms=1, success=True, cost=0))
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.provider_chain == ["p2", "p1"]


def test_provider_chain_prefers_smallest_capable_provider_before_observations() -> None:
    compiler = build_compiler()
    compiler.providers["p1"] = compiler.providers["p1"].model_copy(update={"vram_gb": 80, "max_model_len": 32768})
    compiler.providers["p2"] = compiler.providers["p2"].model_copy(update={"vram_gb": 24, "max_model_len": 100})
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.provider_chain == ["p2", "p1"]


def test_provider_fit_dominates_observed_score_across_different_fit_classes() -> None:
    compiler = build_compiler()
    compiler.providers["p1"] = compiler.providers["p1"].model_copy(update={"vram_gb": 80, "max_model_len": 32768})
    compiler.providers["p2"] = compiler.providers["p2"].model_copy(update={"vram_gb": 24, "max_model_len": 100})
    compiler.registry.record(ProviderObservation(provider="p1", latency_ms=1, success=True, cost=0))
    compiler.registry.record(ProviderObservation(provider="p2", latency_ms=1000, success=True, cost=100))
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.provider_chain == ["p2", "p1"]


def test_provider_chain_filters_by_request_weight() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(update={"max_model_len": 1000})
    compiler.providers["p2"] = compiler.providers["p2"].model_copy(update={"max_model_len": 1000})
    request = TaskRequest(
        task="infer",
        mode="sync",
        recipe="r1",
        input_refs=[DataRef(uri="s3://bucket/heavy.txt", sha256="a" * 64, bytes=500, content_type="text/plain")],
    )

    plan = compiler.compile(request)

    assert plan.provider_chain == ["p2"]


def test_requested_provider_must_be_eligible() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", requested_provider="missing")

    with pytest.raises(GovernanceError, match="requested provider"):
        compiler.compile(request)


def test_requested_provider_does_not_fall_back_when_circuit_is_open() -> None:
    compiler = build_compiler()
    compiler.registry.breakers["p1"].open = True
    compiler.registry.breakers["p1"].opened_at = datetime.now(timezone.utc).timestamp()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", requested_provider="p1")

    with pytest.raises(GovernanceError, match="circuit breaker"):
        compiler.compile(request)


def test_requested_provider_is_single_provider_chain() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", requested_provider="p1")

    plan = compiler.compile(request)

    assert plan.provider_chain == ["p1"]


def test_open_circuit_allows_half_open_after_timeout() -> None:
    registry = ObservedRegistry()
    breaker = registry.breakers["p1"]
    breaker.recovery_timeout_seconds = 0
    for _ in range(breaker.failure_threshold):
        registry.record(ProviderObservation(provider="p1", latency_ms=1, success=False, cost=0))

    assert registry.is_available("p1") is True


def test_observed_registry_preserves_input_order_for_equal_scores() -> None:
    registry = ObservedRegistry()

    assert registry.rank(["p2", "p1"]) == ["p2", "p1"]


def test_observed_registry_persists_observations(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ObservedRegistry(path=path)

    registry.record(ProviderObservation(provider="p1", latency_ms=12, success=True, cost=0.5))
    loaded = ObservedRegistry(path=path)

    score = loaded.score("p1")
    assert score.samples == 1
    assert score.p50_latency_ms == 12


def test_observed_registry_persists_circuit_breaker_state(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ObservedRegistry(path=path)
    for _ in range(3):
        registry.record(ProviderObservation(provider="p1", latency_ms=1, success=False, cost=0))

    loaded = ObservedRegistry(path=path)

    assert loaded.breakers["p1"].open is True


def test_observed_registry_caps_observations_per_provider(tmp_path) -> None:
    registry = ObservedRegistry(path=tmp_path / "registry.db", max_observations_per_provider=3)
    for i in range(5):
        registry.record(ProviderObservation(provider="p1", latency_ms=i, success=True, cost=0))

    loaded = ObservedRegistry(path=tmp_path / "registry.db", max_observations_per_provider=3)

    assert loaded.score("p1").samples == 3
    assert [row.latency_ms for row in loaded.observations["p1"]] == [2, 3, 4]
