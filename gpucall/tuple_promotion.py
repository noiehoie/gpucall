from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.config import default_state_dir, load_config
from gpucall.domain import ExecutionMode, ExecutionTupleSpec
from gpucall.execution.registry import adapter_descriptor, vendor_family_for_adapter
from gpucall.recipe_materialize import to_yaml

def promote_production_tuple(
    *,
    review: Mapping[str, Any],
    candidate_name: str | None,
    config_dir: str | Path,
    work_dir: str | Path,
    validation_dir: str | Path | None = None,
    run_validation: bool = False,
    activate: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    config_root = Path(config_dir).expanduser()
    workspace = Path(work_dir).expanduser()
    if not config_root.exists():
        raise FileNotFoundError(f"config_dir does not exist: {config_root}")
    candidate = _select_review_candidate(review, candidate_name)
    recipe = _mapping(review.get("canonical_recipe"))
    if not recipe:
        raise ValueError("review does not contain canonical_recipe")
    active_config = load_config(config_root)
    candidate_path = candidate.get("path")
    if not candidate_path:
        raise ValueError("candidate match does not include source path")
    candidate_payload = _load_candidate_payload(config_root, candidate)
    tuple = _tuple_from_candidate(candidate_payload, active_config=active_config)
    started = datetime.now(timezone.utc).isoformat()
    promotion_config = workspace / "config"
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    _copy_config_tree(config_root, promotion_config, force=force)
    recipe_path = _write_yaml_guarded(promotion_config / "recipes" / f"{recipe['name']}.yml", recipe, force=force)
    tuple_path = _write_yaml_guarded(promotion_config / "tuples" / f"{tuple['name']}.yml", tuple, force=force)
    surface_path, worker_path = _write_split_tuple(promotion_config, tuple, force=force)
    validation_mode = _validation_mode(recipe["name"], promotion_config)
    promotion_report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "tuple-candidate-promotion",
        "started_at": started,
        "candidate": candidate,
        "recipe": recipe["name"],
        "tuple": tuple["name"],
        "promotion_config_dir": str(promotion_config),
        "generated_recipe_path": str(recipe_path),
        "generated_tuple_path": str(tuple_path),
        "generated_surface_path": str(surface_path),
        "generated_worker_path": str(worker_path),
        "validation": None,
        "activated": False,
        "activation_paths": {},
        "next_actions": [
            f"run gpucall validate-config --config-dir {promotion_config}",
            f"run gpucall tuple-smoke {tuple['name']} --config-dir {promotion_config} --recipe {recipe['name']} --mode {validation_mode} --write-artifact",
            "rerun gpucall-recipe-admin review with the validation artifact directory",
            "activate only after validation passes for the exact recipe/tuple/model/engine tuple",
        ],
    }
    try:
        _validate_config_dir(promotion_config)
        promotion_report["config_valid"] = True
    except ConfigError as exc:
        promotion_report["config_valid"] = False
        promotion_report["config_error"] = str(exc)
        promotion_report["decision"] = "READY_FOR_ENDPOINT_CONFIGURATION"
        promotion_report["next_actions"].insert(
            0,
            "fill execution-surface required fields in the generated tuple YAML, then rerun promote with --run-validation",
        )
        if run_validation or activate:
            raise ValueError("refusing validation/activation because generated promotion config is not valid: " + str(exc)) from exc
        return promotion_report
    if run_validation:
        validation = _run_tuple_validation(tuple["name"], recipe["name"], promotion_config, mode=validation_mode, validation_dir=validation_dir)
        promotion_report["validation"] = validation
        if validation.get("returncode") != 0 or validation.get("passed") is not True:
            promotion_report["decision"] = "VALIDATION_FAILED"
            return promotion_report
    else:
        existing_validation = _find_validation_for_promotion(
            tuple=tuple["name"],
            recipe=recipe["name"],
            model_ref=tuple.get("model_ref"),
            engine_ref=tuple.get("engine_ref"),
            config_dir=promotion_config,
            validation_dir=Path(validation_dir).expanduser() if validation_dir else None,
        )
        promotion_report["validation"] = existing_validation
        if not existing_validation.get("matched"):
            promotion_report["decision"] = "READY_FOR_BILLABLE_VALIDATION"
            if activate:
                raise ValueError("refusing to activate without matching live validation artifact")
            return promotion_report
    if activate:
        active_recipe = _write_yaml_guarded(config_root / "recipes" / f"{recipe['name']}.yml", recipe, force=force)
        active_tuple = _write_yaml_guarded(config_root / "tuples" / f"{tuple['name']}.yml", tuple, force=force)
        active_surface = _write_yaml_guarded(
            config_root / "surfaces" / f"{tuple['name']}.yml",
            _load_yaml_file(surface_path),
            force=force,
        )
        active_worker = _write_yaml_guarded(
            config_root / "workers" / f"{tuple['name']}.yml",
            _load_yaml_file(worker_path),
            force=force,
        )
        _validate_config_dir(config_root)
        promotion_report["activated"] = True
        promotion_report["activation_paths"] = {
            "recipe": str(active_recipe),
            "tuple": str(active_tuple),
            "surface": str(active_surface),
            "worker": str(active_worker),
        }
        promotion_report["decision"] = "ACTIVATED"
    else:
        promotion_report["decision"] = "VALIDATED_READY_TO_ACTIVATE"
    return promotion_report


