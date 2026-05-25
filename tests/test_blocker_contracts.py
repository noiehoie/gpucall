from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from gpucall.blocker_taxonomy import (
    ADMIN_OWNER,
    CALLER_OWNER,
    HANDOFF_BY_OWNER,
    PROVIDER_OWNER,
    typed_intake_blocker,
)
from gpucall.migrate import recipe_intakes_from_contract
from gpucall.shipment_gap import build_shipment_gap_report, classify_workload_demand


REQUIRED_TYPED_BLOCKER_FIELDS = {
    "code",
    "owner",
    "handoff",
    "reason",
    "next_action",
    "next_artifact_required",
}
REQUIRED_SHIPMENT_BLOCKER_FIELDS = REQUIRED_TYPED_BLOCKER_FIELDS | {"category", "label"}
REQUIRED_REJECT_FIELDS = {
    "reject_type",
    "owner",
    "handoff",
    "next_action",
    "next_artifact_required",
    "typed_blockers",
}


def _assert_typed_blocker_shape(blocker: Mapping[str, Any]) -> None:
    missing = REQUIRED_TYPED_BLOCKER_FIELDS - set(blocker)
    assert missing == set()
    for field in REQUIRED_TYPED_BLOCKER_FIELDS:
        assert isinstance(blocker[field], str)
        assert blocker[field]
    assert blocker["owner"] in HANDOFF_BY_OWNER
    assert blocker["handoff"] == HANDOFF_BY_OWNER[blocker["owner"]]


def _assert_shipment_blocker_shape(blocker: Mapping[str, Any]) -> None:
    missing = REQUIRED_SHIPMENT_BLOCKER_FIELDS - set(blocker)
    assert missing == set()
    _assert_typed_blocker_shape(blocker)
    assert isinstance(blocker["category"], str)
    assert blocker["category"]
    assert isinstance(blocker["label"], str)
    assert blocker["label"]


def _workload(intent: str = "rank_text_items", *, modes: list[str] | None = None, context: int = 32768) -> dict[str, object]:
    return {
        "id": f"infer.{intent}",
        "task": "infer",
        "intent": intent,
        "modes": modes or ["sync"],
        "input_profile": {"context_budget_tokens": context},
    }


def _readiness(recipe: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "phase": "readiness", "recipes": [recipe]}


def _recipe(intent: str = "rank_text_items", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "recipe": f"infer-{intent}",
        "task": "infer",
        "intent": intent,
        "auto_select": True,
        "allowed_modes": ["sync"],
        "context_budget_tokens": 32768,
        "production_activated": True,
        "eligible_tuple_count": 1,
        "eligible_tuples": [
            {
                "tuple": "runpod-h100",
                "mode": "sync",
                "price_freshness": "fresh",
                "route_validation_required": True,
                "live_validation_artifact": "/state/tuple-validation/ok.json",
            }
        ],
        "live_ready_tuple_count": 1,
        "live_ready_tuples": [
            {
                "tuple": "runpod-h100",
                "mode": "sync",
                "price_freshness": "fresh",
                "route_validation_required": True,
                "live_validation_artifact": "/state/tuple-validation/ok.json",
            }
        ],
        "live_blocked_tuples": [],
    }
    base.update(overrides)
    if "eligible_tuples" in overrides and "eligible_tuple_count" not in overrides:
        base["eligible_tuple_count"] = len(base["eligible_tuples"])  # type: ignore[arg-type]
    if "live_ready_tuples" in overrides and "live_ready_tuple_count" not in overrides:
        base["live_ready_tuple_count"] = len(base["live_ready_tuples"])  # type: ignore[arg-type]
    return base


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "baseline command returned non-zero status",
            {
                "code": "CALLER_BASELINE_FAILED",
                "owner": CALLER_OWNER,
                "handoff": "caller-c-kit",
                "next_artifact_required": "workload-trace.json",
            },
        ),
        (
            "baseline metrics must not be empty",
            {
                "code": "CALLER_QUALITY_BASELINE_MISSING",
                "owner": CALLER_OWNER,
                "handoff": "caller-c-kit",
                "next_artifact_required": "workload-trace.json",
            },
        ),
        (
            "detected statically but not observed in the supplied baseline trace",
            {
                "code": "CALLER_WORKLOAD_NOT_OBSERVED",
                "owner": CALLER_OWNER,
                "handoff": "caller-c-kit",
                "next_artifact_required": "workload-trace.json",
            },
        ),
        (
            "unknown workload intent is not present in the production intent registry",
            {
                "code": "ADMIN_RECIPE_MISSING",
                "owner": ADMIN_OWNER,
                "handoff": "gpucall-recipe-admin",
                "next_artifact_required": "recipe-candidate.yml",
            },
        ),
    ],
)
def test_intake_taxonomy_emits_complete_typed_blocker_schema(message: str, expected: dict[str, str]) -> None:
    blocker = typed_intake_blocker(message)

    _assert_typed_blocker_shape(blocker)
    assert {key: blocker[key] for key in expected} == expected


