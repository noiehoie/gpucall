from __future__ import annotations

from pathlib import Path
import json

import yaml

from gpucall.cli import build_launch_report
from gpucall.config import ConfigError


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    root = tmp_path / "config"
    for path in source.rglob("*.yml"):
        target = root / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def test_launch_report_is_go_for_sample_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))

    report = build_launch_report(copy_config(tmp_path), profile="static")

    assert report["go"] is True
    assert report["blockers"] == []
    checks = report["checks"]
    assert checks["config_valid"] is True
    assert checks["secret_scan_ok"] is True
    assert "/v2/tasks/sync" in checks["openapi_paths"]
    assert checks["mvp_scope"]["tasks"] == ["infer", "vision"]
    assert checks["cost_audit"]["tuples"]
    assert all(row["metadata_complete"] for row in checks["cost_audit"]["tuples"])
    assert checks["cleanup_audit"]["ok"] is True


def test_launch_report_blocks_missing_cost_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    root = copy_config(tmp_path)
    provider_path = root / "surfaces" / "modal-a10g.yml"
    tuple = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
    tuple.pop("scaledown_window_seconds", None)
    provider_path.write_text(yaml.safe_dump(tuple, sort_keys=False), encoding="utf-8")

    report = build_launch_report(root, profile="static")

    assert report["go"] is False
    assert any(blocker["check"] == "cost_metadata" for blocker in report["blockers"])


def test_launch_report_blocks_active_cleanup_lease(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    lease = state / "hyperstack_leases.jsonl"
    lease.parent.mkdir(parents=True)
    lease.write_text(
        '{"event":"provision.created","vm_id":"vm-1","expires_at":"2000-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )

    report = build_launch_report(copy_config(tmp_path), profile="static")

    assert report["go"] is False
    assert any(blocker["check"] == "cleanup_audit" for blocker in report["blockers"])


def test_production_launch_report_blocks_without_live_requirements(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))

    report = build_launch_report(copy_config(tmp_path), profile="production")

    assert report["go"] is False
    checks = {blocker["check"] for blocker in report["blockers"]}
    assert "gateway_auth" in checks
    assert "gateway_live_smoke" in checks
    assert "tuple_live_validation" in checks
    assert "tuple_live_cost_audit" in checks
    assert report["checks"]["cost_audit_live_ok"] is False
    assert report["checks"]["cost_audit_live_findings"]
    tuple_live_validation = report["tuple_live_validation"]
    assert tuple_live_validation["missing_tuples"] == tuple_live_validation["required_tuples"]
    labels = {row["label"] for row in tuple_live_validation["required_tuples"]}
    assert "function_runtime:modal-function:qwen2.5-1.5b-instruct:modal-vllm" in labels
    assert "managed_endpoint:openai-chat-completions:qwen2.5-1.5b-instruct:runpod-vllm-openai" not in labels


def test_gateway_smoke_uses_v2_inline_inputs(monkeypatch) -> None:
    from gpucall.cli import _gateway_smoke_summary

    requests: list[dict[str, object]] = []

    class Response:
        def __init__(self, status_code: int, body: dict[str, object]) -> None:
            self.status_code = status_code
            self._body = body

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise AssertionError(f"unexpected status {self.status_code}")

        def json(self) -> dict[str, object]:
            return self._body

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, path: str) -> Response:
            if path == "/healthz":
                return Response(200, {"status": "ok"})
            if path == "/readyz":
                return Response(200, {"status": "ready", "object_store": False})
            raise AssertionError(path)

        def post(self, path: str, *, json: dict[str, object]) -> Response:
            requests.append({"path": path, "json": json})
            assert path == "/v2/tasks/sync"
            assert "messages" not in json
            assert "recipe" not in json
            assert json["inline_inputs"] == {
                "prompt": {
                    "value": "Reply with exactly: gpucall smoke",
                    "content_type": "text/plain",
                }
            }
            return Response(
                200,
                {
                    "plan": {"selected_tuple": "modal-a10g", "recipe_name": "text-infer-light"},
                    "result": {"kind": "inline", "value": "gpucall smoke"},
                },
            )

    monkeypatch.setattr("gpucall.cli.httpx.Client", Client)
    monkeypatch.setattr("gpucall.cli.httpx.post", lambda *args, **kwargs: Response(401, {}))

    summary = _gateway_smoke_summary("http://gateway.example.internal", api_key="gpk_test")

    assert summary["ok"] is True
    assert summary["auth_required"] is True
    assert requests[0]["json"]["task"] == "infer"


def test_required_live_validation_tuples_respects_policy_deny(tmp_path, monkeypatch) -> None:
    from gpucall.cli import _required_live_validation_tuples
    from gpucall.config import load_config

    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    root = copy_config(tmp_path)
    policy_path = root / "policy.yml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy["tuples"]["deny"] = ["modal-vision-a10g"]
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    config = load_config(root)
    required = _required_live_validation_tuples(config)

    assert "modal-vision-a10g" not in {row["tuple"] for row in required}


def test_live_validation_artifact_accepts_policy_only_config_hash_drift(tmp_path, monkeypatch) -> None:
    from gpucall.cli import _git_commit, _live_validation_artifacts_by_tuple
    from gpucall.config import load_config
    from gpucall.execution.contracts import official_contract, official_contract_hash, tuple_evidence_key

    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    root = copy_config(tmp_path)
    config = load_config(root)
    tuple_spec = config.tuples["modal-a10g"]
    contract = official_contract(tuple_spec)
    artifact_dir = tmp_path / "state" / "tuple-validation"
    artifact_dir.mkdir(parents=True)
    payload = {
        "tuple": tuple_spec.name,
        "recipe": "text-infer-light",
        "mode": "sync",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "commit": _git_commit(),
        "config_hash": "policy-only-drift",
        "governance_hash": "c" * 64,
        "validation_schema_version": 1,
        "passed": True,
        "cleanup": {"required": False, "completed": None},
        "cost": {"observed": None, "estimated": None},
        "audit": {"event_ids": []},
        "official_contract": contract,
        "official_contract_hash": official_contract_hash(contract),
    }
    (artifact_dir / "modal-a10g.json").write_text(json.dumps(payload), encoding="utf-8")

    artifacts = _live_validation_artifacts_by_tuple(config, config_dir=root)

    assert tuple_evidence_key(tuple_spec) in artifacts


def test_launch_report_blocks_smoke_provider_in_auto_recipe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    root = copy_config(tmp_path)
    for path in (root / "surfaces").glob("*.yml"):
        tuple = yaml.safe_load(path.read_text(encoding="utf-8"))
        tuple["adapter"] = "echo"
        path.write_text(yaml.safe_dump(tuple, sort_keys=False), encoding="utf-8")
    for path in (root / "workers").glob("*.yml"):
        worker = yaml.safe_load(path.read_text(encoding="utf-8"))
        worker["adapter"] = "echo"
        worker.pop("model", None)
        path.write_text(yaml.safe_dump(worker, sort_keys=False), encoding="utf-8")

    try:
        report = build_launch_report(root)
    except ConfigError as exc:
        assert "no tuple satisfying" in str(exc)
        return

    assert report["go"] is False
    assert any(blocker["check"] == "routing_hygiene" for blocker in report["blockers"])
