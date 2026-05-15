from __future__ import annotations

from pathlib import Path
import yaml

from fastapi.testclient import TestClient
import pytest

from gpucall.app import (
    compiled_plan_hash,
    create_app,
    governance_status_code,
    idempotency_execution_lock,
    plan_with_worker_refs,
    recover_interrupted_jobs,
    safe_tenant_object_prefix,
    warning_headers,
    worker_readable_request,
)
from gpucall.audit import AuditTrail
from gpucall.compiler import GovernanceError
from gpucall.credentials import save_credentials
from gpucall.dispatcher import Dispatcher, JobStore
from gpucall.domain import CompiledPlan, DataRef, ExecutionMode, JobState, PresignGetResponse, TupleResult, TaskRequest
from gpucall.registry import ObservedRegistry
from gpucall.sqlite_store import SQLiteJobStore


class ChangingPresignObjectStore:
    def __init__(self) -> None:
        self.calls = 0

    def presign_get(self, request):
        self.calls += 1
        data = request.data_ref.model_dump(mode="json")
        data.update({"uri": f"https://storage.example/input.txt?X-Amz-Signature=secret-{self.calls}", "gateway_presigned": True})
        ref = DataRef.model_validate(data)
        return PresignGetResponse(download_url=str(ref.uri), data_ref=ref)


class TenantPrefixObjectStore:
    def __init__(self) -> None:
        self.tenant_prefix = None

    def presign_put(self, request, *, tenant_prefix=None):
        self.tenant_prefix = tenant_prefix
        from gpucall.domain import DataRef, PresignPutResponse

        return PresignPutResponse(
            upload_url="https://storage.example/upload",
            data_ref=DataRef(uri=f"s3://bucket/gpucall/tenants/{tenant_prefix}/object.txt", sha256=request.sha256, bytes=request.bytes),
        )

def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parent / "fixtures" / "config"
    root = tmp_path / "config"
    root.mkdir(parents=True, exist_ok=True)
    for subdir in ["tuples", "surfaces", "workers", "recipes", "models", "engines", "tenants", "accounts"]:
        (root / subdir).mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*.yml"):
        target = root / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return root


import pytest


