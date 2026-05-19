from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from gpucall.panopticon import load_panopticon_evidence, merge_panopticon_evidence, store_panopticon_evidence
from gpucall.price_cache import store_live_price_evidence


def test_panopticon_persists_full_provider_evidence(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc)

    store_panopticon_evidence(
        {
            "runpod-h100": {
                "tuple": "runpod-h100",
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "runpod-h100",
                        "adapter": "runpod-vllm-serverless",
                        "dimension": "stock",
                        "severity": "info",
                        "live_stock_state": "available",
                        "raw": {"workers": {"ready": 1, "running": 1, "inQueue": 0}},
                    },
                    {
                        "tuple": "runpod-h100",
                        "adapter": "runpod-vllm-serverless",
                        "dimension": "price",
                        "severity": "info",
                        "source": "runpod",
                        "live_price_per_second": 0.00042,
                    },
                ],
            }
        },
        path,
        now=now,
    )

    loaded = load_panopticon_evidence(path, now=now)

    assert loaded["runpod-h100"]["status"] == "live_revalidated"
    assert loaded["runpod-h100"]["panopticon_stale"] is False
    assert loaded["runpod-h100"]["panopticon_age_seconds"] == 0.0
    assert {item["dimension"] for item in loaded["runpod-h100"]["findings"]} == {"price", "stock"}
    stock_finding = next(item for item in loaded["runpod-h100"]["findings"] if item["dimension"] == "stock")
    assert stock_finding["details"]["worker_ready"] == 1
    assert stock_finding["details"]["worker_running"] == 1
    assert stock_finding["details"]["worker_in_queue"] == 0


def test_panopticon_expired_snapshot_fails_closed(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc)

    store_panopticon_evidence(
        {
            "runpod-h100": {
                "tuple": "runpod-h100",
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "runpod-h100",
                        "adapter": "runpod-vllm-serverless",
                        "dimension": "stock",
                        "severity": "info",
                        "live_stock_state": "available",
                    }
                ],
            }
        },
        path,
        now=now,
        ttl_seconds=1,
    )

    loaded = load_panopticon_evidence(path, now=datetime(2026, 5, 19, 8, 0, 2, tzinfo=timezone.utc))

    assert loaded["runpod-h100"]["status"] == "blocked"
    assert loaded["runpod-h100"]["panopticon_stale"] is True
    assert loaded["runpod-h100"]["findings"][0]["raw"]["live_reason"] == "panopticon_evidence_expired"


def test_panopticon_price_update_preserves_other_dimensions(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc)

    store_panopticon_evidence(
        {
            "runpod-h100": {
                "tuple": "runpod-h100",
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "runpod-h100",
                        "adapter": "runpod-vllm-serverless",
                        "dimension": "stock",
                        "severity": "info",
                        "live_stock_state": "available",
                    }
                ],
            }
        },
        path,
        now=now,
        ttl_seconds=300,
    )

    store_live_price_evidence(
        {
            "runpod-h100": {
                "findings": [
                    {
                        "tuple": "runpod-h100",
                        "adapter": "runpod-vllm-serverless",
                        "dimension": "price",
                        "severity": "info",
                        "live_price_per_second": 0.00042,
                    }
                ],
            }
        },
        path,
        now=now,
        ttl_seconds=3600,
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    row = raw["tuples"]["runpod-h100"]
    assert row["dimensions"] == ["price", "stock"]
    assert {item["dimension"] for item in row["findings"]} == {"price", "stock"}


def test_panopticon_merge_preserves_dimensions_and_dedupes_findings() -> None:
    finding = {
        "tuple": "runpod-h100",
        "adapter": "runpod-vllm-serverless",
        "dimension": "stock",
        "severity": "info",
        "live_stock_state": "available",
    }
    merged = merge_panopticon_evidence(
        {"runpod-h100": {"tuple": "runpod-h100", "status": "live_revalidated", "checked": True, "findings": [finding]}},
        {"runpod-h100": {"tuple": "runpod-h100", "status": "live_revalidated", "checked": True, "findings": [finding]}},
    )

    assert merged["runpod-h100"]["dimensions"] == ["stock"]
    assert len(merged["runpod-h100"]["findings"]) == 1
    for key, value in finding.items():
        assert merged["runpod-h100"]["findings"][0][key] == value


def test_panopticon_store_refuses_corrupt_snapshot(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid provider panopticon snapshot JSON"):
        store_panopticon_evidence(
            {
                "runpod-h100": {
                    "findings": [
                        {
                            "tuple": "runpod-h100",
                            "dimension": "stock",
                            "severity": "info",
                        }
                    ]
                }
            },
            path,
        )

    assert path.read_text(encoding="utf-8") == "{not-json"


def test_panopticon_snapshot_without_expiry_fails_closed(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tuples": {
                    "runpod-h100": {
                        "tuple": "runpod-h100",
                        "status": "live_revalidated",
                        "checked": True,
                        "dimensions": ["stock"],
                        "findings": [{"tuple": "runpod-h100", "dimension": "stock", "severity": "info"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_panopticon_evidence(path, now=datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc))


def test_panopticon_merge_accepts_loaded_snapshot_output(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc)
    store_panopticon_evidence(
        {
            "runpod-h100": {
                "tuple": "runpod-h100",
                "adapter": "runpod",
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "runpod-h100",
                        "adapter": "runpod",
                        "dimension": "stock",
                        "severity": "info",
                        "live_stock_state": "available",
                    }
                ],
            }
        },
        path,
        now=now,
    )

    loaded = load_panopticon_evidence(path, now=now)
    merged = merge_panopticon_evidence(loaded)

    assert merged["runpod-h100"]["status"] == "live_revalidated"
    assert merged["runpod-h100"]["dimensions"] == ["stock"]
    assert merged["runpod-h100"]["panopticon_observed_at"] == now.isoformat()


def test_panopticon_expired_dimensions_include_expiry_finding_dimension(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc)
    store_panopticon_evidence(
        {
            "runpod-h100": {
                "tuple": "runpod-h100",
                "adapter": "runpod",
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "runpod-h100",
                        "adapter": "runpod",
                        "dimension": "stock",
                        "severity": "info",
                        "live_stock_state": "available",
                    }
                ],
            }
        },
        path,
        now=now,
        ttl_seconds=1,
    )

    loaded = load_panopticon_evidence(path, now=datetime(2026, 5, 19, 8, 0, 2, tzinfo=timezone.utc))

    assert loaded["runpod-h100"]["dimensions"] == ["panopticon", "stock"]
