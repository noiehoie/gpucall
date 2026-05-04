from __future__ import annotations

from pathlib import Path

from gpucall.credential_registry import register_configured_probe, register_env_override


register_env_override("runpod", "api_key", "GPUCALL_RUNPOD_API_KEY")
register_env_override("runpod", "endpoint_id", "GPUCALL_RUNPOD_ENDPOINT_ID")
register_env_override("hyperstack", "api_key", "GPUCALL_HYPERSTACK_API_KEY")
register_env_override("hyperstack", "ssh_key_path", "GPUCALL_HYPERSTACK_SSH_KEY_PATH")
register_env_override("aws", "access_key_id", "AWS_ACCESS_KEY_ID")
register_env_override("aws", "secret_access_key", "AWS_SECRET_ACCESS_KEY")
register_env_override("aws", "region", "AWS_REGION")
register_env_override("aws", "endpoint_url", "AWS_ENDPOINT_URL_S3")
register_env_override("auth", "api_keys", "GPUCALL_API_KEYS")


@register_configured_probe("modal")
def _modal_token_exists(_creds: dict[str, dict[str, str]]) -> bool:
    return any(path.exists() for path in (Path.home() / ".modal.toml", Path.home() / ".config" / "modal" / "modal.toml"))


@register_configured_probe("runpod-flash")
def _flash_token_exists(_creds: dict[str, dict[str, str]]) -> bool:
    return any(path.exists() for path in (Path.home() / ".flash" / "config.json", Path.home() / ".runpod" / "config.toml"))


@register_configured_probe("runpod-serverless")
def _runpod_serverless_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("runpod"))


@register_configured_probe("hyperstack")
def _hyperstack_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("hyperstack"))


@register_configured_probe("cloudflare-r2")
def _cloudflare_r2_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("aws"))


@register_configured_probe("auth")
def _auth_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("auth"))
