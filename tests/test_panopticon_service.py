from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gpucall.domain import ExecutionTupleSpec
from gpucall.panopticon_service import (
    PANOPTICON_DEFAULT_PORT,
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
    )
    monkeypatch.setattr("gpucall.panopticon_service.load_config", lambda _config_dir: SimpleNamespace(tuples={"runpod-h100": tuple_spec}))
    monkeypatch.setattr("gpucall.panopticon_service.load_credentials", lambda: {"runpod": {"api_key": "test"}})

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
