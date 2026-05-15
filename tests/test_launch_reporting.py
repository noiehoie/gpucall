from __future__ import annotations

from pathlib import Path
import json

import yaml

from gpucall.cli import build_launch_report
from gpucall.config import ConfigError
from gpucall.domain import ExecutionTupleSpec


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


def test_live_cost_audit_uses_policy_allowlist_when_present(tmp_path, monkeypatch) -> None:
    from gpucall.cli import _live_cost_audit_tuples
    from gpucall.config import load_config

    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    config = load_config(copy_config(tmp_path))
    config.policy.tuples.allow = ["runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"]

    tuples = _live_cost_audit_tuples(config)

    assert set(tuples) == {"runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"}


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


def test_gateway_smoke_marks_vision_failure_as_not_ok(monkeypatch) -> None:
    from gpucall.cli import _gateway_smoke_summary

    put_timeouts: list[float] = []

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
                return Response(200, {"status": "ready", "object_store": True})
            raise AssertionError(path)

        def post(self, path: str, *, json: dict[str, object]) -> Response:
            if path == "/v2/tasks/sync" and json["task"] == "infer":
                return Response(200, {"plan": {"selected_tuple": "modal-a10g"}, "result": {"kind": "inline", "value": "ok"}})
            if path == "/v2/tasks/sync" and json["task"] == "vision":
                return Response(500, {"error": "vision failed"})
            if path == "/v2/objects/presign-put":
                return Response(
                    200,
                    {
                        "upload_url": f"https://objects.example/{json['name']}",
                        "data_ref": {"uri": f"s3://bucket/{json['name']}", "bytes": json["bytes"]},
                    },
                )
            raise AssertionError(path)

    def fake_put(*args, **kwargs) -> Response:
        put_timeouts.append(kwargs["timeout"])
        return Response(200, {})

    monkeypatch.setenv("GPUCALL_GATEWAY_SMOKE_TIMEOUT_SECONDS", "123")
    monkeypatch.setattr("gpucall.cli.httpx.Client", Client)
    monkeypatch.setattr("gpucall.cli.httpx.post", lambda *args, **kwargs: Response(401, {}))
    monkeypatch.setattr("gpucall.cli.httpx.put", fake_put)

    summary = _gateway_smoke_summary("http://gateway.example.internal", api_key="gpk_test")

    assert summary["ok"] is False
    assert summary["vision"]["ok"] is False
    assert put_timeouts == [123.0, 123.0]


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


def test_live_cost_audit_flags_unapproved_runpod_warm_workers() -> None:
    from gpucall.cli import _live_cost_audit_findings, _runpod_endpoint_runtime_cost

    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00016,
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )

    runtime_cost = _runpod_endpoint_runtime_cost(tuple, {"id": "endpoint-1", "workersMin": 1, "workersMax": 3, "workers": []})
    live = {"managed_endpoint": {"configured": True, "endpoints": [{"runtime_cost_findings": runtime_cost["findings"]}]}}

    findings = _live_cost_audit_findings(live)

    assert runtime_cost["summary"]["unmanaged_standing_cost"] is True
    assert findings[0]["check"] == "runpod_unmanaged_standing_workers"
    assert "standing_cost_per_second" in findings[0]["reason"]


def test_live_cost_audit_accepts_approved_runpod_warm_workers() -> None:
    from gpucall.cli import _runpod_endpoint_runtime_cost

    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00016,
        standing_cost_per_second=0.00016,
        standing_cost_window_seconds=3600,
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        provider_params={
            "cost_approval": {
                "standing_workers_approved": True,
                "approved_by": "operator",
                "approved_at": "2026-05-14T00:00:00Z",
                "reason": "bounded warm pool for scheduled production window",
            }
        },
    )

    runtime_cost = _runpod_endpoint_runtime_cost(tuple, {"id": "endpoint-1", "workersMin": 1, "workersMax": 3})

    assert runtime_cost["summary"]["unmanaged_standing_cost"] is False
    assert runtime_cost["findings"] == []
    assert runtime_cost["live_blocked"] is True
    assert runtime_cost["live_blockers"][0]["check"] == "runpod_serverless_billing_guard"


def test_live_cost_audit_flags_unmanaged_runpod_warm_endpoint() -> None:
    from gpucall.cli import _live_cost_audit_findings, _runpod_unmanaged_endpoint_findings

    inventory = {"ok": True, "body": [{"id": "endpoint-1", "workersMin": 1, "workersMax": 2}]}

    unmanaged = _runpod_unmanaged_endpoint_findings(inventory, configured_endpoint_ids=set())
    live = {"managed_endpoint": {"configured": True, "unmanaged_endpoint_findings": unmanaged}}
    findings = _live_cost_audit_findings(live)

    assert findings[0]["check"] == "runpod_unmanaged_endpoint_live_blocked"
    assert findings[0]["endpoint_id"] == "endpoint-1"
    assert "not declared" in findings[0]["reason"]


def test_live_cost_audit_worker_count_falls_through_invalid_alias() -> None:
    from gpucall.cli import _runpod_unmanaged_endpoint_findings

    inventory = {"ok": True, "body": [{"id": "endpoint-1", "workersMin": "invalid", "workers_min": 1}]}

    findings = _runpod_unmanaged_endpoint_findings(inventory, configured_endpoint_ids=set())

    assert findings[0]["workers_min"] == 1


def test_live_cost_audit_does_not_treat_workers_standby_as_standing_cost() -> None:
    from gpucall.cli import _runpod_unmanaged_endpoint_findings

    inventory = {"ok": True, "body": [{"id": "endpoint-1", "workersMin": 0, "workersStandby": 1}]}

    findings = _runpod_unmanaged_endpoint_findings(inventory, configured_endpoint_ids=set())

    assert findings == []


def test_runpod_billing_guard_ignores_exited_workers() -> None:
    from gpucall.execution_surfaces.managed_endpoint import runpod_serverless_billing_guard_summary

    summary = runpod_serverless_billing_guard_summary(
        endpoint={
            "id": "endpoint-1",
            "workersMin": 0,
            "workersMax": 2,
            "workers": [{"id": "worker-1", "desiredStatus": "EXITED"}],
        },
        health={"workers": {"ready": 0, "running": 0, "initializing": 0, "throttled": 0, "unhealthy": 0}},
    )

    assert summary["active_workers"] == 0
    assert summary["live_blocked"] is False


def test_live_cost_audit_ignores_exited_workers_in_runtime_summary() -> None:
    from gpucall.cli import _runpod_endpoint_runtime_cost

    runtime_cost = _runpod_endpoint_runtime_cost(
        object(),
        {
            "id": "endpoint-1",
            "workersMin": 0,
            "workersMax": 2,
            "workers": [
                {"id": "worker-1", "desiredStatus": "EXITED"},
                {"id": "worker-2", "status": "terminated"},
            ],
        },
    )

    assert runtime_cost["summary"]["active_workers"] == 0
    assert runtime_cost["summary"]["live_blocked"] is False


def test_runpod_billing_guard_does_not_block_ready_serverless_workers() -> None:
    from gpucall.execution_surfaces.managed_endpoint import runpod_serverless_billing_guard_findings

    findings = runpod_serverless_billing_guard_findings(
        object(),
        endpoint={
            "id": "endpoint-1",
            "workersMin": 0,
            "workersMax": 2,
            "workers": [{"id": "worker-1", "desiredStatus": "RUNNING"}],
        },
        health={"workers": {"ready": 1, "running": 0, "initializing": 0, "throttled": 0, "unhealthy": 0}},
    )

    assert findings == []


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
