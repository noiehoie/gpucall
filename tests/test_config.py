from __future__ import annotations

import shutil
import subprocess
import sys
import os
import json
import ast
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gpucall.config import ConfigError, load_config
from gpucall.cli import (
    _cap_provider_smoke_admission_lease_ttl,
    _iaas_vm_live_cost_audit,
    _bounded_live_tuple_catalog_findings,
    _managed_endpoint_live_cost_audit,
    _optional_float,
    _optional_int,
    _provider_smoke_request,
    _provider_smoke_process_wall_seconds,
    _provider_smoke_signal_wall_seconds,
    _provider_smoke_wait_seconds,
    _runpod_endpoint_inventory,
    _runpod_endpoint_inventory_by_id,
)
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
        if url == "https://rest.runpod.io/v1/endpoints?includeWorkers=true&includeTemplate=true":
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


def test_runpod_endpoint_inventory_follows_next_page(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def fake_http_json(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        seen_urls.append(url)
        if url == "https://rest.runpod.io/v1/endpoints?includeWorkers=true&includeTemplate=true":
            return {"ok": True, "body": {"endpoints": [{"id": "endpoint-1"}], "next": "https://rest.runpod.io/v1/endpoints?page=2"}}
        if url == "https://rest.runpod.io/v1/endpoints?page=2":
            return {"ok": True, "body": {"items": [{"id": "endpoint-2"}]}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("gpucall.cli._http_json", fake_http_json)

    inventory = _runpod_endpoint_inventory("secret")
    by_id = _runpod_endpoint_inventory_by_id(inventory)

    assert seen_urls == [
        "https://rest.runpod.io/v1/endpoints?includeWorkers=true&includeTemplate=true",
        "https://rest.runpod.io/v1/endpoints?page=2",
    ]
    assert sorted(by_id) == ["endpoint-1", "endpoint-2"]


def test_runpod_endpoint_inventory_preserves_partial_rows_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_http_json(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        if url == "https://rest.runpod.io/v1/endpoints?includeWorkers=true&includeTemplate=true":
            return {"ok": True, "body": {"endpoints": [{"id": "endpoint-1"}], "next": "https://rest.runpod.io/v1/endpoints?page=2"}}
        if url == "https://rest.runpod.io/v1/endpoints?page=2":
            return {"ok": False, "status_code": 502, "body": {"endpoints": [{"id": "endpoint-2"}], "partial": True}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("gpucall.cli._http_json", fake_http_json)

    inventory = _runpod_endpoint_inventory("secret")
    by_id = _runpod_endpoint_inventory_by_id(inventory)

    assert inventory["ok"] is False
    assert sorted(by_id) == ["endpoint-1", "endpoint-2"]


def test_runpod_endpoint_inventory_rejects_pagination_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_http_json(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        assert url == "https://rest.runpod.io/v1/endpoints?includeWorkers=true&includeTemplate=true"
        return {"ok": True, "body": {"endpoints": [{"id": "endpoint-1"}], "next": url}}

    monkeypatch.setattr("gpucall.cli._http_json", fake_http_json)

    inventory = _runpod_endpoint_inventory("secret")
    by_id = _runpod_endpoint_inventory_by_id(inventory)

    assert inventory["ok"] is False
    assert inventory["error"] == "RunPod endpoint inventory pagination loop detected"
    assert sorted(by_id) == ["endpoint-1"]


def test_runpod_endpoint_inventory_rejects_malformed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_http_json(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        assert url == "https://rest.runpod.io/v1/endpoints?includeWorkers=true&includeTemplate=true"
        return {"ok": True, "body": {"unexpected": [{"id": "endpoint-hidden"}]}}

    monkeypatch.setattr("gpucall.cli._http_json", fake_http_json)

    inventory = _runpod_endpoint_inventory("secret")

    assert inventory["ok"] is False
    assert inventory["error"] == "RunPod endpoint inventory response did not contain endpoint rows"


def test_runpod_endpoint_inventory_accepts_empty_endpoint_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_http_json(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        assert url == "https://rest.runpod.io/v1/endpoints?includeWorkers=true&includeTemplate=true"
        return {"ok": True, "body": {"endpoints": []}}

    monkeypatch.setattr("gpucall.cli._http_json", fake_http_json)

    inventory = _runpod_endpoint_inventory("secret")

    assert inventory == {"ok": True, "status_code": 200, "body": {"endpoints": []}}


def test_optional_numeric_manifest_fields_reject_invalid_values() -> None:
    with pytest.raises(SystemExit, match="requests_per_minute must be an integer"):
        _optional_int("not-int", field="requests_per_minute")
    with pytest.raises(SystemExit, match="requests_per_minute must be an integer"):
        _optional_int(True, field="requests_per_minute")
    with pytest.raises(SystemExit, match="requests_per_minute must be an integer"):
        _optional_int(1.5, field="requests_per_minute")
    with pytest.raises(SystemExit, match="requests_per_minute must be >= 1"):
        _optional_int(0, field="requests_per_minute")
    with pytest.raises(SystemExit, match="daily_budget_usd must be a number"):
        _optional_float("not-float", field="daily_budget_usd")
    with pytest.raises(SystemExit, match="daily_budget_usd must be a number"):
        _optional_float(False, field="daily_budget_usd")
    with pytest.raises(SystemExit, match="daily_budget_usd must be a finite number"):
        _optional_float(float("nan"), field="daily_budget_usd")
    with pytest.raises(SystemExit, match="daily_budget_usd must be a finite number"):
        _optional_float(float("inf"), field="daily_budget_usd")
    with pytest.raises(SystemExit, match="daily_budget_usd must be >= 0"):
        _optional_float(-0.1, field="daily_budget_usd")


def test_managed_endpoint_live_cost_audit_keeps_runpod_rows_with_unsupported_family(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_http_json(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        if url == "https://rest.runpod.io/v1/endpoints?includeWorkers=true&includeTemplate=true":
            return {"ok": True, "body": [{"id": "endpoint-1", "workersMin": 0}]}
        if url == "https://api.runpod.ai/v2/endpoint-1/health":
            return {"ok": True, "body": {"healthy": True}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("gpucall.cli._http_json", fake_http_json)
    report = _managed_endpoint_live_cost_audit(
        {
            "runpod": SimpleNamespace(
                name="runpod",
                adapter="runpod-vllm-serverless",
                execution_surface=SimpleNamespace(value="managed_endpoint"),
                endpoint="https://api.runpod.ai/v2",
                target="endpoint-1",
            ),
            "other": SimpleNamespace(
                name="other",
                adapter="other-managed",
                execution_surface=SimpleNamespace(value="managed_endpoint"),
                endpoint="https://other.example/v1",
                target="endpoint-2",
            ),
        },
        {"runpod": {"api_key": "secret"}},
    )

    assert report["ok"] is False
    assert report["unsupported_credential_families"] == ["other-managed"]
    assert report["endpoints"][0]["endpoint_id"] == "endpoint-1"


def test_iaas_vm_live_cost_audit_checks_each_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def fake_http_json(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        seen_urls.append(url)
        return {"ok": True, "body": []}

    monkeypatch.setattr("gpucall.cli._http_json", fake_http_json)
    report = _iaas_vm_live_cost_audit(
        {
            "one": SimpleNamespace(adapter="hyperstack", execution_surface=SimpleNamespace(value="iaas_vm"), endpoint="https://one.example/v1"),
            "two": SimpleNamespace(adapter="hyperstack", execution_surface=SimpleNamespace(value="iaas_vm"), endpoint="https://two.example/v1"),
        },
        {"hyperstack": {"api_key": "secret"}},
    )

    assert seen_urls == [
        "https://one.example/v1/core/virtual-machines",
        "https://two.example/v1/core/virtual-machines",
    ]
    assert sorted(report["virtual_machines_by_endpoint"]) == ["https://one.example/v1", "https://two.example/v1"]


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
            {"modal-vision-catalog-l4-microsoft-florence-2-large-ft", "modal-vision-catalog-h200-qwen2-5-vl-7b-instruct"},
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
    runpod_surface = root / "surfaces" / "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct.yml"
    runpod_surface_payload = yaml.safe_load(runpod_surface.read_text(encoding="utf-8"))
    runpod_surface_payload["configured_price_observed_at"] = datetime.now(timezone.utc).isoformat()
    runpod_surface.write_text(yaml.safe_dump(runpod_surface_payload, sort_keys=False), encoding="utf-8")
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

    assert plan.tuple_chain[0] == "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"
    assert "modal-vision-catalog-rtx-pro-6000-qwen2-5-vl-3b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-h200-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-b200-qwen2-5-vl-32b-instruct" in plan.tuple_chain
    assert "modal-h100-florence-2-large-ft" not in plan.tuple_chain

    request = TaskRequest(
        task="vision",
        mode=ExecutionMode.SYNC,
        intent="understand_document_image",
        input_refs=[DataRef(uri="s3://bucket/image.png", sha256="c" * 64, bytes=2_000_000, content_type="image/png")],
        response_format={"type": "json_schema", "json_schema": {"type": "object", "required": ["articles"], "properties": {"articles": {"type": "array"}}}},
    )

    plan = compiler.compile(request)

    assert plan.tuple_chain[0] == "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"
    assert "modal-vision-catalog-rtx-pro-6000-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-rtx-pro-6000-qwen2-5-vl-32b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-b200-qwen2-5-vl-7b-instruct" in plan.tuple_chain
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
    assert "modal-vision-catalog-rtx-pro-6000-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-rtx-pro-6000-qwen2-5-vl-32b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-b200-qwen2-5-vl-7b-instruct" in plan.tuple_chain
    assert "modal-vision-catalog-b200-qwen2-5-vl-32b-instruct" in plan.tuple_chain
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
    assert request.max_tokens == 16
    assert request.timeout_seconds == recipe.timeout_seconds
    assert request.lease_ttl_seconds == recipe.lease_ttl_seconds


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
    assert '"summary":' in result.stdout
    assert '"tuple_count":' not in result.stdout
    assert '"omitted":' in result.stdout


def test_validate_config_cli_verbose_lists_names(tmp_path) -> None:
    root = copy_config(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "validate-config",
            "--config-dir",
            str(root),
            "--verbose",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"valid": true' in result.stdout
    assert '"runtimes": [' in result.stdout
    assert '"tuples": [' in result.stdout


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
    payload = json.loads(result.stdout)
    recipe = next(item for item in payload["recipes"] if item["recipe"] == "infer-summarize-text-draft")
    assert recipe["context_budget_tokens"] > 0
    assert recipe["allowed_modes"]


def test_local_author_ollama_requires_route_validation_evidence(tmp_path) -> None:
    from gpucall.validation_evidence import route_validation_required_for_tuple

    config = load_config(copy_config(tmp_path))

    assert route_validation_required_for_tuple(config.tuples["local-echo"]) is False
    assert route_validation_required_for_tuple(config.tuples["local-author-ollama"]) is True


def test_readiness_blocks_local_author_ollama_without_validation(tmp_path, monkeypatch) -> None:
    from gpucall.readiness import build_readiness_report

    root = copy_config(tmp_path)
    state = tmp_path / "state"
    artifact_dir = state / "tuple-validation"
    artifact_dir.mkdir(parents=True)
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {})

    report = build_readiness_report(config_dir=root, intent="translate_text", validation_dir=artifact_dir)
    recipe = next(item for item in report["recipes"] if item["recipe"] == "infer-translate-text-draft")

    assert not any(item["tuple"] == "local-author-ollama" for item in recipe["live_ready_tuples"])
    blocked = next(item for item in recipe["live_blocked_tuples"] if item["tuple"] == "local-author-ollama")
    assert blocked["route_validation_required"] is True
    assert blocked["live_reason"] == "missing_route_validation_evidence"


def test_readiness_reports_live_catalog_blocked_tuple(tmp_path, monkeypatch) -> None:
    from gpucall.readiness import build_readiness_report

    root = copy_config(tmp_path)
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {"runpod": {"api_key": "rk_test"}})
    monkeypatch.setattr(
        "gpucall.readiness.live_tuple_catalog_evidence",
        lambda tuples, credentials: {
            name: {
                "tuple": name,
                "adapter": tuples[name].adapter,
                "status": "blocked",
                "checked": True,
                "findings": [
                    {
                        "tuple": name,
                        "adapter": tuples[name].adapter,
                        "dimension": "endpoint",
                        "severity": "error",
                        "reason": "RunPod serverless billing guard blocked this endpoint",
                        "field": "runpod_serverless_billing_guard",
                        "raw": {"live_reason": "active_workers_present"},
                    }
                ],
            }
            for name in tuples
        },
    )

    report = build_readiness_report(config_dir=root, intent="standard_text_inference", live=True)

    recipe = report["recipes"][0]
    blocked = recipe["live_blocked_tuples"]
    assert recipe["eligible_tuple_count"] > 0
    assert blocked
    assert any(item["live_reason"] == "active_workers_present" for item in blocked)


def test_readiness_uses_panopticon_snapshot_without_live_probe(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timezone

    from gpucall.panopticon import store_panopticon_evidence
    from gpucall.readiness import build_readiness_report

    root = copy_config(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {"runpod": {"api_key": "rk_test"}})

    def fail_live_probe(*_args, **_kwargs):
        raise AssertionError("readiness must not call live provider probes without live=True")

    monkeypatch.setattr("gpucall.readiness.live_tuple_catalog_evidence", fail_live_probe)
    store_panopticon_evidence(
        {
            "local-author-ollama": {
                "tuple": "local-author-ollama",
                "status": "blocked",
                "checked": True,
                "findings": [
                    {
                        "tuple": "local-author-ollama",
                        "adapter": "local-ollama",
                        "dimension": "stock",
                        "severity": "error",
                        "reason": "cached provider snapshot says tuple is not ready",
                        "raw": {"live_reason": "panopticon_test_block"},
                    }
                ],
            }
        },
        state / "catalog" / "provider-panopticon.json",
        now=datetime.now(timezone.utc),
        ttl_seconds=86400,
    )

    report = build_readiness_report(config_dir=root, intent="standard_text_inference")

    recipe = report["recipes"][0]
    assert report["panopticon"]["source"] == "panopticon_snapshot"
    assert report["panopticon"]["status"] == "ok"
    assert str(report["panopticon"]["snapshot_hash"]).startswith("sha256:")
    assert any(item["tuple"] == "local-author-ollama" and item["live_reason"] == "panopticon_test_block" for item in recipe["live_blocked_tuples"])


def test_readiness_requires_exact_recipe_mode_validation_evidence(tmp_path, monkeypatch) -> None:
    from gpucall.execution.contracts import official_contract, official_contract_hash
    from gpucall.readiness import build_readiness_report
    from gpucall.validation_evidence import config_hash, git_commit

    root = copy_config(tmp_path)
    state = tmp_path / "state"
    artifact_dir = state / "tuple-validation"
    artifact_dir.mkdir(parents=True)
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {})

    report = build_readiness_report(config_dir=root, intent="summarize_text", validation_dir=artifact_dir)
    recipe_report = next(item for item in report["recipes"] if item["eligible_tuple_count"] > 0)
    blocked = [
        item
        for item in recipe_report["live_blocked_tuples"]
        if item.get("live_reason") == "missing_route_validation_evidence"
    ]
    assert blocked
    tuple_name = blocked[0]["tuple"]
    recipe_name = recipe_report["recipe"]
    mode = blocked[0]["mode"]
    config = load_config(root)
    tuple_spec = config.tuples[tuple_name]
    contract = official_contract(tuple_spec)
    base = {
        "tuple": tuple_name,
        "mode": mode,
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "commit": git_commit(),
        "config_hash": config_hash(root),
        "governance_hash": "c" * 64,
        "validation_schema_version": 1,
        "passed": True,
        "cleanup": {"required": False, "completed": None},
        "cost": {"observed": None, "estimated": None},
        "audit": {"event_ids": []},
        "official_contract": contract,
        "official_contract_hash": official_contract_hash(contract),
    }
    wrong = dict(base, recipe="wrong-recipe")
    (artifact_dir / "wrong.json").write_text(json.dumps(wrong), encoding="utf-8")

    wrong_report = build_readiness_report(config_dir=root, recipe=recipe_name, validation_dir=artifact_dir)
    wrong_recipe_report = wrong_report["recipes"][0]
    assert any(
        item["tuple"] == tuple_name and item.get("live_reason") == "missing_route_validation_evidence"
        for item in wrong_recipe_report["live_blocked_tuples"]
    )

    exact = dict(base, recipe=recipe_name)
    (artifact_dir / "exact.json").write_text(json.dumps(exact), encoding="utf-8")

    exact_report = build_readiness_report(config_dir=root, recipe=recipe_name, validation_dir=artifact_dir)
    exact_recipe_report = exact_report["recipes"][0]
    assert any(
        item["tuple"] == tuple_name and str(item.get("live_validation_artifact") or "").endswith("exact.json")
        for item in exact_recipe_report["live_ready_tuples"]
    )


def test_readiness_evaluates_route_validation_for_each_allowed_mode(tmp_path, monkeypatch) -> None:
    from gpucall.execution.contracts import official_contract, official_contract_hash
    from gpucall.readiness import build_readiness_report
    from gpucall.validation_evidence import config_hash, git_commit

    root = copy_config(tmp_path)
    state = tmp_path / "state"
    artifact_dir = state / "tuple-validation"
    artifact_dir.mkdir(parents=True)
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {})

    report = build_readiness_report(config_dir=root, intent="summarize_text", validation_dir=artifact_dir)
    candidates: dict[str, set[str]] = {}
    for recipe_report in report["recipes"]:
        if "sync" not in recipe_report["allowed_modes"] or "async" not in recipe_report["allowed_modes"]:
            continue
        for row in recipe_report["live_blocked_tuples"]:
            if row.get("live_reason") == "missing_route_validation_evidence":
                candidates.setdefault(f"{recipe_report['recipe']}::{row['tuple']}", set()).add(row["mode"])
    key = next((item for item, modes in candidates.items() if {"sync", "async"} <= modes), None)
    assert key is not None
    recipe_name, tuple_name = key.split("::", 1)

    config = load_config(root)
    tuple_spec = config.tuples[tuple_name]
    contract = official_contract(tuple_spec)
    exact = {
        "tuple": tuple_name,
        "recipe": recipe_name,
        "mode": "async",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "commit": git_commit(),
        "config_hash": config_hash(root),
        "governance_hash": "c" * 64,
        "validation_schema_version": 1,
        "passed": True,
        "cleanup": {"required": False, "completed": None},
        "cost": {"observed": None, "estimated": None},
        "audit": {"event_ids": []},
        "official_contract": contract,
        "official_contract_hash": official_contract_hash(contract),
    }
    (artifact_dir / "async-exact.json").write_text(json.dumps(exact), encoding="utf-8")

    exact_report = build_readiness_report(config_dir=root, recipe=recipe_name, validation_dir=artifact_dir)
    recipe_report = exact_report["recipes"][0]
    assert recipe_report["production_activated"] is True
    assert recipe_report["selected_mode"] == "async"
    assert recipe_report["mode_readiness"]["async"]["live_ready_tuple_count"] >= 1
    assert recipe_report["mode_readiness"]["sync"]["live_blocked_tuple_count"] >= 1
    assert any(
        item["tuple"] == tuple_name
        and item["mode"] == "async"
        and str(item.get("live_validation_artifact") or "").endswith("async-exact.json")
        for item in recipe_report["live_ready_tuples"]
    )
    assert any(
        item["tuple"] == tuple_name
        and item["mode"] == "sync"
        and item.get("live_reason") == "missing_route_validation_evidence"
        for item in recipe_report["live_blocked_tuples"]
    )


def test_readiness_reports_latest_failed_route_validation_artifact(tmp_path, monkeypatch) -> None:
    from gpucall.execution.contracts import official_contract, official_contract_hash
    from gpucall.readiness import build_readiness_report
    from gpucall.validation_evidence import config_hash, git_commit

    root = copy_config(tmp_path)
    state = tmp_path / "state"
    artifact_dir = state / "tuple-validation"
    artifact_dir.mkdir(parents=True)
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {})

    report = build_readiness_report(config_dir=root, intent="summarize_text", validation_dir=artifact_dir)
    recipe_report = next(item for item in report["recipes"] if item["recipe"] == "infer-summarize-text-draft")
    blocked = next(item for item in recipe_report["live_blocked_tuples"] if item.get("live_reason") == "missing_route_validation_evidence")
    tuple_name = blocked["tuple"]
    recipe_name = recipe_report["recipe"]
    mode = blocked["mode"]
    config = load_config(root)
    tuple_spec = config.tuples[tuple_name]
    contract = official_contract(tuple_spec)
    failed = {
        "tuple": tuple_name,
        "recipe": recipe_name,
        "mode": mode,
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "commit": git_commit(),
        "config_hash": config_hash(root),
        "governance_hash": "c" * 64,
        "validation_schema_version": 1,
        "passed": False,
        "error": {"code": "PROVIDER_RESOURCE_EXHAUSTED", "retryable": True, "status_code": 503},
        "cleanup": {"required": False, "completed": None},
        "cost": {"observed": None, "estimated": None},
        "audit": {"event_ids": []},
        "official_contract": contract,
        "official_contract_hash": official_contract_hash(contract),
    }
    (artifact_dir / "failed.json").write_text(json.dumps(failed), encoding="utf-8")

    failed_report = build_readiness_report(config_dir=root, recipe=recipe_name, validation_dir=artifact_dir)
    failed_recipe_report = failed_report["recipes"][0]
    row = next(item for item in failed_recipe_report["live_blocked_tuples"] if item["tuple"] == tuple_name and item["mode"] == mode)

    assert row["live_reason"] == "latest_route_validation_failed:PROVIDER_RESOURCE_EXHAUSTED"
    assert row["route_validation_status"] == "rejected"
    assert row["route_validation_reason"] == "latest_route_validation_failed:PROVIDER_RESOURCE_EXHAUSTED"
    assert str(row["latest_route_validation_artifact"]).endswith("failed.json")
    assert any("rerun explicit tuple validation" in action for action in failed_recipe_report["next_actions"])


def test_readiness_does_not_label_unloaded_accepted_validation_as_rejected(tmp_path, monkeypatch) -> None:
    from gpucall.execution.contracts import official_contract, official_contract_hash
    from gpucall.readiness import build_readiness_report
    from gpucall.validation_evidence import config_hash

    root = copy_config(tmp_path)
    state = tmp_path / "state"
    artifact_dir = state / "tuple-validation"
    artifact_dir.mkdir(parents=True)
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {})
    monkeypatch.setattr("gpucall.validation_evidence.git_commit", lambda: None)

    report = build_readiness_report(config_dir=root, intent="summarize_text", validation_dir=artifact_dir)
    recipe_report = next(item for item in report["recipes"] if item["recipe"] == "infer-summarize-text-draft")
    blocked = next(item for item in recipe_report["live_blocked_tuples"] if item.get("live_reason") == "missing_route_validation_evidence")
    tuple_name = blocked["tuple"]
    mode = blocked["mode"]
    tuple_spec = load_config(root).tuples[tuple_name]
    contract = official_contract(tuple_spec)
    accepted_but_unloaded = {
        "tuple": tuple_name,
        "recipe": recipe_report["recipe"],
        "mode": mode,
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "commit": "unavailable-during-test",
        "config_hash": config_hash(root),
        "governance_hash": "c" * 64,
        "validation_schema_version": 1,
        "passed": True,
        "cleanup": {"required": False, "completed": None},
        "cost": {"observed": None, "estimated": None},
        "audit": {"event_ids": []},
        "official_contract": contract,
        "official_contract_hash": official_contract_hash(contract),
    }
    (artifact_dir / "accepted-but-unloaded.json").write_text(json.dumps(accepted_but_unloaded), encoding="utf-8")

    checked = build_readiness_report(config_dir=root, recipe=recipe_report["recipe"], validation_dir=artifact_dir)
    row = next(item for item in checked["recipes"][0]["live_blocked_tuples"] if item["tuple"] == tuple_name and item["mode"] == mode)

    assert row["route_validation_status"] == "accepted"
    assert row["live_reason"] == "missing_route_validation_evidence"


def test_readiness_reports_policy_blocked_replacement_candidates(tmp_path, monkeypatch) -> None:
    from gpucall.readiness import build_readiness_report

    root = copy_config(tmp_path)
    config = load_config(root)
    config.policy.tuples.allow = ["local-echo"]
    monkeypatch.setattr("gpucall.readiness.load_credentials", lambda: {})

    report = build_readiness_report(config_dir=root, config=config, intent="summarize_text", validation_dir=tmp_path / "validation")
    recipe_report = next(item for item in report["recipes"] if item["recipe"] == "infer-summarize-text-draft")

    assert recipe_report["rejected_tuple_reasons"]["tuple is not in policy allowlist"] > 0
    assert recipe_report["policy_blocked_candidate_tuples"]
    assert all(item["max_model_len"] >= recipe_report["context_budget_tokens"] for item in recipe_report["policy_blocked_candidate_tuples"])
    assert any("add validated replacement tuple to policy allowlist" in action for action in recipe_report["next_actions"])


def test_runpod_endpoint_inventory_miss_exposes_machine_reason(monkeypatch) -> None:
    from gpucall.execution_surfaces import managed_endpoint

    monkeypatch.setattr(managed_endpoint, "_runpod_endpoint_live_inventory_rows", lambda api_key, base_url: [])
    monkeypatch.setattr(managed_endpoint, "_runpod_network_volume_live_inventory_rows", lambda api_key, base_url: [])
    tuple_spec = SimpleNamespace(
        name="runpod-vllm-a100-80gb-qwen2-5-7b-instruct-1m-524k",
        adapter="runpod-vllm-serverless",
        target="yum0u6a4khw7gi",
        endpoint=None,
    )

    findings = managed_endpoint.runpod_endpoint_catalog_findings([tuple_spec], {"runpod": {"api_key": "rk_test"}})

    assert findings == [
        {
            "adapter": "runpod-vllm-serverless",
            "dimension": "endpoint",
            "field": "runpod_endpoint_inventory",
            "raw": {"endpoint_id": "yum0u6a4khw7gi", "live_reason": "endpoint_missing_from_inventory"},
            "reason": "configured RunPod endpoint was not present in live endpoint inventory",
            "severity": "error",
            "source": "https://rest.runpod.io/v1/endpoints",
            "tuple": "runpod-vllm-a100-80gb-qwen2-5-7b-instruct-1m-524k",
        }
    ]


def test_runpod_network_volume_inventory_blocks_unattached_undeclared_storage(monkeypatch) -> None:
    from gpucall.execution_surfaces import managed_endpoint

    monkeypatch.setattr(managed_endpoint, "_runpod_endpoint_live_inventory_rows", lambda api_key, base_url: [])
    monkeypatch.setattr(
        managed_endpoint,
        "_runpod_network_volume_live_inventory_rows",
        lambda api_key, base_url: [
            {"id": "le9b9gqqu6", "name": "news-llm-models", "dataCenterId": "US-NC-1", "size": 80}
        ],
    )
    tuple_spec = SimpleNamespace(
        name="runpod-vllm-a100-80gb-qwen2-5-7b-instruct-1m-524k",
        adapter="runpod-vllm-serverless",
        target="yum0u6a4khw7gi",
        endpoint=None,
        provider_params={},
    )

    findings = managed_endpoint.runpod_endpoint_catalog_findings([tuple_spec], {"runpod": {"api_key": "rk_test"}})

    storage_finding = next(item for item in findings if item["dimension"] == "storage")
    assert storage_finding["tuple"] == "runpod-network-volume-le9b9gqqu6"
    assert storage_finding["severity"] == "error"
    assert storage_finding["raw"]["resource_type"] == "network_volume"
    assert storage_finding["raw"]["resource_id"] == "le9b9gqqu6"
    assert storage_finding["raw"]["storage_size_gb"] == 80
    assert storage_finding["raw"]["estimated_monthly_usd"] == 5.6
    assert storage_finding["raw"]["attached_endpoint_count"] == 0
    assert storage_finding["raw"]["declared_by_tuple_count"] == 0
    assert storage_finding["raw"]["content_inventory_status"] == "missing_runpod_s3_credentials"
    assert storage_finding["raw"]["live_reason"] == "persistent_storage_unattached_undeclared"


def test_runpod_network_volume_inventory_marks_declared_or_attached_storage_info(monkeypatch) -> None:
    from gpucall.execution_surfaces import managed_endpoint

    monkeypatch.setattr(
        managed_endpoint,
        "_runpod_endpoint_live_inventory_rows",
        lambda api_key, base_url: [{"id": "endpoint-1", "networkVolumeId": "vol-attached", "workersMin": 0, "workersMax": 0}],
    )
    monkeypatch.setattr(
        managed_endpoint,
        "_runpod_network_volume_live_inventory_rows",
        lambda api_key, base_url: [
            {"id": "vol-attached", "name": "attached", "dataCenterId": "US-NC-1", "size": 10},
            {"id": "vol-declared", "name": "declared", "dataCenterId": "US-NC-1", "size": 20},
        ],
    )

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"workers": {"ready": 1, "idle": 0, "running": 0, "inQueue": 0}}

    class Session:
        def get(self, url: str, **kwargs: object) -> Response:
            if url.endswith("/health"):
                return Response()
            raise AssertionError(url)

    monkeypatch.setattr(managed_endpoint, "requests_session", lambda: Session())
    tuple_spec = SimpleNamespace(
        name="runpod-vllm-a100",
        adapter="runpod-vllm-serverless",
        target="endpoint-1",
        endpoint=None,
        endpoint_contract=None,
        provider_params={"persistent_resources": {"runpod_network_volumes": [{"id": "vol-declared"}]}},
        standing_cost_per_second=None,
        standing_cost_window_seconds=None,
    )

    findings = managed_endpoint.runpod_endpoint_catalog_findings([tuple_spec], {"runpod": {"api_key": "rk_test"}})

    storage_findings = {item["raw"]["resource_id"]: item for item in findings if item["dimension"] == "storage"}
    assert storage_findings["vol-attached"]["severity"] == "info"
    assert storage_findings["vol-attached"]["raw"]["attached_endpoint_ids"] == ["endpoint-1"]
    assert storage_findings["vol-declared"]["severity"] == "info"
    assert storage_findings["vol-declared"]["raw"]["declared_by_tuples"] == ["runpod-vllm-a100"]


def test_runpod_openai_models_probe_timeout_blocks_serving_readiness(monkeypatch) -> None:
    from gpucall.execution_surfaces import managed_endpoint

    monkeypatch.setattr(
        managed_endpoint,
        "_runpod_endpoint_live_inventory_rows",
        lambda api_key, base_url: [{"id": "30g7ze5wb2n3xw", "workersMin": 0, "workersMax": 1}],
    )
    monkeypatch.setattr(managed_endpoint, "_runpod_network_volume_live_inventory_rows", lambda api_key, base_url: [])

    class Response:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self) -> dict[str, object]:
            return self._payload

    class Session:
        def get(self, url: str, **kwargs: object) -> Response:
            if url.endswith("/health"):
                return Response(200, {"workers": {"ready": 1, "idle": 1, "running": 0, "inQueue": 0}})
            if url.endswith("/openai/v1/models"):
                raise TimeoutError("models probe timed out")
            raise AssertionError(url)

    monkeypatch.setattr(managed_endpoint, "requests_session", lambda: Session())
    tuple_spec = SimpleNamespace(
        name="runpod-vllm-h100-80gb-qwen2-5-7b-instruct",
        adapter="runpod-vllm-serverless",
        target="30g7ze5wb2n3xw",
        endpoint=None,
        endpoint_contract="openai-chat-completions",
        model="Qwen/Qwen2.5-7B-Instruct",
        provider_params={},
        standing_cost_per_second=None,
        standing_cost_window_seconds=None,
    )

    findings = managed_endpoint.runpod_endpoint_catalog_findings([tuple_spec], {"runpod": {"api_key": "rk_test"}})

    models_finding = next(item for item in findings if item["dimension"] == "models")
    assert models_finding["severity"] == "error"
    assert models_finding["field"] == "openai_models"
    assert models_finding["raw"]["live_reason"] == "models_probe_timeout"
    assert models_finding["raw"]["error_code"] == "PROVIDER_TIMEOUT"


def test_readiness_shipment_status_treats_models_probe_error_as_provider_lack() -> None:
    from gpucall.readiness import classify_shipment_status

    report = {
        "eligible_tuple_count": 1,
        "live_ready_tuple_count": 0,
        "live_blocked_tuples": [
            {
                "live_reason": "models_probe_timeout",
                "live_catalog_findings": [
                    {
                        "dimension": "models",
                        "severity": "error",
                        "field": "openai_models",
                        "raw": {"live_reason": "models_probe_timeout", "error_code": "PROVIDER_TIMEOUT"},
                    }
                ],
            }
        ],
    }

    assert classify_shipment_status(report) == "provider_lack"


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


def test_tuple_smoke_poll_timeout_is_bounded() -> None:
    assert _provider_smoke_wait_seconds(3600, 300.0) == pytest.approx(300.0)
    assert _provider_smoke_wait_seconds(120, 300.0) == pytest.approx(120.0)
    assert _provider_smoke_wait_seconds(3600, None) == pytest.approx(3600.0)
    assert _provider_smoke_process_wall_seconds("sync", 15.0) == pytest.approx(20.0)
    assert _provider_smoke_process_wall_seconds("async", 15.0) == pytest.approx(40.0)
    assert _provider_smoke_signal_wall_seconds(15.0) == pytest.approx(20.0)


def test_tuple_smoke_caps_admission_lease_ttl(monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_ADMISSION_LEASE_TTL_SECONDS", "3600")

    _cap_provider_smoke_admission_lease_ttl(15.0)

    assert float(os.environ["GPUCALL_ADMISSION_LEASE_TTL_SECONDS"]) == pytest.approx(45.0)


def test_tuple_smoke_child_handles_string_system_exit(tmp_path, monkeypatch, capsys) -> None:
    import gpucall.cli as cli_module

    async def fake_provider_smoke_command(*_args, **_kwargs):
        raise SystemExit("tuple-smoke budget exceeded before execution")

    monkeypatch.setattr(cli_module, "provider_smoke_command", fake_provider_smoke_command)

    code = cli_module._run_provider_smoke_command_child(
        tmp_path,
        "tuple",
        "recipe",
        "sync",
        budget_usd=0.01,
        allow_zero_estimate=False,
        poll_timeout_seconds=1.0,
        write_artifact=False,
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "budget exceeded" in captured.err


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
