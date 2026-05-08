from __future__ import annotations

from datetime import datetime, timezone

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