@pytest.fixture(autouse=True)
def isolate_gateway_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GPUCALL_ALLOW_FAKE_AUTO_TUPLES", "1")
    monkeypatch.setenv("GPUCALL_ALLOW_UNAUTHENTICATED", "1")
    credentials = tmp_path / "credentials.yml"
    credentials.write_text("version: 1\nproviders: {}\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))
    monkeypatch.delenv("GPUCALL_API_KEY", raising=False)
    monkeypatch.delenv("GPUCALL_API_KEYS", raising=False)
    monkeypatch.delenv("GPUCALL_PUBLIC_METRICS", raising=False)


def test_sync_endpoint_returns_200(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 200
    assert response.json()["result"]["kind"] == "inline"


def test_database_url_selects_postgres_stores(monkeypatch, tmp_path) -> None:
    from gpucall import app as app_module

    class FakePostgresJobStore:
        def __init__(self, dsn):
            self.dsn = dsn

    class FakePostgresIdempotencyStore:
        def __init__(self, dsn):
            self.dsn = dsn

    class FakePostgresAdmissionController:
        def __init__(self, dsn, tuples):
            self.dsn = dsn
            self.tuples = tuples

    class FakePostgresTenantUsageLedger:
        def __init__(self, dsn):
            self.dsn = dsn

    class FakePostgresArtifactRegistry:
        def __init__(self, dsn):
            self.dsn = dsn

    monkeypatch.setattr(app_module, "PostgresJobStore", FakePostgresJobStore)
    monkeypatch.setattr(app_module, "PostgresIdempotencyStore", FakePostgresIdempotencyStore)
    monkeypatch.setattr(app_module, "PostgresAdmissionController", FakePostgresAdmissionController)
    monkeypatch.setattr("gpucall.tenant.PostgresTenantUsageLedger", FakePostgresTenantUsageLedger)
    monkeypatch.setattr("gpucall.artifacts.PostgresArtifactRegistry", FakePostgresArtifactRegistry)
    monkeypatch.setenv("GPUCALL_DATABASE_URL", "postgresql://user:pass@db/gpucall")

    assert app_module._job_store(tmp_path).dsn == "postgresql://user:pass@db/gpucall"
    assert app_module._idempotency_store(tmp_path).dsn == "postgresql://user:pass@db/gpucall"
    assert app_module._admission_controller({}).dsn == "postgresql://user:pass@db/gpucall"
    assert app_module.build_tenant_usage_ledger(tmp_path).dsn == "postgresql://user:pass@db/gpucall"
    assert app_module.build_artifact_registry(tmp_path).dsn == "postgresql://user:pass@db/gpucall"


def test_sync_endpoint_auto_selects_recipe(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={
                "task": "infer",
                "mode": "sync",
                "inline_inputs": {"prompt": {"value": "hello", "content_type": "text/plain"}},
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["kind"] == "inline"


def test_sync_endpoint_accepts_intent_without_caller_routing(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={
                "task": "infer",
                "mode": "sync",
                "intent": "standard_text_inference",
                "inline_inputs": {"prompt": {"value": "hello", "content_type": "text/plain"}},
            },
    )

    assert response.status_code == 200
    assert response.json()["result"]["kind"] == "inline"


def test_batch_endpoint_executes_sync_requests(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/batch",
            json={
                "requests": [
                    {"task": "infer", "mode": "sync", "inline_inputs": {"prompt": {"value": "one", "content_type": "text/plain"}}},
                    {"task": "infer", "mode": "sync", "inline_inputs": {"prompt": {"value": "two", "content_type": "text/plain"}}},
                ]
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert [item["result"]["kind"] for item in payload["results"]] == ["inline", "inline"]


def test_sync_endpoint_returns_structured_context_overflow(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={
                "task": "infer",
                "mode": "sync",
                "input_refs": [
                    {
                        "uri": "s3://bucket/large.txt",
                        "sha256": "a" * 64,
                        "bytes": 1000000,
                        "content_type": "text/plain",
                    }
                ],
            },
        )

    payload = response.json()
    assert response.status_code == 422
    assert payload["code"] == "NO_AUTO_SELECTABLE_RECIPE"
    assert payload["context"]["required_model_len"] > payload["context"]["largest_auto_recipe_model_len"]
    assert payload["context"]["largest_auto_recipe_model_len"] == 32768
    artifact = payload["failure_artifact"]
    assert artifact["schema_version"] == 1
    assert artifact["failure_id"].startswith("gf-")
    assert artifact["failure_kind"] == "no_recipe"
    assert artifact["recipe_request_recommended"] is True
    assert artifact["caller_action"] == "run_gpucall_recipe_draft_intake"
    assert artifact["capability_gap"] == "context_window_too_small"
    assert artifact["safe_request_summary"]["input_ref_content_types"] == ["text/plain"]
    assert artifact["safe_request_summary"]["input_ref_max_bytes"] == 1000000
    assert artifact["redaction_guarantee"]["data_ref_uri_included"] is False
    assert "text-infer-standard" in artifact["rejection_matrix"]["recipes"]


def test_readyz_is_minimal_and_details_report_recipe_and_provider_capacity(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.get("/readyz")
        details = client.get("/readyz/details")

    payload = response.json()
    assert response.status_code == 200
    assert payload == {"status": "ready"}
    payload = details.json()
    assert details.status_code == 200
    assert "credentials_path" not in str(payload)
    assert "tenants_dir" not in payload["trusted_bootstrap"]
    assert payload["recipes"]["text-infer-standard"]["context_budget_tokens"] == 32768
    assert payload["tuples"]["local-echo"]["max_model_len"] == 32768


def test_intent_readiness_separates_static_eligible_from_live_blocked(tmp_path) -> None:
    import asyncio

    with TestClient(create_app(copy_config(tmp_path))) as client:
        asyncio.run(client.app.state.runtime.dispatcher.admission.suppress("local-echo", code="PROVIDER_RESOURCE_EXHAUSTED"))
        response = client.get("/v2/readiness/intents/standard_text_inference")

    assert response.status_code == 200
    recipe = response.json()["recipes"][0]
    assert recipe["eligible_tuple_count"] == 1
    assert recipe["live_ready_tuple_count"] == 0
    assert recipe["live_blocked_tuples"][0]["tuple"] == "local-echo"
    assert recipe["live_blocked_tuples"][0]["live_reason"] == "tuple_suppressed"
    assert recipe["current_caller_action"] == "retry_later_or_contact_gpucall_admin"


def test_runtime_state_uses_xdg_state_dir(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    config_dir = copy_config(tmp_path)
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state_dir))

    with TestClient(create_app(config_dir)) as client:
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 200
    assert (state_dir / "state.db").exists()
    assert (state_dir / "audit" / "trail.jsonl").exists()
    assert not (config_dir / "state.db").exists()
    assert not (config_dir / "audit").exists()


def test_async_endpoint_returns_202_and_job_id(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/tasks/async", json={"task": "infer", "mode": "async"})

    assert response.status_code == 202
    assert response.json()["job_id"]
    assert response.json()["state"] == "QUEUED"
    assert response.json()["status_url"].startswith("/v2/jobs/")


def test_async_job_status_is_scoped_to_caller_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_API_KEYS", "k1,k2")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        created = client.post(
            "/v2/tasks/async",
            json={"task": "infer", "mode": "async"},
            headers={"authorization": "Bearer k1"},
        )
        own = client.get(created.json()["status_url"], headers={"authorization": "Bearer k1"})
        other = client.get(created.json()["status_url"], headers={"authorization": "Bearer k2"})

    assert created.status_code == 202
    assert own.status_code == 200
    assert other.status_code == 404


def test_async_job_status_includes_inline_result(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        created = client.post("/v2/tasks/async", json={"task": "infer", "mode": "async"})
        status_url = created.json()["status_url"]
        payload = {}
        for _ in range(20):
            status = client.get(status_url)
            payload = status.json()
            if payload["state"] == "COMPLETED":
                break

    assert payload["state"] == "COMPLETED"
    assert payload["result"]["kind"] == "inline"
    assert payload["result"]["value"] == "ok:infer:local-echo"


async def test_sqlite_job_store_persists_inline_result(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path / "state.db")
    job = await store.create(
        CompiledPlan(
            policy_version="test",
            recipe_name="r1",
            task="infer",
            mode=ExecutionMode.ASYNC,
            tuple_chain=["local-echo"],
            timeout_seconds=2,
            lease_ttl_seconds=10,
            token_estimation_profile="qwen",
            token_budget=None,
            input_refs=[],
            inline_inputs={},
        )
    )

    assert job.state is JobState.QUEUED
    await store.update(job.job_id, state=JobState.COMPLETED, result=TupleResult(kind="inline", value="ok"))
    loaded = await SQLiteJobStore(tmp_path / "state.db").get(job.job_id)

    assert loaded.result.kind == "inline"
    assert loaded.result.value == "ok"


def test_async_job_status_does_not_persist_inline_inputs(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        created = client.post(
            "/v2/tasks/async",
            json={
                "task": "infer",
                "mode": "async",
                "inline_inputs": {"prompt": {"value": "secret prompt"}},
            },
        )
        status = client.get(created.json()["status_url"])

    assert status.status_code == 200
    payload = status.json()
    assert payload["plan"]["inline_inputs"] == {}
    assert "secret prompt" not in str(payload)


def test_wrong_mode_on_sync_endpoint_is_rejected(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "async"})

    assert response.status_code == 400


def test_oversized_request_is_rejected_before_validation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_MAX_REQUEST_BYTES", "32")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            content='{"task":"infer","mode":"sync","inline_inputs":{"prompt":{"value":"large"}}}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413


def test_api_key_auth_when_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_API_KEYS", "secret")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        unauthorized = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})
        authorized = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync"},
            headers={"authorization": "Bearer secret"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_openapi_schema_is_public_when_gateway_auth_is_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_API_KEYS", "secret")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        openapi = client.get("/openapi.json")
        task = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert openapi.status_code == 200
    assert openapi.json()["openapi"]
    assert task.status_code == 401


def test_api_key_auth_fails_closed_without_explicit_dev_override(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GPUCALL_ALLOW_UNAUTHENTICATED", raising=False)
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 401


def test_trusted_bootstrap_issues_tenant_key_without_existing_auth(tmp_path, monkeypatch) -> None:
    root = copy_config(tmp_path)
    (root / "admin.yml").write_text(
        "\n".join(
            [
                "api_key_handoff_mode: trusted_bootstrap",
                "api_key_bootstrap_allowed_hosts: [testclient]",
                "api_key_bootstrap_gateway_url: http://gpucall.internal:18088",
                "api_key_bootstrap_recipe_inbox: admin@gpucall.internal:/opt/gpucall/state/recipe_requests/inbox",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with TestClient(create_app(root)) as client:
        response = client.post("/v2/bootstrap/tenant-key", json={"system_name": "new-system"})
        issued = response.json()
        second = client.post("/v2/bootstrap/tenant-key", json={"system_name": "new-system"})

    assert response.status_code == 200
    assert issued["api_key"].startswith("gpk_")
    assert issued["handoff"]["GPUCALL_TENANT"] == "new-system"
    assert issued["handoff"]["GPUCALL_BASE_URL"] == "http://gpucall.internal:18088"
    assert issued["handoff"]["GPUCALL_RECIPE_INBOX"] == "admin@gpucall.internal:/opt/gpucall/state/recipe_requests/inbox"
    assert issued["handoff"]["GPUCALL_QUALITY_FEEDBACK_INBOX"] == "admin@gpucall.internal:/opt/gpucall/state/quality_feedback/inbox"
    assert "api_key" not in second.text
    assert second.status_code == 409


def test_trusted_bootstrap_rejects_untrusted_client(tmp_path, monkeypatch) -> None:
    root = copy_config(tmp_path)
    (root / "admin.yml").write_text(
        "\n".join(
            [
                "api_key_handoff_mode: trusted_bootstrap",
                "api_key_bootstrap_allowed_cidrs: [10.0.0.0/8]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with TestClient(create_app(root)) as client:
        response = client.post("/v2/bootstrap/tenant-key", json={"system_name": "new-system"})

    assert response.status_code == 403


def test_readyz_reports_trusted_bootstrap_writable_state(tmp_path, monkeypatch) -> None:
    root = copy_config(tmp_path)
    (root / "admin.yml").write_text(
        "\n".join(
            [
                "api_key_handoff_mode: trusted_bootstrap",
                "api_key_bootstrap_allowed_hosts: [testclient]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with TestClient(create_app(root)) as client:
        response = client.get("/readyz/details")

    payload = response.json()["trusted_bootstrap"]
    assert payload["enabled"] is True
    assert payload["tenants_dir_writable"] is True
    assert payload["credentials_writable"] is True


def test_trusted_bootstrap_disabled_by_default(tmp_path, monkeypatch) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/bootstrap/tenant-key", json={"system_name": "new-system"})

    assert response.status_code == 403


def test_tenant_api_key_sets_tenant_header_and_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_TENANT_API_KEYS", "tenant-a:secret-a")
    root = copy_config(tmp_path)
    tenant_path = root / "tenants" / "tenant-a.yml"
    tenant_path.write_text(
        "name: tenant-a\nrequests_per_minute: 120\ndaily_budget_usd: 10\nmonthly_budget_usd: 100\nobject_prefix: tenant-a\n",
        encoding="utf-8",
    )
    with TestClient(create_app(root)) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync"},
            headers={"authorization": "Bearer secret-a"},
        )

    assert response.status_code == 200
    assert response.headers["X-GPUCall-Tenant"] == "tenant-a"


def test_tenant_budget_rejects_before_provider_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_TENANT_API_KEYS", "tenant-a:secret-a")
    root = copy_config(tmp_path)
    provider_path = root / "surfaces" / "local-echo.yml"
    tuple = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
    tuple["cost_per_second"] = 1
    provider_path.write_text(yaml.safe_dump(tuple, sort_keys=False), encoding="utf-8")
    policy_path = root / "policy.yml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy["cost_policy"]["require_budget_for_high_cost_tuple"] = False
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    tenant_path = root / "tenants" / "tenant-a.yml"
    tenant_path.write_text(
        "name: tenant-a\nrequests_per_minute: 120\nmax_request_estimated_cost_usd: 0.01\nobject_prefix: tenant-a\n",
        encoding="utf-8",
    )
    with TestClient(create_app(root)) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync"},
            headers={"authorization": "Bearer secret-a"},
        )

    assert response.status_code == 402
    assert response.json()["code"] == "TENANT_BUDGET_EXCEEDED"


def test_tenant_budget_reservation_is_refunded_on_tuple_error(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import TupleError

        raise TupleError("provider failed", retryable=True, status_code=503, code="PROVIDER_ERROR")

    monkeypatch.setenv("GPUCALL_TENANT_API_KEYS", "tenant-a:secret-a")
    root = copy_config(tmp_path)
    surface_path = root / "surfaces" / "local-echo.yml"
    surface = yaml.safe_load(surface_path.read_text(encoding="utf-8"))
    surface["cost_per_second"] = 1
    surface_path.write_text(yaml.safe_dump(surface, sort_keys=False), encoding="utf-8")
    tenant_path = root / "tenants" / "tenant-a.yml"
    tenant_path.write_text("name: tenant-a\nrequests_per_minute: 120\ndaily_budget_usd: 10\nobject_prefix: tenant-a\n", encoding="utf-8")

    with TestClient(create_app(root)) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync"},
            headers={"authorization": "Bearer secret-a"},
        )
        usage = client.app.state.runtime.tenant_usage.summary(client.app.state.runtime.tenants)

    assert response.status_code == 503
    assert usage["tenant-a"]["daily_estimated_spend_usd"] == 0.0


def test_tenant_object_prefix_is_applied_to_presign_put(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_TENANT_API_KEYS", "tenant-a:secret-a")
    root = copy_config(tmp_path)
    tenant_path = root / "tenants" / "tenant-a.yml"
    tenant_path.write_text("name: tenant-a\nobject_prefix: tenant-a\n", encoding="utf-8")
    store = TenantPrefixObjectStore()
    with TestClient(create_app(root)) as client:
        client.app.state.runtime.object_store = store
        response = client.post(
            "/v2/objects/presign-put",
            json={"name": "input.txt", "bytes": 1, "sha256": "a" * 64},
            headers={"authorization": "Bearer secret-a"},
        )

    assert response.status_code == 200
    assert store.tenant_prefix == "tenant-a"


def test_anonymous_object_store_access_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GPUCALL_ALLOW_ANONYMOUS_OBJECTS", raising=False)
    store = TenantPrefixObjectStore()
    with TestClient(create_app(copy_config(tmp_path))) as client:
        client.app.state.runtime.object_store = store
        response = client.post(
            "/v2/objects/presign-put",
            json={"name": "input.txt", "bytes": 1, "sha256": "a" * 64},
        )

    assert response.status_code == 401


def test_tenant_object_prefix_rejects_path_traversal() -> None:
    with pytest.raises(Exception):
        safe_tenant_object_prefix("../tenant-b")
    with pytest.raises(Exception):
        safe_tenant_object_prefix("tenants/tenant-b")


def test_production_auth_fails_closed_without_configured_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_ENV", "production")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        health = client.get("/healthz")
        task = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert health.status_code == 200
    assert task.status_code == 401


def test_idempotency_key_reuses_sync_response(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        first = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same"},
        )
        second = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["plan_id"] == second.json()["plan_id"]


def test_idempotency_key_reuses_sync_response_after_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    config_dir = copy_config(tmp_path)
    with TestClient(create_app(config_dir)) as client:
        first = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same"},
        )
    with TestClient(create_app(config_dir)) as client:
        second = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["plan_id"] == second.json()["plan_id"]


def test_idempotency_cache_expires_by_ttl(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_IDEMPOTENCY_TTL_SECONDS", "0")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        first = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same"},
        )
        second = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["plan_id"] != second.json()["plan_id"]


def test_idempotency_key_reuse_with_different_body_returns_conflict(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        first = client.post(
            "/v2/tasks/sync",
            json={
                "task": "infer",
                "mode": "sync",
                "idempotency_key": "same",
                "inline_inputs": {"prompt": {"value": "first", "content_type": "text/plain"}},
            },
        )
        second = client.post(
            "/v2/tasks/sync",
            json={
                "task": "infer",
                "mode": "sync",
                "idempotency_key": "same",
                "inline_inputs": {"prompt": {"value": "second", "content_type": "text/plain"}},
            },
        )

    assert first.status_code == 200
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_idempotency_execution_lock_serializes_same_key() -> None:
    import asyncio

    locks: dict[str, asyncio.Lock] = {}
    guard = asyncio.Lock()
    events: list[str] = []

    async def first() -> None:
        async with idempotency_execution_lock(locks, guard, "same"):
            events.append("first-start")
            await asyncio.sleep(0.01)
            events.append("first-end")

    async def second() -> None:
        await asyncio.sleep(0)
        async with idempotency_execution_lock(locks, guard, "same"):
            events.append("second")

    await asyncio.gather(first(), second())

    assert events == ["first-start", "first-end", "second"]
    assert locks == {}


@pytest.mark.asyncio
async def test_idempotency_execution_lock_keeps_waited_lock_until_waiters_finish() -> None:
    import asyncio

    locks: dict[str, asyncio.Lock] = {}
    guard = asyncio.Lock()
    events: list[str] = []

    async def first() -> None:
        async with idempotency_execution_lock(locks, guard, "same"):
            events.append("first-start")
            await asyncio.sleep(0.01)
            events.append("first-end")

    async def second() -> None:
        await asyncio.sleep(0)
        async with idempotency_execution_lock(locks, guard, "same"):
            events.append("second")

    async def third() -> None:
        await asyncio.sleep(0.001)
        async with idempotency_execution_lock(locks, guard, "same"):
            events.append("third")

    await asyncio.gather(first(), second(), third())

    assert events == ["first-start", "first-end", "second", "third"]
    assert locks == {}


def test_idempotency_key_is_scoped_to_authenticated_caller(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_API_KEYS", "k1,k2")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        first = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same"},
            headers={"authorization": "Bearer k1"},
        )
        second = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same"},
            headers={"authorization": "Bearer k2"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["plan_id"] != second.json()["plan_id"]


def test_idempotency_replay_with_data_ref_uses_caller_body_hash(tmp_path, monkeypatch) -> None:
    data_ref = {
        "uri": "s3://bucket/prompt.txt",
        "sha256": "a" * 64,
        "bytes": 100,
        "content_type": "text/plain",
    }
    monkeypatch.setenv("GPUCALL_ALLOW_ANONYMOUS_OBJECTS", "1")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        store = ChangingPresignObjectStore()
        client.app.state.runtime.object_store = store
        first = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same", "input_refs": [data_ref]},
        )
        second = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "idempotency_key": "same", "input_refs": [data_ref]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["plan_id"] == second.json()["plan_id"]
    assert store.calls == 1


def test_warning_header_is_emitted_for_local_fallback(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 200
    assert "local_fallback_tuple" in response.headers["x-gpucall-warning"]


def test_metrics_groups_job_ids_by_route_template(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        first = client.post("/v2/tasks/async", json={"task": "infer", "mode": "async"}).json()["status_url"]
        second = client.post("/v2/tasks/async", json={"task": "infer", "mode": "async"}).json()["status_url"]
        client.get(first)
        client.get(second)
        response = client.get("/metrics")

    assert response.status_code == 403

    with TestClient(create_app(copy_config(tmp_path))) as client:
        client.app.state.runtime.metrics["requests"].clear()
        first = client.post("/v2/tasks/async", json={"task": "infer", "mode": "async"}).json()["status_url"]
        second = client.post("/v2/tasks/async", json={"task": "infer", "mode": "async"}).json()["status_url"]
        client.get(first)
        client.get(second)
        metrics = client.app.state.runtime.metrics["requests"]

    assert any("GET /v2/jobs/{job_id} 200" == key for key in metrics)
    assert not any(first in key or second in key for key in metrics)


def test_metrics_buckets_unmatched_paths(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        client.app.state.runtime.metrics["requests"].clear()
        client.get("/does-not-exist-alpha")
        client.get("/does-not-exist-beta")
        metrics = client.app.state.runtime.metrics["requests"]

    assert metrics == {"GET UNMATCHED 404": 2}


def test_warning_header_announces_remote_worker_cold_start() -> None:
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="text-infer-standard",
        task="infer",
        mode=ExecutionMode.ASYNC,
        tuple_chain=["modal-a10g"],
        timeout_seconds=30,
        lease_ttl_seconds=60,
        token_estimation_profile="qwen",
        token_budget=None,
        input_refs=[],
        inline_inputs={},
    )

    headers = warning_headers(plan)
    assert headers["X-GPUCall-Warning"] == "remote_worker_cold_start_possible"
    assert headers["X-GPUCall-Timeout-Seconds"] == "30"
    assert headers["X-GPUCall-Lease-TTL-Seconds"] == "60"
    assert headers["X-GPUCall-Min-Client-Timeout-Seconds"] == "30"


def test_warning_header_announces_dataref_worker_fetch() -> None:
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="text-infer-standard",
        task="infer",
        mode=ExecutionMode.ASYNC,
        tuple_chain=["modal-a10g"],
        timeout_seconds=30,
        lease_ttl_seconds=60,
        token_estimation_profile="qwen",
        token_budget=None,
        input_refs=[DataRef(uri="s3://bucket/prompt.txt", sha256="a" * 64, bytes=32768, content_type="text/plain")],
        inline_inputs={},
    )

    headers = warning_headers(plan)
    assert headers["X-GPUCall-Warning"] == "remote_worker_cold_start_possible, dataref_worker_fetch"
    assert headers["X-GPUCall-Min-Client-Timeout-Seconds"] == "30"


def test_rate_limit_rejects_excess_requests(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_RATE_LIMIT_PER_MINUTE", "1")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        first = client.get("/readyz")
        limited = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})
        third = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert first.status_code == 200
    assert limited.status_code == 200
    assert third.status_code == 429


def test_rate_limit_identity_cache_is_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_RATE_LIMIT_PER_MINUTE", "100")
    monkeypatch.setenv("GPUCALL_RATE_LIMIT_IDENTITY_MAX", "1")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        first = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"}, headers={"X-Forwarded-For": "1.1.1.1"})
        second = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"}, headers={"X-Forwarded-For": "2.2.2.2"})

    assert first.status_code == 200
    assert second.status_code == 200


def test_metrics_endpoint_reports_requests(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_PUBLIC_METRICS", "1")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})
        metrics = client.get("/metrics")

    assert metrics.status_code == 200
    assert metrics.json()["latency_samples"] >= 1


def test_prometheus_metrics_endpoint_reports_requests(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_PUBLIC_METRICS", "1")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})
        metrics = client.get("/metrics/prometheus")

    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
    assert 'gpucall_request_total{method="POST",route="/v2/tasks/sync",status="200"}' in metrics.text
    assert "gpucall_latency_samples" in metrics.text
    assert "gpucall_tuple_success_rate" in metrics.text
    assert "gpucall_governance_error_total" in metrics.text


def test_metrics_access_reflects_legacy_key_added_after_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GPUCALL_PUBLIC_METRICS", raising=False)
    with TestClient(create_app(copy_config(tmp_path))) as client:
        before = client.get("/metrics")
        save_credentials("auth", {"api_keys": "late-key"})
        after = client.get("/metrics", headers={"authorization": "Bearer late-key"})

    assert before.status_code == 403
    assert after.status_code == 200


def test_public_task_endpoint_rejects_caller_recipe(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync", "recipe": "text-infer-standard"})

    assert response.status_code == 400
    assert "caller-controlled routing is disabled" in response.json()["detail"]


def test_public_task_endpoint_rejects_requested_tuple(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "requested_tuple": "local-echo"},
        )

    assert response.status_code == 400
    assert "requested_tuple" in response.json()["detail"]


def test_public_task_endpoint_has_no_requested_gpu_field(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "requested_gpu": "A100"},
        )

    assert response.status_code == 422
    assert "requested_gpu" in str(response.json()["detail"])


def test_debug_flag_allows_caller_routing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_ALLOW_CALLER_ROUTING", "1")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "recipe": "text-infer-standard", "requested_tuple": "local-echo"},
        )

    assert response.status_code == 200


def test_debug_flag_does_not_allow_public_circuit_bypass(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_ALLOW_CALLER_ROUTING", "1")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={
                "task": "infer",
                "mode": "sync",
                "recipe": "text-infer-standard",
                "requested_tuple": "local-echo",
                "bypass_circuit_for_validation": True,
            },
        )

    assert response.status_code == 400
    assert "bypass_circuit_for_validation" in response.json()["detail"]


def test_production_ignores_debug_auth_and_routing_flags(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_ENV", "production")
    monkeypatch.setenv("GPUCALL_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv("GPUCALL_ALLOW_CALLER_ROUTING", "1")
    monkeypatch.setenv("GPUCALL_TENANT_API_KEYS", "test:gpk_prod")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        unauthenticated = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})
        routed = client.post(
            "/v2/tasks/sync",
            headers={"authorization": "Bearer gpk_prod"},
            json={"task": "infer", "mode": "sync", "recipe": "text-infer-standard"},
        )

    assert unauthenticated.status_code == 401
    assert routed.status_code == 400
    assert "caller-controlled routing is disabled" in routed.json()["detail"]


def test_openai_chat_completions_facade_returns_compatible_shape(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "gpucall:auto"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert payload["choices"][0]["message"]["content"] == "ok:infer:local-echo"


def test_openai_chat_completions_facade_accepts_gpucall_chat_alias(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:chat",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["model"] == "gpucall:chat"
    assert payload["gpucall"]["recipe_name"] == "text-infer-standard"


def test_openai_chat_completions_facade_accepts_external_model_as_hint(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["model"] == "gpucall:auto"
    assert response.json()["gpucall"]["requested_model"] == "gpt-4o-mini"


def test_openai_chat_completions_promotes_metadata_intent_and_preserves_hints(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_execute_sync(plan):
        captured["recipe"] = plan.recipe_name
        captured["metadata"] = plan.metadata
        return TupleResult(kind="inline", value='{"ok": true}', output_validated=True)

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "return json"}],
                "response_format": {"type": "json_object"},
                "metadata": {"task_family": "standard_text_inference", "caller": "editorial_checker"},
            },
        )

    assert response.status_code == 200
    assert captured["recipe"] == "text-infer-standard"
    assert captured["metadata"]["task_family"] == "standard_text_inference"
    assert captured["metadata"]["intent"] == "standard_text_inference"
    assert captured["metadata"]["openai.model"] == "gpt-4o-mini"


def test_openai_facade_preserves_developer_role(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_execute_sync(plan):
        captured["roles"] = [message.role for message in plan.messages]
        return TupleResult(kind="inline", value="ok")

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "developer", "content": "follow policy"},
                    {"role": "user", "content": "hello"},
                ],
            },
        )

    assert response.status_code == 200
    assert captured["roles"][-2:] == ["developer", "user"]


def test_openai_facade_rejects_conflicting_max_token_fields(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 32,
                "max_completion_tokens": 64,
            },
        )

    assert response.status_code == 400
    assert "max_tokens and max_completion_tokens conflict" in response.json()["error"]["message"]


def test_openai_facade_preserves_advisory_tool_fields(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_execute_sync(plan):
        captured["tools"] = plan.tools
        captured["tool_choice"] = plan.tool_choice
        captured["metadata"] = plan.metadata
        return TupleResult(kind="inline", value="ok")

    config_dir = copy_config(tmp_path)
    worker_path = config_dir / "workers" / "local-echo.yml"
    worker = yaml.safe_load(worker_path.read_text(encoding="utf-8"))
    worker["endpoint_contract"] = "openai-chat-completions"
    worker["output_contract"] = "openai-chat-completions"
    worker_path.write_text(yaml.safe_dump(worker, sort_keys=False), encoding="utf-8")

    with TestClient(create_app(config_dir)) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "call tool"}],
                "tools": [{"type": "function", "function": {"name": "noop"}}],
                "tool_choice": "auto",
            },
        )

    assert response.status_code == 200
    assert captured["tools"] == [{"type": "function", "function": {"name": "noop"}}]
    assert captured["tool_choice"] == "auto"
    assert captured["metadata"]["openai.tools"] == '[{"function":{"name":"noop"},"type":"function"}]'
    assert captured["metadata"]["openai.tool_choice"] == "auto"


def test_openai_facade_surfaces_backend_tool_calls(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        return TupleResult(
            kind="inline",
            value=None,
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                }
            ],
        )

    config_dir = copy_config(tmp_path)
    worker_path = config_dir / "workers" / "local-echo.yml"
    worker = yaml.safe_load(worker_path.read_text(encoding="utf-8"))
    worker["endpoint_contract"] = "openai-chat-completions"
    worker["output_contract"] = "openai-chat-completions"
    worker_path.write_text(yaml.safe_dump(worker, sort_keys=False), encoding="utf-8")

    with TestClient(create_app(config_dir)) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "call tool"}],
                "tools": [{"type": "function", "function": {"name": "noop"}}],
                "tool_choice": "required",
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    assert payload["choices"][0]["message"]["content"] is None
    assert payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "noop"


def test_openai_facade_preserves_multiple_backend_choices_for_n(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_execute_sync(plan):
        captured["n"] = plan.n
        return TupleResult(
            kind="inline",
            value="first",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            openai_choices=[
                {"index": 0, "message": {"role": "assistant", "content": "first"}, "finish_reason": "stop"},
                {"index": 1, "message": {"role": "assistant", "content": "second"}, "finish_reason": "stop"},
            ],
        )

    config_dir = copy_config(tmp_path)
    worker_path = config_dir / "workers" / "local-echo.yml"
    worker = yaml.safe_load(worker_path.read_text(encoding="utf-8"))
    worker["endpoint_contract"] = "openai-chat-completions"
    worker["output_contract"] = "openai-chat-completions"
    worker_path.write_text(yaml.safe_dump(worker, sort_keys=False), encoding="utf-8")

    with TestClient(create_app(config_dir)) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "two variants"}],
                "n": 2,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert captured["n"] == 2
    assert len(payload["choices"]) == 2
    assert payload["choices"][1]["message"]["content"] == "second"


def test_openai_facade_rejects_tools_when_no_tuple_declares_openai_chat_contract(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "call tool"}],
                "tools": [{"type": "function", "function": {"name": "noop"}}],
                "tool_choice": "required",
            },
        )

    assert response.status_code == 503
    assert "OpenAI chat completions tool/function contract" in response.text


