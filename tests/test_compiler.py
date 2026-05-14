from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gpucall.compiler import GovernanceCompiler, GovernanceError
from gpucall.domain import CostPolicy, DataRef, EngineSpec, ExecutionMode, ModelSpec, Policy, TuplePolicy, ExecutionTupleSpec, Recipe, RecipeQualityFloor, ResponseFormat, ResponseFormatType, TaskRequest
from gpucall.domain import TupleObservation
from gpucall.registry import ObservedRegistry


def build_compiler() -> GovernanceCompiler:
    policy = Policy(
        version="test",
        inline_bytes_limit=10,
        default_lease_ttl_seconds=30,
        max_lease_ttl_seconds=60,
        max_timeout_seconds=30,
        tokenizer_safety_multiplier=1.25,
        tuples=TuplePolicy(allow=["p1", "p2"], deny=[]),
    )
    recipe = Recipe(
        name="r1",
        task="infer",
        allowed_modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
        min_vram_gb=24,
        max_model_len=100,
        timeout_seconds=10,
        lease_ttl_seconds=20,
        token_estimation_profile="qwen",
    )
    tuples = {
        "p1": ExecutionTupleSpec(
            name="p1",
            adapter="modal",
            gpu="L4",
            vram_gb=24,
            max_model_len=100,
            cost_per_second=0.001,
            modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
            target="app:fn",
            model="test-model-small",
        ),
        "p2": ExecutionTupleSpec(
            name="p2",
            adapter="modal",
            gpu="L4",
            vram_gb=24,
            max_model_len=100,
            cost_per_second=0.001,
            modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
            target="app:fn",
            model="test-model-large",
        ),
    }
    return GovernanceCompiler(policy=policy, recipes={"r1": recipe}, tuples=tuples, registry=ObservedRegistry())


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
    assert plan.tuple_chain == ["p1", "p2"]


def test_compiler_auto_selects_matching_intent_when_provided() -> None:
    compiler = build_compiler()
    extract = compiler.recipes["r1"].model_copy(update={"name": "extract", "intent": "extract_json"})
    translate = compiler.recipes["r1"].model_copy(update={"name": "translate", "intent": "translate_text"})
    compiler.recipes = {"extract": extract, "translate": translate}

    plan = compiler.compile(TaskRequest(task="infer", mode="sync", intent="translate_text"))

    assert plan.recipe_name == "translate"


def test_compiler_intentless_selection_prefers_production_quality_recipe() -> None:
    compiler = build_compiler()
    draft = compiler.recipes["r1"].model_copy(
        update={
            "name": "draft",
            "intent": "standard_text_inference",
            "quality_floor": RecipeQualityFloor.DRAFT,
            "context_budget_tokens": 100,
            "max_input_bytes": 100,
        }
    )
    standard = compiler.recipes["r1"].model_copy(
        update={
            "name": "standard",
            "intent": "standard_text_inference",
            "quality_floor": RecipeQualityFloor.STANDARD,
            "context_budget_tokens": 1000,
            "max_input_bytes": 1000,
        }
    )
    compiler.recipes = {"draft": draft, "standard": standard}
    compiler.tuples["p2"] = compiler.tuples["p2"].model_copy(update={"max_model_len": 1000})

    plan = compiler.compile(TaskRequest(task="infer", mode="sync", max_tokens=20))

    assert plan.recipe_name == "standard"


def test_compiler_ignores_intent_specific_recipes_without_request_intent() -> None:
    compiler = build_compiler()
    generic = compiler.recipes["r1"].model_copy(
        update={"name": "generic", "intent": "standard_text_inference", "context_budget_tokens": 1000}
    )
    specific = compiler.recipes["r1"].model_copy(update={"name": "specific", "intent": "extract_json"})
    compiler.recipes = {"generic": generic, "specific": specific}

    plan = compiler.compile(TaskRequest(task="infer", mode="sync"))

    assert plan.recipe_name == "generic"


