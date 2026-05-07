from __future__ import annotations

import os
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from gpucall.domain import EngineSpec, ModelSpec, ObjectStoreConfig, Policy, ProviderSpec, Recipe, TenantSpec
from gpucall.execution.registry import adapter_descriptor
from gpucall.routing import provider_route_rejection_reason

T = TypeVar("T", bound=BaseModel)


class ConfigError(RuntimeError):
    pass


class GpucallConfig(BaseModel):
    policy: Policy
    recipes: dict[str, Recipe]
    providers: dict[str, ProviderSpec]
    models: dict[str, ModelSpec] = {}
    engines: dict[str, EngineSpec] = {}
    object_store: ObjectStoreConfig | None = None
    tenants: dict[str, TenantSpec] = {}


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
    split_payloads = _load_split_provider_payloads(root)
    if split_payloads:
        for path, payload in split_payloads:
            provider = _provider_from_payload(path, payload)
            if provider.name in providers:
                raise ConfigError(f"duplicate provider name {provider.name!r} in {path}")
            providers[provider.name] = provider
    else:
        for path in sorted((root / "providers").glob("*.yml")):
            provider = _load_provider(path)
            if provider.name in providers:
                raise ConfigError(f"duplicate provider name {provider.name!r} in {path}")
            providers[provider.name] = provider
    if not providers:
        raise ConfigError(f"no providers found in {root / 'surfaces'} or {root / 'providers'}")
    return providers


def _load_split_provider_payloads(root: Path) -> list[tuple[Path, dict[str, object]]]:
    surfaces_root = root / "surfaces"
    workers_root = root / "workers"
    if not surfaces_root.exists() or not workers_root.exists():
        return []
    payloads: list[tuple[Path, dict[str, object]]] = []
    used_worker_keys: set[str] = set()
    workers: dict[str, tuple[Path, dict[str, object]]] = {}
    for worker_path in sorted(workers_root.glob("*.yml")):
        worker = _load_yaml(worker_path)
        if not isinstance(worker, dict):
            raise ConfigError(f"invalid worker YAML in {worker_path}: root must be a mapping")
        worker_key = _worker_binding_ref(worker, worker_path)
        workers[worker_key] = (worker_path, worker)
    for surface_path in sorted(surfaces_root.glob("*.yml")):
        surface = _load_yaml(surface_path)
        if not isinstance(surface, dict):
            raise ConfigError(f"invalid surface YAML in {surface_path}: root must be a mapping")
        surface_ref = _required_ref(surface, "surface_ref", surface_path)
        worker_key = _required_ref(surface, "worker_ref", surface_path)
        worker_entry = workers.get(worker_key)
        if worker_entry is None:
            raise ConfigError(f"surface {surface_ref!r} references missing worker {worker_key!r}")
        worker_path, worker = worker_entry
        # Surface and worker files are intentionally joined by worker_ref, not by
        # cloud provider. One execution surface can host different worker contracts.
        for field in ("account_ref", "adapter", "execution_surface"):
            surface_value = surface.get(field)
            worker_value = worker.get(field)
            if surface_value is not None and worker_value is not None and surface_value != worker_value:
                raise ConfigError(
                    f"surface {surface_path} and worker {worker_path} disagree on {field}: "
                    f"{surface_value!r} != {worker_value!r}"
                )
        payload: dict[str, object] = {**surface, **worker}
        payload["name"] = surface_ref
        used_worker_keys.add(worker_key)
        payload.pop("surface_ref", None)
        payload.pop("worker_ref", None)
        payload.pop("stock_state", None)
        payloads.append((surface_path, payload))
    orphan_workers = sorted(name for name in workers if name not in used_worker_keys)
    if orphan_workers:
        raise ConfigError(f"worker YAML has no matching surface YAML: {', '.join(orphan_workers)}")
    return payloads


def _worker_binding_ref(worker: dict[str, object], worker_path: Path) -> str:
    return _required_ref(worker, "worker_ref", worker_path)


def _required_ref(payload: dict[str, object], field: str, path: Path) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ConfigError(f"{path} must define {field}")
    return value


def _provider_from_payload(path: Path, payload: dict[str, object]) -> ProviderSpec:
    if not payload.get("execution_surface"):
        adapter = str(payload.get("adapter") or "echo")
        descriptor = adapter_descriptor(adapter)
        if descriptor is not None and descriptor.execution_surface is not None:
            payload = dict(payload)
            payload["execution_surface"] = descriptor.execution_surface.value
    try:
        return ProviderSpec.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(f"invalid {path}: {_validation_error_summary(exc)}") from exc


def _load_provider(path: Path) -> ProviderSpec:
    payload = _load_yaml(path)
    if not isinstance(payload, dict):
        raise ConfigError(f"invalid provider YAML in {path}: root must be a mapping")
    return _provider_from_payload(path, payload)

def load_models(config_dir: Path | None = None) -> dict[str, ModelSpec]:
    root = config_dir or default_config_dir()
    models: dict[str, ModelSpec] = {}
    for path in sorted((root / "models").glob("*.yml")):
        model = load_model(path, ModelSpec)
        if model.name in models:
            raise ConfigError(f"duplicate model name {model.name!r} in {path}")
        models[model.name] = model
    return models


