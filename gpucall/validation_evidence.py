from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from gpucall.config import default_state_dir
from gpucall.domain import ExecutionTupleSpec
from gpucall.execution.contracts import official_contract_hash
from gpucall.routing import is_local_execution_tuple, is_production_route_candidate


RouteValidationKey = tuple[str, str, str]


@dataclass(frozen=True)
class RouteValidationEvidence:
    tuple_name: str
    recipe_name: str
    mode: str
    path: str
    mtime: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class RouteValidationStatus:
    tuple_name: str
    recipe_name: str
    mode: str
    path: str
    mtime: str
    accepted: bool
    reason: str | None
    data: Mapping[str, Any]


def route_validation_key(tuple_name: str, recipe_name: str, mode: str) -> RouteValidationKey:
    return (tuple_name, recipe_name, mode)


def route_validation_required_for_tuple(tuple: ExecutionTupleSpec) -> bool:
    if is_local_execution_tuple(tuple) and tuple.adapter in {"echo", "local", "local-echo"}:
        return False
    return is_production_route_candidate(tuple)


def load_route_validation_evidence(
    *,
    config_dir: str | Path | None = None,
    validation_dir: str | Path | None = None,
    strict_config_hash: bool | None = None,
) -> dict[RouteValidationKey, RouteValidationEvidence]:
    root = Path(validation_dir) if validation_dir else default_state_dir() / "tuple-validation"
    if not root.exists():
        return {}
    expected_commit = git_commit()
    expected_config_hash = config_hash(Path(config_dir)) if config_dir is not None else None
    strict_hash = route_validation_strict_config_hash_enabled() if strict_config_hash is None else strict_config_hash
    if strict_hash and config_dir is not None and expected_config_hash is None:
        return {}
    evidence: dict[RouteValidationKey, RouteValidationEvidence] = {}
    candidates: list[tuple[float, Path]] = []
    for path in root.glob("*.json"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if expected_commit and data.get("commit") != expected_commit:
            continue
        if strict_hash and data.get("config_hash") != expected_config_hash:
            continue
        if not live_validation_artifact_valid(data):
            continue
        tuple_name = str(data.get("tuple") or "")
        recipe_name = str(data.get("recipe") or "")
        mode = str(data.get("mode") or "")
        if not tuple_name or not recipe_name or not mode:
            continue
        key = route_validation_key(tuple_name, recipe_name, mode)
        if key in evidence:
            continue
        evidence[key] = RouteValidationEvidence(
            tuple_name=tuple_name,
            recipe_name=recipe_name,
            mode=mode,
            path=str(path),
            mtime=datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            data=data,
        )
    return evidence


def load_route_validation_statuses(
    *,
    config_dir: str | Path | None = None,
    validation_dir: str | Path | None = None,
    strict_config_hash: bool | None = None,
) -> dict[RouteValidationKey, RouteValidationStatus]:
    root = Path(validation_dir) if validation_dir else default_state_dir() / "tuple-validation"
    if not root.exists():
        return {}
    expected_commit = git_commit()
    expected_config_hash = config_hash(Path(config_dir)) if config_dir is not None else None
    strict_hash = route_validation_strict_config_hash_enabled() if strict_config_hash is None else strict_config_hash
    statuses: dict[RouteValidationKey, RouteValidationStatus] = {}
    candidates: list[tuple[float, Path]] = []
    for path in root.glob("*.json"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        tuple_name = str(data.get("tuple") or "")
        recipe_name = str(data.get("recipe") or "")
        mode = str(data.get("mode") or "")
        if not tuple_name or not recipe_name or not mode:
            continue
        key = route_validation_key(tuple_name, recipe_name, mode)
        if key in statuses:
            continue
        reason = route_validation_rejection_reason(
            data,
            expected_commit=expected_commit,
            expected_config_hash=expected_config_hash,
            strict_config_hash=strict_hash,
            config_dir_provided=config_dir is not None,
        )
        statuses[key] = RouteValidationStatus(
            tuple_name=tuple_name,
            recipe_name=recipe_name,
            mode=mode,
            path=str(path),
            mtime=datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            accepted=reason is None,
            reason=reason,
            data=data,
        )
    return statuses


def validated_route_keys(
    *,
    config_dir: str | Path | None = None,
    validation_dir: str | Path | None = None,
    strict_config_hash: bool | None = None,
) -> set[RouteValidationKey]:
    return set(
        load_route_validation_evidence(
            config_dir=config_dir,
            validation_dir=validation_dir,
            strict_config_hash=strict_config_hash,
        )
    )


def latest_route_validation_evidence(
    *,
    tuple_name: str,
    recipe_name: str,
    mode: str,
    config_dir: str | Path | None = None,
    validation_dir: str | Path | None = None,
    strict_config_hash: bool | None = None,
) -> RouteValidationEvidence | None:
    return load_route_validation_evidence(
        config_dir=config_dir,
        validation_dir=validation_dir,
        strict_config_hash=strict_config_hash,
    ).get(route_validation_key(tuple_name, recipe_name, mode))


def live_validation_artifact_valid(data: Mapping[str, Any]) -> bool:
    return route_validation_rejection_reason(
        data,
        expected_commit=None,
        expected_config_hash=None,
        strict_config_hash=False,
        config_dir_provided=False,
    ) is None


def route_validation_rejection_reason(
    data: Mapping[str, Any],
    *,
    expected_commit: str | None,
    expected_config_hash: str | None,
    strict_config_hash: bool,
    config_dir_provided: bool,
) -> str | None:
    required = {
        "tuple",
        "recipe",
        "mode",
        "started_at",
        "ended_at",
        "commit",
        "config_hash",
        "governance_hash",
        "official_contract",
        "official_contract_hash",
    }
    if not required.issubset(data):
        missing = sorted(required - set(data))
        return "missing_validation_fields:" + ",".join(missing)
    if expected_commit is not None and data.get("commit") != expected_commit:
        return "validation_commit_mismatch"
    if strict_config_hash and config_dir_provided:
        if expected_config_hash is None:
            return "expected_config_hash_unavailable"
        if data.get("config_hash") != expected_config_hash:
            return "validation_config_hash_mismatch"
    if data.get("validation_schema_version") != 1:
        return "invalid_validation_schema_version"
    if data.get("passed") is not True:
        error = data.get("error") if isinstance(data.get("error"), Mapping) else {}
        code = error.get("code") if isinstance(error, Mapping) else None
        return "latest_route_validation_failed" + (f":{code}" if code else "")
    if not isinstance(data.get("cleanup"), dict):
        return "missing_cleanup_evidence"
    if not isinstance(data.get("cost"), dict):
        return "missing_cost_evidence"
    if not isinstance(data.get("audit"), dict):
        return "missing_audit_evidence"
    contract = data.get("official_contract")
    if not isinstance(contract, dict):
        return "missing_official_contract"
    if not contract.get("adapter"):
        return "official_contract_missing_adapter"
    if not contract.get("endpoint_contract") or contract.get("endpoint_contract") != contract.get("expected_endpoint_contract"):
        return "endpoint_contract_mismatch"
    if not contract.get("output_contract") or contract.get("output_contract") != contract.get("expected_output_contract"):
        return "output_contract_mismatch"
    expected_stream_contract = contract.get("expected_stream_contract")
    if expected_stream_contract is not None and contract.get("stream_contract") != expected_stream_contract:
        return "stream_contract_mismatch"
    if not contract.get("official_sources"):
        return "missing_official_sources"
    if data.get("official_contract_hash") != official_contract_hash(contract):
        return "official_contract_hash_mismatch"
    return None


def route_validation_strict_config_hash_enabled() -> bool:
    value = os.getenv("GPUCALL_STRICT_ROUTE_VALIDATION_CONFIG_HASH")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def route_validation_required_from_env() -> bool:
    value = os.getenv("GPUCALL_REQUIRE_ROUTE_VALIDATION")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def config_hash(config_dir: Path) -> str | None:
    digest = hashlib.sha256()
    for path in sorted(config_dir.rglob("*.yml")):
        try:
            digest.update(path.relative_to(config_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            return None
    return digest.hexdigest()


def git_commit() -> str | None:
    env_commit = os.getenv("GPUCALL_GIT_COMMIT")
    if env_commit:
        return env_commit
    root = Path(__file__).resolve().parents[1]
    build_commit = root / "BUILD_COMMIT"
    try:
        value = build_commit.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    head = root / ".git" / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
        if value.startswith("ref: "):
            ref_name = value.removeprefix("ref: ")
            ref = root / ".git" / ref_name
            try:
                return ref.read_text(encoding="utf-8").strip()
            except OSError:
                return _packed_git_ref(root / ".git" / "packed-refs", ref_name)
        return value
    except OSError:
        return None


def _packed_git_ref(path: Path, ref_name: str) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref_name:
                return parts[0]
    except OSError:
        return None
    return None
