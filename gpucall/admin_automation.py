from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import yaml

from gpucall.config import default_state_dir, load_admin_automation
from gpucall.domain import ApiKeyHandoffMode, RecipeAdminAutomationConfig
from gpucall.recipe_admin import process_inbox


ADMIN_SYNTHETIC_DRY_RUN_TTL_SECONDS = 3600


def admin_automation_summary(config_dir: Path) -> dict[str, object]:
    config = load_admin_automation(config_dir)
    synthetic = load_admin_automation_synthetic_dry_run()
    return {
        "admin_yml": str(config_dir / "admin.yml"),
        "recipe_inbox_auto_materialize": config.recipe_inbox_auto_materialize,
        "recipe_inbox_auto_validate_existing_tuples": config.recipe_inbox_auto_validate_existing_tuples,
        "recipe_inbox_auto_activate_existing_validated_recipe": config.recipe_inbox_auto_activate_existing_validated_recipe,
        "recipe_inbox_auto_promote_candidates": config.recipe_inbox_auto_promote_candidates,
        "recipe_inbox_auto_provision_supply": config.recipe_inbox_auto_provision_supply,
        "recipe_inbox_auto_apply_supply": config.recipe_inbox_auto_apply_supply,
        "recipe_inbox_auto_billable_validation": config.recipe_inbox_auto_billable_validation,
        "recipe_inbox_auto_validation_budget_usd": config.recipe_inbox_auto_validation_budget_usd,
        "recipe_inbox_auto_activate_validated": config.recipe_inbox_auto_activate_validated,
        "recipe_inbox_auto_require_auto_select_safe": config.recipe_inbox_auto_require_auto_select_safe,
        "recipe_inbox_auto_set_auto_select": config.recipe_inbox_auto_set_auto_select,
        "recipe_inbox_auto_run_validate_config": config.recipe_inbox_auto_run_validate_config,
        "recipe_inbox_auto_run_launch_check": config.recipe_inbox_auto_run_launch_check,
        "recipe_inbox_promotion_work_dir": config.recipe_inbox_promotion_work_dir,
        "api_key_handoff_mode": config.api_key_handoff_mode.value,
        "trusted_bootstrap": {
            "enabled": config.api_key_handoff_mode is ApiKeyHandoffMode.TRUSTED_BOOTSTRAP,
            "allowed_cidrs": list(config.api_key_bootstrap_allowed_cidrs),
            "allowed_hosts": list(config.api_key_bootstrap_allowed_hosts),
            "gateway_url": config.api_key_bootstrap_gateway_url,
            "recipe_inbox": config.api_key_bootstrap_recipe_inbox,
        },
        "handoff_assets": {
            "onboarding_prompt_url": config.onboarding_prompt_url,
            "onboarding_manual_url": config.onboarding_manual_url,
            "caller_sdk_wheel_url": config.caller_sdk_wheel_url,
        },
        "handoff_file_enabled": config.api_key_handoff_mode is ApiKeyHandoffMode.HANDOFF_FILE,
        "synthetic_dry_run": synthetic,
    }


def load_admin_automation_synthetic_dry_run(*, now: datetime | None = None) -> dict[str, object]:
    path = admin_automation_synthetic_dry_run_path()
    current = now or datetime.now(timezone.utc)
    if not path.exists():
        return {
            "status": "missing",
            "path": str(path),
            "fresh": False,
            "reason": "synthetic dry-run evidence missing",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "path": str(path),
            "fresh": False,
            "reason": f"synthetic dry-run evidence unreadable: {type(exc).__name__}",
        }
    expires_raw = payload.get("expires_at")
    expires_at = _parse_datetime(str(expires_raw or ""))
    fresh = expires_at is not None and expires_at > current
    return {
        **payload,
        "path": str(path),
        "fresh": fresh,
        "status": payload.get("status") if fresh else "stale",
        "reason": payload.get("reason") if fresh else "synthetic dry-run evidence stale",
    }