def test_recipe_intake_rejections_are_complete_and_caller_handoff_owned() -> None:
    contract = {
        "phase": "workload-contract",
        "source": "fixture",
        "primary_workload_id": "infer.rank_text_items",
        "workloads": [
            {
                "id": "infer.rank_text_items",
                "task": "infer",
                "intent": "rank_text_items",
                "classification": "confidential",
                "modes": ["async"],
                "input_profile": {"content_types": ["text/plain"], "context_budget_tokens": 131072},
                "output_profile": {"output_contract": "json_object"},
                "quality_contract": {"missing_baseline_metrics": True, "metrics": {}},
            },
            {
                "id": "infer.static_only",
                "task": "infer",
                "intent": "rank_text_items",
                "classification": "confidential",
                "modes": ["async"],
                "input_profile": {"content_types": ["text/plain"], "context_budget_tokens": 131072},
                "output_profile": {"output_contract": "json_object"},
                "quality_contract": {"metrics": {"min_topics": 12}},
                "observed_in_baseline": False,
                "materialization_candidate": False,
            },
        ],
    }

    bundle = recipe_intakes_from_contract(contract)

    assert bundle["count"] == 0
    assert bundle["rejected_count"] == 2
    assert bundle["rejected_owner_counts"] == {"caller": 2}
    assert bundle["rejected_handoff_counts"] == {"caller-c-kit": 2}
    assert bundle["rejected_type_counts"] == {
        "CALLER_QUALITY_BASELINE_MISSING": 1,
        "CALLER_WORKLOAD_NOT_OBSERVED": 1,
    }
    for rejected in bundle["rejected"]:
        missing = REQUIRED_REJECT_FIELDS - set(rejected)
        assert missing == set()
        assert rejected["owner"] == CALLER_OWNER
        assert rejected["handoff"] == "caller-c-kit"
        assert rejected["next_action"]
        assert rejected["next_artifact_required"] == "workload-trace.json"
        assert rejected["typed_blockers"]
        for blocker in rejected["typed_blockers"]:
            _assert_typed_blocker_shape(blocker)


def test_shipment_blockers_are_complete_and_match_golden_owner_routes() -> None:
    validation_row = {
        "tuple": "runpod-h100",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "missing_route_validation_evidence",
    }
    price_row = {
        "tuple": "runpod-h100",
        "mode": "sync",
        "price_freshness": "unknown",
        "route_validation_required": True,
        "live_validation_artifact": "/state/tuple-validation/ok.json",
    }
    stale_row = {
        "tuple": "runpod-a100-dead",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "endpoint_missing_from_inventory",
        "live_catalog_findings": [{"reason": "configured endpoint not present", "status_code": 404}],
    }
    supply_row = {
        "tuple": "runpod-needs-supply",
        "target": "RUNPOD_ENDPOINT_ID_PLACEHOLDER",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "RunPod endpoint target is not configured",
    }
    cases = [
        (
            classify_workload_demand(
                _workload(),
                _readiness(_recipe(eligible_tuples=[validation_row], live_ready_tuples=[], live_blocked_tuples=[validation_row])),
            ),
            {("validation_missing", "ADMIN_VALIDATION_MISSING", ADMIN_OWNER, "gpucall-recipe-admin", "validation-evidence.json")},
        ),
        (
            classify_workload_demand(
                _workload(),
                _readiness(_recipe(eligible_tuples=[price_row], live_ready_tuples=[price_row], live_blocked_tuples=[])),
            ),
            {("price_unknown", "ADMIN_PRICE_EVIDENCE_MISSING", ADMIN_OWNER, "gpucall-recipe-admin", "provider-panopticon.json")},
        ),
        (
            classify_workload_demand(
                _workload(),
                _readiness(_recipe(eligible_tuples=[], live_ready_tuples=[], live_blocked_tuples=[])),
            ),
            {
                (
                    "provider_missing",
                    "ADMIN_TUPLE_MISSING",
                    ADMIN_OWNER,
                    "gpucall-recipe-admin",
                    "recipe-candidate.yml, tuple-candidate.yml, surface-candidate.yml, worker-candidate.yml",
                )
            },
        ),
        (
            classify_workload_demand(
                _workload(),
                _readiness(_recipe(eligible_tuples=[stale_row], live_ready_tuples=[], live_blocked_tuples=[stale_row])),
            ),
            {
                ("endpoint_stale", "PROVIDER_ENDPOINT_STALE", PROVIDER_OWNER, "provider-ops", "provider-panopticon.json"),
                ("validation_missing", "ADMIN_VALIDATION_MISSING", ADMIN_OWNER, "gpucall-recipe-admin", "validation-evidence.json"),
            },
        ),
        (
            classify_workload_demand(
                _workload(),
                _readiness(_recipe(eligible_tuples=[supply_row], live_ready_tuples=[], live_blocked_tuples=[supply_row])),
            ),
            {
                (
                    "supply_provisioning_required",
                    "PROVIDER_SUPPLY_MISSING",
                    PROVIDER_OWNER,
                    "provider-ops",
                    "provider-supply-provisioning-plan.json",
                )
            },
        ),
    ]

    for result, expected in cases:
        blockers = result["blockers"]
        assert blockers
        for blocker in blockers:
            _assert_shipment_blocker_shape(blocker)
        actual = {
            (
                blocker["category"],
                blocker["code"],
                blocker["owner"],
                blocker["handoff"],
                blocker["next_artifact_required"],
            )
            for blocker in blockers
        }
        assert actual >= expected