def test_openai_facade_requires_openai_contract_for_n_greater_than_one(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "two variants"}],
                "n": 2,
            },
        )

    assert response.status_code == 503
    assert "OpenAI chat completions tool/function contract" in response.text


def test_openai_facade_rejects_message_without_content_or_tool_call(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "contnet": "typo"}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request_error"


def test_openai_facade_rejects_unknown_openai_fields(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "unknown_openai_field": True,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_openai_field"
    assert "unknown.unknown_openai_field" in response.json()["error"]["message"]


def test_openai_facade_rejects_official_but_unsupported_openai_fields_as_known(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "modalities": ["text"],
                "web_search_options": {},
            },
        )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "modalities" in message
    assert "web_search_options" in message
    assert "unknown.modalities" not in message
    assert "unknown.web_search_options" not in message


def test_openai_facade_rejects_empty_logit_bias(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "logit_bias": {},
            },
        )

    assert response.status_code == 400
    assert "logit_bias" in response.json()["error"]["message"]


def test_openai_facade_preserves_sampling_fields_in_plan(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_execute_sync(plan):
        captured["top_p"] = plan.top_p
        captured["seed"] = plan.seed
        captured["presence_penalty"] = plan.presence_penalty
        captured["frequency_penalty"] = plan.frequency_penalty
        captured["stop_tokens"] = plan.stop_tokens
        return TupleResult(kind="inline", value="ok")

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "top_p": 0.9,
                "seed": 7,
                "presence_penalty": 0.25,
                "frequency_penalty": 0.5,
                "stop": ["<stop>"],
            },
        )

    assert response.status_code == 200
    assert captured["top_p"] == 0.9
    assert captured["seed"] == 7
    assert captured["presence_penalty"] == 0.25
    assert captured["frequency_penalty"] == 0.5
    assert "<stop>" in captured["stop_tokens"]


