from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from gpucall.credential_registry import configured_probes, env_overrides
import gpucall.credential_targets  # noqa: F401 - registers credential sources


def credentials_path() -> Path:
    explicit = os.getenv("GPUCALL_CREDENTIALS")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(xdg).expanduser() / "gpucall" / "credentials.yml"


def load_credentials() -> dict[str, dict[str, str]]:
    path = credentials_path()
    providers: dict[str, dict[str, str]] = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            raw = data.get("providers", {})
            if isinstance(raw, dict):
                providers = {
                    str(name): {str(key): str(value) for key, value in values.items() if value is not None}
                    for name, values in raw.items()
                    if isinstance(values, dict)
                }
        except Exception as exc:
            print(f"Warning: failed to load credentials file ({type(exc).__name__})")

    for override in env_overrides():
        _env_override(providers, override.provider, override.key, override.env)
    return providers


def save_credentials(provider: str, values: dict[str, str]) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"version": 1, "providers": {}}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass
    data.setdefault("providers", {})
    data["providers"][provider] = {key: value for key, value in values.items() if value}
    payload = yaml.safe_dump(data, default_flow_style=False, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    os.chmod(path, 0o600)


def configured_credentials() -> list[str]:
    creds = load_credentials()
    return [probe.contract for probe in configured_probes() if probe.is_configured(creds)]


def _env_override(providers: dict[str, dict[str, str]], provider: str, key: str, env: str) -> None:
    value = os.getenv(env)
    if value:
        providers.setdefault(provider, {})[key] = value