def test_invalid_workload_context_budget_is_classified_without_crashing() -> None:
    result = classify_workload_demand(
        _workload(context=32768.5),  # type: ignore[arg-type]
        _readiness(_recipe()),
    )

    assert result["category"] == "provider_missing"
    assert result["shipment_ready"] is False
    invalid_blockers = [blocker for blocker in result["blockers"] if blocker["reason"] == "invalid_workload_contract_context_budget_tokens"]
    assert invalid_blockers
    assert invalid_blockers[0]["code"] == "CALLER_CONTRACT_INCOMPLETE"
    assert invalid_blockers[0]["owner"] == CALLER_OWNER
    assert invalid_blockers[0]["next_artifact_required"] == "workload-contract.json"
    assert [blocker["reason"] for blocker in result["blockers"]] == ["invalid_workload_contract_context_budget_tokens"]


def test_invalid_recipe_context_budget_is_classified_without_crashing() -> None:
    result = classify_workload_demand(
        _workload(),
        _readiness(_recipe(context_budget_tokens=32768.5)),
    )

    assert result["category"] == "provider_missing"
    assert result["shipment_ready"] is False
    assert any(blocker["reason"] == "no_contract_compatible_readiness_recipe" for blocker in result["blockers"])


def test_shipment_report_summary_counts_only_complete_typed_blockers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contract = tmp_path / "workload-contract.json"
    contract.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "workload-contract",
                "source": "fixture",
                "workloads": [
                    _workload("rank_text_items"),
                    _workload("summarize_text"),
                    _workload("extract_json"),
                ],
            }
        ),
        encoding="utf-8",
    )
    validation_row = {
        "tuple": "runpod-h100",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "missing_route_validation_evidence",
    }
    price_row = {
        "tuple": "runpod-h100",
        "mode": "sync",
        "price_freshness": "unknown",
        "route_validation_required": True,
        "live_validation_artifact": "/state/tuple-validation/ok.json",
    }

    def fake_readiness(*, intent: str, **_kwargs: object) -> dict[str, object]:
        if intent == "rank_text_items":
            return _readiness(_recipe(intent, eligible_tuples=[validation_row], live_ready_tuples=[], live_blocked_tuples=[validation_row]))
        if intent == "summarize_text":
            return _readiness(_recipe(intent, eligible_tuples=[], live_ready_tuples=[], live_blocked_tuples=[]))
        return _readiness(_recipe(intent, eligible_tuples=[price_row], live_ready_tuples=[price_row], live_blocked_tuples=[]))

    monkeypatch.setattr("gpucall.shipment_gap.build_readiness_report", fake_readiness)

    report = build_shipment_gap_report(config_dir=tmp_path, contract_path=contract)
    blockers = [blocker for demand in report["demands"] for blocker in demand["blockers"]]

    assert report["go"] is False
    assert len(blockers) == 3
    for blocker in blockers:
        _assert_shipment_blocker_shape(blocker)
    assert report["summary"]["owner_counts"] == dict(sorted(Counter(blocker["owner"] for blocker in blockers).items()))
    assert report["summary"]["code_counts"] == dict(sorted(Counter(blocker["code"] for blocker in blockers).items()))
    assert report["summary"]["handoff_counts"] == dict(sorted(Counter(blocker["handoff"] for blocker in blockers).items()))
