from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from gpucall.config import default_state_dir


PROVIDER_REGISTRY_SCHEMA_VERSION = 1
_SECRET_KEY_PARTS = ("api_key", "token", "secret", "password", "authorization")


def provider_registry_path() -> Path:
    return default_state_dir() / "setup" / "provider-registry.json"


def load_provider_registry(path: Path | None = None) -> dict[str, Any]:
    target = path or provider_registry_path()
    if not target.exists():
        return {"schema_version": PROVIDER_REGISTRY_SCHEMA_VERSION, "providers": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": PROVIDER_REGISTRY_SCHEMA_VERSION, "providers": {}}
    if not isinstance(payload, dict):
        return {"schema_version": PROVIDER_REGISTRY_SCHEMA_VERSION, "providers": {}}
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    return {"schema_version": PROVIDER_REGISTRY_SCHEMA_VERSION, **payload, "providers": providers}


def save_provider_metadata(
    provider: str,
    metadata: Mapping[str, Any],
    *,
    state: str = "provider-configured",
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    target = path or provider_registry_path()
    current = load_provider_registry(target)
    providers = dict(current.get("providers") or {})
    clean = _non_secret_metadata(metadata)
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    previous = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
    providers[provider] = {
        **previous,
        "provider": provider,
        "state": state,
        "metadata": {**dict(previous.get("metadata") or {}), **clean},
        "updated_at": timestamp,
    }
    payload = {
        "schema_version": PROVIDER_REGISTRY_SCHEMA_VERSION,
        "updated_at": timestamp,
        "providers": providers,
    }
    _write_json(target, payload)
    return payload


def provider_registry_snapshot_hash(path: Path | None = None) -> str:
    payload = load_provider_registry(path)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def provider_registry_configured_contracts(path: Path | None = None) -> set[str]:
    payload = load_provider_registry(path)
    providers = payload.get("providers") if isinstance(payload, dict) else {}
    if not isinstance(providers, dict):
        return set()
    configured: set[str] = set()
    hyperstack = providers.get("hyperstack") if isinstance(providers.get("hyperstack"), dict) else {}
    hyperstack_metadata = hyperstack.get("metadata") if isinstance(hyperstack, dict) else {}
    if isinstance(hyperstack_metadata, dict) and hyperstack_metadata.get("ssh_key_path"):
        configured.add("ssh_key:hyperstack")
    return configured


def _non_secret_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        normalized = str(key).strip()
        if not normalized or _looks_secret_key(normalized):
            continue
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[normalized] = value
        elif isinstance(value, (list, tuple)):
            clean[normalized] = [str(item) for item in value if item is not None]
        else:
            clean[normalized] = str(value)
    return clean


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
