from __future__ import annotations

from pathlib import Path

from gpucall.credential_registry import register_configured_probe, register_env_override


register_env_override("runpod", "api_key", "GPUCALL_RUNPOD_API_KEY")
register_env_override("runpod", "endpoint_id", "GPUCALL_RUNPOD_ENDPOINT_ID")
register_env_override("hyperstack", "api_key", "GPUCALL_HYPERSTACK_API_KEY")
register_env_override("hyperstack", "ssh_key_path", "GPUCALL_HYPERSTACK_SSH_KEY_PATH")
register_env_override("azure", "subscription_id", "AZURE_SUBSCRIPTION_ID")
register_env_override("gcp", "project_id", "GOOGLE_CLOUD_PROJECT")
register_env_override("scaleway", "secret_key", "SCW_SECRET_KEY")
register_env_override("scaleway", "project_id", "SCW_PROJECT_ID")
register_env_override("ovhcloud", "endpoint", "OVH_ENDPOINT")
register_env_override("ovhcloud", "service_name", "OVH_CLOUD_PROJECT_SERVICE_NAME")
register_env_override("aws", "access_key_id", "AWS_ACCESS_KEY_ID")
register_env_override("aws", "secret_access_key", "AWS_SECRET_ACCESS_KEY")
register_env_override("aws", "region", "AWS_REGION")
register_env_override("aws", "endpoint_url", "AWS_ENDPOINT_URL_S3")
register_env_override("auth", "api_keys", "GPUCALL_API_KEYS")


@register_configured_probe("sdk_profile:modal")
def _modal_token_exists(_creds: dict[str, dict[str, str]]) -> bool:
    return any(path.exists() for path in (Path.home() / ".modal.toml", Path.home() / ".config" / "modal" / "modal.toml"))


@register_configured_probe("sdk_profile:runpod-flash")
def _flash_token_exists(_creds: dict[str, dict[str, str]]) -> bool:
    return any(path.exists() for path in (Path.home() / ".flash" / "config.json", Path.home() / ".runpod" / "config.toml"))


@register_configured_probe("api_key:runpod")
def _runpod_api_key_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("runpod", {}).get("api_key"))


@register_configured_probe("endpoint_ref:runpod")
def _runpod_endpoint_ref_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("runpod", {}).get("endpoint_id"))


@register_configured_probe("api_key:hyperstack")
def _hyperstack_api_key_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("hyperstack", {}).get("api_key"))


@register_configured_probe("ssh_key:hyperstack")
def _hyperstack_ssh_key_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("hyperstack", {}).get("ssh_key_path"))


@register_configured_probe("cloud_subscription:azure")
def _azure_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("azure"))


@register_configured_probe("cloud_project:gcp")
def _gcp_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("gcp"))


@register_configured_probe("api_key:scaleway")
def _scaleway_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("scaleway"))


@register_configured_probe("cloud_project:ovhcloud")
def _ovhcloud_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("ovhcloud"))


@register_configured_probe("object_store:s3")
def _cloudflare_r2_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("aws"))


@register_configured_probe("gateway_auth:api_keys")
def _auth_configured(creds: dict[str, dict[str, str]]) -> bool:
    return bool(creds.get("auth"))
