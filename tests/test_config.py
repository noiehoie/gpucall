from __future__ import annotations

import shutil
import subprocess
import sys
import os
from pathlib import Path

import pytest

from gpucall.config import ConfigError, load_config
from gpucall.domain import SecurityTier


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
    assert config.recipes["text-infer-standard"].max_model_len == 32768
    assert config.recipes["text-infer-standard"].max_input_bytes == 16777216
    assert config.recipes["vision-image-standard"].task == "vision"
    assert config.recipes["vision-image-standard"].auto_select is False
    assert config.recipes["vision-image-standard"].allowed_mime_prefixes == ["image/"]
    assert config.providers["hyperstack-a100"].max_model_len == 32768
    assert config.providers["hyperstack-a100"].declared_model_max_len == 32768
    assert config.providers["hyperstack-a100"].trust_profile.dedicated_gpu is True
    assert config.providers["modal-a10g"].trust_profile.security_tier is SecurityTier.ENCRYPTED_CAPSULE
    assert config.providers["modal-a10g"].supports_vision is False
    assert "image" not in config.providers["modal-a10g"].input_contracts


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