def test_compiler_fails_closed_when_intent_has_no_auto_recipe() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(update={"intent": "extract_json"})

    with pytest.raises(GovernanceError) as exc_info:
        compiler.compile(TaskRequest(task="infer", mode="sync", intent="translate_text"))

    assert exc_info.value.code == "NO_AUTO_SELECTABLE_RECIPE"
    assert "intent is 'extract_json'" in str(exc_info.value)


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

    assert [message.model_dump(exclude_none=True) for message in plan.messages] == [
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


def test_compiler_counts_tool_calls_in_context_estimate() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(update={"context_budget_tokens": 1000})
    compiler.tuples = {
        name: spec.model_copy(
            update={
                "max_model_len": 1000,
                "endpoint_contract": "openai-chat-completions",
                "output_contract": "openai-chat-completions",
            }
        )
        for name, spec in compiler.tuples.items()
    }
    base = compiler.compile(
        TaskRequest(
            task="infer",
            mode="sync",
            messages=[{"role": "user", "content": "hello"}],
        )
    )
    with_tool_call = compiler.compile(
        TaskRequest(
            task="infer",
            mode="sync",
            messages=[
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{\"query\":\"long context accounting\"}"},
                        }
                    ],
                },
            ],
        )
    )

    assert with_tool_call.attestations["context_estimate"]["required_model_len"] > base.attestations["context_estimate"]["required_model_len"]


def test_compiler_routes_inline_text_to_chat_only_openai_tuple() -> None:
    compiler = build_compiler()
    compiler.policy = compiler.policy.model_copy(update={"tuples": TuplePolicy(allow=["chat"], deny=[])})
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(
        update={
            "required_model_capabilities": ["summarization"],
            "output_contract": "plain-text",
        }
    )
    compiler.tuples = {
        "chat": ExecutionTupleSpec(
            name="chat",
            adapter="runpod-vllm-serverless",
            execution_surface="managed_endpoint",
            gpu="RUNPOD_RTX4000_ADA",
            vram_gb=20,
            max_model_len=32768,
            cost_per_second=0.00016,
            modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
            target="rp-endpoint",
            model_ref="qwen-chat",
            engine_ref="runpod-vllm-openai",
            input_contracts=["chat_messages"],
            output_contract="openai-chat-completions",
            endpoint_contract="openai-chat-completions",
            model="Qwen/Qwen2.5-1.5B-Instruct",
        )
    }
    compiler.models = {
        "qwen-chat": ModelSpec(
            name="qwen-chat",
            provider_model_id="Qwen/Qwen2.5-1.5B-Instruct",
            capabilities=["summarization"],
            max_model_len=32768,
            min_vram_gb=16,
            supported_engines=["runpod-vllm-openai"],
            input_contracts=["chat_messages"],
            output_contracts=["plain-text", "openai-chat-completions"],
        )
    }
    compiler.engines = {
        "runpod-vllm-openai": EngineSpec(
            name="runpod-vllm-openai",
            kind="openai-compatible-chat",
            input_contracts=["chat_messages"],
            output_contracts=["openai-chat-completions"],
        )
    }

    plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode="sync",
            recipe="r1",
            inline_inputs={"prompt": {"value": "hi", "content_type": "text/plain"}},
            max_tokens=8,
        )
    )

    assert plan.tuple_chain == ["chat"]


