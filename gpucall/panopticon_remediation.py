from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, ValidationError, model_validator

from gpucall.config import default_state_dir
from gpucall.credentials import load_credentials
from gpucall.domain import PanopticonRemediationActionPolicy, PanopticonRemediationPolicy, Policy, RemediationMode
from gpucall.execution_surfaces.managed_endpoint import RUNPOD_REST_API_BASE, json_or_error, requests_session
from gpucall.panopticon import default_panopticon_path, load_panopticon_evidence
from gpucall.provider_contracts import CLOUD_PROVIDER_FAMILIES


SUPPORTED_PROVIDER_MUTATION_REMEDIATION_PROVIDERS = ("runpod",)
UNSUPPORTED_PROVIDER_MUTATION_REMEDIATION_PROVIDERS = tuple(
    provider for provider in CLOUD_PROVIDER_FAMILIES if provider not in SUPPORTED_PROVIDER_MUTATION_REMEDIATION_PROVIDERS
)
RemediationActionName = Literal[
    "exclude_from_routing",
    "scale_workers_min_to_zero",
    "delete_endpoint",
    "delete_network_volume",
]
RemediationResourceType = Literal["tuple", "endpoint", "network_volume"]
RemediationProvider = Literal["gateway", "runpod"]
ApplyStatus = Literal["dry_run", "applied", "skipped", "failed"]


class PanopticonRemediationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    action: RemediationActionName
    provider: RemediationProvider
    resource_type: RemediationResourceType
    resource_id: str
    tuple: str | None = None
    adapter: str | None = None
    reason: str
    policy_mode: RemediationMode
    requires_approval: bool
    provider_mutation: bool
    destructive: bool
    apply_supported: bool
    apply_blocked_reasons: list[str] = Field(default_factory=list)
    preconditions: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    source_finding: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action_contract(self) -> "PanopticonRemediationAction":
        if not self.action_id.strip():
            raise ValueError("action_id must be non-empty")
        if not self.resource_id.strip():
            raise ValueError("resource_id must be non-empty")
        if self.policy_mode == "disabled":
            raise ValueError("disabled remediation actions must not be materialized")
        if self.apply_supported and self.apply_blocked_reasons:
            raise ValueError("apply_supported actions must not carry apply_blocked_reasons")
        if self.action in {"delete_endpoint", "delete_network_volume"} and not self.destructive:
            raise ValueError("delete actions must be destructive")
        if self.action == "exclude_from_routing" and self.provider_mutation:
            raise ValueError("exclude_from_routing must not be a provider mutation")
        return self


class PanopticonRemediationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    phase: Literal["provider-panopticon-remediation-plan"] = "provider-panopticon-remediation-plan"
    generated_at: datetime
    source_snapshot_path: str | None = None
    non_generation_probe_only: Literal[True] = True
    status: Literal["no_actions", "actions_proposed"]
    action_count: NonNegativeInt
    policy: dict[str, Any]
    actions: list[PanopticonRemediationAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan_count(self) -> "PanopticonRemediationPlan":
        if self.action_count != len(self.actions):
            raise ValueError("action_count must equal len(actions)")
        expected_status = "actions_proposed" if self.actions else "no_actions"
        if self.status != expected_status:
            raise ValueError("status must reflect whether actions are present")
        return self


class PanopticonRemediationApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    phase: Literal["provider-panopticon-remediation-apply"] = "provider-panopticon-remediation-apply"
    generated_at: datetime
    dry_run: bool
    non_generation_probe_only: Literal[True] = True
    plan_action_count: NonNegativeInt
    applied_count: NonNegativeInt
    skipped_count: NonNegativeInt
    failed_count: NonNegativeInt
    results: list[dict[str, Any]]


def default_remediation_plan_path(*, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return default_state_dir() / "panopticon" / f"remediation-plan-{stamp}.json"


def build_remediation_plan(
    snapshot: Mapping[str, Mapping[str, Any]],
    policy: Policy,
    *,
    source_snapshot_path: Path | None = None,
    now: datetime | None = None,
) -> PanopticonRemediationPlan:
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    remediation_policy = policy.panopticon_remediation
    actions: list[PanopticonRemediationAction] = []
    seen: set[tuple[str, str, str]] = set()

    for tuple_name, row in sorted(snapshot.items()):
        if not isinstance(row, Mapping):
            continue
        row_tuple = str(row.get("tuple") or tuple_name)
        adapter = str(row.get("adapter") or "")
        findings = [item for item in row.get("findings") or [] if isinstance(item, Mapping)]
        if _row_needs_routing_exclusion(row, findings):
            action = _exclude_from_routing_action(row_tuple, adapter, row, findings, remediation_policy)
            if action is not None:
                _append_action(actions, seen, action)
        for finding in findings:
            scale = _scale_workers_min_to_zero_action(row_tuple, adapter, finding, remediation_policy)
            if scale is not None:
                _append_action(actions, seen, scale)
            volume_delete = _delete_network_volume_action(row_tuple, adapter, finding, remediation_policy)
            if volume_delete is not None:
                _append_action(actions, seen, volume_delete)
            endpoint_delete = _delete_endpoint_action(row_tuple, adapter, finding, remediation_policy)
            if endpoint_delete is not None:
                _append_action(actions, seen, endpoint_delete)

    return PanopticonRemediationPlan(
        generated_at=generated_at,
        source_snapshot_path=str(source_snapshot_path) if source_snapshot_path is not None else None,
        status="actions_proposed" if actions else "no_actions",
        action_count=len(actions),
        policy=_policy_summary(remediation_policy),
        actions=actions,
    )


def build_remediation_plan_from_path(
    *,
    policy: Policy,
    panopticon_path: Path | None = None,
    now: datetime | None = None,
) -> PanopticonRemediationPlan:
    path = panopticon_path or default_panopticon_path()
    snapshot = load_panopticon_evidence(path, now=now)
    return build_remediation_plan(snapshot, policy, source_snapshot_path=path, now=now)


def load_remediation_plan(path: Path) -> PanopticonRemediationPlan:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read panopticon remediation plan: {path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid panopticon remediation plan JSON: {path}") from exc
    try:
        return PanopticonRemediationPlan.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"invalid panopticon remediation plan schema: {path}: {exc}") from exc


def dumps_remediation_plan(plan: PanopticonRemediationPlan) -> str:
    return json.dumps(plan.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True) + "\n"


def dumps_apply_result(result: PanopticonRemediationApplyResult) -> str:
    return json.dumps(result.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True) + "\n"


def apply_remediation_plan(
    plan: PanopticonRemediationPlan,
    *,
    credentials: dict[str, dict[str, str]] | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> PanopticonRemediationApplyResult:
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    creds = credentials if credentials is not None else ({} if dry_run else load_credentials())
    results: list[dict[str, Any]] = []
    applied = skipped = failed = 0
    for action in plan.actions:
        result = _apply_one_action(action, credentials=creds, dry_run=dry_run)
        results.append(result)
        status = result.get("status")
        if status == "applied":
            applied += 1
        elif status == "failed":
            failed += 1
        else:
            skipped += 1
    return PanopticonRemediationApplyResult(
        generated_at=generated_at,
        dry_run=dry_run,
        plan_action_count=len(plan.actions),
        applied_count=applied,
        skipped_count=skipped,
        failed_count=failed,
        results=results,
    )


def _append_action(
    actions: list[PanopticonRemediationAction],
    seen: set[tuple[str, str, str]],
    action: PanopticonRemediationAction,
) -> None:
    key = (action.action, action.provider, action.resource_id)
    if key in seen:
        return
    seen.add(key)
    actions.append(action)


def _row_needs_routing_exclusion(row: Mapping[str, Any], findings: list[Mapping[str, Any]]) -> bool:
    if row.get("status") == "blocked" or row.get("panopticon_stale") is True:
        return True
    return any(_finding_blocks_routing(finding) for finding in findings)


def _finding_blocks_routing(finding: Mapping[str, Any]) -> bool:
    return finding.get("severity") == "error" or finding.get("live_stock_state") == "unavailable"


def _exclude_from_routing_action(
    tuple_name: str,
    adapter: str,
    row: Mapping[str, Any],
    findings: list[Mapping[str, Any]],
    policy: PanopticonRemediationPolicy,
) -> PanopticonRemediationAction | None:
    action_policy = policy.exclude_from_routing
    if action_policy.mode == "disabled":
        return None
    source_finding = _first_blocking_finding(findings)
    reason = str(source_finding.get("reason") or "provider panopticon marks tuple unavailable")
    if row.get("panopticon_stale") is True:
        reason = "provider panopticon evidence is stale"
    return _action(
        action="exclude_from_routing",
        provider="gateway",
        resource_type="tuple",
        resource_id=tuple_name,
        tuple_name=tuple_name,
        adapter=adapter,
        reason=reason,
        action_policy=action_policy,
        provider_mutation=False,
        destructive=False,
        apply_supported=False,
        apply_blocked_reasons=["gateway already fails closed from panopticon evidence"],
        source_finding=source_finding,
    )


def _scale_workers_min_to_zero_action(
    tuple_name: str,
    adapter: str,
    finding: Mapping[str, Any],
    policy: PanopticonRemediationPolicy,
) -> PanopticonRemediationAction | None:
    details = _finding_details(finding)
    if details.get("live_reason") != "workers_min_positive":
        return None
    endpoint_id = _string(details.get("endpoint_id"))
    workers_min = _non_negative_int(details.get("workers_min"))
    if not endpoint_id or workers_min is None or workers_min <= 0:
        return None
    action_policy = policy.scale_workers_min_to_zero
    if action_policy.mode == "disabled":
        return None
    active_workers = _non_negative_int(details.get("active_workers")) or 0
    active_pods = _non_negative_int(details.get("active_pods")) or 0
    blocked = []
    if action_policy.require_no_inflight_jobs and (active_workers > 0 or active_pods > 0):
        blocked.append("inflight_workers_or_pods_present")
    if action_policy.require_no_low_latency_sla and _truthy(details.get("low_latency_sla")):
        blocked.append("low_latency_sla_present")
    return _action(
        action="scale_workers_min_to_zero",
        provider="runpod",
        resource_type="endpoint",
        resource_id=endpoint_id,
        tuple_name=tuple_name,
        adapter=adapter,
        reason="RunPod Serverless endpoint has unapproved workersMin > 0",
        action_policy=action_policy,
        provider_mutation=True,
        destructive=False,
        apply_supported=not blocked,
        apply_blocked_reasons=blocked,
        preconditions={
            "workers_min_observed": workers_min,
            "active_workers": active_workers,
            "active_pods": active_pods,
            "require_no_inflight_jobs": action_policy.require_no_inflight_jobs,
            "require_no_low_latency_sla": action_policy.require_no_low_latency_sla,
        },
        parameters={"workersMin": 0},
        source_finding=finding,
    )


def _delete_network_volume_action(
    tuple_name: str,
    adapter: str,
    finding: Mapping[str, Any],
    policy: PanopticonRemediationPolicy,
) -> PanopticonRemediationAction | None:
    details = _finding_details(finding)
    if details.get("resource_type") != "network_volume":
        return None
    if details.get("live_reason") != "persistent_storage_unattached_undeclared":
        return None
    volume_id = _string(details.get("resource_id"))
    if not volume_id:
        return None
    action_policy = policy.delete_network_volume
    if action_policy.mode == "disabled":
        return None
    return _action(
        action="delete_network_volume",
        provider="runpod",
        resource_type="network_volume",
        resource_id=volume_id,
        tuple_name=tuple_name if not tuple_name.startswith("runpod-network-volume-") else None,
        adapter=adapter,
        reason="RunPod network volume is unattached and undeclared",
        action_policy=action_policy,
        provider_mutation=True,
        destructive=True,
        apply_supported=False,
        apply_blocked_reasons=["delete_network_volume is not supported by gpucall v2 apply"],
        preconditions={
            "attached_endpoint_count": _non_negative_int(details.get("attached_endpoint_count")) or 0,
            "declared_by_tuple_count": _non_negative_int(details.get("declared_by_tuple_count")) or 0,
            "content_inventory_status": details.get("content_inventory_status"),
        },
        source_finding=finding,
    )


def _delete_endpoint_action(
    tuple_name: str,
    adapter: str,
    finding: Mapping[str, Any],
    policy: PanopticonRemediationPolicy,
) -> PanopticonRemediationAction | None:
    details = _finding_details(finding)
    if details.get("resource_type") not in {"endpoint", "serverless_endpoint"}:
        return None
    if details.get("live_reason") not in {"endpoint_unattached_undeclared", "endpoint_stale_undeclared", "unmanaged_endpoint_stale"}:
        return None
    endpoint_id = _string(details.get("resource_id") or details.get("endpoint_id"))
    if not endpoint_id:
        return None
    action_policy = policy.delete_endpoint
    if action_policy.mode == "disabled":
        return None
    return _action(
        action="delete_endpoint",
        provider="runpod",
        resource_type="endpoint",
        resource_id=endpoint_id,
        tuple_name=tuple_name,
        adapter=adapter,
        reason="RunPod endpoint is stale or undeclared",
        action_policy=action_policy,
        provider_mutation=True,
        destructive=True,
        apply_supported=False,
        apply_blocked_reasons=["delete_endpoint is not supported by gpucall v2 apply"],
        source_finding=finding,
    )


def _action(
    *,
    action: RemediationActionName,
    provider: RemediationProvider,
    resource_type: RemediationResourceType,
    resource_id: str,
    tuple_name: str | None,
    adapter: str | None,
    reason: str,
    action_policy: PanopticonRemediationActionPolicy,
    provider_mutation: bool,
    destructive: bool,
    apply_supported: bool,
    apply_blocked_reasons: list[str] | None = None,
    preconditions: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
    source_finding: Mapping[str, Any] | None = None,
) -> PanopticonRemediationAction:
    blocked = apply_blocked_reasons or []
    action_id = _action_id(action, provider, resource_id)
    return PanopticonRemediationAction(
        action_id=action_id,
        action=action,
        provider=provider,
        resource_type=resource_type,
        resource_id=resource_id,
        tuple=tuple_name,
        adapter=adapter,
        reason=reason,
        policy_mode=action_policy.mode,
        requires_approval=action_policy.mode == "approval_required" or destructive,
        provider_mutation=provider_mutation,
        destructive=destructive,
        apply_supported=apply_supported,
        apply_blocked_reasons=blocked,
        preconditions=preconditions or {},
        parameters=parameters or {},
        source_finding=dict(source_finding or {}),
    )


def _first_blocking_finding(findings: list[Mapping[str, Any]]) -> dict[str, Any]:
    for finding in findings:
        if _finding_blocks_routing(finding):
            return dict(finding)
    return dict(findings[0]) if findings else {}


def _finding_details(finding: Mapping[str, Any]) -> dict[str, Any]:
    details = finding.get("details")
    raw = finding.get("raw")
    merged: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        merged.update(raw)
    if isinstance(details, Mapping):
        merged.update(details)
    return merged


def _policy_summary(policy: PanopticonRemediationPolicy) -> dict[str, Any]:
    return policy.model_dump(mode="json")


def _action_id(action: str, provider: str, resource_id: str) -> str:
    digest = hashlib.sha256(f"{action}:{provider}:{resource_id}".encode("utf-8")).hexdigest()[:16]
    return f"remediate-{digest}"


def _apply_one_action(
    action: PanopticonRemediationAction,
    *,
    credentials: dict[str, dict[str, str]],
    dry_run: bool,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action_id": action.action_id,
        "action": action.action,
        "provider": action.provider,
        "resource_type": action.resource_type,
        "resource_id": action.resource_id,
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
    if action.action != "scale_workers_min_to_zero" or action.provider != "runpod":
        return {**base, "status": "skipped", "reason": "only RunPod workersMin remediation is supported in gpucall v2"}
    api_key = credentials.get("runpod", {}).get("api_key")
    if not api_key:
        return {**base, "status": "failed", "reason": "missing RunPod API key"}
    try:
        response = _runpod_patch_endpoint_workers_min_to_zero(action.resource_id, api_key)
    except Exception as exc:
        return {**base, "status": "failed", "reason": str(exc)}
    return {**base, "status": "applied", "response": response}


def _runpod_patch_endpoint_workers_min_to_zero(endpoint_id: str, api_key: str, *, base_url: str = RUNPOD_REST_API_BASE) -> dict[str, Any]:
    response = requests_session().patch(
        f"{base_url.rstrip('/')}/endpoints/{endpoint_id}",
        headers={"authorization": f"Bearer {api_key}", "content-type": "application/json", "accept": "application/json"},
        json={"workersMin": 0},
        timeout=30,
    )
    return json_or_error(response, "RunPod endpoint workersMin remediation failed")


def _non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _string(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
