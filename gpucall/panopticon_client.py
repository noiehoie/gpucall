from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urljoin

import httpx

from gpucall.domain import ExecutionTupleSpec
from gpucall.panopticon import default_panopticon_path, load_panopticon_evidence, merge_panopticon_evidence


PanopticonSourceKind = Literal["file", "http"]
PanopticonFetchStatus = Literal["ok", "stale", "missing", "unreachable", "invalid"]


@dataclass(frozen=True)
class PanopticonClientConfig:
    source_kind: PanopticonSourceKind = "file"
    path: Path | None = None
    url: str | None = None
    timeout_seconds: float = 2.0
    fail_closed_on_missing: bool = False
    fail_closed_on_unreachable: bool = False
    fail_closed_on_invalid: bool = False


def panopticon_client_config_from_env(*, path: Path | None = None) -> PanopticonClientConfig:
    raw_source = os.getenv("GPUCALL_PANOPTICON_SOURCE", "").strip().lower()
    raw_url = os.getenv("GPUCALL_PANOPTICON_URL", "").strip()
    source_kind: PanopticonSourceKind = "http" if raw_url else "file"
    if raw_source:
        if raw_source not in {"file", "http"}:
            raise ValueError("GPUCALL_PANOPTICON_SOURCE must be file or http")
        source_kind = raw_source  # type: ignore[assignment]
    raw_path = os.getenv("GPUCALL_PANOPTICON_PATH", "").strip()
    timeout = _float_env("GPUCALL_PANOPTICON_TIMEOUT_SECONDS", default=2.0)
    return PanopticonClientConfig(
        source_kind=source_kind,
        path=path or (Path(raw_path).expanduser() if raw_path else None),
        url=raw_url or None,
        timeout_seconds=timeout,
        fail_closed_on_missing=_truthy_env("GPUCALL_PANOPTICON_FAIL_CLOSED_ON_MISSING"),
        fail_closed_on_unreachable=_truthy_env("GPUCALL_PANOPTICON_FAIL_CLOSED_ON_UNREACHABLE"),
        fail_closed_on_invalid=_truthy_env("GPUCALL_PANOPTICON_FAIL_CLOSED_ON_INVALID"),
    )