def test_compiler_treats_openai_chat_contract_as_structured_output_capable() -> None:
    compiler = build_compiler()
    compiler.policy = compiler.policy.model_copy(update={"tuples": TuplePolicy(allow=["chat"], deny=[])})
    compiler.tuples = {
        "chat": ExecutionTupleSpec(
            name="chat",
            adapter="runpod-vllm-serverless",
            execution_surface="managed_endpoint",
            gpu="RUNPOD_RTX4000_ADA",
            vram_gb=20,
            max_model_len=32768,
            cost_per_second=0.00016,
            modes=[ExecutionMode.SYNC],
            target="rp-endpoint",
            model_ref="qwen-chat",
            engine_ref="runpod-vllm-openai",
            input_contracts=["chat_messages"],
            output_contract="openai-chat-completions",
            endpoint_contract="openai-chat-completions",
            model="Qwen/Qwen2.5-1.5B-Instruct",
        )
    }
    compiler.models = {
        "qwen-chat": ModelSpec(
            name="qwen-chat",
            provider_model_id="Qwen/Qwen2.5-1.5B-Instruct",
            max_model_len=32768,
            min_vram_gb=16,
            supported_engines=["runpod-vllm-openai"],
            input_contracts=["chat_messages"],
            output_contracts=["openai-chat-completions"],
        )
    }
    compiler.engines = {
        "runpod-vllm-openai": EngineSpec(
            name="runpod-vllm-openai",
            kind="openai-compatible-chat",
            input_contracts=["chat_messages"],
            output_contracts=["openai-chat-completions"],
        )
    }

    plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode="sync",
            recipe="r1",
            response_format={"type": "json_schema", "json_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
        )
    )

    assert plan.tuple_chain == ["chat"]


