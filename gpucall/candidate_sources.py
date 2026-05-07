from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml


def load_tuple_candidate_payloads(config_dir: Path) -> list[dict[str, Any]]:
    """Return explicit and generated candidate tuples in the legacy payload shape."""
    payloads = _load_explicit_candidates(config_dir / "provider_candidates")
    payloads.extend(_load_generated_candidates(config_dir / "candidate_sources"))
    return sorted(payloads, key=lambda item: str(item.get("name") or ""))


def _load_explicit_candidates(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.yml")):
        payload = _load_mapping(path)
        if not payload.get("name") or not payload.get("adapter"):
            raise ValueError(f"tuple candidate must define name and adapter: {path}")
        payload["_path"] = str(path)
        payloads.append(payload)
    return payloads


def _load_generated_candidates(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.yml")):
        source = _load_mapping(path)
        kind = str(source.get("kind") or "")
        if kind != "runpod_serverless_candidate_matrix":
            raise ValueError(f"unknown candidate source kind {kind!r}: {path}")
        payloads.extend(_runpod_serverless_candidates(source, source_path=path))
    return payloads


def _runpod_serverless_candidates(source: Mapping[str, Any], *, source_path: Path) -> list[dict[str, Any]]:
    gpus = {str(row["ref"]): row for row in _mapping_rows(source.get("gpus"), source_path, "gpus")}
    models = {str(row["ref"]): row for row in _mapping_rows(source.get("models"), source_path, "models")}
    common = dict(source.get("common") or {})
    generated: list[dict[str, Any]] = []
    for family in _mapping_rows(source.get("families"), source_path, "families"):
        generated.extend(_runpod_family_candidates(family, gpus=gpus, models=models, common=common, source_path=source_path))
    return generated


def _runpod_family_candidates(
    family: Mapping[str, Any],
    *,
    gpus: Mapping[str, Mapping[str, Any]],
    models: Mapping[str, Mapping[str, Any]],
    common: Mapping[str, Any],
    source_path: Path,
) -> list[dict[str, Any]]:
    prefix = str(family["prefix"])
    generated: list[dict[str, Any]] = []
    for gpu_ref, model_ref in _matrix_pairs(family):
        gpu = gpus[gpu_ref]
        model = models[model_ref]
        # The generated rows intentionally match hand-authored candidate YAML.
        # Downstream audit and promotion code can stay tuple-oriented while the
        # catalog source remains compact and reviewable.
        row = {
            "name": f"{prefix}-{gpu['slug']}-{model['slug']}",
            "status": "candidate",
            "adapter": family["adapter"],
            "execution_surface": "managed_endpoint",
            "gpu": gpu["ref"],
            "vram_gb": gpu["vram_gb"],
            "max_model_len": model["max_model_len"],
            "model_ref": model_ref,
            "engine_ref": family["engine_ref"],
            "modes": ["sync", "async"],
            "endpoint_contract": family["endpoint_contract"],
            "input_contracts": list(family["input_contracts"]),
            "output_contract": family["output_contract"],
            "stream_contract": family["stream_contract"],
            "max_data_classification": common.get("max_data_classification", "confidential"),
            "cost_per_second": common.get("cost_per_second", 0.0),
            "scaledown_window_seconds": common.get("scaledown_window_seconds", 5),
            "min_billable_seconds": common.get("min_billable_seconds", 1),
            "billing_granularity_seconds": common.get("billing_granularity_seconds", 1),
            "official_doc_refs": list(family.get("official_doc_refs") or []),
            "live_validation_required": True,
            "notes": family.get("notes"),
            "_source": str(source_path),
            "_path": f"{source_path}#{prefix}-{gpu['slug']}-{model['slug']}",
        }
        row.update(_worker_fields(family, model))
        generated.append(row)
    return generated


def _worker_fields(family: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, Any]:
    provider_model_id = str(model["provider_model_id"])
    if family["endpoint_contract"] == "openai-chat-completions":
        return {
            "image": str(family.get("image") or "runpod/worker-v1-vllm:v2.18.1"),
            "provider_params": {
                "worker_env": {
                    "MODEL_NAME": provider_model_id,
                    "OPENAI_SERVED_MODEL_NAME_OVERRIDE": provider_model_id,
                    "MAX_MODEL_LEN": str(model["max_model_len"]),
                    "GPU_MEMORY_UTILIZATION": "0.95",
                    "MAX_CONCURRENCY": "30",
                }
            },
        }
    return {
        "model": provider_model_id,
        "provider_params": {
            "worker_contract": "gpucall-provider-result",
            "worker_image_required": True,
            "worker_env": {
                "GPUCALL_WORKER_MODEL": provider_model_id,
                "GPUCALL_WORKER_MAX_MODEL_LEN": str(model["max_model_len"]),
            },
        },
    }


def _matrix_pairs(family: Mapping[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    matrix = family.get("matrix") or {}
    if not isinstance(matrix, Mapping):
        raise ValueError("candidate family matrix must be a mapping")
    for gpu_ref, model_refs in matrix.items():
        for model_ref in model_refs or []:
            pairs.append((str(gpu_ref), str(model_ref)))
    return pairs


def _mapping_rows(value: object, source_path: Path, key: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        raise ValueError(f"{source_path} {key} must be a list of mappings")
    return value


def _load_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return payload
