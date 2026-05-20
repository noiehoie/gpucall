from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpucall.shipment_gap import build_shipment_gap_report, classify_workload_demand


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


def _recipe(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "recipe": "infer-rank-text-items-light",
        "task": "infer",
        "intent": "rank_text_items",
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
    return base


def test_classifies_shipment_ready() -> None:
    result = classify_workload_demand(_workload(), _readiness(_recipe()))

    assert result["category"] == "shipment_ready"
    assert result["label"] == "出荷可能"
    assert result["blockers"] == []


def test_classifies_validation_missing() -> None:
    row = {
        "tuple": "runpod-h100",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "missing_route_validation_evidence",
    }
    result = classify_workload_demand(
        _workload(),
        _readiness(
            _recipe(
                production_activated=False,
                eligible_tuples=[row],
                live_ready_tuple_count=0,
                live_ready_tuples=[],
                live_blocked_tuples=[row],
            )
        ),
    )

    assert result["category"] == "validation_missing"
    assert result["blockers"][0]["label"] == "validation 不足"


def test_classifies_price_unknown_even_when_route_is_live_ready() -> None:
    row = {
        "tuple": "runpod-h100",
        "mode": "sync",
        "price_freshness": "unknown",
        "route_validation_required": True,
        "live_validation_artifact": "/state/tuple-validation/ok.json",
    }
    result = classify_workload_demand(
        _workload(),
        _readiness(_recipe(eligible_tuples=[row], live_ready_tuples=[row])),
    )

    assert result["category"] == "price_unknown"
    assert result["blockers"][0]["label"] == "price 不明"


def test_ignores_blocked_tuple_for_unrequested_mode_when_requested_mode_is_ready() -> None:
    sync_ready = {
        "tuple": "sync-ok",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_validation_artifact": "/state/tuple-validation/sync-ok.json",
    }
    async_blocked = {
        "tuple": "async-missing-validation",
        "mode": "async",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "missing_route_validation_evidence",
    }
    result = classify_workload_demand(
        _workload(modes=["sync"]),
        _readiness(
            _recipe(
                allowed_modes=["sync", "async"],
                eligible_tuples=[sync_ready, async_blocked],
                live_ready_tuples=[sync_ready],
                live_blocked_tuples=[async_blocked],
            )
        ),
    )

    assert result["category"] == "shipment_ready"
    assert result["blockers"] == []


def test_classifies_endpoint_stale_before_validation_missing() -> None:
    row = {
        "tuple": "runpod-a100-dead",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "endpoint_missing_from_inventory",
        "live_catalog_findings": [{"reason": "configured endpoint not present", "status_code": 404}],
    }
    result = classify_workload_demand(
        _workload(),
        _readiness(
            _recipe(
                production_activated=False,
                eligible_tuples=[row],
                live_ready_tuple_count=0,
                live_ready_tuples=[],
                live_blocked_tuples=[row],
            )
        ),
    )

    assert result["category"] == "endpoint_stale"
    assert {item["category"] for item in result["blockers"]} == {"endpoint_stale", "validation_missing"}


def test_classifies_provider_missing_when_no_compatible_recipe() -> None:
    result = classify_workload_demand(
        _workload(context=131072),
        _readiness(_recipe(context_budget_tokens=32768)),
    )

    assert result["category"] == "provider_missing"
    assert result["blockers"][0]["reason"] == "no_contract_compatible_readiness_recipe"


def test_readiness_status_override_adds_matching_blocker() -> None:
    result = classify_workload_demand(
        _workload(),
        _readiness(_recipe(shipment_status="validation_lack")),
    )

    assert result["category"] == "validation_missing"
    assert result["blockers"] == [
        {
            "category": "validation_missing",
            "label": "validation 不足",
            "reason": "readiness_shipment_status_validation_lack",
        }
    ]


def test_build_report_reuses_readiness_for_duplicate_intents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contract = tmp_path / "workload-contract.json"
    contract.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "workload-contract",
                "source": "fixture",
                "workloads": [_workload(), _workload()],
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_readiness(**kwargs: object) -> dict[str, object]:
        calls.append(str(kwargs["intent"]))
        return _readiness(_recipe())

    monkeypatch.setattr("gpucall.shipment_gap.build_readiness_report", fake_readiness)

    report = build_shipment_gap_report(config_dir=tmp_path, contract_path=contract)

    assert calls == ["rank_text_items"]
    assert report["summary"]["shipment_ready_count"] == 2


def test_build_report_rejects_invalid_contract_json(tmp_path: Path) -> None:
    contract = tmp_path / "workload-contract.json"
    contract.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        build_shipment_gap_report(config_dir=tmp_path, contract_path=contract)


def test_shipment_check_cli_fails_on_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from gpucall.cli import main

    contract = tmp_path / "workload-contract.json"
    contract.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "workload-contract",
                "source": "fixture",
                "workloads": [_workload()],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gpucall.cli.build_shipment_gap_report",
        lambda **_kwargs: {
            "schema_version": 1,
            "phase": "product-shipment-gap",
            "go": False,
            "summary": {"blocker_count": 1},
            "demands": [],
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "gpucall",
            "shipment-check",
            "--config-dir",
            str(tmp_path),
            "--contract",
            str(contract),
            "--fail-on-blocker",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["phase"] == "product-shipment-gap"
    assert output["go"] is False
    assert output["summary"]["blocker_count"] == 1