def test_compiler_auto_selects_smallest_capable_recipe_for_weight() -> None:
    compiler = build_compiler()
    small = compiler.recipes["r1"].model_copy(
        update={
            "name": "small",
            "context_budget_tokens": 100,
            "max_input_bytes": 100,
        }
    )
    large = compiler.recipes["r1"].model_copy(
        update={
            "name": "large",
            "context_budget_tokens": 1000,
            "max_input_bytes": 1000,
        }
    )
    compiler.recipes = {"small": small, "large": large}
    compiler.tuples["p2"] = compiler.tuples["p2"].model_copy(update={"max_model_len": 1000})

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
    assert small_plan.tuple_chain == ["p1", "p2"]
    assert large_plan.recipe_name == "large"
    assert large_plan.tuple_chain == ["p2"]


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
    compiler.policy.tuples.allow.append("runpod")
    compiler.tuples["runpod"] = ExecutionTupleSpec(
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

    assert "runpod" not in plan.tuple_chain


def test_runpod_provider_with_placeholder_endpoint_is_not_auto_routed() -> None:
    compiler = build_compiler()
    compiler.policy.tuples.allow.append("runpod")
    compiler.tuples["runpod"] = ExecutionTupleSpec(
        name="runpod",
        adapter="runpod-vllm-serverless",
        gpu="L4",
        vram_gb=24,
        max_model_len=100,
        cost_per_second=0,
        model="Qwen/Qwen2.5-1.5B-Instruct",
        image="runpod/worker-v1-vllm:v2.18.1",
        target="RUNPOD_ENDPOINT_ID_PLACEHOLDER",
    )

    plan = compiler.compile(TaskRequest(task="infer", mode="sync", max_tokens=10))

    assert "runpod" not in plan.tuple_chain
    assert compiler._tuple_rejections(TaskRequest(task="infer", mode="sync", max_tokens=10), compiler.recipes["r1"], auto_selected=True)["runpod"] == (
        "RunPod endpoint target is not configured"
    )


def test_hyperstack_provider_with_documentation_cidr_is_not_auto_routed() -> None:
    compiler = build_compiler()
    compiler.policy.tuples.allow.append("hyperstack-doc")
    compiler.tuples["hyperstack-doc"] = ExecutionTupleSpec(
        name="hyperstack-doc",
        adapter="hyperstack",
        gpu="A100",
        vram_gb=80,
        max_model_len=32768,
        cost_per_second=0.001,
        modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
        target="default-CANADA-1",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        instance="n3-A100x1",
        image="Ubuntu Server 22.04 LTS R570 CUDA 12.8 with Docker",
        key_name="gpucall-key",
        ssh_remote_cidr="203.0.113.10/32",
    )

    plan = compiler.compile(TaskRequest(task="infer", mode="sync", max_tokens=10))

    assert "hyperstack-doc" not in plan.tuple_chain
    assert compiler._tuple_rejections(TaskRequest(task="infer", mode="sync", max_tokens=10), compiler.recipes["r1"], auto_selected=True)[
        "hyperstack-doc"
    ] == "IaaS ssh_remote_cidr is not configured for live execution"


def test_modal_stream_requires_explicit_stream_target() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(update={"allowed_modes": [ExecutionMode.STREAM]})
    compiler.tuples["p1"] = compiler.tuples["p1"].model_copy(update={"modes": [ExecutionMode.STREAM], "stream_target": None})
    compiler.tuples["p2"] = compiler.tuples["p2"].model_copy(
        update={"modes": [ExecutionMode.STREAM], "stream_target": "app:stream", "stream_contract": "token-incremental"}
    )

    plan = compiler.compile(TaskRequest(task="infer", mode="stream", max_tokens=10))

    assert plan.tuple_chain == ["p2"]


def test_compiler_auto_selection_ignores_non_auto_recipes() -> None:
    compiler = build_compiler()
    smoke = compiler.recipes["r1"].model_copy(update={"name": "smoke", "auto_select": False})
    compiler.recipes = {"smoke": smoke}
    request = TaskRequest(task="infer", mode="sync")

    with pytest.raises(GovernanceError, match="no auto-selectable recipe"):
        compiler.compile(request)


def test_compiler_auto_selection_uses_request_content_type() -> None:
    compiler = build_compiler()
    compiler.tuples = {
        name: tuple.model_copy(update={"input_contracts": ["text", "data_refs", "image"], "max_model_len": 512})
        for name, tuple in compiler.tuples.items()
    }
    text_recipe = compiler.recipes["r1"].model_copy(update={"name": "text", "allowed_mime_prefixes": ["text/"]})
    image_recipe = compiler.recipes["r1"].model_copy(
        update={"name": "image", "task": "vision", "allowed_mime_prefixes": ["image/"], "context_budget_tokens": 512}
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
    compiler.tuples = {
        name: tuple.model_copy(update={"input_contracts": ["text", "data_refs", "image"], "max_model_len": 512})
        for name, tuple in compiler.tuples.items()
    }
    vision = compiler.recipes["r1"].model_copy(
        update={
            "name": "vision",
            "task": "vision",
            "allowed_mime_prefixes": ["image/"],
            "allowed_inline_mime_prefixes": ["text/"],
            "context_budget_tokens": 512,
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


def test_compiler_rejects_unconfigured_train_recipe() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="train", mode="sync", recipe="r1")

    with pytest.raises(GovernanceError, match="does not match recipe task"):
        compiler.compile(request)


def test_compiler_carries_response_format_into_plan() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", response_format={"type": "json_object"})

    plan = compiler.compile(request)

    assert plan.response_format.type == "json_object"


def test_compiler_normalizes_openai_json_schema_response_format() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(update={"guided_decoding": True})
    compiler.tuples["p1"] = compiler.tuples["p1"].model_copy(update={"model_ref": "json-model", "engine_ref": "json-engine"})
    compiler.models = {
        "json-model": ModelSpec(
            name="json-model",
            provider_model_id="json-model",
            max_model_len=100,
            min_vram_gb=1,
            input_contracts=["text"],
            output_contracts=["plain-text", "json_schema"],
            supports_guided_decoding=True,
        )
    }
    compiler.engines = {
        "json-engine": EngineSpec(
            name="json-engine",
            kind="test",
            input_contracts=["text"],
            output_contracts=["plain-text", "json_schema"],
            supports_guided_decoding=True,
        )
    }
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}

    plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode="sync",
            recipe="r1",
            response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": False}},
        )
    )

    assert plan.response_format is not None
    assert plan.response_format.json_schema == schema
    assert plan.response_format.strict is False


