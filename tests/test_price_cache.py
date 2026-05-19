from __future__ import annotations

import json
from datetime import datetime, timezone

from gpucall.panopticon import store_panopticon_evidence
from gpucall.live_catalog import price_per_second_from_mapping
from gpucall.price_cache import load_cached_price_evidence, store_live_price_evidence


def test_price_cache_persists_live_price_with_ttl(tmp_path) -> None:
    path = tmp_path / "live-price-cache.json"
    now = datetime(2026, 5, 8, tzinfo=timezone.utc)
    store_live_price_evidence(
        {
            "modal-a10g": {
                "findings": [
                    {
                        "tuple": "modal-a10g",
                        "adapter": "modal",
                        "dimension": "price",
                        "severity": "info",
                        "source": "test",
                        "live_price_per_second": 0.000306,
                    }
                ]
            }
        },
        path,
        now=now,
        ttl_seconds=3600,
    )

    loaded = load_cached_price_evidence(path, now=now)

    assert loaded["modal-a10g"]["status"] == "live_revalidated"
    assert loaded["modal-a10g"]["findings"][0]["live_price_per_second"] == 0.000306


def test_price_cache_marks_expired_price_unknown(tmp_path) -> None:
    path = tmp_path / "live-price-cache.json"
    now = datetime(2026, 5, 8, tzinfo=timezone.utc)
    store_live_price_evidence(
        {
            "modal-a10g": {
                "findings": [
                    {
                        "tuple": "modal-a10g",
                        "adapter": "modal",
                        "dimension": "price",
                        "severity": "info",
                        "source": "test",
                        "live_price_per_second": 0.000306,
                    }
                ]
            }
        },
        path,
        now=now,
        ttl_seconds=1,
    )

    loaded = load_cached_price_evidence(path, now=datetime(2026, 5, 8, 0, 0, 2, tzinfo=timezone.utc))

    assert loaded["modal-a10g"]["status"] == "unknown"
    assert loaded["modal-a10g"]["findings"][0]["reason"] == "cached live price TTL expired"


def test_price_cache_does_not_return_stale_live_price_from_mixed_snapshot(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 8, tzinfo=timezone.utc)
    store_panopticon_evidence(
        {
            "modal-a10g": {
                "tuple": "modal-a10g",
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "modal-a10g",
                        "adapter": "modal",
                        "dimension": "price",
                        "severity": "info",
                        "source": "test",
                        "live_price_per_second": 0.000306,
                    },
                    {
                        "tuple": "modal-a10g",
                        "adapter": "modal",
                        "dimension": "stock",
                        "severity": "info",
                        "live_stock_state": "available",
                    },
                ],
            }
        },
        path,
        now=now,
        ttl_seconds=1,
    )

    loaded = load_cached_price_evidence(path, now=datetime(2026, 5, 8, 0, 0, 2, tzinfo=timezone.utc))

    assert loaded["modal-a10g"]["status"] == "unknown"
    assert loaded["modal-a10g"]["findings"][0]["reason"] == "cached live price TTL expired"
    assert "live_price_per_second" not in loaded["modal-a10g"]["findings"][0]


def test_price_cache_ignores_stock_only_stale_snapshot(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 8, tzinfo=timezone.utc)
    store_panopticon_evidence(
        {
            "modal-a10g": {
                "tuple": "modal-a10g",
                "adapter": "modal",
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "modal-a10g",
                        "adapter": "modal",
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

    assert load_cached_price_evidence(path, now=datetime(2026, 5, 8, 0, 0, 2, tzinfo=timezone.utc)) == {}


def test_price_cache_keeps_fresh_price_when_other_dimension_expired(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    now = datetime(2026, 5, 8, tzinfo=timezone.utc)
    store_panopticon_evidence(
        {
            "modal-a10g": {
                "tuple": "modal-a10g",
                "adapter": "modal",
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "modal-a10g",
                        "adapter": "modal",
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
    store_live_price_evidence(
        {
            "modal-a10g": {
                "findings": [
                    {
                        "tuple": "modal-a10g",
                        "adapter": "modal",
                        "dimension": "price",
                        "severity": "info",
                        "source": "test",
                        "live_price_per_second": 0.000306,
                    }
                ]
            }
        },
        path,
        now=now,
        ttl_seconds=3600,
    )

    loaded = load_cached_price_evidence(path, now=datetime(2026, 5, 8, 0, 0, 2, tzinfo=timezone.utc))

    assert loaded["modal-a10g"]["findings"][0]["live_price_per_second"] == 0.000306


def test_price_cache_reads_legacy_single_finding_snapshot(tmp_path) -> None:
    path = tmp_path / "live-price-cache.json"
    now = datetime(2026, 5, 8, tzinfo=timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tuples": {
                    "modal-a10g": {
                        "finding": {
                            "severity": "info",
                            "live_price_per_second": 0.000306,
                        },
                        "observed_at": now.isoformat(),
                        "expires_at": datetime(2026, 5, 8, 1, 0, tzinfo=timezone.utc).isoformat(),
                        "ttl_seconds": 3600,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_cached_price_evidence(path, now=now)

    assert loaded["modal-a10g"]["status"] == "live_revalidated"
    assert loaded["modal-a10g"]["findings"][0]["live_price_per_second"] == 0.000306


def test_live_price_parser_rejects_bool_and_infinite_values() -> None:
    assert price_per_second_from_mapping({"price_per_second": True}) == (None, None)
    assert price_per_second_from_mapping({"price_per_second": "inf"}) == (None, None)
