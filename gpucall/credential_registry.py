from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gpucall.plugin_loader import load_entry_point_group


@dataclass(frozen=True)
class CredentialEnvOverride:
    provider: str
    key: str
    env: str


@dataclass(frozen=True)
class ConfiguredCredentialProbe:
    contract: str
    is_configured: Callable[[dict[str, dict[str, str]]], bool]


_ENV_OVERRIDES: list[CredentialEnvOverride] = []
_CONFIGURED_PROBES: list[ConfiguredCredentialProbe] = []


def register_env_override(provider: str, key: str, env: str) -> None:
    override = CredentialEnvOverride(provider=provider, key=key, env=env)
    if override not in _ENV_OVERRIDES:
        _ENV_OVERRIDES.append(override)


def env_overrides() -> list[CredentialEnvOverride]:
    load_entry_point_group("gpucall.credential_sources")
    return list(_ENV_OVERRIDES)


def register_configured_probe(contract: str) -> Callable[[Callable[[dict[str, dict[str, str]]], bool]], Callable[[dict[str, dict[str, str]]], bool]]:
    def decorator(func: Callable[[dict[str, dict[str, str]]], bool]) -> Callable[[dict[str, dict[str, str]]], bool]:
        if not any(probe.contract == contract for probe in _CONFIGURED_PROBES):
            _CONFIGURED_PROBES.append(ConfiguredCredentialProbe(contract=contract, is_configured=func))
        return func

    return decorator


def configured_probes() -> list[ConfiguredCredentialProbe]:
    load_entry_point_group("gpucall.credential_sources")
    return list(_CONFIGURED_PROBES)