def test_response_format_requires_structured_output_capable_route() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(update={"guided_decoding": True})
    compiler.tuples["p1"] = compiler.tuples["p1"].model_copy(update={"model_ref": "plain", "engine_ref": "plain-engine"})
    compiler.tuples["p2"] = compiler.tuples["p2"].model_copy(update={"model_ref": "json", "engine_ref": "json-engine"})
    compiler.models = {
        "plain": ModelSpec(
            name="plain",
            provider_model_id="plain",
            max_model_len=100,
            min_vram_gb=1,
            input_contracts=["text"],
            output_contracts=["plain-text"],
            supports_guided_decoding=False,
        ),
        "json": ModelSpec(
            name="json",
            provider_model_id="json",
            max_model_len=100,
            min_vram_gb=1,
            input_contracts=["text"],
            output_contracts=["plain-text", "json_object", "json_schema"],
            supports_guided_decoding=True,
        ),
    }
    compiler.engines = {
        "plain-engine": EngineSpec(
            name="plain-engine",
            kind="test",
            input_contracts=["text"],
            output_contracts=["plain-text"],
            supports_guided_decoding=False,
        ),
        "json-engine": EngineSpec(
            name="json-engine",
            kind="test",
            input_contracts=["text"],
            output_contracts=["plain-text", "json_object", "json_schema"],
            supports_guided_decoding=True,
        ),
    }

    for response_format in (
        {"type": "json_object"},
        {"type": "json_schema", "json_schema": {"type": "object", "properties": {"answer": {"type": "string"}}}},
    ):
        plan = compiler.compile(TaskRequest(task="infer", mode="sync", recipe="r1", response_format=response_format))

        assert plan.tuple_chain == ["p2"]


def test_compiler_carries_generation_params_into_plan() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", max_tokens=64, temperature=0.0)

    plan = compiler.compile(request)

    assert plan.max_tokens == 64
    assert plan.temperature == 0.0


def test_compiler_allows_stream_response_format_when_openai_contract_route_exists() -> None:
    compiler = build_compiler()
    recipe = compiler.recipes["r1"].model_copy(update={"allowed_modes": [ExecutionMode.STREAM]})
    compiler.recipes = {"r1": recipe}
    compiler.tuples = {
        name: spec.model_copy(
            update={
                "modes": [ExecutionMode.STREAM],
                "stream_target": "app:stream",
                "stream_contract": "openai-chat-completions",
                "endpoint_contract": "openai-chat-completions",
                "output_contract": "openai-chat-completions",
            }
        )
        for name, spec in compiler.tuples.items()
    }
    request = TaskRequest(task="infer", mode="stream", recipe="r1", response_format={"type": "json_object"})

    plan = compiler.compile(request)

    assert plan.mode is ExecutionMode.STREAM
    assert plan.response_format is not None


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

    assert plan.tuple_chain == ["p2"]


def test_observed_registry_ranks_all_eligible_providers() -> None:
    compiler = build_compiler()
    compiler.registry.record(TupleObservation(tuple="p1", latency_ms=1000, success=True, cost=10))
    compiler.registry.record(TupleObservation(tuple="p2", latency_ms=1, success=True, cost=0))
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p2", "p1"]


def test_tuple_chain_prefers_smallest_capable_tuple_before_observations() -> None:
    compiler = build_compiler()
    compiler.tuples["p1"] = compiler.tuples["p1"].model_copy(update={"vram_gb": 80, "max_model_len": 32768})
    compiler.tuples["p2"] = compiler.tuples["p2"].model_copy(update={"vram_gb": 24, "max_model_len": 100})
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p2", "p1"]


def test_provider_fit_dominates_observed_score_across_reliable_fit_classes() -> None:
    compiler = build_compiler()
    compiler.tuples["p1"] = compiler.tuples["p1"].model_copy(update={"vram_gb": 80, "max_model_len": 32768})
    compiler.tuples["p2"] = compiler.tuples["p2"].model_copy(update={"vram_gb": 24, "max_model_len": 100})
    compiler.registry.record(TupleObservation(tuple="p1", latency_ms=1, success=True, cost=0))
    compiler.registry.record(TupleObservation(tuple="p2", latency_ms=1000, success=True, cost=100))
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p2", "p1"]


def test_poor_observed_reliability_loses_to_larger_reliable_tuple() -> None:
    compiler = build_compiler()
    compiler.tuples["p1"] = compiler.tuples["p1"].model_copy(update={"vram_gb": 80, "max_model_len": 32768})
    compiler.tuples["p2"] = compiler.tuples["p2"].model_copy(update={"vram_gb": 24, "max_model_len": 100})
    compiler.registry.record(TupleObservation(tuple="p1", latency_ms=1000, success=True, cost=1))
    compiler.registry.record(TupleObservation(tuple="p2", latency_ms=1, success=False, cost=1))
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p1", "p2"]


