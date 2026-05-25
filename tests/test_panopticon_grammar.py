from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from gpucall.panopticon import load_panopticon_evidence, store_panopticon_evidence
from gpucall.price_cache import load_cached_price_evidence


def test_panopticon_rejects_unknown_fields(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": now.isoformat(),
                "tuples": {
                    "runpod-h100": {
                        "tuple": "runpod-h100",
                        "adapter": "runpod",
                        "status": "live_revalidated",
                        "checked": True,
                        "unexpected_key": "fail_me",
                        "findings": [],
                        "dimensions": [],
                        "observed_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=300)).isoformat(),
                        "ttl_seconds": 300,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        load_panopticon_evidence(path)
    
    assert "Extra inputs are not permitted" in str(excinfo.value)


def test_panopticon_enforces_status_consistency(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": now.isoformat(),
                "tuples": {
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
                                "severity": "error",
                                "reason": "out of stock",
                                "observed_at": now.isoformat(),
                                "expires_at": (now + timedelta(seconds=300)).isoformat(),
                            }
                        ],
                        "dimensions": ["stock"],
                        "observed_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=300)).isoformat(),
                        "ttl_seconds": 300,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        load_panopticon_evidence(path)
    
    assert "status must be blocked" in str(excinfo.value)


def test_panopticon_rejects_time_paradox(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    
    evidence = {
        "runpod-h100": {
            "tuple": "runpod-h100",
            "adapter": "runpod",
            "status": "live_revalidated",
            "checked": True,
            "findings": [
                {
                    "tuple": "runpod-h100",
                    "adapter": "runpod",
                    "dimension": "price",
                    "severity": "info",
                    "source": "test",
                    "live_price_per_second": 0.00042,
                    "observed_at": now.isoformat(),
                    "expires_at": (now - timedelta(seconds=1)).isoformat(),
                }
            ],
        }
    }

    with pytest.raises(ValidationError) as excinfo:
        store_panopticon_evidence(evidence, path, now=now)
    
    assert "expires_at must be >= observed_at" in str(excinfo.value)


def test_panopticon_backward_compatibility_finding_singular(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tuples": {
                    "runpod-h100": {
                        "tuple": "runpod-h100",
                        "adapter": "runpod",
                        "status": "live_revalidated",
                        "checked": True,
                        "finding": {
                            "tuple": "runpod-h100",
                            "adapter": "runpod",
                            "dimension": "price",
                            "severity": "info",
                            "source": "legacy",
                            "live_price_per_second": 0.00042,
                        },
                        "observed_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=3600)).isoformat(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_panopticon_evidence(path, now=now)

    loaded = load_cached_price_evidence(path, now=now)
    assert len(loaded["runpod-h100"]["findings"]) == 1
    assert loaded["runpod-h100"]["findings"][0]["live_price_per_second"] == 0.00042


def test_panopticon_merge_validates_and_dedupes() -> None:
    from gpucall.panopticon import merge_panopticon_evidence
    
    finding = {
        "tuple": "runpod-h100",
        "adapter": "runpod",
        "dimension": "stock",
        "severity": "info",
        "live_stock_state": "available",
    }
    
    evidence1 = {
        "runpod-h100": {
            "tuple": "runpod-h100",
            "status": "live_revalidated",
            "checked": True,
            "findings": [finding],
        }
    }
    
    # Second evidence has same finding
    evidence2 = {
        "runpod-h100": {
            "tuple": "runpod-h100",
            "status": "live_revalidated",
            "checked": True,
            "findings": [finding],
        }
    }
    
    merged = merge_panopticon_evidence(evidence1, evidence2)
    
    assert len(merged["runpod-h100"]["findings"]) == 1
    assert merged["runpod-h100"]["status"] == "live_revalidated"


def test_panopticon_merge_promotes_blocked() -> None:
    from gpucall.panopticon import merge_panopticon_evidence
    
    evidence1 = {
        "runpod-h100": {
            "tuple": "runpod-h100",
            "adapter": "runpod",
            "status": "unknown",
            "checked": False,
            "findings": [],
        }
    }
    
    evidence2 = {
        "runpod-h100": {
            "tuple": "runpod-h100",
            "status": "blocked",
            "checked": True,
            "findings": [
                {
                    "tuple": "runpod-h100",
                    "adapter": "runpod",
                    "dimension": "stock",
                    "severity": "error",
                    "reason": "dead",
                }
            ],
        }
    }
    
    merged = merge_panopticon_evidence(evidence1, evidence2)
    assert merged["runpod-h100"]["status"] == "blocked"


def test_panopticon_rejects_tuple_key_mismatch(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": now.isoformat(),
                "tuples": {
                    "runpod-h100": {
                        "tuple": "runpod-a100",
                        "adapter": "runpod",
                        "status": "unknown",
                        "checked": False,
                        "findings": [],
                        "dimensions": [],
                        "observed_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=300)).isoformat(),
                        "ttl_seconds": 300,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="tuple mapping key must match row tuple"):
        load_panopticon_evidence(path, now=now)


def test_panopticon_rejects_finding_unknown_field(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
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
                            "workerz": 1,
                        }
                    ],
                }
            },
            path,
            now=now,
        )


def test_panopticon_rejects_price_info_without_price(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)

    with pytest.raises(ValidationError, match="price info finding requires live_price_per_second"):
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
                            "dimension": "price",
                            "severity": "info",
                            "source": "runpod",
                        }
                    ],
                }
            },
            path,
            now=now,
        )


