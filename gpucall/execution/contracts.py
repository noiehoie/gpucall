from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping

from gpucall.domain import ExecutionTupleSpec
from gpucall.targeting import is_configured_target
from gpucall.execution.registry import adapter_descriptor, vendor_family_for_adapter


def official_contract(spec: ExecutionTupleSpec | None) -> dict[str, object]:
    descriptor = adapter_descriptor(spec) if spec is not None else None
    endpoint_contract = getattr(spec, "endpoint_contract", None)
    execution_surface = getattr(getattr(spec, "execution_surface", None), "value", getattr(spec, "execution_surface", None))
    contract: dict[str, object] = {
        "adapter": getattr(spec, "adapter", None),
        "account_ref": account_ref_for_spec(spec),
        "execution_surface": execution_surface,
        "endpoint_contract": endpoint_contract,
        "expected_endpoint_contract": getattr(descriptor, "endpoint_contract", None),
        "output_contract": getattr(spec, "output_contract", None),
        "expected_output_contract": getattr(descriptor, "output_contract", None),
        "stream_contract": getattr(spec, "stream_contract", None),
        "expected_stream_contract": getattr(descriptor, "stream_contract", None),
        "input_contracts": list(getattr(spec, "input_contracts", []) or []),
        "official_sources": list(getattr(descriptor, "official_sources", ()) or ()),
        "production_eligible": bool(getattr(descriptor, "production_eligible", False)) if descriptor is not None else False,
        "production_rejection_reason": getattr(descriptor, "production_rejection_reason", None),
        "model": getattr(spec, "model", None),
        "model_ref": getattr(spec, "model_ref", None),
        "engine_ref": getattr(spec, "engine_ref", None),
        "max_model_len": getattr(spec, "max_model_len", None),
        "gpu": getattr(spec, "gpu", None),
        "vram_gb": getattr(spec, "vram_gb", None),
    }
    contract.update(_contract_details(spec, endpoint_contract=str(endpoint_contract or ""), execution_surface=str(execution_surface or "")))
    return contract