def test_strict_schema_quality_failure_lowers_tuple_for_same_route() -> None:
    compiler = build_compiler()
    compiler.registry.record_quality_failure(
        "p1",
        recipe="r1",
        task="infer",
        mode="sync",
        code="MALFORMED_OUTPUT",
    )
    request = TaskRequest(
        task="infer",
        mode="sync",
        recipe="r1",
        response_format=ResponseFormat(
            type=ResponseFormatType.JSON_SCHEMA,
            json_schema={"type": "object"},
            strict=True,
        ),
    )

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p2", "p1"]


def test_quality_failure_penalty_is_scoped_to_recipe_task_and_mode() -> None:
    compiler = build_compiler()
    compiler.registry.record_quality_failure(
        "p1",
        recipe="other",
        task="infer",
        mode="sync",
        code="MALFORMED_OUTPUT",
    )
    request = TaskRequest(
        task="infer",
        mode="sync",
        recipe="r1",
        response_format=ResponseFormat(
            type=ResponseFormatType.JSON_SCHEMA,
            json_schema={"type": "object"},
            strict=True,
        ),
    )

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p1", "p2"]


def test_tuple_chain_filters_by_request_weight() -> None:
    compiler = build_compiler()
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(update={"context_budget_tokens": 1000})
    compiler.tuples["p2"] = compiler.tuples["p2"].model_copy(update={"max_model_len": 1000})
    request = TaskRequest(
        task="infer",
        mode="sync",
        recipe="r1",
        input_refs=[DataRef(uri="s3://bucket/heavy.txt", sha256="a" * 64, bytes=500, content_type="text/plain")],
    )

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p2"]


def test_high_cost_tuple_requires_explicit_budget_for_auto_select() -> None:
    compiler = build_compiler()
    compiler.tuples = {
        "p1": compiler.tuples["p1"].model_copy(
            update={
                "cost_per_second": 0.00505,
                "expected_cold_start_seconds": 1000,
                "scaledown_window_seconds": 300,
            }
        )
    }
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    with pytest.raises(GovernanceError) as exc:
        compiler.compile(request)

    assert exc.value.code == "NO_ELIGIBLE_TUPLE"
    assert "without explicit budget" in exc.value.context["tuple_rejections"]["p1"]


def test_recipe_budget_allows_high_cost_tuple_when_within_limit() -> None:
    compiler = build_compiler()
    compiler.tuples = {
        "p1": compiler.tuples["p1"].model_copy(
            update={
                "cost_per_second": 0.00505,
                "expected_cold_start_seconds": 1000,
                "scaledown_window_seconds": 300,
            }
        )
    }
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(
        update={"cost_policy": CostPolicy(max_estimated_cost_usd=7.0)}
    )
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p1"]
    assert plan.attestations["cost_estimate"]["estimated_cost_usd"] == pytest.approx(6.6155)


def test_recipe_budget_rejects_tuple_when_estimate_exceeds_limit() -> None:
    compiler = build_compiler()
    compiler.tuples = {
        "p1": compiler.tuples["p1"].model_copy(
            update={
                "cost_per_second": 0.00505,
                "expected_cold_start_seconds": 1000,
                "scaledown_window_seconds": 300,
            }
        )
    }
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(
        update={"cost_policy": CostPolicy(max_estimated_cost_usd=1.0)}
    )
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    with pytest.raises(GovernanceError) as exc:
        compiler.compile(request)

    assert exc.value.code == "NO_ELIGIBLE_TUPLE"
    assert "max_estimated_cost_usd" in exc.value.context["tuple_rejections"]["p1"]


def test_strict_budget_rejects_unknown_price_freshness() -> None:
    compiler = build_compiler()
    compiler.policy = compiler.policy.model_copy(
        update={"cost_policy": CostPolicy(max_estimated_cost_usd=1.0, require_fresh_price_for_budget=True)}
    )
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    with pytest.raises(GovernanceError) as exc:
        compiler.compile(request)

    assert "tuple price is unknown" in exc.value.context["tuple_rejections"]["p1"]


