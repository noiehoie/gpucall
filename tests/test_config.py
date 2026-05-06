from __future__ import annotations

import shutil
import subprocess
import sys
import os
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gpucall.config import ConfigError, load_config
from gpucall.cli import _provider_smoke_request
from gpucall.compiler import GovernanceCompiler
from gpucall.domain import DataRef, ExecutionMode, ProviderSpec, SecurityTier, TaskRequest
from gpucall.registry import ObservedRegistry


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    root = tmp_path / "config"
    shutil.copytree(source, root)
    return root


def test_load_config_rejects_recipe_without_capable_provider(tmp_path) -> None:
    root = copy_config(tmp_path)
    (root / "recipes" / "text-infer-standard.yml").write_text(
        """
name: text-infer-standard
task: infer
data_classification: confidential
allowed_modes: [sync]
min_vram_gb: 999
max_model_len: 999999
timeout_seconds: 30
lease_ttl_seconds: 120
tokenizer_family: qwen
gpu: L4
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="no provider satisfying"):
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
providers:
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
task: infer
data_classification: confidential
allowed_modes: [sync]
min_vram_gb: 24
max_model_len: 32768
timeout_seconds: 30
lease_ttl_seconds: 120
tokenizer_family: qwen
gpu: L4
""".lstrip(),
        encoding="utf-8",
    )
    (root / "providers" / "local-echo.yml").write_text(
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

    with pytest.raises(ConfigError, match="no provider satisfying"):
        load_config(root)


def test_load_config_rejects_provider_model_len_above_declared_model_capability(tmp_path) -> None:
    root = copy_config(tmp_path)
    (root / "providers" / "hyperstack.yml").write_text(
        """
name: hyperstack-a100
adapter: hyperstack
max_data_classification: restricted
gpu: A100
vram_gb: 80
max_model_len: 131072
declared_model_max_len: 32768
cost_per_second: 0.0012
modes: [sync, async]
target: default-CANADA-1
model: Qwen/Qwen2.5-1.5B-Instruct
instance: n3-A100x1
image: Ubuntu Server 22.04 LTS R570 CUDA 12.8 with Docker
key_name: gpucall-key
lease_manifest_path: null
ssh_remote_cidr: 203.0.113.0/24
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="declared model capability"):
        load_config(root)


def test_load_config_rejects_hyperstack_all_open_ssh_cidr(tmp_path) -> None:
    root = copy_config(tmp_path)
    provider_path = root / "providers" / "hyperstack.yml"
    provider = provider_path.read_text(encoding="utf-8")
    provider = provider.replace("ssh_remote_cidr: 203.0.113.10/32", "ssh_remote_cidr: 0.0.0.0/0")
    provider_path.write_text(provider, encoding="utf-8")

    with pytest.raises(ConfigError, match="must not allow all addresses"):
        load_config(root)


def test_load_config_validation_error_does_not_echo_secret_values(tmp_path) -> None:
    root = copy_config(tmp_path)
    provider_path = root / "providers" / "modal.yml"
    provider = provider_path.read_text(encoding="utf-8")
    provider = provider.replace("vram_gb: 24", "vram_gb: secret-token-123")
    provider += "\napi_key: secret-extra-456\n"
    provider_path.write_text(provider, encoding="utf-8")

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
    assert '"provider_chain"' in result.stdout
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
    assert config.recipes["text-infer-large"].max_model_len == 65536
    assert config.recipes["text-infer-exlarge"].max_model_len == 131072
    assert config.recipes["text-infer-ultralong"].max_model_len == 524288
    assert config.recipes["text-infer-standard"].max_model_len == 32768
    assert config.recipes["text-infer-standard"].max_input_bytes == 16777216
    assert config.recipes["text-infer-light"].output_validation_attempts == 2
    assert config.recipes["text-infer-ultralong"].output_validation_attempts == 2
    assert config.recipes["vision-image-standard"].task == "vision"
    assert config.recipes["vision-image-standard"].auto_select is True
    assert config.recipes["vision-image-standard"].allowed_mime_prefixes == ["image/"]
    assert config.providers["hyperstack-a100"].max_model_len == 32768
    assert config.providers["hyperstack-a100"].declared_model_max_len == 32768
    assert config.providers["hyperstack-qwen-1m"].max_model_len == 524288
    assert config.providers["hyperstack-qwen-1m"].declared_model_max_len == 1010000
    assert config.providers["hyperstack-qwen-1m"].model == "Qwen/Qwen2.5-7B-Instruct-1M"
    assert config.providers["hyperstack-a100"].trust_profile.dedicated_gpu is True
    assert config.providers["modal-a10g"].trust_profile.security_tier is SecurityTier.ENCRYPTED_CAPSULE
    assert config.providers["modal-a10g"].supports_vision is False
    assert "image" not in config.providers["modal-a10g"].input_contracts
    assert config.providers["modal-vision-a10g"].supports_vision is True
    assert config.providers["modal-vision-a10g"].model == "Salesforce/blip-vqa-base"
    assert "image" in config.providers["modal-vision-a10g"].input_contracts


def test_standard_config_routes_news_sized_prompts_to_long_recipes(tmp_path) -> None:
    config = load_config(copy_config(tmp_path))
    compiler = GovernanceCompiler(policy=config.policy, recipes=config.recipes, providers=config.providers, registry=ObservedRegistry())

    large_plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode=ExecutionMode.SYNC,
            input_refs=[DataRef(uri="s3://bucket/chosun.txt", sha256="a" * 64, bytes=32000, content_type="text/plain")],
        )
    )
    ultralong_plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode=ExecutionMode.SYNC,
            input_refs=[DataRef(uri="s3://bucket/integrated.txt", sha256="b" * 64, bytes=220000, content_type="text/plain")],
        )
    )

    assert large_plan.recipe_name == "text-infer-large"
    assert large_plan.provider_chain[0] == "hyperstack-qwen-1m"
    assert ultralong_plan.recipe_name == "text-infer-ultralong"
    assert ultralong_plan.provider_chain[0] == "hyperstack-qwen-1m"


def test_provider_smoke_uses_chat_messages_for_chat_only_provider(tmp_path) -> None:
    config = load_config(copy_config(tmp_path))
    recipe = config.recipes["text-infer-light"]
    provider = ProviderSpec(
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
    runtime = SimpleNamespace(compiler=SimpleNamespace(providers={provider.name: provider}))

    request = _provider_smoke_request(runtime, recipe, ExecutionMode.SYNC, provider.name)

    assert request.messages
    assert request.messages[0].role == "user"
    assert request.messages[0].content == "gpucall provider smoke"
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


def test_provider_smoke_writes_live_validation_artifact(tmp_path, monkeypatch) -> None:
    root = copy_config(tmp_path)
    monkeypatch.setenv("GPUCALL_ALLOW_FAKE_AUTO_PROVIDERS", "1")
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "provider-smoke",
            "local-echo",
            "--config-dir",
            str(root),
            "--recipe",
            "smoke-text-small",
            "--mode",
            "sync",
            "--write-artifact",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"artifact_path"' in result.stdout
    artifacts = list((tmp_path / "state" / "provider-validation").glob("*.json"))
    assert len(artifacts) == 1
    payload = artifacts[0].read_text(encoding="utf-8")
    assert '"provider":"local-echo"' in payload
    assert '"config_hash"' in payload
    assert '"validation_schema_version":1' in payload
    assert '"passed":true' in payload
    assert '"official_contract"' in payload
    assert '"official_contract_hash"' in payload


def test_live_validation_artifact_must_match_current_commit_and_config(tmp_path, monkeypatch) -> None:
    from gpucall.cli import _config_hash, _git_commit, _latest_live_validation_artifact

    root = copy_config(tmp_path)
    state = tmp_path / "state"
    artifact_dir = state / "provider-validation"
    artifact_dir.mkdir(parents=True)
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    (artifact_dir / "old.json").write_text('{"provider":"p","commit":"old","config_hash":"old"}\n', encoding="utf-8")
    current = {
        "provider": "p",
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
    (root / "providers" / "bad.yml").write_text("api_key: secret\n", encoding="utf-8")

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


def test_init_config_writes_flat_provider_files(tmp_path) -> None:
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

    provider = (tmp_path / "out" / "providers" / "modal.yml").read_text(encoding="utf-8")
    assert "initialized gpucall config" in result.stdout
    assert (tmp_path / "out" / "policy.yml").exists()
    assert (tmp_path / "out" / "recipes" / "vision-image-standard.yml").exists()
    assert "config:" not in provider
    assert "target:" in provider


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

    assert not (out / "providers" / "runpod-vllm-serverless.yml").exists()
    assert (out / "providers" / "runpod-vllm-serverless.yml.example").exists()
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


def test_doctor_supports_live_provider_catalog_flag_without_credentials(tmp_path) -> None:
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
            "--live-provider-catalog",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert '"live_provider_catalog"' in result.stdout
    assert '"ok": false' in result.stdout