def fetch_panopticon_snapshot(
    *,
    config: PanopticonClientConfig | None = None,
    tuple_scope: Mapping[str, ExecutionTupleSpec] | None = None,
    now: datetime | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    client_config = config or panopticon_client_config_from_env()
    fetched_at = _utc(now or datetime.now(timezone.utc))
    try:
        if client_config.source_kind == "http":
            return _fetch_http(client_config, tuple_scope=tuple_scope, now=fetched_at, http_client=http_client)
        return _fetch_file(client_config, tuple_scope=tuple_scope, now=fetched_at)
    except Exception as exc:
        return _failure_report(
            config=client_config,
            status="invalid",
            reason="provider panopticon snapshot client failed",
            error=exc,
            tuple_scope=tuple_scope,
            now=fetched_at,
        )


def panopticon_report_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    error = report.get("error") if isinstance(report.get("error"), Mapping) else None
    result: dict[str, Any] = {
        "source_kind": report.get("source_kind"),
        "status": report.get("status"),
        "fail_closed": bool(report.get("fail_closed")),
        "tuple_count": int(report.get("tuple_count") or 0),
        "stale_tuple_count": int(report.get("stale_tuple_count") or 0),
        "snapshot_hash": report.get("snapshot_hash"),
        "fetched_at": report.get("fetched_at"),
    }
    if report.get("snapshot_path") is not None:
        result["path"] = report.get("snapshot_path")
    if report.get("snapshot_url") is not None:
        result["url"] = report.get("snapshot_url")
    if error:
        result["error"] = {"type": error.get("type"), "message": error.get("message")}
    return result


def _fetch_file(
    config: PanopticonClientConfig,
    *,
    tuple_scope: Mapping[str, ExecutionTupleSpec] | None,
    now: datetime,
) -> dict[str, Any]:
    path = config.path or default_panopticon_path()
    if not path.exists():
        return _failure_report(
            config=config,
            status="missing",
            reason="provider panopticon snapshot file is missing",
            tuple_scope=tuple_scope,
            now=now,
            path=path,
        )
    try:
        snapshot = load_panopticon_evidence(path, now=now)
        snapshot = _validated_snapshot(snapshot)
    except Exception as exc:
        return _failure_report(
            config=config,
            status="invalid",
            reason="provider panopticon snapshot file is invalid",
            error=exc,
            tuple_scope=tuple_scope,
            now=now,
            path=path,
        )
    return _success_report(config=config, snapshot=snapshot, now=now, path=path)


def _fetch_http(
    config: PanopticonClientConfig,
    *,
    tuple_scope: Mapping[str, ExecutionTupleSpec] | None,
    now: datetime,
    http_client: httpx.Client | None,
) -> dict[str, Any]:
    url = _snapshot_url(config.url)
    if not url:
        return _failure_report(
            config=config,
            status="missing",
            reason="provider panopticon HTTP URL is not configured",
            tuple_scope=tuple_scope,
            now=now,
            url=None,
        )
    try:
        if http_client is None:
            with httpx.Client(timeout=config.timeout_seconds) as client:
                response = client.get(url)
        else:
            response = http_client.get(url)
        response.raise_for_status()
        payload = response.json()
        snapshot = _snapshot_from_http_payload(payload)
        snapshot = _validated_snapshot(snapshot)
    except httpx.HTTPError as exc:
        return _failure_report(
            config=config,
            status="unreachable",
            reason="provider panopticon snapshot unreachable",
            error=exc,
            tuple_scope=tuple_scope,
            now=now,
            url=url,
        )
    except Exception as exc:
        return _failure_report(
            config=config,
            status="invalid",
            reason="provider panopticon HTTP snapshot is invalid",
            error=exc,
            tuple_scope=tuple_scope,
            now=now,
            url=url,
        )
    return _success_report(config=config, snapshot=snapshot, now=now, url=url)


def _snapshot_from_http_payload(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ValueError("provider panopticon HTTP response must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("provider panopticon HTTP response schema_version must be 1")
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("provider panopticon HTTP response must include snapshot mapping")
    result: dict[str, dict[str, Any]] = {}
    for key, value in snapshot.items():
        if not isinstance(value, Mapping):
            raise ValueError("provider panopticon snapshot rows must be objects")
        result[str(key)] = dict(value)
    return result


def _validated_snapshot(snapshot: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in snapshot.items():
        if not isinstance(value, Mapping):
            raise ValueError("provider panopticon snapshot rows must be objects")
        normalized[str(key)] = dict(value)
    merge_panopticon_evidence(normalized)
    return normalized


def _success_report(
    *,
    config: PanopticonClientConfig,
    snapshot: dict[str, dict[str, Any]],
    now: datetime,
    path: Path | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    stale_count = _stale_tuple_count(snapshot)
    status: PanopticonFetchStatus = "stale" if stale_count else "ok"
    return _base_report(
        config=config,
        status=status,
        snapshot=snapshot,
        now=now,
        path=path,
        url=url,
        fail_closed=False,
        error=None,
    )


def _failure_report(
    *,
    config: PanopticonClientConfig,
    status: PanopticonFetchStatus,
    reason: str,
    tuple_scope: Mapping[str, ExecutionTupleSpec] | None,
    now: datetime,
    error: Exception | None = None,
    path: Path | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    fail_closed = _fail_closed(config, status)
    snapshot = _blocked_snapshot(tuple_scope or {}, reason=reason, now=now) if fail_closed else {}
    report_path = path or config.path or (default_panopticon_path() if config.source_kind == "file" else None)
    report_url = (url or _snapshot_url(config.url)) if config.source_kind == "http" else None
    return _base_report(
        config=config,
        status=status,
        snapshot=snapshot,
        now=now,
        path=report_path,
        url=report_url,
        fail_closed=fail_closed,
        error={
            "type": type(error).__name__ if error is not None else status,
            "message": str(error) if error is not None else reason,
            "reason": _failure_reason_code(status),
        },
    )


def _base_report(
    *,
    config: PanopticonClientConfig,
    status: PanopticonFetchStatus,
    snapshot: dict[str, dict[str, Any]],
    now: datetime,
    path: Path | None,
    url: str | None,
    fail_closed: bool,
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    snapshot_hash = _snapshot_hash(snapshot) if snapshot else None
    return {
        "schema_version": 1,
        "phase": "provider-panopticon-fetch",
        "source_kind": config.source_kind,
        "snapshot_path": str(path) if path is not None else None,
        "snapshot_url": url,
        "fetched_at": now.isoformat(),
        "status": status,
        "fail_closed": fail_closed,
        "snapshot_hash": snapshot_hash,
        "tuple_count": len(snapshot),
        "stale_tuple_count": _stale_tuple_count(snapshot),
        "non_generation_probe_only": True,
        "error": error,
        "snapshot": snapshot,
    }


def _blocked_snapshot(tuple_scope: Mapping[str, ExecutionTupleSpec], *, reason: str, now: datetime) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    timestamp = now.isoformat()
    for name, tuple_spec in tuple_scope.items():
        snapshot[str(name)] = {
            "tuple": str(name),
            "adapter": str(tuple_spec.adapter),
            "status": "blocked",
            "checked": True,
            "dimensions": ["panopticon"],
            "observed_at": timestamp,
            "expires_at": timestamp,
            "ttl_seconds": 0,
            "panopticon_observed_at": timestamp,
            "panopticon_expires_at": timestamp,
            "panopticon_ttl_seconds": 0,
            "panopticon_stale": True,
            "panopticon_age_seconds": 0.0,
            "findings": [
                {
                    "tuple": str(name),
                    "adapter": str(tuple_spec.adapter),
                    "dimension": "panopticon",
                    "severity": "error",
                    "reason": reason,
                    "source": "provider-panopticon-client",
                    "raw": {"live_reason": _failure_reason_code_from_reason(reason)},
                }
            ],
        }
    return snapshot


def _snapshot_url(url: str | None) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.rstrip("/").endswith("/v1/snapshot"):
        return raw.rstrip("/")
    return urljoin(raw.rstrip("/") + "/", "v1/snapshot")


def _snapshot_hash(snapshot: Mapping[str, Mapping[str, Any]]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stale_tuple_count(snapshot: Mapping[str, Mapping[str, Any]]) -> int:
    return sum(1 for row in snapshot.values() if isinstance(row, Mapping) and row.get("panopticon_stale") is True)


def _fail_closed(config: PanopticonClientConfig, status: PanopticonFetchStatus) -> bool:
    if status == "missing":
        return config.fail_closed_on_missing
    if status == "unreachable":
        return config.fail_closed_on_unreachable
    if status == "invalid":
        return config.fail_closed_on_invalid
    return False


def _failure_reason_code(status: PanopticonFetchStatus) -> str:
    return {
        "missing": "panopticon_snapshot_missing",
        "unreachable": "panopticon_snapshot_unreachable",
        "invalid": "panopticon_snapshot_invalid",
        "stale": "panopticon_snapshot_stale",
        "ok": "panopticon_snapshot_ok",
    }[status]


def _failure_reason_code_from_reason(reason: str) -> str:
    lowered = reason.lower()
    if "unreachable" in lowered:
        return "panopticon_snapshot_unreachable"
    if "missing" in lowered or "not configured" in lowered:
        return "panopticon_snapshot_missing"
    if "invalid" in lowered:
        return "panopticon_snapshot_invalid"
    return "panopticon_snapshot_failed"


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, *, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
