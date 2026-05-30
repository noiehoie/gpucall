from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, ValidationError, model_validator

from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.config import load_config
from gpucall.credentials import load_credentials
from gpucall.domain import ExecutionTupleSpec, Policy, RemediationMode
from gpucall.execution_surfaces.managed_endpoint import RUNPOD_REST_API_BASE, json_or_error, requests_session
from gpucall.provider_contracts import CLOUD_PROVIDER_FAMILIES
from gpucall.targeting import is_configured_target
from gpucall.tuple_promotion import _tuple_from_candidate


SUPPORTED_SUPPLY_PROVISIONING_PROVIDERS = ("modal", "runpod")
UNSUPPORTED_SUPPLY_PROVISIONING_PROVIDERS = tuple(
    provider for provider in CLOUD_PROVIDER_FAMILIES if provider not in SUPPORTED_SUPPLY_PROVISIONING_PROVIDERS
)
SupplyProvisioningActionName = Literal["create_runpod_template", "create_runpod_serverless_endpoint"]
SupplyProvisioningResourceType = Literal["template", "endpoint"]
SupplyProvisioningProvider = Literal["runpod"]
SupplyProvisioningStatus = Literal["blocked", "no_actions", "actions_proposed"]


_RUNPOD_GPU_TYPE_IDS_BY_GPU_REF: dict[str, list[str]] = {
    "ADA_24": ["NVIDIA GeForce RTX 4090", "NVIDIA L4"],
    "ADA_48_PRO": ["NVIDIA RTX 6000 Ada Generation"],
    "AMPERE_16": ["NVIDIA RTX A4000"],
    "AMPERE_24": ["NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090"],
    "AMPERE_48": ["NVIDIA RTX A6000", "NVIDIA A40"],
    "AMPERE_80": ["NVIDIA A100-SXM4-80GB", "NVIDIA A100 80GB PCIe"],
    "HOPPER_141": ["NVIDIA H200"],
    "HOPPER_143": ["NVIDIA H200 NVL"],
    "RUNPOD_A4000": ["NVIDIA RTX A4000"],
    "RUNPOD_A4500": ["NVIDIA RTX A4500"],
    "RUNPOD_RTX4000_ADA": ["NVIDIA RTX 4000 Ada Generation"],
    "RUNPOD_RTX4090": ["NVIDIA GeForce RTX 4090"],
    "RUNPOD_L4": ["NVIDIA L4"],
    "RUNPOD_A5000": ["NVIDIA RTX A5000"],
    "RUNPOD_RTX3090": ["NVIDIA GeForce RTX 3090"],
    "RUNPOD_L40": ["NVIDIA L40"],
    "RUNPOD_L40S": ["NVIDIA L40S"],
    "RUNPOD_RTX6000_ADA": ["NVIDIA RTX 6000 Ada Generation"],
    "RUNPOD_A6000": ["NVIDIA RTX A6000"],
    "RUNPOD_A40": ["NVIDIA A40"],
    "RUNPOD_A100_80GB": ["NVIDIA A100-SXM4-80GB", "NVIDIA A100 80GB PCIe"],
    "RUNPOD_H100_80GB": ["NVIDIA H100 80GB HBM3", "NVIDIA H100 PCIe"],
    "RUNPOD_H100_NVL": ["NVIDIA H100 NVL"],
    "RUNPOD_H200_SXM": ["NVIDIA H200"],
    "RUNPOD_H200_NVL": ["NVIDIA H200 NVL"],
    "RUNPOD_B200": ["NVIDIA B200"],
}


class ProviderSupplyProvisioningAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    action: SupplyProvisioningActionName
    provider: SupplyProvisioningProvider
    resource_type: SupplyProvisioningResourceType
    resource_id: str
    tuple: str
    source_kind: Literal["tuple", "candidate"]
    source_path: str | None = None
    reason: str
    policy_mode: RemediationMode
    requires_approval: bool
    provider_mutation: Literal[True] = True
    destructive: Literal[False] = False
    billable_generation_allowed: Literal[False] = False
    apply_supported: bool
    apply_blocked_reasons: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    request: dict[str, Any]
    parameters: dict[str, Any] = Field(default_factory=dict)
    post_apply_config_patch: list[dict[str, Any]] = Field(default_factory=list)
    source_snapshot: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action_contract(self) -> "ProviderSupplyProvisioningAction":
        if not self.action_id.strip():
            raise ValueError("action_id must be non-empty")
        if not self.resource_id.strip():
            raise ValueError("resource_id must be non-empty")
        if not self.tuple.strip():
            raise ValueError("tuple must be non-empty")
        if self.policy_mode == "disabled":
            raise ValueError("disabled provider supply actions must not be materialized")
        if self.apply_supported and self.apply_blocked_reasons:
            raise ValueError("apply_supported actions must not carry apply_blocked_reasons")
        if self.action == "create_runpod_template":
            required = {"imageName", "name", "isServerless", "env"}
            missing = required.difference(self.request)
            if missing:
                raise ValueError(f"create_runpod_template request missing fields: {sorted(missing)}")
        if self.action == "create_runpod_serverless_endpoint":
            required = {"name", "computeType", "gpuCount", "gpuTypeIds", "workersMin", "workersMax"}
            missing = required.difference(self.request)
            if missing:
                raise ValueError(f"create_runpod_serverless_endpoint request missing fields: {sorted(missing)}")
            has_template_id = bool(str(self.request.get("templateId") or "").strip())
            has_template_ref = bool(str(self.parameters.get("template_id_from_action") or "").strip())
            if self.apply_supported and not (has_template_id or has_template_ref):
                raise ValueError("apply-supported endpoint creation must define templateId or template_id_from_action")
        return self


class ProviderSupplyProvisioningPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    phase: Literal["provider-supply-provisioning-plan"] = "provider-supply-provisioning-plan"
    generated_at: datetime
    config_dir: str
    billable_generation_allowed: Literal[False] = False
    status: SupplyProvisioningStatus
    action_count: NonNegativeInt
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    policy: dict[str, Any]
    actions: list[ProviderSupplyProvisioningAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan_count(self) -> "ProviderSupplyProvisioningPlan":
        if self.action_count != len(self.actions):
            raise ValueError("action_count must equal len(actions)")
        expected_status = "actions_proposed" if self.actions else ("blocked" if self.blockers else "no_actions")
        if self.status != expected_status:
            raise ValueError("status must reflect actions and blockers")
        return self


class ProviderSupplyProvisioningApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    phase: Literal["provider-supply-provisioning-apply"] = "provider-supply-provisioning-apply"
    generated_at: datetime
    dry_run: bool
    billable_generation_allowed: Literal[False] = False
    plan_action_count: NonNegativeInt
    applied_count: NonNegativeInt
    skipped_count: NonNegativeInt
    failed_count: NonNegativeInt
    results: list[dict[str, Any]]


@dataclass(frozen=True)
class _SupplySource:
    tuple_name: str
    source_kind: Literal["tuple", "candidate"]
    source_path: str | None
    payload: dict[str, Any]


def build_provider_supply_provisioning_plan(
    *,
    config_dir: str | Path,
    tuple_name: str | None = None,
    candidate_name: str | None = None,
    review_path: str | Path | None = None,
    template_id: str | None = None,
    endpoint_name: str | None = None,
    template_name: str | None = None,
    gpu_type_ids: list[str] | None = None,
    workers_min: int | None = None,
    workers_max: int | None = None,
    network_volume_id: str | None = None,
    data_center_ids: list[str] | None = None,
    container_disk_gb: int | None = None,
    now: datetime | None = None,
) -> ProviderSupplyProvisioningPlan:
    root = Path(config_dir).expanduser()
    config = load_config(root)
    policy = config.policy.provider_supply_provisioning.create_runpod_serverless_endpoint
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    blockers: list[dict[str, Any]] = []
    actions: list[ProviderSupplyProvisioningAction] = []

    if policy.mode == "disabled":
        blockers.append({"reason": "provider_supply_provisioning.create_runpod_serverless_endpoint is disabled"})
        return _plan(root, config.policy, generated_at, actions, blockers)

    source = _load_supply_source(
        root,
        tuple_name=tuple_name,
        candidate_name=candidate_name,
        review_path=Path(review_path).expanduser() if review_path is not None else None,
    )
    payload = source.payload
    if payload.get("adapter") == "modal" and payload.get("execution_surface") == "function_runtime":
        if not is_configured_target(payload.get("target")):
            blockers.append(
                {
                    "tuple": source.tuple_name,
                    "reason": "modal function runtime tuple is missing a deployed function target",
                    "adapter": payload.get("adapter"),
                    "execution_surface": payload.get("execution_surface"),
                }
            )
        return _plan(root, config.policy, generated_at, actions, blockers)
    if payload.get("adapter") != "runpod-vllm-serverless" or payload.get("execution_surface") != "managed_endpoint":
        blockers.append(
            {
                "tuple": source.tuple_name,
                "reason": "provider supply provisioning currently supports only RunPod managed vLLM serverless tuples",
                "adapter": payload.get("adapter"),
                "execution_surface": payload.get("execution_surface"),
            }
        )
        return _plan(root, config.policy, generated_at, actions, blockers)
    if source.source_kind == "tuple" and is_configured_target(payload.get("target")):
        blockers.append({"tuple": source.tuple_name, "reason": "tuple already has a configured provider target"})
        return _plan(root, config.policy, generated_at, actions, blockers)

    requested_workers_min = policy.default_workers_min if workers_min is None else workers_min
    requested_workers_max = policy.default_workers_max if workers_max is None else workers_max
    blocked = _supply_policy_blockers(
        requested_workers_min=requested_workers_min,
        requested_workers_max=requested_workers_max,
        policy=config.policy,
    )
    source_snapshot = _source_snapshot(source, payload)
    stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    safe_tuple = _safe_name(source.tuple_name)
    resolved_template_name = template_name or f"gpucall-{safe_tuple}-template-{stamp}"
    resolved_endpoint_name = endpoint_name or f"gpucall-{safe_tuple}-{stamp}"

    template_action_id: str | None = None
    if not _string(template_id):
        template_blockers = list(blocked)
        template_request = _runpod_template_request(
            payload,
            name=resolved_template_name,
            container_disk_gb=container_disk_gb or policy.default_container_disk_gb,
        )
        if not template_request.get("imageName"):
            template_blockers.append("missing_runpod_template_image")
        if not template_request.get("env"):
            template_blockers.append("missing_runpod_worker_env")
        template_action_id = _action_id("create_runpod_template", source.tuple_name, resolved_template_name)
        actions.append(
            ProviderSupplyProvisioningAction(
                action_id=template_action_id,
                action="create_runpod_template",
                provider="runpod",
                resource_type="template",
                resource_id=resolved_template_name,
                tuple=source.tuple_name,
                source_kind=source.source_kind,
                source_path=source.source_path,
                reason="create RunPod Serverless worker template from gpucall tuple supply contract",
                policy_mode=policy.mode,
                requires_approval=policy.mode == "approval_required",
                apply_supported=not template_blockers,
                apply_blocked_reasons=template_blockers,
                request=template_request,
                source_snapshot=source_snapshot,
            )
        )

    endpoint_blockers = list(blocked)
    resolved_gpu_type_ids = _clean_strings(gpu_type_ids) or _runpod_gpu_type_ids(payload.get("gpu"))
    if not resolved_gpu_type_ids:
        endpoint_blockers.append("missing_runpod_gpu_type_ids")
    endpoint_request = _runpod_endpoint_request(
        payload,
        name=resolved_endpoint_name,
        template_id=_string(template_id) or None,
        gpu_type_ids=resolved_gpu_type_ids,
        workers_min=requested_workers_min,
        workers_max=requested_workers_max,
        network_volume_id=network_volume_id,
        data_center_ids=data_center_ids,
        policy=config.policy,
    )
    parameters: dict[str, Any] = {}
    depends_on: list[str] = []
    if not _string(template_id):
        if template_action_id is None:
            endpoint_blockers.append("missing_template_id")
        else:
            parameters["template_id_from_action"] = template_action_id
            depends_on.append(template_action_id)
    endpoint_action_id = _action_id("create_runpod_serverless_endpoint", source.tuple_name, resolved_endpoint_name)
    actions.append(
        ProviderSupplyProvisioningAction(
            action_id=endpoint_action_id,
            action="create_runpod_serverless_endpoint",
            provider="runpod",
            resource_type="endpoint",
            resource_id=resolved_endpoint_name,
            tuple=source.tuple_name,
            source_kind=source.source_kind,
            source_path=source.source_path,
            reason="create RunPod Serverless endpoint for gpucall tuple supply contract",
            policy_mode=policy.mode,
            requires_approval=policy.mode == "approval_required",
            apply_supported=not endpoint_blockers,
            apply_blocked_reasons=endpoint_blockers,
            depends_on=depends_on,
            request=endpoint_request,
            parameters=parameters,
            post_apply_config_patch=[_target_patch(root, source.tuple_name, endpoint_action_id)],
            source_snapshot=source_snapshot,
        )
    )
    return _plan(root, config.policy, generated_at, actions, blockers)


def load_provider_supply_provisioning_plan(path: Path) -> ProviderSupplyProvisioningPlan:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read provider supply provisioning plan: {path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid provider supply provisioning plan JSON: {path}") from exc
    try:
        return ProviderSupplyProvisioningPlan.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"invalid provider supply provisioning plan schema: {path}: {exc}") from exc


def dumps_provider_supply_provisioning_plan(plan: ProviderSupplyProvisioningPlan) -> str:
    return json.dumps(plan.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True) + "\n"


def dumps_provider_supply_provisioning_apply_result(result: ProviderSupplyProvisioningApplyResult) -> str:
    return json.dumps(result.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True) + "\n"


def apply_provider_supply_provisioning_plan(
    plan: ProviderSupplyProvisioningPlan,
    *,
    credentials: dict[str, dict[str, str]] | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> ProviderSupplyProvisioningApplyResult:
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    creds = credentials if credentials is not None else ({} if dry_run else load_credentials())
    action_outputs: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    applied = skipped = failed = 0
    for action in plan.actions:
        result = _apply_one_action(action, credentials=creds, dry_run=dry_run, action_outputs=action_outputs)
        results.append(result)
        status = result.get("status")
        if status == "applied":
            applied += 1
            if isinstance(result.get("response"), dict):
                action_outputs[action.action_id] = dict(result["response"])
        elif status == "failed":
            failed += 1
        else:
            skipped += 1
    return ProviderSupplyProvisioningApplyResult(
        generated_at=generated_at,
        dry_run=dry_run,
        plan_action_count=len(plan.actions),
        applied_count=applied,
        skipped_count=skipped,
        failed_count=failed,
        results=results,
    )


def _plan(
    config_dir: Path,
    policy: Policy,
    generated_at: datetime,
    actions: list[ProviderSupplyProvisioningAction],
    blockers: list[dict[str, Any]],
) -> ProviderSupplyProvisioningPlan:
    return ProviderSupplyProvisioningPlan(
        generated_at=generated_at,
        config_dir=str(config_dir),
        status="actions_proposed" if actions else ("blocked" if blockers else "no_actions"),
        action_count=len(actions),
        blockers=blockers,
        policy=policy.provider_supply_provisioning.model_dump(mode="json"),
        actions=actions,
    )


def _load_supply_source(
    config_dir: Path,
    *,
    tuple_name: str | None,
    candidate_name: str | None,
    review_path: Path | None,
) -> _SupplySource:
    if tuple_name and (candidate_name or review_path):
        raise ValueError("--tuple cannot be combined with --candidate or --review-json")
    if not tuple_name and not candidate_name and review_path is None:
        raise ValueError("one of --tuple, --candidate, or --review-json is required")
    config = load_config(config_dir)
    if tuple_name:
        tuple_spec = config.tuples.get(tuple_name)
        if tuple_spec is None:
            raise ValueError(f"unknown tuple: {tuple_name}")
        return _SupplySource(
            tuple_name=tuple_name,
            source_kind="tuple",
            source_path=_tuple_source_path(config_dir, tuple_name),
            payload=_tuple_payload(tuple_spec),
        )
    selected_candidate = candidate_name or _candidate_from_review(review_path)
    candidates = load_tuple_candidate_payloads(config_dir)
    for candidate in candidates:
        if candidate.get("name") == selected_candidate:
            tuple_payload = _tuple_from_candidate(candidate, active_config=config)
            return _SupplySource(
                tuple_name=str(tuple_payload["name"]),
                source_kind="candidate",
                source_path=str(candidate.get("_path") or candidate.get("_source") or ""),
                payload=tuple_payload,
            )
    raise ValueError(f"unknown tuple candidate: {selected_candidate}")


def _candidate_from_review(review_path: Path | None) -> str:
    if review_path is None:
        raise ValueError("--review-json is required to select a candidate from review output")
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"failed to read review JSON: {review_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid review JSON: {review_path}") from exc
    matches = review.get("tuple_candidate_matches")
    if not isinstance(matches, list) or not matches:
        raise ValueError("review JSON does not contain tuple_candidate_matches")
    first = matches[0]
    if not isinstance(first, Mapping) or not _string(first.get("name")):
        raise ValueError("first tuple_candidate_matches entry does not contain a candidate name")
    return str(first["name"])


def _tuple_payload(tuple_spec: ExecutionTupleSpec) -> dict[str, Any]:
    return tuple_spec.model_dump(mode="json", exclude_none=True)


def _tuple_source_path(config_dir: Path, tuple_name: str) -> str | None:
    split_worker = config_dir / "workers" / f"{tuple_name}.yml"
    split_surface = config_dir / "surfaces" / f"{tuple_name}.yml"
    if split_worker.exists() or split_surface.exists():
        paths = [str(path) for path in (split_surface, split_worker) if path.exists()]
        return ",".join(paths)
    legacy = config_dir / "tuples" / f"{tuple_name}.yml"
    return str(legacy) if legacy.exists() else None


def _supply_policy_blockers(*, requested_workers_min: int, requested_workers_max: int, policy: Policy) -> list[str]:
    action_policy = policy.provider_supply_provisioning.create_runpod_serverless_endpoint
    blockers: list[str] = []
    if requested_workers_min < 0:
        blockers.append("workers_min_must_be_non_negative")
    if requested_workers_max < 0:
        blockers.append("workers_max_must_be_non_negative")
    if requested_workers_min > requested_workers_max:
        blockers.append("workers_min_exceeds_workers_max")
    if requested_workers_max > action_policy.max_workers_max:
        blockers.append("workers_max_exceeds_policy_limit")
    if requested_workers_min > 0 and not action_policy.allow_warm_workers:
        blockers.append("warm_workers_not_allowed_by_policy")
    return blockers


def _source_snapshot(source: _SupplySource, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tuple": source.tuple_name,
        "source_kind": source.source_kind,
        "source_path": source.source_path,
        "adapter": payload.get("adapter"),
        "execution_surface": payload.get("execution_surface"),
        "gpu": payload.get("gpu"),
        "vram_gb": payload.get("vram_gb"),
        "image": payload.get("image"),
        "model": payload.get("model"),
        "model_ref": payload.get("model_ref"),
        "engine_ref": payload.get("engine_ref"),
        "max_model_len": payload.get("max_model_len"),
        "cost_per_second": payload.get("cost_per_second"),
        "configured_price_source": payload.get("configured_price_source"),
        "configured_price_observed_at": payload.get("configured_price_observed_at"),
        "configured_price_ttl_seconds": payload.get("configured_price_ttl_seconds"),
        "provider_params": payload.get("provider_params") or {},
    }


def _runpod_template_request(payload: Mapping[str, Any], *, name: str, container_disk_gb: int) -> dict[str, Any]:
    provider_params = payload.get("provider_params") if isinstance(payload.get("provider_params"), Mapping) else {}
    worker_env = provider_params.get("worker_env") if isinstance(provider_params, Mapping) else {}
    request = {
        "imageName": _string(payload.get("image")),
        "name": _truncate_name(name),
        "category": "NVIDIA",
        "containerDiskInGb": int(container_disk_gb),
        "dockerEntrypoint": [],
        "dockerStartCmd": [],
        "env": _string_mapping(worker_env if isinstance(worker_env, Mapping) else {}),
        "isPublic": False,
        "isServerless": True,
        "ports": [],
        "readme": "Generated by gpucall provider supply provisioning. Production routing still requires validation evidence.",
    }
    return request


def _runpod_endpoint_request(
    payload: Mapping[str, Any],
    *,
    name: str,
    template_id: str | None,
    gpu_type_ids: list[str],
    workers_min: int,
    workers_max: int,
    network_volume_id: str | None,
    data_center_ids: list[str] | None,
    policy: Policy,
) -> dict[str, Any]:
    action_policy = policy.provider_supply_provisioning.create_runpod_serverless_endpoint
    request: dict[str, Any] = {
        "name": _truncate_name(name),
        "computeType": "GPU",
        "gpuCount": _gpu_count(payload.get("gpu")) or action_policy.default_gpu_count,
        "gpuTypeIds": gpu_type_ids,
        "workersMin": int(workers_min),
        "workersMax": int(workers_max),
        "idleTimeout": action_policy.default_idle_timeout_seconds,
        "scalerType": action_policy.default_scaler_type,
        "scalerValue": action_policy.default_scaler_value,
    }
    if template_id:
        request["templateId"] = template_id
    if _string(network_volume_id):
        request["networkVolumeId"] = _string(network_volume_id)
    clean_data_centers = _clean_strings(data_center_ids)
    if clean_data_centers:
        request["dataCenterIds"] = clean_data_centers
    return request


def _target_patch(config_dir: Path, tuple_name: str, endpoint_action_id: str) -> dict[str, Any]:
    worker_path = config_dir / "workers" / f"{tuple_name}.yml"
    legacy_tuple_path = config_dir / "tuples" / f"{tuple_name}.yml"
    relative_path = f"workers/{tuple_name}.yml" if worker_path.exists() or not legacy_tuple_path.exists() else f"tuples/{tuple_name}.yml"
    return {
        "kind": "worker_target",
        "config_dir_relative_path": relative_path,
        "json_pointer": "/target",
        "value_from_action": {"action_id": endpoint_action_id, "response_field": "id"},
        "preconditions": ["target is absent or an unconfigured placeholder", "live readiness and validation evidence have passed"],
    }


def _apply_one_action(
    action: ProviderSupplyProvisioningAction,
    *,
    credentials: dict[str, dict[str, str]],
    dry_run: bool,
    action_outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action_id": action.action_id,
        "action": action.action,
        "provider": action.provider,
        "resource_type": action.resource_type,
        "resource_id": action.resource_id,
        "tuple": action.tuple,
    }
    if dry_run:
        return {**base, "status": "dry_run", "reason": "dry run; no provider mutation performed"}
    if action.apply_blocked_reasons or not action.apply_supported:
        return {
            **base,
            "status": "skipped",
            "reason": "action is not apply-supported",
            "apply_blocked_reasons": action.apply_blocked_reasons or ["unsupported action"],
        }
    api_key = credentials.get("runpod", {}).get("api_key")
    if not api_key:
        return {**base, "status": "failed", "reason": "missing RunPod API key"}
    try:
        if action.action == "create_runpod_template":
            response = _runpod_create_template(action.request, api_key)
            return {**base, "status": "applied", "response": response}
        if action.action == "create_runpod_serverless_endpoint":
            request = _resolve_endpoint_request(action, action_outputs)
            response = _runpod_create_endpoint(request, api_key)
            return {
                **base,
                "status": "applied",
                "response": response,
                "materialized_config_patch": _materialized_config_patch(action, response),
            }
    except Exception as exc:
        return {**base, "status": "failed", "reason": str(exc)}
    return {**base, "status": "skipped", "reason": "unsupported provider supply provisioning action"}


def _resolve_endpoint_request(
    action: ProviderSupplyProvisioningAction,
    action_outputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    request = dict(action.request)
    if request.get("templateId"):
        return request
    template_action_id = _string(action.parameters.get("template_id_from_action"))
    template_response = action_outputs.get(template_action_id)
    template_id = _string(template_response.get("id") if isinstance(template_response, Mapping) else None)
    if not template_id:
        raise ValueError(f"missing template id from dependency action {template_action_id!r}")
    request["templateId"] = template_id
    return request


def _materialized_config_patch(
    action: ProviderSupplyProvisioningAction,
    response: Mapping[str, Any],
) -> list[dict[str, Any]]:
    endpoint_id = _string(response.get("id"))
    if not endpoint_id:
        raise ValueError(f"provider response missing resource id for action {action.action_id}")
    patches: list[dict[str, Any]] = []
    for patch in action.post_apply_config_patch:
        materialized = dict(patch)
        materialized["value"] = endpoint_id
        patches.append(materialized)
    return patches


def _runpod_create_template(request: Mapping[str, Any], api_key: str, *, base_url: str = RUNPOD_REST_API_BASE) -> dict[str, Any]:
    response = requests_session().post(
        f"{base_url.rstrip('/')}/templates",
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json", "accept": "application/json"},
        json=dict(request),
        timeout=30,
    )
    return json_or_error(response, "RunPod template provisioning failed")


def _runpod_create_endpoint(request: Mapping[str, Any], api_key: str, *, base_url: str = RUNPOD_REST_API_BASE) -> dict[str, Any]:
    response = requests_session().post(
        f"{base_url.rstrip('/')}/endpoints",
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json", "accept": "application/json"},
        json=dict(request),
        timeout=30,
    )
    return json_or_error(response, "RunPod endpoint provisioning failed")


def _action_id(action: str, tuple_name: str, resource_name: str) -> str:
    digest = hashlib.sha256(f"{action}:{tuple_name}:{resource_name}".encode("utf-8")).hexdigest()[:16]
    return f"supply-{digest}"


def _runpod_gpu_type_ids(gpu_ref: Any) -> list[str]:
    base = re.sub(r"\s*x\d+$", "", _string(gpu_ref), flags=re.IGNORECASE)
    return list(_RUNPOD_GPU_TYPE_IDS_BY_GPU_REF.get(base.upper(), []))


def _gpu_count(gpu_ref: Any) -> int | None:
    match = re.search(r"x(\d+)$", _string(gpu_ref))
    if match:
        return max(1, int(match.group(1)))
    return None


def _string_mapping(value: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): str(raw) for key, raw in sorted(value.items()) if raw is not None}


def _clean_strings(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [value.strip() for value in values if value and value.strip()]


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe or "tuple"


def _truncate_name(value: str) -> str:
    safe = _safe_name(value)
    return safe[:191]


def _string(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()