def run_admin_automation_synthetic_dry_run(
    recipe_inbox: str | None,
    *,
    config_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    current = now or datetime.now(timezone.utc)
    path = admin_automation_synthetic_dry_run_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, object] = {
        "schema_version": 1,
        "phase": "admin-automation-synthetic-dry-run",
        "status": "failed",
        "fresh": False,
        "created_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=ADMIN_SYNTHETIC_DRY_RUN_TTL_SECONDS)).isoformat(),
        "ttl_seconds": ADMIN_SYNTHETIC_DRY_RUN_TTL_SECONDS,
        "synthetic_intake_id": "synthetic-oob-intake",
        "admin_run_id": "admin-run-" + current.strftime("%Y%m%dT%H%M%SZ"),
        "provider_mutation_performed": False,
        "generation_performed": False,
        "billable_validation_performed": False,
        "raw_payload_written": False,
        "next_command": "gpucall setup section recipe-inbox",
    }
    inbox_path = _local_path_from_inbox_spec(recipe_inbox)
    if inbox_path is None:
        evidence.update(
            {
                "status": "pending",
                "reason": "recipe inbox is missing or remote; local synthetic inbox dry-run not possible",
                "recipe_inbox": _redact_inbox(recipe_inbox),
            }
        )
        _write_json(path, evidence)
        return evidence
    try:
        synthetic_dir = inbox_path / ".gpucall-synthetic-dry-run"
        synthetic_inbox = synthetic_dir / "inbox"
        synthetic_output = synthetic_dir / "recipes"
        synthetic_processed = synthetic_dir / "processed"
        synthetic_failed = synthetic_dir / "failed"
        synthetic_reports = synthetic_dir / "reports"
        synthetic_dir.mkdir(parents=True, exist_ok=True)
        synthetic_dir.chmod(0o700)
        synthetic_inbox.mkdir(parents=True, exist_ok=True)
        synthetic_inbox.chmod(0o700)
        intake_path = synthetic_inbox / "synthetic-oob-intake.json"
        payload = {
            "phase": "deterministic-intake",
            "source": "gpucall-oob-synthetic",
            "sanitized_request": {
                "task": "infer",
                "mode": "sync",
                "intent": "summarize_text",
                "classification": "internal",
                "expected_output": "plain_text",
                "desired_capabilities": ["instruction_following", "summarization"],
                "error": {"context": {"context_budget_tokens": 8192}},
            },
            "redaction_report": {
                "prompt_body_forwarded": False,
                "data_ref_uri_forwarded": False,
                "presigned_url_forwarded": False,
                "raw_payload_forwarded": False,
            },
        }
        _write_json(intake_path, payload)
        loaded = json.loads(intake_path.read_text(encoding="utf-8"))
        redaction = loaded.get("redaction_report") if isinstance(loaded, dict) else {}
        if not isinstance(redaction, dict) or any(redaction.get(key) is not False for key in ("prompt_body_forwarded", "data_ref_uri_forwarded", "presigned_url_forwarded")):
            raise ValueError("synthetic payload redaction failed")
        processed = process_inbox(
            inbox_dir=synthetic_inbox,
            output_dir=synthetic_output,
            processed_dir=synthetic_processed,
            failed_dir=synthetic_failed,
            report_dir=synthetic_reports,
            config_dir=config_dir,
            accept_all=True,
            automation_override=RecipeAdminAutomationConfig(recipe_inbox_auto_materialize=True),
        )
        ok = bool(processed and processed[0].get("ok") is True)
        if not ok:
            reason = str(processed[0].get("error") if processed else "no synthetic inbox result")
            evidence.update(
                {
                    "status": "failed",
                    "reason": f"synthetic intake was not materialized: {reason}",
                    "recipe_inbox": _redact_inbox(str(inbox_path)),
                    "synthetic_dir": _redact_path(str(synthetic_dir)),
                    "classification": "failed",
                }
            )
            _write_json(path, evidence)
            return evidence
        report_path = str(processed[0].get("report") or "")
        recipe_path = str(processed[0].get("recipe") or processed[0].get("recipe_path") or "")
        evidence.update(
            {
                "status": "processed",
                "fresh": True,
                "reason": "synthetic intake parsed by admin automation and materialized in isolated dry-run workspace",
                "recipe_inbox": _redact_inbox(str(inbox_path)),
                "synthetic_dir": _redact_path(str(synthetic_dir)),
                "classification": "processed",
                "materialization_mode": "isolated_dry_run",
                "recipe_candidate": Path(recipe_path).stem if recipe_path else "infer-summarize-text-draft",
                "admin_report_path": _redact_inbox(report_path),
                "next_command": "gpucall setup status",
            }
        )
    except Exception as exc:
        evidence.update(
            {
                "status": "failed",
                "reason": f"synthetic dry-run failed: {type(exc).__name__}: {exc}",
                "recipe_inbox": _redact_inbox(str(inbox_path)),
            }
        )
    _write_json(path, evidence)
    return evidence