def load_engines(config_dir: Path | None = None) -> dict[str, EngineSpec]:
    root = config_dir or default_config_dir()
    engines: dict[str, EngineSpec] = {}
    for path in sorted((root / "engines").glob("*.yml")):
        engine = load_model(path, EngineSpec)
        if engine.name in engines:
            raise ConfigError(f"duplicate engine name {engine.name!r} in {path}")
        engines[engine.name] = engine
    return engines


def load_config(config_dir: Path | None = None) -> GpucallConfig:
    root = config_dir or default_config_dir()
    config = GpucallConfig(
        policy=load_policy(root),
        recipes=load_recipes(root),
        providers=load_providers(root),
        models=load_models(root),
        engines=load_engines(root),
        object_store=load_object_store(root),
        tenants=load_tenants(root),
    )
    validate_config(config)
    return config


def load_object_store(config_dir: Path | None = None) -> ObjectStoreConfig | None:
    root = config_dir or default_config_dir()
    path = root / "object_store.yml"
    if not path.exists():
        return None
    return load_model(path, ObjectStoreConfig)


def load_tenants(config_dir: Path | None = None) -> dict[str, TenantSpec]:
    root = config_dir or default_config_dir()
    directory = root / "tenants"
    if not directory.exists():
        return {}
    tenants: dict[str, TenantSpec] = {}
    for path in sorted(directory.glob("*.yml")):
        tenant = load_model(path, TenantSpec)
        if tenant.name in tenants:
            raise ConfigError(f"duplicate tenant name {tenant.name!r} in {path}")
        tenants[tenant.name] = tenant
    return tenants


def validate_config(config: GpucallConfig) -> None:
    tuple_names = set(config.providers)
    allowed = set(config.policy.providers.allow)
    denied = set(config.policy.providers.deny)
    missing_policy = (allowed | denied) - tuple_names
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
                    model=config.models.get(provider.model_ref) if provider.model_ref else None,
                    engine=config.engines.get(provider.engine_ref) if provider.engine_ref else None,
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
        if provider.model_ref:
            model = config.models.get(provider.model_ref)
            if model is None:
                raise ConfigError(f"provider {provider.name!r} references unknown model {provider.model_ref!r}")
            if provider.model and provider.model != model.provider_model_id:
                raise ConfigError(
                    f"provider {provider.name!r} model {provider.model!r} does not match model catalog provider_model_id "
                    f"{model.provider_model_id!r}"
                )
            if provider.max_model_len > model.max_model_len:
                raise ConfigError(
                    f"provider {provider.name!r} max_model_len {provider.max_model_len} exceeds model catalog capability "
                    f"{model.max_model_len}"
                )
        if provider.engine_ref:
            engine = config.engines.get(provider.engine_ref)
            if engine is None:
                raise ConfigError(f"provider {provider.name!r} references unknown engine {provider.engine_ref!r}")
            if provider.model_ref:
                model = config.models[provider.model_ref]
                if model.supported_engines and provider.engine_ref not in model.supported_engines:
                    raise ConfigError(
                        f"provider {provider.name!r} engine {provider.engine_ref!r} is not supported by model {provider.model_ref!r}"
                    )
        if provider.declared_model_max_len is not None and provider.max_model_len > provider.declared_model_max_len:
            raise ConfigError(
                f"provider {provider.name!r} max_model_len {provider.max_model_len} exceeds declared model capability "
                f"{provider.declared_model_max_len}"
            )
        descriptor = adapter_descriptor(provider)
        if (
            descriptor is not None
            and descriptor.execution_surface is not None
            and provider.execution_surface is not None
            and provider.execution_surface != descriptor.execution_surface
        ):
            raise ConfigError(
                f"provider {provider.name!r} execution_surface {provider.execution_surface!r} does not match adapter "
                f"{provider.adapter!r} surface {descriptor.execution_surface!r}"
            )
        requires_contracts = descriptor.requires_contracts if descriptor is not None else True
        if requires_contracts:
            if not provider.endpoint_contract:
                raise ConfigError(f"provider {provider.name!r} must declare endpoint_contract")
            if not provider.input_contracts:
                raise ConfigError(f"provider {provider.name!r} must declare input_contracts")
            if not provider.output_contract:
                raise ConfigError(f"provider {provider.name!r} must declare output_contract")
        expected = descriptor.endpoint_contract if descriptor is not None else None
        if expected is not None and provider.endpoint_contract != expected:
            raise ConfigError(f"provider {provider.name!r} endpoint_contract must be {expected!r}")
        expected_output = descriptor.output_contract if descriptor is not None else None
        if expected_output is not None and provider.output_contract != expected_output:
            raise ConfigError(f"provider {provider.name!r} output_contract must be {expected_output!r}")
        expected_stream = descriptor.stream_contract if descriptor is not None else None
        if expected_stream is not None and provider.stream_contract != expected_stream:
            raise ConfigError(f"provider {provider.name!r} stream_contract must be {expected_stream!r}")
        if descriptor is not None and descriptor.config_validator is not None:
            findings = descriptor.config_validator(provider)
            if findings:
                raise ConfigError("; ".join(findings))
