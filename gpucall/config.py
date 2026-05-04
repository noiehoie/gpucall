from __future__ import annotations

import os
import ipaddress
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from gpucall.domain import ObjectStoreConfig, Policy, ProviderSpec, Recipe
from gpucall.routing import provider_route_rejection_reason

T = TypeVar("T", bound=BaseModel)


class ConfigError(RuntimeError):
    pass


class GpucallConfig(BaseModel):
    policy: Policy
    recipes: dict[str, Recipe]
    providers: dict[str, ProviderSpec]
    object_store: ObjectStoreConfig | None = None


def default_config_dir() -> Path:
    explicit = os.getenv("GPUCALL_CONFIG_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "gpucall"
    return Path.home() / ".config" / "gpucall"


def default_state_dir() -> Path:
    explicit = os.getenv("GPUCALL_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.getenv("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "gpucall"
    return Path.home() / ".local" / "state" / "gpucall"


def _load_yaml(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def load_model(path: Path, model: type[T]) -> T:
    try:
        return model.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"invalid {path}: {_validation_error_summary(exc)}") from exc


def _validation_error_summary(exc: ValidationError) -> str:
    entries: list[str] = []
    for error in exc.errors(include_input=False):
        loc = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        kind = str(error.get("type", "validation_error"))
        message = str(error.get("msg", "invalid value"))
        entries.append(f"{loc}: {kind}: {message}")
    if not entries:
        return "validation failed"
    return "; ".join(entries)


def load_policy(config_dir: Path | None = None) -> Policy:
    root = config_dir or default_config_dir()
    return load_model(root / "policy.yml", Policy)


def load_recipes(config_dir: Path | None = None) -> dict[str, Recipe]:
    root = config_dir or default_config_dir()
    recipes: dict[str, Recipe] = {}
    for path in sorted((root / "recipes").glob("*.yml")):
        recipe = load_model(path, Recipe)
        if recipe.name in recipes:
            raise ConfigError(f"duplicate recipe name {recipe.name!r} in {path}")
        recipes[recipe.name] = recipe
    if not recipes:
        raise ConfigError(f"no recipes found in {root / 'recipes'}")
    return recipes


def load_providers(config_dir: Path | None = None) -> dict[str, ProviderSpec]:
    root = config_dir or default_config_dir()
    providers: dict[str, ProviderSpec] = {}
    for path in sorted((root / "providers").glob("*.yml")):
        provider = load_model(path, ProviderSpec)
        if provider.name in providers:
            raise ConfigError(f"duplicate provider name {provider.name!r} in {path}")
        providers[provider.name] = provider
    if not providers:
        raise ConfigError(f"no providers found in {root / 'providers'}")
    return providers


def load_config(config_dir: Path | None = None) -> GpucallConfig:
    root = config_dir or default_config_dir()
    config = GpucallConfig(
        policy=load_policy(root),
        recipes=load_recipes(root),
        providers=load_providers(root),
        object_store=load_object_store(root),
    )
    validate_config(config)
    return config


def load_object_store(config_dir: Path | None = None) -> ObjectStoreConfig | None:
    root = config_dir or default_config_dir()
    path = root / "object_store.yml"
    if not path.exists():
        return None
    return load_model(path, ObjectStoreConfig)


def validate_config(config: GpucallConfig) -> None:
    provider_names = set(config.providers)
    allowed = set(config.policy.providers.allow)
    denied = set(config.policy.providers.deny)
    missing_policy = (allowed | denied) - provider_names
    if missing_policy:
        raise ConfigError(f"policy references unknown providers: {', '.join(sorted(missing_policy))}")
    overlap = allowed & denied
    if overlap:
        raise ConfigError(f"providers cannot be both allowed and denied: {', '.join(sorted(overlap))}")

    for recipe in config.recipes.values():
        if not recipe.allowed_modes:
            raise ConfigError(f"recipe {recipe.name!r} must define at least one allowed mode")
        if not config.policy.providers.max_data_classification.permits(recipe.data_classification):
            raise ConfigError(
                f"recipe {recipe.name!r} data_classification {recipe.data_classification} exceeds policy ceiling "
                f"{config.policy.providers.max_data_classification}"
            )
        if recipe.auto_select:
            has_capable_provider = False
            for provider in config.providers.values():
                reason = provider_route_rejection_reason(
                    policy=config.policy,
                    recipe=recipe,
                    provider=provider,
                    required_len=recipe.max_model_len,
                    auto_selected=True,
                )
                if reason is not None:
                    continue
                has_capable_provider = True
                break
            if not has_capable_provider:
                raise ConfigError(f"recipe {recipe.name!r} has no provider satisfying its declared requirements")
    for provider in config.providers.values():
        if provider.declared_model_max_len is not None and provider.max_model_len > provider.declared_model_max_len:
            raise ConfigError(
                f"provider {provider.name!r} max_model_len {provider.max_model_len} exceeds declared model capability "
                f"{provider.declared_model_max_len}"
            )
        adapter = provider.adapter.lower()
        if adapter != "echo":
            if not provider.endpoint_contract:
                raise ConfigError(f"provider {provider.name!r} must declare endpoint_contract")
            if not provider.input_contracts:
                raise ConfigError(f"provider {provider.name!r} must declare input_contracts")
            if not provider.output_contract:
                raise ConfigError(f"provider {provider.name!r} must declare output_contract")
        expected_contracts = {
            "modal": "modal-function",
            "hyperstack": "hyperstack-vm",
            "local-ollama": "ollama-generate",
            "ollama": "ollama-generate",
            "runpod-serverless": "runpod-serverless",
            "runpod-vllm-serverless": "openai-chat-completions",
            "runpod-vllm-flashboot": "openai-chat-completions",
            "runpod-flash": "openai-chat-completions",
        }
        expected = expected_contracts.get(adapter)
        if expected and provider.endpoint_contract != expected:
            raise ConfigError(f"provider {provider.name!r} endpoint_contract must be {expected!r}")
        expected_outputs = {
            "modal": "plain-text",
            "hyperstack": "plain-text",
            "local-ollama": "ollama-generate",
            "ollama": "ollama-generate",
            "runpod-serverless": "gpucall-provider-result",
            "runpod-vllm-serverless": "openai-chat-completions",
            "runpod-vllm-flashboot": "gpucall-provider-result",
            "runpod-flash": "openai-chat-completions",
        }
        expected_output = expected_outputs.get(adapter)
        if expected_output and provider.output_contract != expected_output:
            raise ConfigError(f"provider {provider.name!r} output_contract must be {expected_output!r}")
        if adapter == "hyperstack":
            _validate_hyperstack_ssh_cidr(provider)


def _validate_hyperstack_ssh_cidr(provider: ProviderSpec) -> None:
    if not provider.ssh_remote_cidr:
        raise ConfigError(f"provider {provider.name!r} must declare ssh_remote_cidr")
    try:
        network = ipaddress.ip_network(provider.ssh_remote_cidr, strict=False)
    except ValueError as exc:
        raise ConfigError(f"provider {provider.name!r} ssh_remote_cidr is invalid") from exc
    if network.prefixlen == 0:
        raise ConfigError(f"provider {provider.name!r} ssh_remote_cidr must not allow all addresses")