def test_openai_chat_completions_accepts_openai_json_schema_wrapper(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_execute_sync(plan):
        captured["response_format"] = plan.response_format
        return TupleResult(kind="inline", value='{"answer": "ok"}', output_validated=True)

    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "return json"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": False}},
            },
        )

    assert response.status_code == 200
    assert captured["response_format"].json_schema == schema
    assert captured["response_format"].strict is False


def test_openai_facade_prompt_does_not_add_user_prefix(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_execute_sync(plan):
        captured["prompt"] = plan.messages[-1].content
        return TupleResult(kind="inline", value="ok")

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
        )

    assert response.status_code == 200
    assert captured["prompt"] == "hello"


def test_openai_facade_accepts_text_content_parts(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok:infer:local-echo"


def test_openai_facade_rejects_multimodal_content_parts(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert "DataRef APIs" in response.json()["error"]["message"]


def test_tuple_error_response_does_not_expose_internal_detail(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import TupleError

        raise TupleError(
            "upstream failed https://bucket/object?X-Amz-Signature=secret Authorization: Bearer token",
            retryable=True,
            status_code=503,
            code="PROVIDER_UPSTREAM",
        )

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    raw = response.text
    assert response.status_code == 503
    assert "X-Amz-Signature" not in raw
    assert "Bearer" not in raw
    assert response.json()["detail"] == "tuple execution failed (PROVIDER_UPSTREAM)"
    artifact = response.json()["failure_artifact"]
    assert artifact["failure_kind"] == "tuple_runtime"
    assert artifact["retryable"] is True
    assert artifact["caller_action"] == "retry_later"
    assert artifact["redaction_guarantee"]["tuple_raw_output_included"] is False


def test_provider_temporary_failure_artifact_marks_fallback_and_cancel(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import TupleError

        raise TupleError(
            "provider queue saturated",
            retryable=True,
            status_code=503,
            code="PROVIDER_QUEUE_SATURATED",
        )

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 503
    artifact = response.json()["failure_artifact"]
    assert artifact["failure_kind"] == "provider_temporary_unavailable"
    assert artifact["fallback_eligible"] is True
    assert artifact["cancel_remote"] is True
    assert artifact["provider_error_class"]["typical_state"] == "IN_QUEUE"


def test_code_less_tuple_error_is_not_provider_temporary_artifact(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import TupleError

        raise TupleError("tuple runtime failed", retryable=True, status_code=502)

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 502
    artifact = response.json()["failure_artifact"]
    assert artifact["failure_kind"] == "tuple_runtime"
    assert artifact["fallback_eligible"] is True
    assert "provider_error_class" not in artifact


def test_provider_quota_failure_artifact_is_not_blind_fallback(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import TupleError

        raise TupleError(
            "provider quota exceeded",
            retryable=True,
            status_code=503,
            code="PROVIDER_QUOTA_EXCEEDED",
        )

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 503
    artifact = response.json()["failure_artifact"]
    assert artifact["failure_kind"] == "provider_temporary_unavailable"
    assert artifact["fallback_eligible"] is False
    assert artifact["caller_action"] == "contact_gpucall_admin_or_use_a_different_provider_account"


def test_tuple_error_response_does_not_expose_raw_output(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import TupleError

        raise TupleError(
            "malformed structured output",
            retryable=True,
            status_code=422,
            code="MALFORMED_OUTPUT",
            raw_output='{"secret": "caller content"',
        )

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 422
    assert "caller content" not in response.text
    assert response.json()["detail"] == "tuple execution failed (MALFORMED_OUTPUT)"
    assert response.json()["failure_artifact"]["redaction_guarantee"]["tuple_raw_output_included"] is False


def test_validation_error_response_does_not_expose_request_input(tmp_path) -> None:
    secret = "secret-prompt-" + ("x" * 9000)
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={
                "task": "infer",
                "mode": "sync",
                "inline_inputs": {"prompt": {"value": secret, "content_type": "text/plain"}},
            },
        )

    assert response.status_code == 422
    assert "secret-prompt" not in response.text
    assert all("input" not in error for error in response.json()["detail"])


def test_openai_validation_error_response_does_not_expose_message_content(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "invalid", "content": "secret message content"}],
            },
        )

    assert response.status_code == 400
    assert "secret message content" not in response.text
    assert response.json()["error"]["code"] == "invalid_request_error"
    assert "detail" not in response.json()


def test_async_failed_job_does_not_expose_raw_output(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import TupleError

        raise TupleError(
            "malformed structured output",
            retryable=False,
            status_code=422,
            code="MALFORMED_OUTPUT",
            raw_output='{"secret": "async caller content"',
        )

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        created = client.post("/v2/tasks/async", json={"task": "infer", "mode": "async"})
        payload = {}
        for _ in range(20):
            status = client.get(created.json()["status_url"])
            payload = status.json()
            if payload["state"] == "FAILED":
                break

    assert payload["state"] == "FAILED"
    assert payload["result"] is None
    assert "async caller content" not in str(payload)


def test_openai_chat_completions_sets_output_validated_header_for_json(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "return json"}],
                "response_format": {"type": "json_object"},
            },
        )

    assert response.status_code == 422
    assert response.headers["x-gpucall-output-validated"] == "false"
    assert response.json()["error"]["code"] == "MALFORMED_OUTPUT"
    assert "raw_output" not in response.json()["error"]


def test_openai_chat_completions_exposes_output_validated_in_body(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        return TupleResult(kind="inline", value='{"ok": true}', output_validated=True)

    with TestClient(create_app(copy_config(tmp_path))) as client:
        monkeypatch.setattr(client.app.state.runtime.dispatcher, "execute_sync", fake_execute_sync)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "return json"}],
                "response_format": {"type": "json_object"},
            },
        )

    assert response.status_code == 200
    assert response.headers["x-gpucall-output-validated"] == "true"
    assert response.json()["output_validated"] is True


