from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from gpucall.domain import ExecutionTupleSpec
from gpucall.panopticon import store_panopticon_evidence
from gpucall.panopticon_client import PanopticonClientConfig, fetch_panopticon_snapshot, panopticon_client_config_from_env


NOW = datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc)


def test_panopticon_client_file_missing_is_non_fail_closed_by_default(tmp_path) -> None:
    report = fetch_panopticon_snapshot(
        config=PanopticonClientConfig(source_kind="file", path=tmp_path / "missing.json"),
        tuple_scope={"runpod-h100": _tuple("runpod-h100")},
        now=NOW,
    )

    assert report["status"] == "missing"
    assert report["fail_closed"] is False
    assert report["snapshot"] == {}
    assert report["snapshot_hash"] is None


def test_panopticon_client_file_missing_can_fail_closed(tmp_path) -> None:
    report = fetch_panopticon_snapshot(
        config=PanopticonClientConfig(source_kind="file", path=tmp_path / "missing.json", fail_closed_on_missing=True),
        tuple_scope={"runpod-h100": _tuple("runpod-h100")},
        now=NOW,
    )

    assert report["status"] == "missing"
    assert report["fail_closed"] is True
    assert report["snapshot"]["runpod-h100"]["status"] == "blocked"
    assert report["snapshot"]["runpod-h100"]["findings"][0]["raw"]["live_reason"] == "panopticon_snapshot_missing"


def test_panopticon_client_file_snapshot_reports_hash_and_stale(tmp_path) -> None:
    path = tmp_path / "provider-panopticon.json"
    store_panopticon_evidence(_stock_evidence(), path, now=NOW, ttl_seconds=1)

    report = fetch_panopticon_snapshot(
        config=PanopticonClientConfig(source_kind="file", path=path),
        now=datetime(2026, 5, 20, 1, 0, 2, tzinfo=timezone.utc),
    )

    assert report["status"] == "stale"
    assert report["snapshot_hash"].startswith("sha256:")
    assert report["stale_tuple_count"] == 1
    assert report["snapshot"]["runpod-h100"]["panopticon_stale"] is True


def test_panopticon_client_http_success_reads_snapshot_wrapper() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:18090/v1/snapshot"
        return httpx.Response(200, json={"schema_version": 1, "snapshot": _runtime_snapshot()})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = fetch_panopticon_snapshot(
            config=PanopticonClientConfig(source_kind="http", url="http://127.0.0.1:18090"),
            http_client=client,
            now=NOW,
        )

    assert report["status"] == "ok"
    assert report["source_kind"] == "http"
    assert report["snapshot_url"] == "http://127.0.0.1:18090/v1/snapshot"
    assert report["snapshot"]["runpod-h100"]["status"] == "live_revalidated"


def test_panopticon_client_http_unreachable_can_fail_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = fetch_panopticon_snapshot(
            config=PanopticonClientConfig(
                source_kind="http",
                url="http://127.0.0.1:18090",
                fail_closed_on_unreachable=True,
            ),
            tuple_scope={"runpod-h100": _tuple("runpod-h100")},
            http_client=client,
            now=NOW,
        )

    assert report["status"] == "unreachable"
    assert report["fail_closed"] is True
    assert report["snapshot"]["runpod-h100"]["status"] == "blocked"
    assert report["snapshot"]["runpod-h100"]["findings"][0]["raw"]["live_reason"] == "panopticon_snapshot_unreachable"


def test_panopticon_client_http_invalid_can_fail_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"schema_version": 2, "snapshot": {}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = fetch_panopticon_snapshot(
            config=PanopticonClientConfig(
                source_kind="http",
                url="http://127.0.0.1:18090/v1/snapshot",
                fail_closed_on_invalid=True,
            ),
            tuple_scope={"runpod-h100": _tuple("runpod-h100")},
            http_client=client,
            now=NOW,
        )

    assert report["status"] == "invalid"
    assert report["fail_closed"] is True
    assert report["snapshot"]["runpod-h100"]["findings"][0]["raw"]["live_reason"] == "panopticon_snapshot_invalid"


def test_panopticon_client_env_does_not_use_http_without_url(monkeypatch) -> None:
    monkeypatch.delenv("GPUCALL_PANOPTICON_URL", raising=False)
    monkeypatch.delenv("GPUCALL_PANOPTICON_SOURCE", raising=False)
    config = panopticon_client_config_from_env()

    assert config.source_kind == "file"
    assert config.url is None


def _tuple(name: str) -> ExecutionTupleSpec:
    return ExecutionTupleSpec(
        name=name,
        adapter="runpod-vllm-serverless",
        gpu="H100",
        vram_gb=80,
        max_model_len=8192,
        cost_per_second=0.001,
    )


def _stock_evidence() -> dict[str, dict[str, object]]:
    return {
        "runpod-h100": {
            "tuple": "runpod-h100",
            "adapter": "runpod-vllm-serverless",
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
    }


def _runtime_snapshot() -> dict[str, dict[str, object]]:
    return {
        "runpod-h100": {
            "tuple": "runpod-h100",
            "adapter": "runpod-vllm-serverless",
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
            "dimensions": ["stock"],
            "observed_at": NOW.isoformat(),
            "expires_at": datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc).isoformat(),
            "ttl_seconds": 300,
            "panopticon_observed_at": NOW.isoformat(),
            "panopticon_expires_at": datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc).isoformat(),
            "panopticon_ttl_seconds": 300,
            "panopticon_stale": False,
            "panopticon_age_seconds": 0.0,
        }
    }