def test_panopticon_rejects_stock_info_without_stock_state(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)

    with pytest.raises(ValidationError, match="stock info finding requires live_stock_state"):
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
                        }
                    ],
                }
            },
            path,
            now=now,
        )


def test_panopticon_rejects_dimensions_mismatch(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": now.isoformat(),
                "tuples": {
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
                                "observed_at": now.isoformat(),
                                "expires_at": (now + timedelta(seconds=300)).isoformat(),
                            }
                        ],
                        "dimensions": ["price"],
                        "observed_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=300)).isoformat(),
                        "ttl_seconds": 300,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="dimensions must equal sorted unique finding dimensions"):
        load_panopticon_evidence(path, now=now)


def test_panopticon_rejects_live_revalidated_without_findings(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": now.isoformat(),
                "tuples": {
                    "runpod-h100": {
                        "tuple": "runpod-h100",
                        "adapter": "runpod",
                        "status": "live_revalidated",
                        "checked": True,
                        "findings": [],
                        "dimensions": [],
                        "observed_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=300)).isoformat(),
                        "ttl_seconds": 300,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="requires at least one finding"):
        load_panopticon_evidence(path, now=now)


def test_panopticon_preserves_raw_but_extracts_structured_details(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    store_panopticon_evidence(
        {
            "runpod-h100": {
                "tuple": "runpod-h100",
                "adapter": "runpod",
                "status": "blocked",
                "checked": True,
                "findings": [
                    {
                        "tuple": "runpod-h100",
                        "adapter": "runpod",
                        "dimension": "endpoint",
                        "severity": "error",
                        "reason": "endpoint missing",
                        "raw": {
                            "endpoint_id": "30g7ze5wb2n3xw",
                            "live_reason": "endpoint_missing_from_inventory",
                            "provider_private_payload": {"kept": True},
                        },
                    }
                ],
            }
        },
        path,
        now=now,
    )

    loaded = load_panopticon_evidence(path, now=now)
    finding = loaded["runpod-h100"]["findings"][0]
    assert finding["details"]["endpoint_id"] == "30g7ze5wb2n3xw"
    assert finding["details"]["live_reason"] == "endpoint_missing_from_inventory"
    assert finding["raw"]["provider_private_payload"] == {"kept": True}


def test_panopticon_accepts_storage_dimension_and_extracts_resource_details(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    store_panopticon_evidence(
        {
            "runpod-network-volume-le9b9gqqu6": {
                "tuple": "runpod-network-volume-le9b9gqqu6",
                "adapter": "runpod",
                "status": "blocked",
                "checked": True,
                "findings": [
                    {
                        "tuple": "runpod-network-volume-le9b9gqqu6",
                        "adapter": "runpod",
                        "dimension": "storage",
                        "severity": "error",
                        "reason": "unattached persistent storage",
                        "raw": {
                            "resource_type": "network_volume",
                            "resource_id": "le9b9gqqu6",
                            "resource_name": "news-llm-models",
                            "data_center_id": "US-NC-1",
                            "storage_size_gb": 80,
                            "estimated_monthly_usd": 5.6,
                            "attached_endpoint_count": 0,
                            "attached_endpoint_ids": [],
                            "declared_by_tuple_count": 0,
                            "declared_by_tuples": [],
                            "content_inventory_status": "missing_runpod_s3_credentials",
                            "live_reason": "persistent_storage_unattached_undeclared",
                        },
                    }
                ],
            }
        },
        path,
        now=now,
    )

    loaded = load_panopticon_evidence(path, now=now)
    row = loaded["runpod-network-volume-le9b9gqqu6"]
    finding = row["findings"][0]
    assert row["dimensions"] == ["storage"]
    assert finding["details"]["resource_type"] == "network_volume"
    assert finding["details"]["resource_id"] == "le9b9gqqu6"
    assert finding["details"]["storage_size_gb"] == 80
    assert finding["details"]["estimated_monthly_usd"] == 5.6
    assert finding["details"]["content_inventory_status"] == "missing_runpod_s3_credentials"


def test_panopticon_normalizes_naive_datetimes_to_utc(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 19, 8, 0)
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

    loaded = load_panopticon_evidence(path, now=datetime(2026, 5, 19, 8, 1))
    assert loaded["runpod-h100"]["panopticon_age_seconds"] == 60.0


def test_panopticon_rejects_bool_and_infinite_price_values(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime.now(timezone.utc)
    for price in (True, float("inf")):
        with pytest.raises(ValidationError, match="numeric evidence value must be finite|boolean is not a valid numeric evidence value"):
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
                                "dimension": "price",
                                "severity": "info",
                                "source": "runpod",
                                "live_price_per_second": price,
                            }
                        ],
                    }
                },
                path,
                now=now,
            )
