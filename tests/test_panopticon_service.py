from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gpucall.domain import ExecutionTupleSpec
from gpucall.panopticon_service import (
    PANOPTICON_DEFAULT_PORT,
    PANOPTICON_DEFAULT_REFRESH_INTERVAL_SECONDS,
    assert_safe_panopticon_host,
    create_panopticon_app,
    refresh_panopticon,
    snapshot_panopticon,
)


def test_refresh_panopticon_writes_strict_snapshot(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-panopticon.json"
    _patch_one_tuple_config(monkeypatch)

    report = refresh_panopticon(config_dir=tmp_path, panopticon_path=path, tuple_names=["runpod-h100"])

    assert report["phase"] == "provider-panopticon-refresh"
    assert report["non_generation_probe_only"] is True
    assert report["observed_tuple_count"] == 1
    assert report["selected_tuple_count"] == 1
    assert report["snapshot"]["runpod-h100"]["status"] == "live_revalidated"
    assert report["snapshot"]["runpod-h100"]["dimensions"] == ["stock"]
    assert json.loads(path.read_text(encoding="utf-8"))["tuples"]["runpod-h100"]["adapter"] == "runpod-vllm-serverless"


def test_refresh_panopticon_missing_provider_credentials_returns_bounded_blocker(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-panopticon.json"
    tuple_spec = ExecutionTupleSpec(
        name="runpod-h100",
        adapter="runpod-vllm-serverless",
        gpu="H100",
        vram_gb=80,
        max_model_len=8192,
        cost_per_second=0.001,
        target="rp-endpoint",
    )
    monkeypatch.setattr("gpucall.panopticon_service.load_config", lambda _config_dir: SimpleNamespace(tuples={"runpod-h100": tuple_spec}))
    monkeypatch.setattr("gpucall.panopticon_service.load_credentials", lambda: {})
    monkeypatch.setattr("gpucall.panopticon_service.configured_credentials", lambda: [])

    def fail_live_tuple_catalog_evidence(_tuples, _credentials):
        raise AssertionError("missing provider credentials must not call live provider probes")

    monkeypatch.setattr("gpucall.panopticon_service.live_tuple_catalog_evidence", fail_live_tuple_catalog_evidence)

    report = refresh_panopticon(config_dir=tmp_path, panopticon_path=path, tuple_names=["runpod-h100"])

    assert report["phase"] == "provider-panopticon-refresh"
    assert report["status"] == "blocked"
    assert report["non_generation_probe_only"] is True
    assert report["observed_tuple_count"] == 0
    assert report["selected_tuple_count"] == 1
    assert report["provider_counts"] == {"runpod": 1}
    assert report["probe_tuple_count"] == 0
    assert report["skipped_tuple_count"] == 1
    assert report["skipped_provider_counts"] == {"runpod": 1}
    assert report["snapshot"] == {}
    assert not path.exists()
    assert report["blockers"] == [
        {
            "code": "PROVIDER_CREDENTIALS_MISSING",
            "owner": "gpucall-admin",
            "provider": "runpod",
            "tuple_count": 1,
            "missing_contracts": ["api_key:runpod"],
            "next_action": "Run `gpucall configure runpod-serverless` or add providers.runpod.api_key to the gpucall credentials store.",
        }
    ]


def test_refresh_panopticon_configured_provider_missing_target_returns_bounded_blocker(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-panopticon.json"
    tuple_spec = ExecutionTupleSpec(
        name="runpod-h100",
        adapter="runpod-vllm-serverless",
        gpu="H100",
        vram_gb=80,
        max_model_len=8192,
        cost_per_second=0.001,
    )
    monkeypatch.setattr("gpucall.panopticon_service.load_config", lambda _config_dir: SimpleNamespace(tuples={"runpod-h100": tuple_spec}))
    monkeypatch.setattr("gpucall.panopticon_service.load_credentials", lambda: {"runpod": {"api_key": "test"}})
    monkeypatch.setattr("gpucall.panopticon_service.configured_credentials", lambda: [])

    def fail_live_tuple_catalog_evidence(_tuples, _credentials):
        raise AssertionError("missing provider targets must not call live provider probes")

    monkeypatch.setattr("gpucall.panopticon_service.live_tuple_catalog_evidence", fail_live_tuple_catalog_evidence)

    report = refresh_panopticon(config_dir=tmp_path, panopticon_path=path, tuple_names=["runpod-h100"])

    assert report["phase"] == "provider-panopticon-refresh"
    assert report["status"] == "blocked"
    assert report["observed_tuple_count"] == 0
    assert report["provider_counts"] == {"runpod": 1}
    assert report["probe_tuple_count"] == 0
    assert report["skipped_tuple_count"] == 1
    assert report["skipped_provider_counts"] == {"runpod": 1}
    assert report["snapshot"] == {}
    assert not path.exists()
    assert report["blockers"] == [
        {
            "code": "PROVIDER_ENDPOINT_TARGET_MISSING",
            "owner": "provider-ops",
            "provider": "runpod",
            "tuple_count": 1,
            "missing_fields": ["target"],
            "next_action": "Run provider supply provisioning for RunPod, or set tuple target to a live RunPod endpoint before refreshing endpoint evidence.",
        }
    ]


def test_refresh_panopticon_live_probe_timeout_returns_bounded_evidence(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-panopticon.json"
    tuple_spec = ExecutionTupleSpec(
        name="runpod-h100",
        adapter="runpod-vllm-serverless",
        gpu="H100",
        vram_gb=80,
        max_model_len=8192,
        cost_per_second=0.001,
        target="rp-endpoint",
    )
    monkeypatch.setenv("GPUCALL_PANOPTICON_REFRESH_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr("gpucall.panopticon_service._probe_start_method", lambda: "fork")
    monkeypatch.setattr("gpucall.panopticon_service.load_config", lambda _config_dir: SimpleNamespace(tuples={"runpod-h100": tuple_spec}))
    monkeypatch.setattr("gpucall.panopticon_service.load_credentials", lambda: {"runpod": {"api_key": "test"}})
    monkeypatch.setattr("gpucall.panopticon_service.configured_credentials", lambda: [])

    def hanging_live_tuple_catalog_evidence(_tuples, _credentials):
        time.sleep(5)
        return {}

    monkeypatch.setattr("gpucall.panopticon_service.live_tuple_catalog_evidence", hanging_live_tuple_catalog_evidence)

    started = time.monotonic()
    report = refresh_panopticon(config_dir=tmp_path, panopticon_path=path, tuple_names=["runpod-h100"])
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert report["probe_timeout_seconds"] == 0.1
    assert report["observed_tuple_count"] == 1
    assert report["snapshot"]["runpod-h100"]["status"] == "blocked"
    finding = report["snapshot"]["runpod-h100"]["findings"][0]
    assert finding["dimension"] == "live_tuple_catalog"
    assert "timed out after 0.1s" in finding["reason"]


def test_panopticon_refresh_start_method_env_override(monkeypatch) -> None:
    import gpucall.panopticon_service as service

    monkeypatch.setenv("GPUCALL_PANOPTICON_REFRESH_START_METHOD", "fork")
    monkeypatch.setattr(service.mp, "get_all_start_methods", lambda: ["fork", "spawn"])

    assert service._probe_start_method() == "fork"


def test_panopticon_refresh_start_method_rejects_unknown_env(monkeypatch) -> None:
    import gpucall.panopticon_service as service

    monkeypatch.setenv("GPUCALL_PANOPTICON_REFRESH_START_METHOD", "missing")
    monkeypatch.setattr(service.mp, "get_all_start_methods", lambda: ["fork", "spawn"])

    with pytest.raises(ValueError, match="unsupported Panopticon refresh start method"):
        service._probe_start_method()


def test_panopticon_app_refresh_missing_credentials_returns_bounded_blocker(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-panopticon.json"
    tuple_spec = ExecutionTupleSpec(
        name="runpod-h100",
        adapter="runpod-vllm-serverless",
        gpu="H100",
        vram_gb=80,
        max_model_len=8192,
        cost_per_second=0.001,
        target="rp-endpoint",
    )
    monkeypatch.setattr("gpucall.panopticon_service.load_config", lambda _config_dir: SimpleNamespace(tuples={"runpod-h100": tuple_spec}))
    monkeypatch.setattr("gpucall.panopticon_service.load_credentials", lambda: {})
    monkeypatch.setattr("gpucall.panopticon_service.configured_credentials", lambda: [])

    def fail_live_tuple_catalog_evidence(_tuples, _credentials):
        raise AssertionError("missing provider credentials must not call live provider probes")

    monkeypatch.setattr("gpucall.panopticon_service.live_tuple_catalog_evidence", fail_live_tuple_catalog_evidence)
    app = create_panopticon_app(config_dir=tmp_path, panopticon_path=path, refresh_interval_seconds=None)

    with TestClient(app) as client:
        refreshed = client.post("/v1/refresh")

    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["status"] == "blocked"
    assert body["observed_tuple_count"] == 0
    assert body["probe_tuple_count"] == 0
    assert body["skipped_tuple_count"] == 1
    assert body["blockers"][0]["code"] == "PROVIDER_CREDENTIALS_MISSING"


def test_snapshot_panopticon_does_not_call_live_probe(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-panopticon.json"
    _patch_one_tuple_config(monkeypatch)
    refresh_panopticon(config_dir=tmp_path, panopticon_path=path)

    def fail_live_probe(_tuples, _credentials):
        raise AssertionError("snapshot must not call live provider probes")

    monkeypatch.setattr("gpucall.panopticon_service.live_tuple_catalog_evidence", fail_live_probe)

    report = snapshot_panopticon(panopticon_path=path)

    assert report["phase"] == "provider-panopticon-snapshot"
    assert report["snapshot"]["runpod-h100"]["status"] == "live_revalidated"


def test_panopticon_app_refresh_and_snapshot(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-panopticon.json"
    _patch_one_tuple_config(monkeypatch)
    app = create_panopticon_app(config_dir=tmp_path, panopticon_path=path, refresh_interval_seconds=None)

    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["service"] == "provider-panopticon"

        refreshed = client.post("/v1/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["snapshot"]["runpod-h100"]["status"] == "live_revalidated"

        snapshot = client.get("/v1/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["tuple_count"] == 1


def test_panopticon_cli_refresh_outputs_json(tmp_path, monkeypatch, capsys) -> None:
    from gpucall.cli import main

    path = tmp_path / "provider-panopticon.json"
    _patch_one_tuple_config(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gpucall",
            "panopticon",
            "refresh",
            "--config-dir",
            str(tmp_path),
            "--panopticon-path",
            str(path),
            "--tuple",
            "runpod-h100",
        ],
    )

    main()
    output = json.loads(capsys.readouterr().out)

    assert output["phase"] == "provider-panopticon-refresh"
    assert output["snapshot"]["runpod-h100"]["status"] == "live_revalidated"


def test_panopticon_cli_refresh_unknown_tuple_exits_without_traceback(tmp_path, monkeypatch) -> None:
    from gpucall.cli import main

    path = tmp_path / "provider-panopticon.json"
    _patch_one_tuple_config(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gpucall",
            "panopticon",
            "refresh",
            "--config-dir",
            str(tmp_path),
            "--panopticon-path",
            str(path),
            "--tuple",
            "missing-tuple",
        ],
    )

    with pytest.raises(SystemExit, match="unknown tuple"):
        main()


def test_panopticon_cli_serve_defaults_to_localhost(tmp_path, monkeypatch) -> None:
    from gpucall.cli import main

    called: dict[str, object] = {}

    def fake_run(app, *, host, port):
        called["host"] = host
        called["port"] = port
        called["title"] = app.title

    monkeypatch.setattr("gpucall.cli_commands.panopticon.uvicorn.run", fake_run)
    monkeypatch.setattr(sys, "argv", ["gpucall", "panopticon", "serve", "--config-dir", str(tmp_path), "--no-refresh-loop"])

    main()

    assert called == {"host": "127.0.0.1", "port": PANOPTICON_DEFAULT_PORT, "title": "gpucall Provider Panopticon"}


def test_panopticon_default_refresh_interval_stays_inside_snapshot_ttl() -> None:
    assert PANOPTICON_DEFAULT_REFRESH_INTERVAL_SECONDS < 300


def test_panopticon_cli_serve_enables_default_refresh_loop(tmp_path, monkeypatch) -> None:
    from gpucall.cli import main

    observed: dict[str, object] = {}

    def fake_create_panopticon_app(*, config_dir, panopticon_path=None, refresh_interval_seconds=None):
        observed["config_dir"] = config_dir
        observed["panopticon_path"] = panopticon_path
        observed["refresh_interval_seconds"] = refresh_interval_seconds
        return SimpleNamespace(title="gpucall Provider Panopticon")

    def fake_run(app, *, host, port):
        observed["host"] = host
        observed["port"] = port
        observed["title"] = app.title

    monkeypatch.setattr("gpucall.cli_commands.panopticon.create_panopticon_app", fake_create_panopticon_app)
    monkeypatch.setattr("gpucall.cli_commands.panopticon.uvicorn.run", fake_run)
    monkeypatch.setattr(sys, "argv", ["gpucall", "panopticon", "serve", "--config-dir", str(tmp_path)])

    main()

    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == PANOPTICON_DEFAULT_PORT
    assert observed["refresh_interval_seconds"] == PANOPTICON_DEFAULT_REFRESH_INTERVAL_SECONDS


def test_panopticon_serve_rejects_public_bind() -> None:
    with pytest.raises(ValueError, match="local-only"):
        assert_safe_panopticon_host("0.0.0.0")


def test_panopticon_cli_serve_public_bind_exits_without_traceback(tmp_path, monkeypatch) -> None:
    from gpucall.cli import main

    monkeypatch.setattr(
        sys,
        "argv",
        ["gpucall", "panopticon", "serve", "--config-dir", str(tmp_path), "--host", "0.0.0.0", "--no-refresh-loop"],
    )

    with pytest.raises(SystemExit, match="local-only"):
        main()


def _patch_one_tuple_config(monkeypatch) -> None:
    tuple_spec = ExecutionTupleSpec(
        name="runpod-h100",
        adapter="runpod-vllm-serverless",
        gpu="H100",
        vram_gb=80,
        max_model_len=8192,
        cost_per_second=0.001,
        target="rp-endpoint",
    )
    monkeypatch.setattr("gpucall.panopticon_service.load_config", lambda _config_dir: SimpleNamespace(tuples={"runpod-h100": tuple_spec}))
    monkeypatch.setattr("gpucall.panopticon_service.load_credentials", lambda: {"runpod": {"api_key": "test"}})
    monkeypatch.setattr("gpucall.panopticon_service.configured_credentials", lambda: [])
    monkeypatch.setenv("GPUCALL_PANOPTICON_REFRESH_TIMEOUT_SECONDS", "0")

    def fake_live_tuple_catalog_evidence(tuples, credentials):
        assert list(tuples) == ["runpod-h100"]
        assert credentials == {"runpod": {"api_key": "test"}}
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

    monkeypatch.setattr("gpucall.panopticon_service.live_tuple_catalog_evidence", fake_live_tuple_catalog_evidence)