def test_strict_budget_accepts_fresh_configured_price() -> None:
    compiler = build_compiler()
    compiler.policy = compiler.policy.model_copy(
        update={"cost_policy": CostPolicy(max_estimated_cost_usd=1.0, require_fresh_price_for_budget=True)}
    )
    fresh = {
        "configured_price_source": "test-price-sheet",
        "configured_price_observed_at": datetime.now(timezone.utc).isoformat(),
        "configured_price_ttl_seconds": 3600,
    }
    compiler.tuples = {name: tuple.model_copy(update=fresh) for name, tuple in compiler.tuples.items()}
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.attestations["cost_estimate"]["price_freshness"] == "fresh"


def test_strict_budget_rejects_stale_configured_price() -> None:
    compiler = build_compiler()
    compiler.policy = compiler.policy.model_copy(
        update={"cost_policy": CostPolicy(max_estimated_cost_usd=1.0, require_fresh_price_for_budget=True)}
    )
    stale = {
        "configured_price_source": "test-price-sheet",
        "configured_price_observed_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        "configured_price_ttl_seconds": 3600,
    }
    compiler.tuples = {name: tuple.model_copy(update=stale) for name, tuple in compiler.tuples.items()}
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    with pytest.raises(GovernanceError) as exc:
        compiler.compile(request)

    assert "tuple price is stale" in exc.value.context["tuple_rejections"]["p1"]


def test_compiler_routes_with_v3_recipe_resource_class_requirements() -> None:
    policy = Policy(
        version="test",
        inline_bytes_limit=10,
        default_lease_ttl_seconds=30,
        max_lease_ttl_seconds=120,
        max_timeout_seconds=120,
        tuples=TuplePolicy(allow=["small", "large"], deny=[]),
    )
    recipe = Recipe(
        name="large-v3",
        task="infer",
        recipe_schema_version=3,
        resource_class="large",
        context_budget_tokens=65536,
        allowed_modes=[ExecutionMode.SYNC],
        token_estimation_profile="qwen",
    )
    tuples = {
        "small": ExecutionTupleSpec(
            name="small",
            adapter="modal",
            gpu="A10G",
            vram_gb=24,
            max_model_len=65536,
            cost_per_second=0.001,
            modes=[ExecutionMode.SYNC],
            target="app:small",
            model="small-model",
        ),
        "large": ExecutionTupleSpec(
            name="large",
            adapter="modal",
            gpu="H100",
            vram_gb=80,
            max_model_len=65536,
            cost_per_second=0.002,
            modes=[ExecutionMode.SYNC],
            target="app:large",
            model="large-model",
        ),
    }
    compiler = GovernanceCompiler(policy=policy, recipes={"large-v3": recipe}, tuples=tuples, registry=ObservedRegistry())

    plan = compiler.compile(TaskRequest(task="infer", mode="sync", recipe="large-v3"))

    assert plan.tuple_chain == ["large"]


def test_standing_tuple_cost_counts_toward_budget_guard() -> None:
    compiler = build_compiler()
    compiler.tuples = {
        "p1": compiler.tuples["p1"].model_copy(
            update={
                "cost_per_second": 0.0,
                "standing_cost_per_second": 0.001,
                "standing_cost_window_seconds": 7200,
                "endpoint_cost_per_second": 0.001,
                "endpoint_cost_window_seconds": 3600,
            }
        )
    }
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    with pytest.raises(GovernanceError) as exc:
        compiler.compile(request)

    assert exc.value.code == "NO_ELIGIBLE_TUPLE"
    assert "without explicit budget" in exc.value.context["tuple_rejections"]["p1"]