def test_openai_chat_completions_includes_gpucall_plan_metadata(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["gpucall"]["selected_tuple"] == "local-echo"
    assert "selected_tuple_model" in payload["gpucall"]
    assert payload["gpucall"]["governance_hash"]


def test_plan_with_worker_refs_rehashes_executable_plan() -> None:
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.SYNC,
        tuple_chain=["p1"],
        timeout_seconds=1,
        lease_ttl_seconds=10,
        token_estimation_profile="qwen",
        token_budget=None,
        input_refs=[],
        inline_inputs={},
        attestations={"governance_hash": "caller-hash"},
    )
    ref = DataRef(uri="https://storage.example/prompt.txt?sig=redacted", content_type="text/plain")

    updated = plan_with_worker_refs(plan, [ref])

    assert updated.attestations["caller_governance_hash"] == "caller-hash"
    assert updated.attestations["governance_hash"] == compiled_plan_hash(updated)
    assert updated.attestations["governance_hash"] != "caller-hash"


def test_openai_chat_completions_streams_sse(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert '"object":"chat.completion.chunk"' in response.text
    assert "ok:infer:local-echo" in response.text
    assert "data: [DONE]" in response.text


def test_openai_chat_completions_stream_response_format_requires_openai_contract(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "return json"}],
                "stream": True,
                "response_format": {"type": "json_object"},
            },
        )

    assert response.status_code == 503
    assert "OpenAI chat completions tool/function contract" in response.text


