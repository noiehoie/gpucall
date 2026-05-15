from __future__ import annotations

import shutil
import subprocess
import sys
import os
import json
import ast
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gpucall.config import ConfigError, load_config
from gpucall.cli import _bounded_live_tuple_catalog_findings, _managed_endpoint_live_cost_audit, _provider_smoke_request
from gpucall.compiler import GovernanceCompiler, GovernanceError
from gpucall.domain import DataRef, ExecutionMode, ExecutionTupleSpec, InlineValue, ObjectStoreConfig, Recipe, SecurityTier, TaskRequest, recipe_requirements
from gpucall.registry import ObservedRegistry
from gpucall.targeting import is_configured_cidr


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    root = tmp_path / "config"
    shutil.copytree(source, root)
    return root


def enable_recipe_auto_select(root: Path, *recipe_names: str) -> None:
    for recipe_name in recipe_names:
        path = root / "recipes" / f"{recipe_name}.yml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload["auto_select"] = True
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_modal_config_targets_match_worker_app_and_functions() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "gpucall" / "worker_contracts" / "modal.py").read_text(encoding="utf-8")
    app_default = re.search(r'modal\.App\(os\.getenv\("GPUCALL_MODAL_WORKER_APP_NAME", "([^"]+)"\)\)', source)
    assert app_default is not None
    app_name = app_default.group(1)
    function_names = {node.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef)}
    target_files = [
        *sorted((root / "config" / "workers").glob("modal*.yml")),
        *sorted((root / "config" / "tuple_candidates").glob("modal*.yml")),
        *sorted((root / "config" / "tuples").glob("modal*.example")),
        *sorted((root / "gpucall" / "config_templates" / "workers").glob("modal*.yml")),
        *sorted((root / "gpucall" / "config_templates" / "tuple_candidates").glob("modal*.yml")),
        *sorted((root / "gpucall" / "config_templates" / "tuples").glob("modal*.example")),
    ]

    errors: list[str] = []
    for path in target_files:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for field in ("target", "stream_target"):
            value = payload.get(field)
            if not isinstance(value, str) or not value:
                continue
            if ":" not in value:
                errors.append(f"{path.relative_to(root)}:{field}: missing '<app>:<function>'")
                continue
            target_app, target_function = value.split(":", 1)
            if target_app != app_name:
                errors.append(f"{path.relative_to(root)}:{field}: app {target_app!r} != {app_name!r}")
            if target_function not in function_names:
                errors.append(f"{path.relative_to(root)}:{field}: unknown function {target_function!r}")

    assert errors == []


def test_runpod_vllm_tuple_examples_include_official_worker_env() -> None:
    root = Path(__file__).resolve().parents[1]
    target_files = [
        *sorted((root / "config" / "tuples").glob("runpod-vllm*.example")),
        *sorted((root / "gpucall" / "config_templates" / "tuples").glob("runpod-vllm*.example")),
    ]
    required_env = {
        "MODEL_NAME",
        "MAX_MODEL_LEN",
        "BASE_PATH",
        "GPU_MEMORY_UTILIZATION",
        "MAX_CONCURRENCY",
    }

    errors: list[str] = []
    for path in target_files:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if payload.get("adapter") != "runpod-vllm-serverless":
            continue
        worker_env = ((payload.get("provider_params") or {}).get("worker_env") or {})
        model_storage = ((payload.get("provider_params") or {}).get("model_storage") or {})
        missing = sorted(required_env.difference(worker_env))
        if missing:
            errors.append(f"{path.relative_to(root)} missing worker_env keys: {missing}")
        if not isinstance(model_storage, dict) or model_storage.get("storage_kind") != "runpod_cached_model":
            errors.append(f"{path.relative_to(root)} must default to runpod_cached_model storage")
        if model_storage.get("mount_path") != "/runpod-volume":
            errors.append(f"{path.relative_to(root)} model_storage.mount_path must be /runpod-volume")
        if worker_env.get("BASE_PATH") != "/runpod-volume":
            errors.append(f"{path.relative_to(root)} worker_env.BASE_PATH must be /runpod-volume")
        declared_model = str(payload.get("model") or "")
        if declared_model and declared_model not in {
            str(worker_env.get("MODEL_NAME") or ""),
            str(worker_env.get("OPENAI_SERVED_MODEL_NAME_OVERRIDE") or ""),
        }:
            errors.append(f"{path.relative_to(root)} model does not match worker_env")
        if declared_model and model_storage.get("cached_model_ref") != declared_model:
            errors.append(f"{path.relative_to(root)} cached_model_ref does not match model")
        try:
            max_model_len = int(worker_env.get("MAX_MODEL_LEN"))
        except (TypeError, ValueError):
            errors.append(f"{path.relative_to(root)} worker_env.MAX_MODEL_LEN is not an integer")
        else:
            if max_model_len < int(payload.get("max_model_len") or 0):
                errors.append(f"{path.relative_to(root)} worker_env.MAX_MODEL_LEN is below tuple max_model_len")

    assert errors == []