def official_contract_hash(contract: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def account_ref_for_spec(spec: ExecutionTupleSpec | None) -> str | None:
    if spec is None:
        return None
    explicit = getattr(spec, "account_ref", None)
    if explicit:
        return str(explicit)
    return vendor_family_for_adapter(str(getattr(spec, "adapter", "") or ""))


def tuple_evidence_key(spec: ExecutionTupleSpec) -> str:
    # This key names what was validated, not who sold the GPU. It stays stable
    # across tuple display-name changes but changes when the executable
    # contract, worker, model, or account boundary changes.
    payload = {
        "account_ref": account_ref_for_spec(spec),
        "execution_surface": spec.execution_surface.value if spec.execution_surface else None,
        "endpoint_contract": spec.endpoint_contract,
        "output_contract": spec.output_contract,
        "stream_contract": spec.stream_contract,
        "model_ref": spec.model_ref,
        "engine_ref": spec.engine_ref,
        "target": is_configured_target(spec.target),
    }
    return official_contract_hash(payload)


def tuple_evidence_label(spec: ExecutionTupleSpec) -> str:
    surface = spec.execution_surface.value if spec.execution_surface else "unknown_surface"
    contract = spec.endpoint_contract or "unknown_contract"
    model = spec.model_ref or "unknown_model"
    engine = spec.engine_ref or "unknown_engine"
    return f"{surface}:{contract}:{model}:{engine}"


def artifact_tuple_evidence_key(data: Mapping[str, object], tuple: ExecutionTupleSpec) -> str | None:
    contract = data.get("official_contract") if isinstance(data.get("official_contract"), dict) else {}
    if not contract:
        return None
    payload = {
        "account_ref": contract.get("account_ref") or account_ref_for_spec(tuple),
        "execution_surface": contract.get("execution_surface"),
        "endpoint_contract": contract.get("endpoint_contract"),
        "output_contract": contract.get("output_contract"),
        "stream_contract": contract.get("stream_contract"),
        "model_ref": data.get("model_ref") or contract.get("model_ref"),
        "engine_ref": data.get("engine_ref") or contract.get("engine_ref"),
        "target": is_configured_target(getattr(tuple, "target", None)),
    }
    return official_contract_hash(payload)


def _contract_details(spec: ExecutionTupleSpec | None, *, endpoint_contract: str, execution_surface: str) -> dict[str, object]:
    if spec is None:
        return {}
    if endpoint_contract == "modal-function":
        app_name, function_name = _split_target(getattr(spec, "target", None))
        stream_app_name, stream_function_name = _split_target(getattr(spec, "stream_target", None))
        return {
            "function_runtime": {
                "runtime_contract": "deployed_function_invocation",
                "target_app": app_name,
                "target_function": function_name,
                "stream_app": stream_app_name,
                "stream_function": stream_function_name,
                "provider_params": getattr(spec, "provider_params", None) or {},
                "autoscaler_env": {
                    "GPUCALL_MODAL_A10G_MIN_CONTAINERS": os.getenv("GPUCALL_MODAL_A10G_MIN_CONTAINERS"),
                    "GPUCALL_MODAL_A10G_SCALEDOWN_WINDOW": os.getenv("GPUCALL_MODAL_A10G_SCALEDOWN_WINDOW"),
                    "GPUCALL_MODAL_H200X4_MIN_CONTAINERS": os.getenv("GPUCALL_MODAL_H200X4_MIN_CONTAINERS"),
                    "GPUCALL_MODAL_H200X4_SCALEDOWN_WINDOW": os.getenv("GPUCALL_MODAL_H200X4_SCALEDOWN_WINDOW"),
                    "GPUCALL_MODAL_VISION_H100_MIN_CONTAINERS": os.getenv("GPUCALL_MODAL_VISION_H100_MIN_CONTAINERS"),
                    "GPUCALL_MODAL_VISION_H100_SCALEDOWN_WINDOW": os.getenv("GPUCALL_MODAL_VISION_H100_SCALEDOWN_WINDOW"),
                },
            }
        }
    if endpoint_contract == "openai-chat-completions":
        params = getattr(spec, "provider_params", None) or {}
        return {
            "managed_endpoint": {
                "runtime_contract": "openai_compatible_chat_completions",
                "endpoint_id": getattr(spec, "target", None),
                "base_url": str(getattr(spec, "endpoint", None) or "https://api.runpod.ai/v2"),
                "chat_completions_path": "/openai/v1/chat/completions",
                "health_path": "/health",
                "image": getattr(spec, "image", None),
                "worker_env": params.get("worker_env") if isinstance(params, dict) else None,
                "data_refs_supported": "data_refs" in set(getattr(spec, "input_contracts", []) or []),
            }
        }
    if endpoint_contract == "runpod-serverless":
        return {
            "managed_endpoint": {
                "runtime_contract": "queue_job_endpoint",
                "endpoint_id": getattr(spec, "target", None),
                "base_url": str(getattr(spec, "endpoint", None) or "https://api.runpod.ai/v2"),
                "run_path": "/run",
                "status_path": "/status/{job_id}",
                "output_contract": getattr(spec, "output_contract", None),
            }
        }
    if endpoint_contract == "hyperstack-vm" or execution_surface == "iaas_vm":
        return {
            "iaas_vm": {
                "runtime_contract": "vm_lease_with_worker_bootstrap",
                "api_base": str(getattr(spec, "endpoint", None) or "https://infrahub-api.nexgencloud.com/v1"),
                "auth_header": "api_key",
                "environment_name": getattr(spec, "target", None),
                "flavor_name": getattr(spec, "instance", None),
                "image_name": getattr(spec, "image", None),
                "key_name": getattr(spec, "key_name", None),
                "ssh_remote_cidr": getattr(spec, "ssh_remote_cidr", None),
                "create_payload_validated_by_official_sdk": True,
                "security_rules_inline": True,
                "worker_bootstrap_contract": "gpucall-managed-ssh-vllm",
            }
        }
    return {}


def _split_target(target: object) -> tuple[str | None, str | None]:
    text = str(target or "")
    if ":" not in text:
        return (text or None), None
    left, right = text.split(":", 1)
    return left or None, right or None