def promote_candidate(
    *,
    review: Mapping[str, Any],
    candidate_name: str | None,
    config_dir: str | Path,
    work_dir: str | Path,
    validation_dir: str | Path | None = None,
    run_validation: bool = False,
    activate: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    return promote_production_tuple(
        review=review,
        candidate_name=candidate_name,
        config_dir=config_dir,
        work_dir=work_dir,
        validation_dir=validation_dir,
        run_validation=run_validation,
        activate=activate,
        force=force,
    )


def _select_review_candidate(review: Mapping[str, Any], candidate_name: str | None) -> Mapping[str, Any]:
    matches = review.get("tuple_candidate_matches")
    if not isinstance(matches, list) or not matches:
        raise ValueError("review does not contain tuple_candidate_matches")
    if candidate_name is None:
        selected = matches[0]
        if not isinstance(selected, Mapping):
            raise ValueError("invalid tuple_candidate_matches entry")
        return selected
    for match in matches:
        if isinstance(match, Mapping) and match.get("name") == candidate_name:
            return match
    raise ValueError(f"candidate {candidate_name!r} is not present in review tuple_candidate_matches")


def _tuple_from_candidate(candidate: Mapping[str, Any], *, active_config: Any) -> dict[str, Any]:
    name = str(candidate.get("name") or "")
    model_ref = str(candidate.get("model_ref") or "")
    engine_ref = str(candidate.get("engine_ref") or "")
    if not name or not model_ref or not engine_ref:
        raise ValueError("candidate must define name, model_ref, and engine_ref")
    model = active_config.models.get(model_ref)
    if model is None:
        raise ValueError(f"candidate references unknown model_ref {model_ref!r}")
    source: dict[str, Any] = {
        "name": name,
        "adapter": str(candidate.get("adapter") or ""),
        "execution_surface": candidate.get("execution_surface") or _surface_for_adapter(str(candidate.get("adapter") or "")),
        "max_data_classification": str(candidate.get("max_data_classification") or "confidential"),
        "trust_profile": {
            "security_tier": str(candidate.get("security_tier") or "encrypted_capsule"),
            "sovereign_jurisdiction": candidate.get("sovereign_jurisdiction") or "unknown",
            "dedicated_gpu": bool(candidate.get("dedicated_gpu", False)),
            "requires_attestation": bool(candidate.get("requires_attestation", False)),
            "supports_key_release": bool(candidate.get("supports_key_release", False)),
            "allows_worker_s3_credentials": bool(candidate.get("allows_worker_s3_credentials", False)),
        },
        "gpu": str(candidate.get("gpu") or "unknown"),
        "vram_gb": _positive_int(candidate.get("vram_gb"), default=1),
        "max_model_len": _positive_int(candidate.get("max_model_len"), default=model.max_model_len),
        "cost_per_second": float(candidate.get("cost_per_second") or 0.0),
        "configured_price_source": candidate.get("configured_price_source"),
        "configured_price_observed_at": candidate.get("configured_price_observed_at"),
        "configured_price_ttl_seconds": candidate.get("configured_price_ttl_seconds"),
        "expected_cold_start_seconds": candidate.get("expected_cold_start_seconds"),
        "scaledown_window_seconds": candidate.get("scaledown_window_seconds"),
        "min_billable_seconds": candidate.get("min_billable_seconds"),
        "billing_granularity_seconds": candidate.get("billing_granularity_seconds"),
        "modes": _strings(candidate.get("modes") or ["sync", "async"]),
        "endpoint": candidate.get("endpoint"),
        "endpoint_contract": candidate.get("endpoint_contract"),
        "input_contracts": _strings(candidate.get("input_contracts")),
        "output_contract": candidate.get("output_contract"),
        "stream_contract": candidate.get("stream_contract") or "none",
        "supports_vision": bool(model.supports_vision),
        "target": candidate.get("target") or "",
        "stream_target": candidate.get("stream_target"),
        "model": model.provider_model_id,
        "declared_model_max_len": model.max_model_len,
        "model_ref": model_ref,
        "engine_ref": engine_ref,
        "provider_params": dict(candidate.get("provider_params") or {}),
    }
    for key in ("project_id", "region", "zone", "resource_group", "network", "subnet", "service_account", "instance", "image", "key_name", "ssh_remote_cidr", "lease_manifest_path"):
        if key in candidate:
            source[key] = candidate.get(key)
    ExecutionTupleSpec.model_validate(source)
    return source


def _copy_config_tree(source: Path, destination: Path, *, force: bool) -> None:
    if destination.exists():
        if not force:
            raise FileExistsError(f"promotion config already exists: {destination}")
        shutil.rmtree(destination)
    ignore = shutil.ignore_patterns("*.db", "*.db-shm", "*.db-wal", "__pycache__")
    shutil.copytree(source, destination, ignore=ignore)


def _write_yaml_guarded(path: Path, payload: Mapping[str, Any], *, force: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.write_text(to_yaml(payload), encoding="utf-8")
    return path


def _write_split_tuple(config_root: Path, tuple: Mapping[str, Any], *, force: bool) -> tuple[Path, Path]:
    name = str(tuple["name"])
    account_ref = _account_ref(str(tuple.get("adapter") or ""))
    surface = _drop_none(
        {
            "surface_ref": name,
            "worker_ref": name,
            "account_ref": account_ref,
            "adapter": tuple.get("adapter"),
            "execution_surface": tuple.get("execution_surface"),
            "max_data_classification": tuple.get("max_data_classification"),
            "trust_profile": tuple.get("trust_profile"),
            "gpu": tuple.get("gpu"),
            "vram_gb": tuple.get("vram_gb"),
            "max_model_len": tuple.get("max_model_len"),
            "cost_per_second": tuple.get("cost_per_second"),
            "configured_price_source": tuple.get("configured_price_source"),
            "configured_price_observed_at": tuple.get("configured_price_observed_at"),
            "configured_price_ttl_seconds": tuple.get("configured_price_ttl_seconds"),
            "expected_cold_start_seconds": tuple.get("expected_cold_start_seconds"),
            "scaledown_window_seconds": tuple.get("scaledown_window_seconds"),
            "min_billable_seconds": tuple.get("min_billable_seconds"),
            "billing_granularity_seconds": tuple.get("billing_granularity_seconds"),
            "endpoint": tuple.get("endpoint"),
            "region": tuple.get("region"),
            "zone": tuple.get("zone"),
            "instance": tuple.get("instance"),
            "image": tuple.get("image"),
            "key_name": tuple.get("key_name"),
            "ssh_remote_cidr": tuple.get("ssh_remote_cidr"),
            "lease_manifest_path": tuple.get("lease_manifest_path"),
            "supports_vision": tuple.get("supports_vision"),
            "stock_state": "configured",
        }
    )
    worker = _drop_none(
        {
            "worker_ref": name,
            "account_ref": account_ref,
            "adapter": tuple.get("adapter"),
            "execution_surface": tuple.get("execution_surface"),
            "model_ref": tuple.get("model_ref"),
            "engine_ref": tuple.get("engine_ref"),
            "modes": tuple.get("modes"),
            "input_contracts": tuple.get("input_contracts"),
            "output_contract": tuple.get("output_contract"),
            "stream_contract": tuple.get("stream_contract"),
            "target": tuple.get("target"),
            "stream_target": tuple.get("stream_target"),
            "endpoint_contract": tuple.get("endpoint_contract"),
            "model": tuple.get("model"),
            "declared_model_max_len": tuple.get("declared_model_max_len"),
            "provider_params": tuple.get("provider_params") or {},
        }
    )
    surface_path = _write_yaml_guarded(config_root / "surfaces" / f"{name}.yml", surface, force=force)
    worker_path = _write_yaml_guarded(config_root / "workers" / f"{name}.yml", worker, force=force)
    return surface_path, worker_path


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _account_ref(adapter: str) -> str:
    return vendor_family_for_adapter(adapter)


def _validate_config_dir(config_dir: Path) -> None:
    load_config(config_dir)


def _validation_mode(recipe: str, config_dir: Path) -> str:
    config = load_config(config_dir)
    recipe_spec = config.recipes.get(recipe)
    if recipe_spec is None:
        return "sync"
    if ExecutionMode.SYNC in recipe_spec.allowed_modes:
        return "sync"
    if not recipe_spec.allowed_modes:
        return "sync"
    return recipe_spec.allowed_modes[0].value


def _run_tuple_validation(tuple: str, recipe: str, config_dir: Path, *, mode: str, validation_dir: str | Path | None) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "gpucall.cli",
        "tuple-smoke",
        tuple,
        "--config-dir",
        str(config_dir),
        "--recipe",
        recipe,
        "--mode",
        mode,
        "--write-artifact",
    ]
    env = dict(os.environ)
    credentials = config_dir / "credentials.yml"
    if credentials.exists() and not env.get("GPUCALL_CREDENTIALS"):
        env["GPUCALL_CREDENTIALS"] = str(credentials)
    modal_config = config_dir / ".modal.toml"
    if not env.get("MODAL_CONFIG_PATH"):
        explicit_modal = env.get("GPUCALL_MODAL_CONFIG_FILE")
        if explicit_modal:
            env["MODAL_CONFIG_PATH"] = explicit_modal
        elif modal_config.exists() and modal_config.stat().st_size > 0:
            env["MODAL_CONFIG_PATH"] = str(modal_config)
    if validation_dir is not None:
        state_dir = Path(validation_dir).expanduser().parent
        env["GPUCALL_STATE_DIR"] = str(state_dir)
    completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    result: dict[str, Any] = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "passed": False,
    }
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
            result["artifact"] = payload
            result["passed"] = payload.get("passed") is True
        except json.JSONDecodeError:
            result["parse_error"] = "tuple-smoke stdout was not JSON"
    return result


