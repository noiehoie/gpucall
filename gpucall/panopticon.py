from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt, field_validator, model_validator

from gpucall.config import default_state_dir


PANOPTICON_SCHEMA_VERSION = 1
PANOPTICON_TTL_BY_DIMENSION = {
    "price": 3600,
    "contract": 86400,
    "credential": 300,
    "cost": 300,
    "endpoint": 300,
    "stock": 300,
    "capacity": 300,
    "health": 300,
    "worker": 300,
    "queue": 300,
    "model": 300,
    "models": 300,
    "live_tuple_catalog": 300,
    "panopticon": 300,
}

PanopticonDimension = Literal[
    "price",
    "contract",
    "credential",
    "cost",
    "endpoint",
    "stock",
    "capacity",
    "health",
    "worker",
    "queue",
    "model",
    "models",
    "live_tuple_catalog",
    "panopticon",
]
PanopticonSeverity = Literal["error", "info"]
PanopticonStatus = Literal["not_checked", "unknown", "live_revalidated", "blocked"]
PanopticonStockState = Literal["available", "unavailable", "unknown"]


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finite_non_bool_float(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid numeric evidence value")
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(candidate):
        raise ValueError("numeric evidence value must be finite")
    return value


class PanopticonFindingDetails(BaseModel):
    """Structured provider evidence extracted from provider-specific raw payloads."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    provider_family: str | None = None
    account_ref: str | None = None
    region: str | None = None
    gpu_type: str | None = None
    endpoint_id: str | None = None
    endpoint_name: str | None = None
    endpoint_status: str | None = None
    target: str | None = None
    template_id: str | None = None
    template_name: str | None = None
    image: str | None = None
    model_name: str | None = None
    served_model_name: str | None = None
    max_model_len: NonNegativeInt | None = None
    configured_model_len: NonNegativeInt | None = None
    http_status: NonNegativeInt | None = None
    worker_running: NonNegativeInt | None = None
    worker_ready: NonNegativeInt | None = None
    worker_idle: NonNegativeInt | None = None
    worker_in_queue: NonNegativeInt | None = None
    workers_min: NonNegativeInt | None = None
    workers_max: NonNegativeInt | None = None
    active_workers: NonNegativeInt | None = None
    active_pods: NonNegativeInt | None = None
    queue_depth: NonNegativeInt | None = None
    served_model_count: NonNegativeInt | None = None
    probe_elapsed_ms: NonNegativeInt | None = None
    price_per_second: NonNegativeFloat | None = None
    price_source: str | None = None
    live_reason: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    source_url: str | None = None

    @field_validator(
        "max_model_len",
        "configured_model_len",
        "http_status",
        "worker_running",
        "worker_ready",
        "worker_idle",
        "worker_in_queue",
        "workers_min",
        "workers_max",
        "active_workers",
        "active_pods",
        "queue_depth",
        "served_model_count",
        "probe_elapsed_ms",
        "price_per_second",
        mode="before",
    )
    @classmethod
    def reject_bool_and_infinite_numeric_details(cls, value: Any) -> Any:
        return _finite_non_bool_float(value)


class PanopticonFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tuple: str
    adapter: str
    dimension: PanopticonDimension
    severity: PanopticonSeverity = "info"
    reason: str | None = None
    field: str | None = None
    source: str | None = None
    live_price_per_second: NonNegativeFloat | None = None
    live_price_source: str | None = None
    live_stock_state: PanopticonStockState | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    details: PanopticonFindingDetails | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", "expires_at")
    @classmethod
    def normalize_optional_datetime(cls, value: datetime | None) -> datetime | None:
        return _utc_datetime(value) if value is not None else None

    @field_validator("live_price_per_second", mode="before")
    @classmethod
    def reject_bool_and_infinite_price(cls, value: Any) -> Any:
        return _finite_non_bool_float(value)

    @field_validator("tuple", "adapter")
    @classmethod
    def require_non_empty_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_finding_contract(self) -> "PanopticonFinding":
        if self.observed_at and self.expires_at and self.expires_at < self.observed_at:
            raise ValueError("expires_at must be >= observed_at")
        if self.severity == "error" and not (self.reason or self.field or self._live_reason()):
            raise ValueError("error finding requires reason, field, or details.live_reason/raw.live_reason")
        if self.dimension == "price" and self.severity == "info":
            if self.live_price_per_second is None:
                raise ValueError("price info finding requires live_price_per_second")
            if not (self.live_price_source or self.source):
                raise ValueError("price info finding requires live_price_source or source")
        if self.dimension == "stock" and self.severity == "info" and self.live_stock_state is None:
            raise ValueError("stock info finding requires live_stock_state")
        return self

    def _live_reason(self) -> str | None:
        if self.details and self.details.live_reason:
            return self.details.live_reason
        value = self.raw.get("live_reason")
        return str(value) if value not in (None, "") else None


class PanopticonInputRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tuple: str | None = None
    adapter: str | None = None
    status: PanopticonStatus | None = None
    checked: bool = True
    dimensions: list[PanopticonDimension] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    catalog_validator: bool | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    ttl_seconds: NonNegativeInt | None = None


class PanopticonRow(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    tuple: str
    adapter: str
    status: PanopticonStatus
    checked: bool = True
    dimensions: list[PanopticonDimension] = Field(default_factory=list)
    findings: list[PanopticonFinding] = Field(default_factory=list)
    observed_at: datetime
    expires_at: datetime
    ttl_seconds: NonNegativeInt
    catalog_validator: bool | None = None

    @field_validator("observed_at", "expires_at")
    @classmethod
    def normalize_datetime(cls, value: datetime) -> datetime:
        return _utc_datetime(value)

    @field_validator("tuple", "adapter")
    @classmethod
    def require_non_empty_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_row_contract(self) -> "PanopticonRow":
        if self.expires_at < self.observed_at:
            raise ValueError("expires_at must be >= observed_at")
        expected_ttl = int((self.expires_at - self.observed_at).total_seconds())
        if int(self.ttl_seconds) != expected_ttl:
            raise ValueError("ttl_seconds must equal expires_at - observed_at")
        expected_dimensions = sorted({finding.dimension for finding in self.findings})
        if self.dimensions != expected_dimensions:
            raise ValueError("dimensions must equal sorted unique finding dimensions")
        for finding in self.findings:
            if finding.tuple != self.tuple:
                raise ValueError("finding tuple must match row tuple")
            if finding.adapter != self.adapter:
                raise ValueError("finding adapter must match row adapter")
        has_blocker = any(f.severity == "error" or f.live_stock_state == "unavailable" for f in self.findings)
        if self.status in {"live_revalidated", "blocked"} and not self.findings:
            raise ValueError("live_revalidated/blocked row requires at least one finding")
        if has_blocker and self.status != "blocked":
            raise ValueError("status must be blocked when findings contain errors or unavailable stock")
        if self.status == "live_revalidated" and not self.checked:
            raise ValueError("live_revalidated row must be checked")
        return self


class PanopticonPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = PANOPTICON_SCHEMA_VERSION
    updated_at: datetime
    tuples: dict[str, PanopticonRow] = Field(default_factory=dict)

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        return _utc_datetime(value)

    @model_validator(mode="after")
    def validate_payload_contract(self) -> "PanopticonPayload":
        for key, row in self.tuples.items():
            if key != row.tuple:
                raise ValueError("tuple mapping key must match row tuple")
        return self


def default_panopticon_path() -> Path:
    return default_state_dir() / "catalog" / "provider-panopticon.json"


def load_panopticon_evidence(
    path: Path | None = None,
    *,
    now: datetime | None = None,
    expired_status: Literal["blocked", "unknown"] = "blocked",
    expired_reason: str = "provider panopticon evidence TTL expired",
    allow_legacy_price_finding: bool = False,
) -> dict[str, dict[str, Any]]:
    path = path or default_panopticon_path()
    now = _utc_datetime(now or datetime.now(timezone.utc))
    payload = _read_payload(path, now=now, allow_legacy_price_finding=allow_legacy_price_finding)

    result: dict[str, dict[str, Any]] = {}
    for tuple_name, row in payload.tuples.items():
        expired = row.expires_at <= now
        status = row.status
        findings = [_finding_dump(finding) for finding in row.findings]

        dimensions = list(row.dimensions)
        if expired:
            status = expired_status
            expiry_finding = _expiry_finding(str(tuple_name), _row_dump(row), expired_reason=expired_reason)
            findings = [expiry_finding, *findings]
            dimensions = sorted({*dimensions, str(expiry_finding["dimension"])})

        result[str(tuple_name)] = {
            "tuple": row.tuple,
            "adapter": row.adapter,
            "status": status,
            "checked": row.checked,
            "findings": findings,
            "dimensions": dimensions,
            "observed_at": row.observed_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "ttl_seconds": row.ttl_seconds,
            "panopticon_observed_at": row.observed_at.isoformat(),
            "panopticon_expires_at": row.expires_at.isoformat(),
            "panopticon_ttl_seconds": row.ttl_seconds,
            "panopticon_stale": expired,
            "panopticon_age_seconds": _age_seconds(row.observed_at, now),
        }
        if row.catalog_validator is not None:
            result[str(tuple_name)]["catalog_validator"] = row.catalog_validator
    return result


def store_panopticon_evidence(
    evidence: Mapping[str, Mapping[str, Any]],
    path: Path | None = None,
    *,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> None:
    path = path or default_panopticon_path()
    now = _utc_datetime(now or datetime.now(timezone.utc))
    payload = _read_payload(path, now=now)
    tuples = dict(payload.tuples)

    for tuple_name, raw_row in evidence.items():
        input_row = PanopticonInputRow.model_validate(dict(raw_row))
        row_tuple = input_row.tuple or str(tuple_name)
        if row_tuple != str(tuple_name):
            raise ValueError("tuple mapping key must match row tuple")

        row_findings_raw = input_row.findings
        ttl = int(ttl_seconds if ttl_seconds is not None else _ttl_seconds_for_findings(row_findings_raw))
        existing = tuples.get(str(tuple_name))
        row_adapter = input_row.adapter or _first_mapping_value(row_findings_raw, "adapter") or (existing.adapter if existing else None)
        if not row_adapter:
            raise ValueError("panopticon row requires adapter or at least one finding adapter")

        enriched_findings: list[PanopticonFinding] = []
        for finding_raw in row_findings_raw:
            finding = _canonical_finding(
                finding_raw,
                tuple_name=row_tuple,
                adapter=str(row_adapter),
                observed_at=now,
                ttl_seconds=ttl,
            )
            enriched_findings.append(finding)

        existing_findings = existing.findings if existing else []
        replace_dimensions = {finding.dimension for finding in enriched_findings}
        kept_findings = [finding for finding in existing_findings if finding.dimension not in replace_dimensions]
        merged_findings = _dedupe_findings([*kept_findings, *enriched_findings])
        expires_at = _earliest_finding_expiry(merged_findings) or (now + timedelta(seconds=ttl))
        observed_at = _earliest_finding_observed_at(merged_findings) or now
        effective_ttl = int((expires_at - observed_at).total_seconds())
        if effective_ttl < 0:
            raise ValueError("panopticon row expiry precedes observation time")

        row = PanopticonRow(
            tuple=row_tuple,
            adapter=str(row_adapter),
            status=_stored_status(input_row.status, merged_findings),
            checked=bool(input_row.checked),
            findings=merged_findings,
            dimensions=sorted({finding.dimension for finding in merged_findings}),
            observed_at=observed_at,
            expires_at=expires_at,
            ttl_seconds=effective_ttl,
            catalog_validator=input_row.catalog_validator if input_row.catalog_validator is not None else (existing.catalog_validator if existing else None),
        )
        tuples[str(tuple_name)] = row

    new_payload = PanopticonPayload(
        schema_version=PANOPTICON_SCHEMA_VERSION,
        updated_at=now,
        tuples=tuples,
    )
    _atomic_write_json(path, _payload_dump(new_payload))


def merge_panopticon_evidence(*items: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    now = datetime.now(timezone.utc)
    rows: dict[str, PanopticonRow] = {}
    for evidence in items:
        if not evidence:
            continue
        for tuple_name, raw_row in evidence.items():
            row = _row_from_loaded_or_input(str(tuple_name), raw_row, now=now)
            if tuple_name not in rows:
                rows[str(tuple_name)] = row
                continue
            current = rows[str(tuple_name)]
            findings = _dedupe_findings([*current.findings, *row.findings])
            observed_at = min(current.observed_at, row.observed_at)
            expires_at = min(current.expires_at, row.expires_at)
            rows[str(tuple_name)] = PanopticonRow(
                tuple=current.tuple,
                adapter=current.adapter,
                status=_merged_status(current.status, row.status, findings),
                checked=current.checked or row.checked,
                findings=findings,
                dimensions=sorted({finding.dimension for finding in findings}),
                observed_at=observed_at,
                expires_at=expires_at,
                ttl_seconds=max(0, int((expires_at - observed_at).total_seconds())),
                catalog_validator=current.catalog_validator if current.catalog_validator is not None else row.catalog_validator,
            )

    return {name: _merged_row_dump(row, now=now) for name, row in rows.items()}


def _read_payload(path: Path, *, now: datetime, allow_legacy_price_finding: bool = False) -> PanopticonPayload:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PanopticonPayload(schema_version=PANOPTICON_SCHEMA_VERSION, updated_at=now, tuples={})
    except OSError as exc:
        raise RuntimeError(f"failed to read provider panopticon snapshot: {path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid provider panopticon snapshot JSON: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid provider panopticon snapshot shape: {path}")
    if allow_legacy_price_finding:
        payload = _normalize_legacy_price_payload(dict(payload), now=now)
    return PanopticonPayload.model_validate(payload)


def _normalize_legacy_price_payload(payload: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    if "updated_at" not in payload:
        payload["updated_at"] = now.isoformat()
    tuples = payload.get("tuples")
    if not isinstance(tuples, Mapping):
        return payload
    normalized: dict[str, Any] = {}
    for tuple_name, raw_row in tuples.items():
        if not isinstance(raw_row, Mapping) or "finding" not in raw_row or "findings" in raw_row:
            normalized[str(tuple_name)] = raw_row
            continue
        finding = raw_row.get("finding")
        if not isinstance(finding, Mapping):
            normalized[str(tuple_name)] = raw_row
            continue
        row = dict(raw_row)
        row.pop("finding", None)
        row.setdefault("tuple", str(tuple_name))
        row.setdefault("adapter", str(finding.get("adapter") or "legacy-price-cache"))
        row.setdefault("status", "live_revalidated")
        row.setdefault("checked", True)
        legacy_finding = dict(finding)
        legacy_finding.setdefault("tuple", str(tuple_name))
        legacy_finding.setdefault("adapter", str(finding.get("adapter") or row["adapter"]))
        legacy_finding.setdefault("dimension", "price")
        legacy_finding.setdefault("source", "legacy-price-cache")
        legacy_finding.setdefault("live_price_source", legacy_finding.get("source") or "legacy-price-cache")
        row["dimensions"] = ["price"]
        row.setdefault("observed_at", finding.get("observed_at") or raw_row.get("observed_at") or now.isoformat())
        row.setdefault("expires_at", finding.get("expires_at") or raw_row.get("expires_at") or now.isoformat())
        legacy_finding.setdefault("observed_at", row["observed_at"])
        legacy_finding.setdefault("expires_at", row["expires_at"])
        row["findings"] = [legacy_finding]
        observed = _parse_time(row["observed_at"])
        expires = _parse_time(row["expires_at"])
        row["ttl_seconds"] = max(0, int((expires - observed).total_seconds())) if observed and expires else int(raw_row.get("ttl_seconds") or 0)
        normalized[str(tuple_name)] = row
    payload["tuples"] = normalized
    return payload


def _canonical_finding(
    raw: Mapping[str, Any],
    *,
    tuple_name: str,
    adapter: str,
    observed_at: datetime,
    ttl_seconds: int,
) -> PanopticonFinding:
    data = dict(raw)
    data.setdefault("tuple", tuple_name)
    data.setdefault("adapter", adapter)
    if data["tuple"] != tuple_name:
        raise ValueError("finding tuple must match row tuple")
    if data["adapter"] != adapter:
        raise ValueError("finding adapter must match row adapter")
    data.setdefault("observed_at", observed_at)
    data.setdefault("expires_at", observed_at + timedelta(seconds=ttl_seconds))
    if data.get("live_price_per_second") is not None and not data.get("live_price_source"):
        data["live_price_source"] = data.get("source") or "provider-panopticon"
    data["details"] = _merge_details(_details_from_raw(data.get("raw")), data.get("details"))
    if not data["details"]:
        data.pop("details", None)
    return PanopticonFinding.model_validate(data)


def _row_from_loaded_or_input(tuple_name: str, raw_row: Mapping[str, Any], *, now: datetime) -> PanopticonRow:
    raw_row = _strip_loaded_row_metadata(raw_row)
    try:
        return PanopticonRow.model_validate(raw_row)
    except Exception:
        input_row = PanopticonInputRow.model_validate(dict(raw_row))
        row_tuple = input_row.tuple or tuple_name
        if row_tuple != tuple_name:
            raise ValueError("tuple mapping key must match row tuple")
        adapter = input_row.adapter or _first_mapping_value(input_row.findings, "adapter")
        if not adapter:
            raise ValueError("panopticon row requires adapter or at least one finding adapter")
        ttl = int(input_row.ttl_seconds or _ttl_seconds_for_findings(input_row.findings))
        findings = [
            _canonical_finding(item, tuple_name=row_tuple, adapter=str(adapter), observed_at=now, ttl_seconds=ttl)
            for item in input_row.findings
        ]
        expires_at = _earliest_finding_expiry(findings) or (now + timedelta(seconds=ttl))
        observed_at = _earliest_finding_observed_at(findings) or now
        return PanopticonRow(
            tuple=row_tuple,
            adapter=str(adapter),
            status=_stored_status(input_row.status, findings),
            checked=input_row.checked,
            findings=findings,
            dimensions=sorted({finding.dimension for finding in findings}),
            observed_at=observed_at,
            expires_at=expires_at,
            ttl_seconds=max(0, int((expires_at - observed_at).total_seconds())),
            catalog_validator=input_row.catalog_validator,
        )


def _strip_loaded_row_metadata(raw_row: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(raw_row)
    for key in (
        "panopticon_observed_at",
        "panopticon_expires_at",
        "panopticon_ttl_seconds",
        "panopticon_stale",
        "panopticon_age_seconds",
    ):
        row.pop(key, None)
    return row


def _stored_status(status: PanopticonStatus | None, findings: list[PanopticonFinding]) -> PanopticonStatus:
    finding_status = _status_from_findings(findings)
    if finding_status == "blocked":
        return "blocked"
    if findings:
        return "live_revalidated"
    if status in {"not_checked", "unknown", "live_revalidated", "blocked"}:
        return status
    return finding_status


def _status_from_findings(findings: list[PanopticonFinding]) -> PanopticonStatus:
    if any(f.severity == "error" or f.live_stock_state == "unavailable" for f in findings):
        return "blocked"
    if findings:
        return "live_revalidated"
    return "unknown"


def _merged_status(left: PanopticonStatus, right: PanopticonStatus, findings: list[PanopticonFinding]) -> PanopticonStatus:
    finding_status = _status_from_findings(findings)
    if finding_status == "blocked" or left == "blocked" or right == "blocked":
        return "blocked"
    if left == "live_revalidated" or right == "live_revalidated":
        return "live_revalidated"
    if left == "unknown" or right == "unknown":
        return "unknown"
    return "not_checked"


def _ttl_seconds_for_findings(findings: list[Any]) -> int:
    dimensions = {str(item.get("dimension")) for item in findings if isinstance(item, Mapping) and item.get("dimension")}
    if not dimensions:
        return 300
    return min(PANOPTICON_TTL_BY_DIMENSION.get(dimension, 300) for dimension in dimensions)


def _dedupe_findings(findings: list[PanopticonFinding]) -> list[PanopticonFinding]:
    seen: set[str] = set()
    result: list[PanopticonFinding] = []
    for finding in findings:
        key = json.dumps(_finding_dump(finding), sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def _earliest_finding_expiry(findings: list[PanopticonFinding]) -> datetime | None:
    expiries = [finding.expires_at for finding in findings if finding.expires_at]
    return min(expiries) if expiries else None


def _earliest_finding_observed_at(findings: list[PanopticonFinding]) -> datetime | None:
    observed = [finding.observed_at for finding in findings if finding.observed_at]
    return min(observed) if observed else None


def _first_mapping_value(items: list[dict[str, Any]], key: str) -> Any:
    for item in items:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _details_from_raw(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    details: dict[str, Any] = {}
    direct_keys = {
        "provider",
        "provider_family",
        "account_ref",
        "region",
        "gpu_type",
        "endpoint_id",
        "endpoint_name",
        "endpoint_status",
        "target",
        "template_id",
        "template_name",
        "image",
        "model_name",
        "served_model_name",
        "max_model_len",
        "configured_model_len",
        "http_status",
        "workers_min",
        "workers_max",
        "active_workers",
        "active_pods",
        "queue_depth",
        "served_model_count",
        "probe_elapsed_ms",
        "price_per_second",
        "price_source",
        "live_reason",
        "error_code",
        "error_type",
        "source_url",
    }
    for key in direct_keys:
        value = raw.get(key)
        if value not in (None, ""):
            details[key] = value
    workers = raw.get("workers")
    if isinstance(workers, Mapping):
        mapping = {
            "running": "worker_running",
            "ready": "worker_ready",
            "idle": "worker_idle",
            "inQueue": "worker_in_queue",
            "in_queue": "worker_in_queue",
        }
        for source_key, target_key in mapping.items():
            value = workers.get(source_key)
            if value not in (None, ""):
                details[target_key] = value
    return PanopticonFindingDetails.model_validate(details).model_dump(exclude_none=True)


def _merge_details(extracted: Mapping[str, Any], explicit: Any) -> dict[str, Any]:
    if explicit is None:
        return dict(extracted)
    if not isinstance(explicit, Mapping):
        PanopticonFindingDetails.model_validate(explicit)
    return PanopticonFindingDetails.model_validate({**dict(extracted), **dict(explicit)}).model_dump(exclude_none=True)


def _payload_dump(payload: PanopticonPayload) -> dict[str, Any]:
    return payload.model_dump(exclude_none=True, mode="json")


def _row_dump(row: PanopticonRow) -> dict[str, Any]:
    payload = row.model_dump(exclude_none=True, mode="json")
    payload["findings"] = [_finding_dump(finding) for finding in row.findings]
    return payload


def _merged_row_dump(row: PanopticonRow, *, now: datetime) -> dict[str, Any]:
    payload = _row_dump(row)
    payload.update(
        {
            "panopticon_observed_at": row.observed_at.isoformat(),
            "panopticon_expires_at": row.expires_at.isoformat(),
            "panopticon_ttl_seconds": row.ttl_seconds,
            "panopticon_stale": row.expires_at <= now,
            "panopticon_age_seconds": _age_seconds(row.observed_at, now),
        }
    )
    return payload


def _finding_dump(finding: PanopticonFinding) -> dict[str, Any]:
    payload = finding.model_dump(exclude_none=True, mode="json")
    if not payload.get("raw"):
        payload.pop("raw", None)
    if not payload.get("details"):
        payload.pop("details", None)
    return payload


def _expiry_finding(tuple_name: str, row: Mapping[str, Any], *, expired_reason: str) -> dict[str, Any]:
    dimensions = [str(item) for item in row.get("dimensions") or []]
    dimension = "price" if dimensions == ["price"] else "panopticon"
    return {
        "tuple": tuple_name,
        "adapter": str(row.get("adapter") or ""),
        "dimension": dimension,
        "severity": "error",
        "reason": expired_reason,
        "source": "provider-panopticon",
        "details": {
            "live_reason": "panopticon_evidence_expired",
        },
        "raw": {
            "live_reason": "panopticon_evidence_expired",
            "observed_at": row.get("observed_at"),
            "expires_at": row.get("expires_at"),
            "ttl_seconds": row.get("ttl_seconds"),
        },
    }


def _age_seconds(observed_at: datetime, now: datetime) -> float:
    return max(0.0, (now - observed_at).total_seconds())


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        Path(tmp_name).replace(path)
    finally:
        tmp = Path(tmp_name)
        if tmp.exists():
            tmp.unlink()


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return _utc_datetime(parsed)