def test_live_cost_audit_ignores_placeholder_runpod_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_http_json(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        if url == "https://rest.runpod.io/v1/endpoints?includeWorkers=true":
            return {"ok": True, "body": []}
        raise AssertionError("placeholder RunPod endpoint health must not be queried")

    monkeypatch.setattr("gpucall.cli._http_json", fake_http_json)
    report = _managed_endpoint_live_cost_audit(
        {
            "runpod": SimpleNamespace(
                name="runpod-vllm-serverless",
                adapter="runpod-vllm-serverless",
                execution_surface=SimpleNamespace(value="managed_endpoint"),
                endpoint="https://api.runpod.ai/v2",
                target="RUNPOD_ENDPOINT_ID_PLACEHOLDER",
            )
        },
        {"runpod": {"api_key": "secret"}},
    )

    assert report["configured"] is True
    assert report["endpoints"] == []
    assert report["unmanaged_endpoint_findings"] == []


def test_recipe_v3_rejects_provider_resource_fields() -> None:
    with pytest.raises(ValueError, match="tuple resource fields"):
        Recipe.model_validate(
            {
                "name": "bad-v3",
                "recipe_schema_version": 3,
                "task": "infer",
                "intent": "bad",
                "data_classification": "confidential",
                "allowed_modes": ["sync"],
                "context_budget_tokens": 8192,
                "resource_class": "light",
                "min_vram_gb": 16,
                "max_model_len": 8192,
                "gpu": "A10G",
                "timeout_seconds": 30,
                "lease_ttl_seconds": 60,
                "token_estimation_profile": "qwen",
            }
        )


def test_object_store_accepts_legacy_provider_key() -> None:
    config = ObjectStoreConfig.model_validate({"provider": "s3", "bucket": "gpucall"})

    assert config.tuple == "s3"


def test_load_config_rejects_recipe_without_capable_provider(tmp_path) -> None:
    root = copy_config(tmp_path)
    (root / "recipes" / "text-infer-standard.yml").write_text(
        """
name: text-infer-standard
recipe_schema_version: 3
task: infer
intent: too_large
data_classification: confidential
allowed_modes: [sync]
context_budget_tokens: 999999999
resource_class: ultralong
timeout_seconds: 30
lease_ttl_seconds: 120
token_estimation_profile: qwen
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="no tuple satisfying"):
        load_config(root)


def test_load_config_rejects_provider_classification_below_recipe(tmp_path) -> None:
    root = copy_config(tmp_path)
    (root / "policy.yml").write_text(
        """
version: test
inline_bytes_limit: 30720
default_lease_ttl_seconds: 60
max_lease_ttl_seconds: 600
max_timeout_seconds: 300
tokenizer_safety_multiplier: 1.25
tuples:
  allow: [local-echo]
  deny: []
  max_data_classification: confidential
immutable_audit: true
""".lstrip(),
        encoding="utf-8",
    )
    (root / "recipes" / "text-infer-standard.yml").write_text(
        """
name: text-infer-standard
recipe_schema_version: 3
task: infer
intent: text_infer
data_classification: confidential
allowed_modes: [sync]
context_budget_tokens: 32768
resource_class: standard
timeout_seconds: 30
lease_ttl_seconds: 120
token_estimation_profile: qwen
""".lstrip(),
        encoding="utf-8",
    )
    (root / "tuples" / "local-echo.yml").write_text(
        """
name: local-echo
adapter: echo
max_data_classification: internal
gpu: L4
vram_gb: 24
max_model_len: 32768
cost_per_second: 0
modes: [sync, async, stream]
endpoint: null
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="no tuple satisfying"):
        load_config(root)


def test_load_config_rejects_provider_model_len_above_declared_model_capability(tmp_path) -> None:
    root = copy_config(tmp_path)
    surface_path = root / "surfaces" / "hyperstack-a100.yml"
    surface = yaml.safe_load(surface_path.read_text(encoding="utf-8"))
    surface["max_model_len"] = 131072
    surface_path.write_text(yaml.safe_dump(surface, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError, match="model catalog capability"):
        load_config(root)


def test_load_config_rejects_hyperstack_all_open_ssh_cidr(tmp_path) -> None:
    root = copy_config(tmp_path)
    surface_path = root / "surfaces" / "hyperstack-a100.yml"
    surface = yaml.safe_load(surface_path.read_text(encoding="utf-8"))
    surface["ssh_remote_cidr"] = "0.0.0.0/0"
    surface_path.write_text(yaml.safe_dump(surface, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError, match="must not allow all addresses"):
        load_config(root)


def test_load_config_validation_error_does_not_echo_secret_values(tmp_path) -> None:
    root = copy_config(tmp_path)
    surface_path = root / "surfaces" / "modal-a10g.yml"
    surface = yaml.safe_load(surface_path.read_text(encoding="utf-8"))
    surface["vram_gb"] = "secret-token-123"
    surface["api_key"] = "secret-extra-456"
    surface_path.write_text(yaml.safe_dump(surface, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_config(root)

    message = str(exc_info.value)
    assert "vram_gb" in message
    assert "secret-token-123" not in message
    assert "secret-extra-456" not in message
    assert "input_value" not in message


def test_explain_config_outputs_execution_spec(tmp_path) -> None:
    root = copy_config(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "explain-config",
            "text-infer-standard",
            "--config-dir",
            str(root),
            "--mode",
            "sync",
            "--max-tokens",
            "10",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"execution_spec"' in result.stdout
    assert '"tuple_chain"' in result.stdout
    assert '"policy_ceiling"' in result.stdout


def test_explain_config_supports_async_mode(tmp_path) -> None:
    root = copy_config(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "explain-config",
            "text-infer-standard",
            "--config-dir",
            str(root),
            "--mode",
            "async",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"mode": "async"' in result.stdout
    assert "webhook" not in result.stdout


def test_standard_config_includes_verified_text_recipes(tmp_path) -> None:
    config = load_config(copy_config(tmp_path))

    assert config.recipes["text-infer-light"].task == "infer"
    assert config.recipes["text-infer-standard"].task == "infer"
    assert recipe_requirements(config.recipes["text-infer-large"]).context_budget_tokens == 65536
    assert recipe_requirements(config.recipes["text-infer-exlarge"]).context_budget_tokens == 131072
    assert recipe_requirements(config.recipes["text-infer-ultralong"]).context_budget_tokens == 524288
    assert recipe_requirements(config.recipes["text-infer-standard"]).context_budget_tokens == 32768
    assert config.recipes["text-infer-standard"].max_input_bytes == 16777216
    assert config.recipes["text-infer-light"].output_validation_attempts == 2
    assert config.recipes["text-infer-ultralong"].output_validation_attempts == 2
    assert config.recipes["vision-image-standard"].task == "vision"
    assert config.recipes["vision-image-standard"].auto_select is True
    assert config.recipes["vision-image-standard"].allowed_mime_prefixes == ["image/"]
    assert config.tuples["hyperstack-a100"].max_model_len == 32768
    assert config.tuples["hyperstack-a100"].declared_model_max_len == 32768
    assert config.tuples["hyperstack-qwen-1m"].max_model_len == 524288
    assert config.tuples["hyperstack-qwen-1m"].declared_model_max_len == 1010000
    assert config.tuples["hyperstack-qwen-1m"].model == "Qwen/Qwen2.5-7B-Instruct-1M"
    assert config.tuples["hyperstack-a100"].trust_profile.dedicated_gpu is True
    assert config.tuples["modal-a10g"].trust_profile.security_tier is SecurityTier.ENCRYPTED_CAPSULE
    assert config.tuples["modal-a10g"].supports_vision is False
    assert "image" not in config.tuples["modal-a10g"].input_contracts
    assert config.tuples["modal-vision-a10g"].supports_vision is True
    assert config.tuples["modal-vision-a10g"].model == "Salesforce/blip-vqa-base"
    assert "image" in config.tuples["modal-vision-a10g"].input_contracts


def test_standard_config_routes_news_sized_prompts_to_long_recipes(tmp_path) -> None:
    root = copy_config(tmp_path)
    enable_recipe_auto_select(root, "text-infer-large", "text-infer-exlarge", "text-infer-ultralong")
    config = load_config(root)
    compiler = GovernanceCompiler(policy=config.policy, recipes=config.recipes, tuples=config.tuples, registry=ObservedRegistry())

    large_plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode=ExecutionMode.ASYNC,
            input_refs=[DataRef(uri="s3://bucket/chosun.txt", sha256="a" * 64, bytes=32000, content_type="text/plain")],
        )
    )
    ultralong_plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode=ExecutionMode.ASYNC,
            input_refs=[DataRef(uri="s3://bucket/integrated.txt", sha256="b" * 64, bytes=220000, content_type="text/plain")],
        )
    )

    assert config.recipes["text-infer-large"].allowed_modes == [ExecutionMode.ASYNC]
    assert config.recipes["text-infer-ultralong"].allowed_modes == [ExecutionMode.ASYNC]
    assert large_plan.recipe_name == "text-infer-large"
    if is_configured_cidr(config.tuples["hyperstack-qwen-1m"].ssh_remote_cidr):
        assert large_plan.tuple_chain[0] == "hyperstack-qwen-1m"
    else:
        assert config.tuples[large_plan.tuple_chain[0]].max_model_len >= recipe_requirements(config.recipes["text-infer-large"]).context_budget_tokens
        assert "modal-b200x2-qwen25-14b-1m" in large_plan.tuple_chain
        assert "hyperstack-qwen-1m" not in large_plan.tuple_chain
    artifact = large_plan.attestations["compile_artifact"]
    assert artifact["selected_tuple"]["tuple"] == large_plan.tuple_chain[0]
    assert artifact["selected_tuple_hash"]
    assert ultralong_plan.recipe_name == "text-infer-ultralong"
    assert ultralong_plan.tuple_chain[0] == "modal-b200x2-qwen25-14b-1m"
    assert "modal-b200x2-qwen25-14b-1m" in ultralong_plan.tuple_chain
    assert config.tuples[ultralong_plan.tuple_chain[0]].max_model_len >= recipe_requirements(config.recipes["text-infer-ultralong"]).context_budget_tokens


def test_standard_config_rejects_sync_for_long_context_text(tmp_path) -> None:
    config = load_config(copy_config(tmp_path))
    compiler = GovernanceCompiler(policy=config.policy, recipes=config.recipes, tuples=config.tuples, registry=ObservedRegistry())

    with pytest.raises(GovernanceError) as excinfo:
        compiler.compile(
            TaskRequest(
                task="infer",
                mode=ExecutionMode.SYNC,
                input_refs=[DataRef(uri="s3://bucket/large.txt", sha256="b" * 64, bytes=32000, content_type="text/plain")],
            )
        )
    assert excinfo.value.code == "NO_AUTO_SELECTABLE_RECIPE"


def test_local_runtime_is_preferred_when_it_satisfies_policy(tmp_path) -> None:
    root = copy_config(tmp_path)
    for rel_path in (
        "runtimes/local-author-ollama.yml",
        "surfaces/local-author-ollama.yml",
        "workers/local-author-ollama.yml",
    ):
        (root / rel_path).unlink(missing_ok=True)
    (root / "surfaces" / "local-openai-test.yml").write_text(
        """
surface_ref: local-openai-test
worker_ref: local-openai-test
account_ref: local
adapter: local-openai-compatible
execution_surface: local_runtime
gpu: local
vram_gb: 256
max_model_len: 8192
region: local
zone: local
cost_per_second: 0
configured_price_source: local-free
configured_price_observed_at: '2026-05-10T00:00:00+00:00'
configured_price_ttl_seconds: 315360000
stock_state: configured
expected_cold_start_seconds: 1
billing_granularity_seconds: 0
max_data_classification: confidential
scaledown_window_seconds: 0
min_billable_seconds: 0
trust_profile:
  security_tier: local
  sovereign_jurisdiction: local
  dedicated_gpu: true
  requires_attestation: false
  supports_key_release: false
  allows_worker_s3_credentials: false
endpoint: http://127.0.0.1:18180/v1
""",
        encoding="utf-8",
    )
    (root / "workers" / "local-openai-test.yml").write_text(
        """
worker_ref: local-openai-test
account_ref: local
adapter: local-openai-compatible
execution_surface: local_runtime
model_ref: deepseek-v4-flash-local
engine_ref: local-openai-compatible-chat
modes: [sync, async, stream]
input_contracts: [text, chat_messages]
output_contract: openai-chat-completions
stream_contract: sse
target: null
stream_target: null
endpoint_contract: openai-chat-completions
model: deepseek-v4-flash
""",
        encoding="utf-8",
    )
    config = load_config(root)
    compiler = GovernanceCompiler(policy=config.policy, recipes=config.recipes, tuples=config.tuples, models=config.models, engines=config.engines, registry=ObservedRegistry())

    plan = compiler.compile(TaskRequest(task="infer", mode=ExecutionMode.SYNC, inline_inputs={"prompt": InlineValue(value="hello local")}))

    assert plan.recipe_name == "text-infer-light"
    assert plan.tuple_chain[0] == "local-openai-test"


def test_standard_config_transport_matrix_is_explicit(tmp_path) -> None:
    root = copy_config(tmp_path)
    enable_recipe_auto_select(root, "text-infer-standard", "text-infer-large", "text-infer-exlarge", "text-infer-ultralong")
    config = load_config(root)
    compiler = GovernanceCompiler(policy=config.policy, recipes=config.recipes, tuples=config.tuples, registry=ObservedRegistry())

    cases = [
        (
            "small inline text",
            TaskRequest(task="infer", mode=ExecutionMode.SYNC, inline_inputs={"prompt": {"kind": "text", "value": "hello"}}),
            "text-infer-light",
            {"local-author-ollama", "modal-t4-qwen25-0.5b"},
        ),
        (
            "standard text dataref",
            TaskRequest(
                task="infer",
                mode=ExecutionMode.SYNC,
                input_refs=[DataRef(uri="s3://bucket/standard.txt", sha256="a" * 64, bytes=16000, content_type="text/plain")],
            ),
            "text-infer-standard",
            {"modal-l4-qwen25-1.5b", "modal-a10g-qwen25-7b"},
        ),
        (
            "large text dataref",
            TaskRequest(
                task="infer",
                mode=ExecutionMode.ASYNC,
                input_refs=[DataRef(uri="s3://bucket/large.txt", sha256="b" * 64, bytes=32000, content_type="text/plain")],
            ),
            "text-infer-large",
            {"modal-rtx-pro-6000-qwen25-7b", "modal-b200x2-qwen25-14b-1m"},
        ),
        (
            "image dataref",
            TaskRequest(
                task="vision",
                mode=ExecutionMode.SYNC,
                input_refs=[DataRef(uri="s3://bucket/image.png", sha256="c" * 64, bytes=2_000_000, content_type="image/png")],
            ),
            "vision-image-standard",
            {"modal-vision-catalog-l4-microsoft-florence-2-large-ft", "modal-vision-catalog-a100-qwen2-5-vl-7b-instruct"},
        ),
    ]

    for label, request, recipe_name, required_tuples in cases:
        plan = compiler.compile(request)
        assert plan.recipe_name == recipe_name, label
        assert required_tuples.issubset(set(plan.tuple_chain)), label


def test_standard_config_routes_structured_vision_to_json_capable_model(tmp_path) -> None:
    root = copy_config(tmp_path)
    runpod_worker = root / "workers" / "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct.yml"
    runpod_payload = yaml.safe_load(runpod_worker.read_text(encoding="utf-8"))
    runpod_payload["target"] = "runpod-endpoint-test"
    runpod_worker.write_text(yaml.safe_dump(runpod_payload, sort_keys=False), encoding="utf-8")
    config = load_config(root)
    compiler = GovernanceCompiler(policy=config.policy, recipes=config.recipes, tuples=config.tuples, models=config.models, engines=config.engines, registry=ObservedRegistry())

    request = TaskRequest(
        task="vision",
        mode=ExecutionMode.SYNC,
        intent="understand_document_image",
        input_refs=[DataRef(uri="s3://bucket/image.png", sha256="c" * 64, bytes=2_000_000, content_type="image/png")],
        response_format={"type": "json_object"},
    )
    plan = compiler.compile(request)

    assert plan.tuple_chain[0] == "modal-vision-catalog-l40s-qwen2-5-vl-3b-instruct"
    assert "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct" not in plan.tuple_chain
    assert "modal-vision-catalog-l40s-qwen2-5-vl-3b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-a100-qwen2-5-vl-3b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-a100-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-h100-florence-2-large-ft" not in plan.tuple_chain

    request = TaskRequest(
        task="vision",
        mode=ExecutionMode.SYNC,
        intent="understand_document_image",
        input_refs=[DataRef(uri="s3://bucket/image.png", sha256="c" * 64, bytes=2_000_000, content_type="image/png")],
        response_format={"type": "json_schema", "json_schema": {"type": "object", "required": ["articles"], "properties": {"articles": {"type": "array"}}}},
    )

    plan = compiler.compile(request)

    assert plan.tuple_chain[0] == "modal-vision-catalog-l40s-qwen2-5-vl-7b-instruct"
    assert "modal-vision-catalog-l40s-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-a100-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-a100-qwen2-5-vl-32b-instruct" in plan.tuple_chain
    assert "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct" not in plan.tuple_chain
    assert "modal-vision-catalog-h200-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-h200-qwen2-5-vl-32b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-h200-qwen2-5-vl-3b-instruct" not in plan.tuple_chain
    assert "modal-h100-florence-2-large-ft" not in plan.tuple_chain


def test_template_config_routes_structured_vision_to_json_capable_model() -> None:
    config = load_config(Path("gpucall/config_templates"))
    compiler = GovernanceCompiler(policy=config.policy, recipes=config.recipes, tuples=config.tuples, models=config.models, engines=config.engines, registry=ObservedRegistry())

    request = TaskRequest(
        task="vision",
        mode=ExecutionMode.SYNC,
        intent="understand_document_image",
        input_refs=[DataRef(uri="s3://bucket/image.png", sha256="c" * 64, bytes=2_000_000, content_type="image/png")],
        response_format={"type": "json_schema", "json_schema": {"type": "object", "required": ["articles"], "properties": {"articles": {"type": "array"}}}},
    )

    plan = compiler.compile(request)

    assert plan.tuple_chain[0] == "modal-vision-catalog-l40s-qwen2-5-vl-7b-instruct"
    assert "modal-vision-catalog-a100-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-a100-qwen2-5-vl-32b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-h200-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-h200-qwen2-5-vl-32b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-h200-qwen2-5-vl-3b-instruct" not in plan.tuple_chain
    assert "modal-h100-florence-2-large-ft" not in plan.tuple_chain


def test_provider_smoke_uses_chat_messages_for_chat_only_provider(tmp_path) -> None:
    config = load_config(copy_config(tmp_path))
    recipe = config.recipes["text-infer-light"]
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        max_data_classification="confidential",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00045,
        modes=["sync", "async"],
        target="endpoint",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        endpoint_contract="openai-chat-completions",
        stream_contract="none",
        model_ref="qwen2.5-1.5b-instruct",
        engine_ref="runpod-vllm-openai",
    )
    runtime = SimpleNamespace(compiler=SimpleNamespace(tuples={tuple.name: tuple}))

    request = _provider_smoke_request(runtime, recipe, ExecutionMode.SYNC, tuple.name)

    assert request.messages
    assert request.messages[0].role == "user"
    assert request.messages[0].content == "gpucall tuple smoke"
    assert request.inline_inputs == {}


def test_validate_config_cli(tmp_path) -> None:
    root = copy_config(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "validate-config",
            "--config-dir",
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"valid": true' in result.stdout
    assert '"runtimes":' in result.stdout


def test_controlled_runtime_registry_is_loaded(tmp_path) -> None:
    config = load_config(copy_config(tmp_path))

    assert "local-echo" in config.runtimes
    runtime = config.runtimes["local-echo"]
    assert runtime.kind == "controlled_runtime"
    assert runtime.runtime_boundary == "gateway_host"
    assert runtime.operator_controlled is True
    assert config.tuples["local-echo"].controlled_runtime_ref == "local-echo"


def test_runtime_add_openai_generates_runtime_surface_and_worker(tmp_path) -> None:
    root = copy_config(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "runtime",
            "add-openai",
            "--config-dir",
            str(root),
            "--name",
            "test-ds4",
            "--endpoint",
            "http://127.0.0.1:18181",
            "--dataref-worker",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    config = load_config(root)
    assert "test-ds4" in config.runtimes
    tuple = config.tuples["test-ds4"]
    assert tuple.controlled_runtime_ref == "test-ds4"
    assert tuple.adapter == "local-dataref-openai-worker"
    assert tuple.input_contracts == ["text", "chat_messages", "data_refs"]
    assert tuple.output_contract == "gpucall-tuple-result"


def test_runtime_add_ollama_generates_runtime_surface_and_worker(tmp_path) -> None:
    root = copy_config(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "runtime",
            "add-ollama",
            "--config-dir",
            str(root),
            "--name",
            "test-ollama",
            "--endpoint",
            "http://127.0.0.1:11434",
            "--model",
            "qwen2.5-32b:latest",
            "--max-model-len",
            "32768",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    config = load_config(root)
    assert "test-ollama" in config.runtimes
    tuple = config.tuples["test-ollama"]
    assert tuple.controlled_runtime_ref == "test-ollama"
    assert tuple.adapter == "local-ollama"
    assert tuple.model == "qwen2.5-32b:latest"
    assert tuple.model_ref == "qwen2.5-32b-ollama-local"
    assert tuple.output_contract == "ollama-generate"


def test_readiness_cli(tmp_path) -> None:
    root = copy_config(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "readiness",
            "--config-dir",
            str(root),
            "--intent",
            "summarize_text",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"phase": "readiness"' in result.stdout
    assert "infer-summarize-text-draft" in result.stdout


def test_readiness_reports_live_catalog_blocked_tuple(tmp_path, monkeypatch) -> None:
    from gpucall.readiness import build_readiness_report

    root = copy_config(tmp_path)
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {"runpod": {"api_key": "rk_test"}})
    monkeypatch.setattr(
        "gpucall.readiness.live_tuple_catalog_evidence",
        lambda tuples, credentials: {
            name: {
                "tuple": name,
                "status": "blocked",
                "checked": True,
                "findings": [
                    {
                        "severity": "error",
                        "field": "runpod_serverless_billing_guard",
                        "raw": {"live_reason": "active_workers_present"},
                    }
                ],
            }
            for name in tuples
        },
    )

    report = build_readiness_report(config_dir=root, intent="standard_text_inference")

    recipe = report["recipes"][0]
    blocked = recipe["live_blocked_tuples"]
    assert recipe["eligible_tuple_count"] > 0
    assert blocked
    assert any(item["live_reason"] == "active_workers_present" for item in blocked)


def test_provider_smoke_writes_live_validation_artifact(tmp_path, monkeypatch) -> None:
    root = copy_config(tmp_path)
    monkeypatch.setenv("GPUCALL_ALLOW_FAKE_AUTO_TUPLES", "1")
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "tuple-smoke",
            "local-echo",
            "--config-dir",
            str(root),
            "--recipe",
            "smoke-text-small",
            "--mode",
            "sync",
            "--budget-usd",
            "0.01",
            "--allow-zero-estimate",
            "--write-artifact",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"artifact_path"' in result.stdout
    artifacts = list((tmp_path / "state" / "tuple-validation").glob("*.json"))
    assert len(artifacts) == 1
    payload = artifacts[0].read_text(encoding="utf-8")
    assert '"tuple":"local-echo"' in payload
    assert '"config_hash"' in payload
    assert '"validation_schema_version":1' in payload
    assert '"passed":true' in payload
    assert '"official_contract"' in payload
    assert '"official_contract_hash"' in payload


def test_tuple_smoke_requires_explicit_budget(tmp_path, monkeypatch) -> None:
    root = copy_config(tmp_path)
    monkeypatch.setenv("GPUCALL_ALLOW_FAKE_AUTO_TUPLES", "1")

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "tuple-smoke",
            "local-echo",
            "--config-dir",
            str(root),
            "--recipe",
            "smoke-text-small",
            "--mode",
            "sync",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--budget-usd" in result.stderr


def test_live_validation_artifact_must_match_current_commit_and_config(tmp_path, monkeypatch) -> None:
    from gpucall.cli import _config_hash, _git_commit, _latest_live_validation_artifact

    root = copy_config(tmp_path)
    state = tmp_path / "state"
    artifact_dir = state / "tuple-validation"
    artifact_dir.mkdir(parents=True)
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    (artifact_dir / "old.json").write_text('{"tuple":"p","commit":"old","config_hash":"old"}\n', encoding="utf-8")
    current = {
        "tuple": "p",
        "recipe": "smoke-text-small",
        "mode": "sync",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "commit": _git_commit(),
        "config_hash": _config_hash(root),
        "governance_hash": "c" * 64,
        "validation_schema_version": 1,
        "passed": True,
        "cleanup": {"required": False, "completed": None},
        "cost": {"observed": None, "estimated": None},
        "audit": {"event_ids": []},
        "official_contract": {
            "adapter": "echo",
            "endpoint_contract": "echo",
            "expected_endpoint_contract": "echo",
            "output_contract": "plain-text",
            "expected_output_contract": "plain-text",
            "stream_contract": "none",
            "expected_stream_contract": "none",
            "official_sources": ["local-test-source"],
        },
    }
    import hashlib

    current["official_contract_hash"] = hashlib.sha256(
        json.dumps(current["official_contract"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    (artifact_dir / "current.json").write_text(json.dumps(current), encoding="utf-8")

    latest = _latest_live_validation_artifact(config_dir=root)

    assert latest is not None
    assert latest["data"]["commit"] == current["commit"]
    assert latest["data"]["config_hash"] == current["config_hash"]


def test_security_scan_rejects_secret_like_yaml(tmp_path) -> None:
    root = copy_config(tmp_path)
    (root / "tuples" / "bad.yml").write_text("api_key: secret\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "security",
            "scan-secrets",
            "--config-dir",
            str(root),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "bad.yml" in result.stdout


def test_init_config_writes_split_tuple_catalog_files(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "init",
            "--config-dir",
            str(tmp_path / "out"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    surface = (tmp_path / "out" / "surfaces" / "modal-a10g.yml").read_text(encoding="utf-8")
    worker = (tmp_path / "out" / "workers" / "modal-a10g.yml").read_text(encoding="utf-8")
    assert "initialized gpucall config" in result.stdout
    assert (tmp_path / "out" / "policy.yml").exists()
    assert (tmp_path / "out" / "recipes" / "vision-image-standard.yml").exists()
    assert "config:" not in surface
    assert "execution_surface: function_runtime" in surface
    assert "target:" in worker
    assert "model_ref:" in worker


def test_init_config_writes_valid_default_config(tmp_path) -> None:
    out = tmp_path / "out"
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "init",
            "--config-dir",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not (out / "tuples" / "runpod-vllm-serverless.yml").exists()
    assert (out / "tuples" / "runpod-vllm-serverless.yml.example").exists()
    load_config(out)


def test_cli_config_dir_does_not_override_xdg_credentials_path(tmp_path) -> None:
    root = copy_config(tmp_path)
    env = os.environ.copy()
    env.pop("GPUCALL_CREDENTIALS", None)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "doctor",
            "--config-dir",
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    payload = result.stdout
    expected_path = Path(env["XDG_CONFIG_HOME"]) / "gpucall" / "credentials.yml"
    assert f'"credentials_path": "{expected_path}"' in payload


def test_doctor_supports_live_tuple_catalog_flag_without_credentials(tmp_path) -> None:
    root = copy_config(tmp_path)
    env = os.environ.copy()
    env["GPUCALL_CREDENTIALS"] = str(tmp_path / "missing-credentials.yml")

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "doctor",
            "--config-dir",
            str(root),
            "--live-tuple-catalog",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert '"live_tuple_catalog"' in result.stdout
    assert '"ok": false' in result.stdout
    assert "skipped bounded live lookup" in result.stdout


def test_doctor_live_tuple_catalog_lookup_is_bounded(monkeypatch) -> None:
    def slow_lookup(_tuples, _credentials):
        time.sleep(1)
        return []

    monkeypatch.setenv("GPUCALL_LIVE_TUPLE_CATALOG_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr("gpucall.cli.live_tuple_catalog_findings", slow_lookup)

    findings = _bounded_live_tuple_catalog_findings({}, {"runpod": {"api_key": "test"}})

    assert findings
    assert findings[0]["dimension"] == "live_tuple_catalog"
    assert "timed out" in findings[0]["reason"]