def _find_validation_for_promotion(
    *,
    tuple: str,
    recipe: str,
    model_ref: str | None,
    engine_ref: str | None,
    config_dir: Path,
    validation_dir: Path | None,
) -> dict[str, Any]:
    root = validation_dir or (default_state_dir() / "tuple-validation")
    result = {"dir": str(root), "matched": []}
    if not root.exists():
        result["reason"] = "validation artifact directory does not exist"
        return result
    expected_hash = _config_hash(config_dir)
    expected_commit = _git_commit(Path.cwd())
    matched: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("validation_schema_version") != 1 or data.get("passed") is not True:
            continue
        if data.get("tuple") != tuple or data.get("recipe") != recipe:
            continue
        if data.get("model_ref") != model_ref or data.get("engine_ref") != engine_ref:
            continue
        if data.get("config_hash") != expected_hash:
            continue
        if expected_commit and data.get("commit") != expected_commit:
            continue
        matched.append({"path": str(path), "tuple": tuple, "recipe": recipe})
    result["matched"] = matched
    return result


def _load_yaml_file(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return payload


def _load_candidate_payload(config_root: Path, candidate: Mapping[str, Any]) -> dict[str, Any]:
    candidate_path = str(candidate.get("path") or "")
    if "#" not in candidate_path:
        return _load_yaml_file(Path(candidate_path))
    candidate_name = str(candidate.get("name") or candidate_path.rsplit("#", 1)[-1])
    for payload in load_tuple_candidate_payloads(config_root):
        if str(payload.get("name") or "") == candidate_name:
            return payload
    raise FileNotFoundError(f"generated candidate not found in catalog: {candidate_name}")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _surface_for_adapter(adapter: str) -> str | None:
    descriptor = adapter_descriptor(adapter)
    if descriptor is None or descriptor.execution_surface is None:
        return None
    return descriptor.execution_surface.value


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _config_hash(config_dir: Path | None) -> str | None:
    if config_dir is None or not config_dir.exists():
        return None
    digest = hashlib.sha256()
    for path in sorted(config_dir.rglob("*.yml")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(config_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_commit(root: Path) -> str | None:
    head = root / ".git" / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
        if value.startswith("ref: "):
            ref = root / ".git" / value.removeprefix("ref: ")
            return ref.read_text(encoding="utf-8").strip()
        return value
    except OSError:
        return None