def admin_automation_synthetic_dry_run_path() -> Path:
    return default_state_dir() / "setup" / "admin-automation-synthetic-dry-run.json"


def configure_admin_automation(
    config_dir: Path,
    *,
    handoff_mode: ApiKeyHandoffMode | None = None,
    recipe_inbox_auto_materialize: bool | None = None,
    recipe_inbox_auto_validate_existing_tuples: bool | None = None,
    recipe_inbox_auto_activate_existing_validated_recipe: bool | None = None,
    recipe_inbox_auto_promote_candidates: bool | None = None,
    recipe_inbox_auto_provision_supply: bool | None = None,
    recipe_inbox_auto_apply_supply: bool | None = None,
    recipe_inbox_auto_billable_validation: bool | None = None,
    recipe_inbox_auto_validation_budget_usd: float | None = None,
    recipe_inbox_auto_activate_validated: bool | None = None,
    recipe_inbox_auto_require_auto_select_safe: bool | None = None,
    recipe_inbox_auto_set_auto_select: bool | None = None,
    recipe_inbox_auto_run_validate_config: bool | None = None,
    recipe_inbox_auto_run_launch_check: bool | None = None,
    recipe_inbox_promotion_work_dir: str | None = None,
    bootstrap_allowed_cidrs: Iterable[str] | None = None,
    bootstrap_allowed_hosts: Iterable[str] | None = None,
    bootstrap_gateway_url: str | None = None,
    bootstrap_recipe_inbox: str | None = None,
    onboarding_prompt_url: str | None = None,
    onboarding_manual_url: str | None = None,
    caller_sdk_wheel_url: str | None = None,
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
    auto_materialize = current.recipe_inbox_auto_materialize if recipe_inbox_auto_materialize is None else recipe_inbox_auto_materialize
    auto_validate_existing = (
        current.recipe_inbox_auto_validate_existing_tuples
        if recipe_inbox_auto_validate_existing_tuples is None
        else recipe_inbox_auto_validate_existing_tuples
    )
    auto_activate_existing = (
        current.recipe_inbox_auto_activate_existing_validated_recipe
        if recipe_inbox_auto_activate_existing_validated_recipe is None
        else recipe_inbox_auto_activate_existing_validated_recipe
    )
    auto_promote = (
        current.recipe_inbox_auto_promote_candidates
        if recipe_inbox_auto_promote_candidates is None
        else recipe_inbox_auto_promote_candidates
    )
    auto_provision_supply = (
        current.recipe_inbox_auto_provision_supply
        if recipe_inbox_auto_provision_supply is None
        else recipe_inbox_auto_provision_supply
    )
    auto_apply_supply = (
        current.recipe_inbox_auto_apply_supply
        if recipe_inbox_auto_apply_supply is None
        else recipe_inbox_auto_apply_supply
    )
    auto_billable_validation = (
        current.recipe_inbox_auto_billable_validation
        if recipe_inbox_auto_billable_validation is None
        else recipe_inbox_auto_billable_validation
    )
    validation_budget = (
        current.recipe_inbox_auto_validation_budget_usd
        if recipe_inbox_auto_validation_budget_usd is None
        else recipe_inbox_auto_validation_budget_usd
    )
    auto_activate = (
        current.recipe_inbox_auto_activate_validated
        if recipe_inbox_auto_activate_validated is None
        else recipe_inbox_auto_activate_validated
    )
    require_auto_select_safe = (
        current.recipe_inbox_auto_require_auto_select_safe
        if recipe_inbox_auto_require_auto_select_safe is None
        else recipe_inbox_auto_require_auto_select_safe
    )
    auto_set_auto_select = (
        current.recipe_inbox_auto_set_auto_select
        if recipe_inbox_auto_set_auto_select is None
        else recipe_inbox_auto_set_auto_select
    )
    auto_run_validate_config = (
        current.recipe_inbox_auto_run_validate_config
        if recipe_inbox_auto_run_validate_config is None
        else recipe_inbox_auto_run_validate_config
    )
    auto_run_launch_check = (
        current.recipe_inbox_auto_run_launch_check
        if recipe_inbox_auto_run_launch_check is None
        else recipe_inbox_auto_run_launch_check
    )
    if auto_promote and not auto_materialize:
        raise ValueError("recipe auto-promotion requires recipe auto-materialize")
    if auto_provision_supply and not auto_promote:
        raise ValueError("recipe supply provisioning requires recipe auto-promotion")
    if auto_apply_supply and not auto_provision_supply:
        raise ValueError("recipe supply apply requires recipe supply provisioning")
    if auto_validate_existing and not auto_materialize:
        raise ValueError("recipe existing tuple validation requires recipe auto-materialize")
    if auto_activate_existing and not auto_validate_existing:
        raise ValueError("recipe existing tuple activation requires existing tuple validation")
    if auto_billable_validation and not (auto_promote or auto_validate_existing):
        raise ValueError("recipe auto billable validation requires recipe auto-promotion or existing tuple validation")
    if auto_activate and not auto_billable_validation:
        raise ValueError("recipe auto-activation requires recipe auto billable validation")
    if auto_set_auto_select and not (auto_activate_existing or auto_activate):
        raise ValueError("recipe auto-select promotion requires an auto-activation path")
    if auto_run_launch_check and not auto_run_validate_config:
        raise ValueError("recipe launch-check automation requires validate-config automation")
    updated = RecipeAdminAutomationConfig(
        recipe_inbox_auto_materialize=auto_materialize,
        recipe_inbox_auto_validate_existing_tuples=auto_validate_existing,
        recipe_inbox_auto_activate_existing_validated_recipe=auto_activate_existing,
        recipe_inbox_auto_promote_candidates=auto_promote,
        recipe_inbox_auto_provision_supply=auto_provision_supply,
        recipe_inbox_auto_apply_supply=auto_apply_supply,
        recipe_inbox_auto_billable_validation=auto_billable_validation,
        recipe_inbox_auto_validation_budget_usd=validation_budget,
        recipe_inbox_auto_activate_validated=auto_activate,
        recipe_inbox_auto_require_auto_select_safe=require_auto_select_safe,
        recipe_inbox_auto_set_auto_select=auto_set_auto_select,
        recipe_inbox_auto_run_validate_config=auto_run_validate_config,
        recipe_inbox_auto_run_launch_check=auto_run_launch_check,
        recipe_inbox_promotion_work_dir=_clean_optional(recipe_inbox_promotion_work_dir, current.recipe_inbox_promotion_work_dir),
        api_key_handoff_mode=handoff_mode or current.api_key_handoff_mode,
        api_key_bootstrap_allowed_cidrs=cidrs,
        api_key_bootstrap_allowed_hosts=hosts,
        api_key_bootstrap_gateway_url=_clean_optional(bootstrap_gateway_url, current.api_key_bootstrap_gateway_url),
        api_key_bootstrap_recipe_inbox=_clean_optional(bootstrap_recipe_inbox, current.api_key_bootstrap_recipe_inbox),
        onboarding_prompt_url=_clean_optional(onboarding_prompt_url, current.onboarding_prompt_url),
        onboarding_manual_url=_clean_optional(onboarding_manual_url, current.onboarding_manual_url),
        caller_sdk_wheel_url=_clean_optional(caller_sdk_wheel_url, current.caller_sdk_wheel_url),
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


def _local_path_from_inbox_spec(value: str | None) -> Path | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith("file://"):
        return Path(text[7:]).expanduser()
    if "@" in text.split(":", 1)[0]:
        return None
    if ":" in text and not text.startswith("/"):
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else None


def _redact_inbox(value: str | None) -> str | None:
    if not value:
        return None
    if "@" in value and ":" in value:
        host_part, _, _ = value.partition(":")
        return f"{host_part}:<redacted-path>" if host_part else "<remote-inbox>"
    return _redact_path(value)


def _redact_path(value: str) -> str:
    home = str(Path.home())
    if home and value.startswith(home):
        return "~" + value[len(home):]
    return value


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