def test_openai_chat_completions_rejects_large_prompt_without_500(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "x" * 9000}],
            },
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert "SDK DataRef" in response.json()["error"]["message"]


async def test_startup_recovery_expires_interrupted_jobs(tmp_path) -> None:
    jobs = JobStore()
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.ASYNC,
        tuple_chain=["local-echo"],
        timeout_seconds=1,
        lease_ttl_seconds=10,
        token_estimation_profile="qwen",
        token_budget=None,
        input_refs=[],
        inline_inputs={},
    )
    job = await jobs.create(plan)
    await jobs.update(job.job_id, state=JobState.RUNNING)
    dispatcher = Dispatcher(adapters={}, registry=ObservedRegistry(), audit=AuditTrail(tmp_path / "audit.jsonl"), jobs=jobs)
    runtime = type("RuntimeStub", (), {"jobs": jobs, "dispatcher": dispatcher})()

    await recover_interrupted_jobs(runtime)

    recovered = await jobs.get(job.job_id)
    assert recovered is not None
    assert recovered.state is JobState.EXPIRED
    assert recovered.error == "gateway restarted before job completion"


def test_worker_readable_request_presigns_s3_refs() -> None:
    class FakeObjectStore:
        def presign_get(self, request):
            return PresignGetResponse(
                download_url="https://example.com/signed",
                data_ref=request.data_ref.model_copy(update={"uri": "https://example.com/signed", "gateway_presigned": True}),
            )

    runtime = type("RuntimeStub", (), {"object_store": FakeObjectStore()})()
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        input_refs=[DataRef(uri="s3://bucket/key.txt", sha256="a" * 64, bytes=12)],
    )

    converted = worker_readable_request(request, runtime)

    assert str(converted.input_refs[0].uri) == "https://example.com/signed"
    assert converted.input_refs[0].gateway_presigned is True
    assert str(request.input_refs[0].uri) == "s3://bucket/key.txt"


def test_worker_readable_request_rejects_untrusted_https_refs() -> None:
    runtime = type("RuntimeStub", (), {"object_store": None})()
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        input_refs=[DataRef(uri="https://169.254.169.254/latest/meta-data", sha256="a" * 64, bytes=12)],
    )

    with pytest.raises(ValueError, match="s3://"):
        worker_readable_request(request, runtime)


def test_worker_readable_request_requires_object_store_for_data_refs() -> None:
    runtime = type("RuntimeStub", (), {"object_store": None})()
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        input_refs=[DataRef(uri="s3://bucket/key.txt", sha256="a" * 64, bytes=12)],
    )

    with pytest.raises(ValueError, match="object store"):
        worker_readable_request(request, runtime)


def test_governance_circuit_errors_are_service_unavailable() -> None:
    assert governance_status_code(GovernanceError("requested tuple 'p1' is unavailable due to circuit breaker")) == 503
    assert governance_status_code(GovernanceError("no eligible tuple after policy, recipe, and circuit constraints")) == 503
    assert governance_status_code(GovernanceError("unsupported task for v2.0 MVP: train")) == 400