def test_standing_tuple_cost_is_reported_in_attestation() -> None:
    compiler = build_compiler()
    compiler.tuples = {
        "p1": compiler.tuples["p1"].model_copy(
            update={
                "cost_per_second": 0.0,
                "standing_cost_per_second": 0.001,
                "standing_cost_window_seconds": 7200,
            }
        )
    }
    compiler.recipes["r1"] = compiler.recipes["r1"].model_copy(
        update={"cost_policy": CostPolicy(max_estimated_cost_usd=8.0)}
    )
    request = TaskRequest(task="infer", mode="sync", recipe="r1")

    plan = compiler.compile(request)

    assert plan.attestations["cost_estimate"]["standing_cost_usd"] == pytest.approx(7.2)
    assert plan.attestations["cost_estimate"]["estimated_cost_usd"] == pytest.approx(7.2)


def test_requested_tuple_must_be_eligible() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", requested_tuple="missing")

    with pytest.raises(GovernanceError, match="requested tuple"):
        compiler.compile(request)


def test_requested_tuple_does_not_fall_back_when_circuit_is_open() -> None:
    compiler = build_compiler()
    compiler.registry.breakers["p1"].open = True
    compiler.registry.breakers["p1"].opened_at = datetime.now(timezone.utc).timestamp()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", requested_tuple="p1")

    with pytest.raises(GovernanceError, match="circuit breaker"):
        compiler.compile(request)


def test_validation_requested_tuple_can_bypass_open_circuit() -> None:
    compiler = build_compiler()
    compiler.registry.breakers["p1"].open = True
    compiler.registry.breakers["p1"].opened_at = datetime.now(timezone.utc).timestamp()
    request = TaskRequest(
        task="infer",
        mode="sync",
        recipe="r1",
        requested_tuple="p1",
        bypass_circuit_for_validation=True,
    )

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p1"]


def test_requested_tuple_is_single_tuple_chain() -> None:
    compiler = build_compiler()
    request = TaskRequest(task="infer", mode="sync", recipe="r1", requested_tuple="p1")

    plan = compiler.compile(request)

    assert plan.tuple_chain == ["p1"]


def test_open_circuit_allows_half_open_after_timeout() -> None:
    registry = ObservedRegistry()
    breaker = registry.breakers["p1"]
    breaker.recovery_timeout_seconds = 0
    for _ in range(breaker.failure_threshold):
        registry.record(TupleObservation(tuple="p1", latency_ms=1, success=False, cost=0))

    assert registry.is_available("p1") is True


def test_observed_registry_preserves_input_order_for_equal_scores() -> None:
    registry = ObservedRegistry()

    assert registry.rank(["p2", "p1"]) == ["p2", "p1"]


def test_observed_registry_persists_observations(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ObservedRegistry(path=path)

    registry.record(TupleObservation(tuple="p1", latency_ms=12, success=True, cost=0.5))
    loaded = ObservedRegistry(path=path)

    score = loaded.score("p1")
    assert score.samples == 1
    assert score.p50_latency_ms == 12


def test_observed_registry_persists_route_quality_failures(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ObservedRegistry(path=path)

    registry.record_quality_failure(
        "p1",
        recipe="r1",
        task="infer",
        mode="sync",
        code="MALFORMED_OUTPUT",
    )
    loaded = ObservedRegistry(path=path)

    assert loaded.quality_failure_count(
        "p1",
        recipe="r1",
        task="infer",
        mode="sync",
        code="MALFORMED_OUTPUT",
    ) == 1


def test_observed_registry_persists_circuit_breaker_state(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ObservedRegistry(path=path)
    for _ in range(3):
        registry.record(TupleObservation(tuple="p1", latency_ms=1, success=False, cost=0))

    loaded = ObservedRegistry(path=path)

    assert loaded.breakers["p1"].open is True


def test_observed_registry_caps_observations_per_tuple(tmp_path) -> None:
    registry = ObservedRegistry(path=tmp_path / "registry.db", max_observations_per_tuple=3)
    for i in range(5):
        registry.record(TupleObservation(tuple="p1", latency_ms=i, success=True, cost=0))

    loaded = ObservedRegistry(path=tmp_path / "registry.db", max_observations_per_tuple=3)

    assert loaded.score("p1").samples == 3
    assert [row.latency_ms for row in loaded.observations["p1"]] == [2, 3, 4]
