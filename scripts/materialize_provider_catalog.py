#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from gpucall.candidate_sources import load_tuple_candidate_payloads


PRODUCTION_ELIGIBLE_PREFIXES = (
    "hyperstack-",
    "runpod-flash-",
    "runpod-vllm-",
)

SURFACE_KEYS = (
    "account_ref",
    "adapter",
    "execution_surface",
    "gpu",
    "vram_gb",
    "max_model_len",
    "region",
    "zone",
    "cost_per_second",
    "configured_price_source",
    "configured_price_observed_at",
    "configured_price_ttl_seconds",
    "stock_state",
    "billing_granularity_seconds",
    "image",
    "max_data_classification",
    "scaledown_window_seconds",
    "min_billable_seconds",
    "endpoint",
    "instance",
    "key_name",
    "ssh_remote_cidr",
    "trust_profile",
)

WORKER_KEYS = (
    "account_ref",
    "adapter",
    "execution_surface",
    "model_ref",
    "engine_ref",
    "modes",
    "input_contracts",
    "output_contract",
    "stream_contract",
    "target",
    "stream_target",
    "endpoint_contract",
    "model",
    "provider_params",
    "declared_model_max_len",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--templates-dir", type=Path, default=Path("gpucall/config_templates"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    changed = 0
    missing_before = 0
    rows = [
        row
        for row in load_tuple_candidate_payloads(args.config_dir)
        if str(row.get("name") or "").startswith(PRODUCTION_ELIGIBLE_PREFIXES)
        and row.get("production_generation_allowed", True) is not False
    ]
    models = load_model_ids(args.config_dir)
    for root in (args.config_dir, args.templates_dir):
        for row in rows:
            missing_before += materialize_row(root, row, models=models, dry_run=True)
            changed += materialize_row(root, row, models=models, dry_run=args.dry_run)
    print(f"candidate_rows={len(rows)} missing_before={missing_before} changed={changed}")


def materialize_row(root: Path, row: dict[str, Any], *, models: dict[str, str], dry_run: bool) -> int:
    name = str(row["name"])
    surface_path = root / "surfaces" / f"{name}.yml"
    worker_path = root / "workers" / f"{name}.yml"
    surface, worker = split_surface_worker(row, models=models)
    if dry_run:
        return 0 if surface_path.exists() and worker_path.exists() else 1
    surface_path.parent.mkdir(parents=True, exist_ok=True)
    worker_path.parent.mkdir(parents=True, exist_ok=True)
    changed = write_yaml_if_changed(surface_path, surface)
    changed |= write_yaml_if_changed(worker_path, worker)
    return int(changed)


def split_surface_worker(row: dict[str, Any], *, models: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    data = dict(row)
    name = str(data["name"])
    data.setdefault("stock_state", "configured")
    data.setdefault("modes", ["sync", "async"])
    if name.startswith("runpod-vllm-"):
        data.setdefault("target", "RUNPOD_ENDPOINT_ID_PLACEHOLDER")
        if not data.get("model"):
            data["model"] = _served_model_from_provider_params(data)
        data["stream_contract"] = "none"
    elif name.startswith("runpod-flash-"):
        data.setdefault("target", "RUNPOD_ENDPOINT_ID_PLACEHOLDER")
    elif name.startswith("hyperstack-"):
        data.setdefault("declared_model_max_len", data.get("max_model_len"))
        if not data.get("model"):
            data["model"] = _served_model_from_provider_params(data) or models.get(str(data.get("model_ref") or ""))
        data.setdefault("trust_profile", trust_profile_from_candidate(data))

    surface = {"surface_ref": name, "worker_ref": name}
    surface.update({key: data.get(key) for key in SURFACE_KEYS if key in data})
    worker = {"worker_ref": name}
    worker.update({key: data.get(key) for key in WORKER_KEYS if key in data})
    return clean(surface), clean(worker)


def clean(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _served_model_from_provider_params(data: dict[str, Any]) -> str | None:
    worker_env = ((data.get("provider_params") or {}).get("worker_env") or {})
    value = worker_env.get("MODEL_NAME") or worker_env.get("OPENAI_SERVED_MODEL_NAME_OVERRIDE")
    return str(value) if value else None


def trust_profile_from_candidate(data: dict[str, Any]) -> dict[str, Any] | None:
    if not any(key in data for key in ("security_tier", "sovereign_jurisdiction", "dedicated_gpu")):
        return None
    return {
        "security_tier": data.get("security_tier", "shared_gpu"),
        "sovereign_jurisdiction": data.get("sovereign_jurisdiction"),
        "dedicated_gpu": bool(data.get("dedicated_gpu", False)),
        "requires_attestation": bool(data.get("requires_attestation", False)),
        "supports_key_release": bool(data.get("supports_key_release", False)),
        "allows_worker_s3_credentials": bool(data.get("allows_worker_s3_credentials", False)),
    }


def load_model_ids(config_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted((config_dir / "models").glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or path.stem)
        provider_id = data.get("provider_model_id") or data.get("hf_repo") or data.get("model")
        if provider_id:
            out[name] = str(provider_id)
    return out


def write_yaml_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


if __name__ == "__main__":
    main()
