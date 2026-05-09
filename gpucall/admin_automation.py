from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from gpucall.config import load_admin_automation
from gpucall.domain import ApiKeyHandoffMode, RecipeAdminAutomationConfig


def admin_automation_summary(config_dir: Path) -> dict[str, object]:
    config = load_admin_automation(config_dir)
    return {
        "admin_yml": str(config_dir / "admin.yml"),
        "recipe_inbox_auto_materialize": config.recipe_inbox_auto_materialize,
        "api_key_handoff_mode": config.api_key_handoff_mode.value,
        "trusted_bootstrap": {
            "enabled": config.api_key_handoff_mode is ApiKeyHandoffMode.TRUSTED_BOOTSTRAP,
            "allowed_cidrs": list(config.api_key_bootstrap_allowed_cidrs),
            "allowed_hosts": list(config.api_key_bootstrap_allowed_hosts),
            "gateway_url": config.api_key_bootstrap_gateway_url,
            "recipe_inbox": config.api_key_bootstrap_recipe_inbox,
        },
        "handoff_file_enabled": config.api_key_handoff_mode is ApiKeyHandoffMode.HANDOFF_FILE,
    }


def configure_admin_automation(
    config_dir: Path,
    *,
    handoff_mode: ApiKeyHandoffMode | None = None,
    recipe_inbox_auto_materialize: bool | None = None,
    bootstrap_allowed_cidrs: Iterable[str] | None = None,
    bootstrap_allowed_hosts: Iterable[str] | None = None,
    bootstrap_gateway_url: str | None = None,
    bootstrap_recipe_inbox: str | None = None,
    clear_bootstrap_allowlist: bool = False,
) -> RecipeAdminAutomationConfig:
    current = load_admin_automation(config_dir)
    cidrs = tuple(_clean_items(current.api_key_bootstrap_allowed_cidrs))
    hosts = tuple(_clean_items(current.api_key_bootstrap_allowed_hosts))
    if clear_bootstrap_allowlist:
        cidrs = ()
        hosts = ()
    if bootstrap_allowed_cidrs is not None:
        cidrs = tuple(_clean_items(bootstrap_allowed_cidrs))
    if bootstrap_allowed_hosts is not None:
        hosts = tuple(_clean_items(bootstrap_allowed_hosts))
    updated = RecipeAdminAutomationConfig(
        recipe_inbox_auto_materialize=(
            current.recipe_inbox_auto_materialize if recipe_inbox_auto_materialize is None else recipe_inbox_auto_materialize
        ),
        api_key_handoff_mode=handoff_mode or current.api_key_handoff_mode,
        api_key_bootstrap_allowed_cidrs=cidrs,
        api_key_bootstrap_allowed_hosts=hosts,
        api_key_bootstrap_gateway_url=_clean_optional(bootstrap_gateway_url, current.api_key_bootstrap_gateway_url),
        api_key_bootstrap_recipe_inbox=_clean_optional(bootstrap_recipe_inbox, current.api_key_bootstrap_recipe_inbox),
    )
    if updated.api_key_handoff_mode is ApiKeyHandoffMode.TRUSTED_BOOTSTRAP and not (
        updated.api_key_bootstrap_allowed_cidrs or updated.api_key_bootstrap_allowed_hosts
    ):
        raise ValueError("trusted_bootstrap requires at least one allowed CIDR or host")
    write_admin_automation(config_dir, updated)
    return updated


def write_admin_automation(config_dir: Path, config: RecipeAdminAutomationConfig) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "admin.yml"
    payload = config.model_dump(mode="json")
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _clean_items(items: Iterable[str]) -> list[str]:
    values: list[str] = []
    for item in items:
        value = str(item).strip()
        if value:
            values.append(value)
    return values


def _clean_optional(value: str | None, fallback: str | None) -> str | None:
    if value is None:
        return fallback
    cleaned = value.strip()
    return cleaned or None
