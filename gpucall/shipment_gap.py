from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gpucall.config import default_config_dir
from gpucall.readiness import build_readiness_report


SHIPMENT_READY = "shipment_ready"
VALIDATION_MISSING = "validation_missing"
PROVIDER_MISSING = "provider_missing"
PRICE_UNKNOWN = "price_unknown"
ENDPOINT_STALE = "endpoint_stale"

CATEGORY_LABELS = {
    SHIPMENT_READY: "出荷可能",
    VALIDATION_MISSING: "validation 不足",
    PROVIDER_MISSING: "provider 不足",
    PRICE_UNKNOWN: "price 不明",
    ENDPOINT_STALE: "endpoint stale",
}

BLOCKER_PRIORITY = (ENDPOINT_STALE, PRICE_UNKNOWN, VALIDATION_MISSING, PROVIDER_MISSING)


def build_shipment_gap_report(
    *,
    config_dir: str | Path = default_config_dir(),
    contract_path: str | Path,
    validation_dir: str | Path | None = None,
    panopticon_path: str | Path | None = None,
    live: bool = False,
    source: str | None = None,
    min_live_ready_tuples: int = 1,
) -> dict[str, Any]:
    contract = _load_json(Path(contract_path))
    workloads = [item for item in contract.get("workloads", []) or [] if isinstance(item, Mapping)]
    demands = [
        classify_workload_demand(
            workload,
            build_readiness_report(
                config_dir=config_dir,
                source=source or str(contract.get("source") or ""),
                intent=str(workload.get("intent") or ""),
                validation_dir=validation_dir,
                live=live,
                panopticon_path=panopticon_path,
            ),
            min_live_ready_tuples=min_live_ready_tuples,
        )
        for workload in workloads
    ]
    category_counts = Counter(str(item.get("category") or PROVIDER_MISSING) for item in demands)
    blocker_counts = Counter(str(blocker.get("category")) for item in demands for blocker in item.get("blockers", []) or [])
    ready_count = category_counts.get(SHIPMENT_READY, 0)
    blocker_count = len(demands) - ready_count
    return {
        "schema_version": 1,
        "phase": "product-shipment-gap",
        "source": source or contract.get("source"),
        "config_dir": str(config_dir),
        "contract_path": str(contract_path),
        "contract_phase": contract.get("phase"),
        "contract_workload_count": len(workloads),
        "min_live_ready_tuples": min_live_ready_tuples,
        "go": blocker_count == 0 and bool(demands),
        "summary": {
            "shipment_ready_count": ready_count,
            "blocker_count": blocker_count,
            "category_counts": dict(sorted(category_counts.items())),
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "demands": demands,
    }


def classify_workload_demand(
    workload: Mapping[str, Any],
    readiness_report: Mapping[str, Any],
    *,
    min_live_ready_tuples: int = 1,
) -> dict[str, Any]:
    task = str(workload.get("task") or "")
    intent = str(workload.get("intent") or "")
    modes = [str(item) for item in workload.get("modes") or [] if str(item)]
    context_budget_tokens = _safe_int(_mapping(workload.get("input_profile")).get("context_budget_tokens"))
    matching = [
        recipe
        for recipe in readiness_report.get("recipes", []) or []
        if isinstance(recipe, Mapping)
        and recipe.get("intent") == intent
        and (not recipe.get("task") or not task or recipe.get("task") == task)
    ]
    compatible = [
        recipe
        for recipe in matching
        if _recipe_is_contract_compatible(recipe, context_budget_tokens=context_budget_tokens, modes=modes)
    ]
    blockers: list[dict[str, Any]] = []
    if not matching:
        blockers.append(_blocker(PROVIDER_MISSING, "no_matching_readiness_recipe"))
    elif not compatible:
        blockers.append(_blocker(PROVIDER_MISSING, "no_contract_compatible_readiness_recipe"))
    else:
        rows = _rows_for_requested_modes([row for recipe in compatible for row in _eligible_rows(recipe)], modes=modes)
        ready_rows = [row for recipe in compatible for row in _ready_rows(recipe, modes=modes)]
        fresh_ready_rows = [row for row in ready_rows if row.get("price_freshness") == "fresh"]
        blockers.extend(_blockers_from_rows(rows=rows, ready_rows=ready_rows, fresh_ready_rows=fresh_ready_rows, min_live_ready_tuples=min_live_ready_tuples))
    category = SHIPMENT_READY if not blockers else _primary_category(blockers)
    # Use recipe's own classification if available as a hint
    if matching and matching[0].get("shipment_status"):
        recipe_status = matching[0]["shipment_status"]
        mapped_category = {
            "shippable": SHIPMENT_READY,
            "validation_lack": VALIDATION_MISSING,
            "provider_lack": PROVIDER_MISSING,
            "price_unknown": PRICE_UNKNOWN,
            "endpoint_stale": ENDPOINT_STALE,
        }.get(recipe_status)
        if mapped_category and mapped_category != category and mapped_category in BLOCKER_PRIORITY:
             category = mapped_category

    return {
        "workload_id": workload.get("id"),
        "task": task,
        "intent": intent,
        "modes": modes,
        "context_budget_tokens": context_budget_tokens,
        "category": category,
        "label": CATEGORY_LABELS[category],
        "shipment_ready": category == SHIPMENT_READY,
        "blockers": blockers,
        "readiness": {
            "matching_recipe_count": len(matching),
            "compatible_recipe_count": len(compatible),
            "recipes": [_bounded_recipe(recipe) for recipe in compatible[:8]],
        },
    }


def dumps_shipment_gap(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _blockers_from_rows(
    *,
    rows: list[Mapping[str, Any]],
    ready_rows: list[Mapping[str, Any]],
    fresh_ready_rows: list[Mapping[str, Any]],
    min_live_ready_tuples: int,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if any(_is_endpoint_stale(row) for row in rows):
        blockers.append(_blocker(ENDPOINT_STALE, "endpoint_stale_or_missing", rows=[row for row in rows if _is_endpoint_stale(row)]))
    if ready_rows and len(fresh_ready_rows) < min_live_ready_tuples:
        blockers.append(_blocker(PRICE_UNKNOWN, "fresh_price_evidence_missing", rows=ready_rows))
    if any(_is_validation_missing(row) for row in rows):
        blockers.append(_blocker(VALIDATION_MISSING, "route_validation_evidence_missing_or_rejected", rows=[row for row in rows if _is_validation_missing(row)]))
    if not ready_rows and not blockers:
        blockers.append(_blocker(PROVIDER_MISSING, "no_live_ready_tuple", rows=rows))
    if ready_rows and len(ready_rows) < min_live_ready_tuples and not any(item["category"] == PRICE_UNKNOWN for item in blockers):
        blockers.append(_blocker(PROVIDER_MISSING, "insufficient_live_ready_tuples", rows=ready_rows))
    if not rows and not blockers:
        blockers.append(_blocker(PROVIDER_MISSING, "no_static_eligible_tuple"))
    return _dedupe_blockers(blockers)


def _blocker(category: str, reason: str, *, rows: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"category": category, "label": CATEGORY_LABELS[category], "reason": reason}
    if rows is not None:
        payload["tuples"] = [_bounded_tuple(row) for row in rows[:12]]
    return payload


def _primary_category(blockers: list[Mapping[str, Any]]) -> str:
    categories = {str(item.get("category") or "") for item in blockers}
    for category in BLOCKER_PRIORITY:
        if category in categories:
            return category
    return PROVIDER_MISSING


def _dedupe_blockers(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for blocker in blockers:
        key = (str(blocker.get("category")), str(blocker.get("reason")))
        if key in seen:
            continue
        seen.add(key)
        result.append(blocker)
    return result


def _recipe_is_contract_compatible(recipe: Mapping[str, Any], *, context_budget_tokens: int, modes: list[str]) -> bool:
    recipe_budget = _safe_int(recipe.get("context_budget_tokens"))
    if context_budget_tokens > 0 and recipe_budget > 0 and recipe_budget < context_budget_tokens:
        return False
    allowed_modes = {str(item) for item in recipe.get("allowed_modes") or [] if str(item)}
    requested_modes = {str(item) for item in modes if str(item)}
    if requested_modes and allowed_modes and not requested_modes.intersection(allowed_modes):
        return False
    return True


def _eligible_rows(recipe: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in recipe.get("eligible_tuples", []) or [] if isinstance(row, Mapping)]


def _ready_rows(recipe: Mapping[str, Any], *, modes: list[str]) -> list[Mapping[str, Any]]:
    requested_modes = {str(item) for item in modes if str(item)}
    rows = [row for row in recipe.get("live_ready_tuples", []) or [] if isinstance(row, Mapping)]
    if not requested_modes:
        return rows
    return [row for row in rows if str(row.get("mode") or "") in requested_modes]


def _rows_for_requested_modes(rows: list[Mapping[str, Any]], *, modes: list[str]) -> list[Mapping[str, Any]]:
    requested_modes = {str(item) for item in modes if str(item)}
    if not requested_modes:
        return rows
    return [row for row in rows if str(row.get("mode") or "") in requested_modes]


def _is_validation_missing(row: Mapping[str, Any]) -> bool:
    if row.get("route_validation_required") is not True:
        return False
    if row.get("live_validation_artifact"):
        return False
    return True


def _is_endpoint_stale(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        [
            str(row.get("live_reason") or ""),
            str(row.get("route_validation_reason") or ""),
            json.dumps(row.get("live_catalog_findings") or [], sort_keys=True, default=str),
        ]
    ).lower()
    return "endpoint" in text and any(token in text for token in ("missing", "stale", "not found", "not present", "404"))


def _bounded_recipe(recipe: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "recipe": recipe.get("recipe"),
        "task": recipe.get("task"),
        "intent": recipe.get("intent"),
        "auto_select": recipe.get("auto_select"),
        "production_activated": recipe.get("production_activated"),
        "context_budget_tokens": recipe.get("context_budget_tokens"),
        "allowed_modes": list(recipe.get("allowed_modes") or []),
        "recommended_mode": recipe.get("recommended_mode"),
        "eligible_tuple_count": recipe.get("eligible_tuple_count"),
        "live_ready_tuple_count": recipe.get("live_ready_tuple_count"),
        "live_blocked_tuple_count": len(recipe.get("live_blocked_tuples") or []),
        "current_caller_action": recipe.get("current_caller_action"),
    }


def _bounded_tuple(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "tuple": row.get("tuple"),
        "mode": row.get("mode"),
        "price_freshness": row.get("price_freshness"),
        "live_reason": row.get("live_reason"),
        "route_validation_status": row.get("route_validation_status"),
        "route_validation_reason": row.get("route_validation_reason"),
        "live_validation_artifact": row.get("live_validation_artifact"),
        "live_catalog_status": row.get("live_catalog_status"),
    }
    return {key: value for key, value in result.items() if value is not None}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"failed to read workload contract: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("workload contract must be a JSON object")
    return payload
