from __future__ import annotations

from pathlib import Path
import yaml

from fastapi.testclient import TestClient
import pytest

from gpucall.app import (
    compiled_plan_hash,
    create_app,
    governance_status_code,
    plan_with_worker_refs,
    recover_interrupted_jobs,
    warning_headers,
    worker_readable_request,
)
from gpucall.audit import AuditTrail
from gpucall.compiler import GovernanceError
from gpucall.dispatcher import Dispatcher, JobStore
from gpucall.domain import CompiledPlan, DataRef, ExecutionMode, JobState, PresignGetResponse, ProviderResult, TaskRequest
from gpucall.registry import ObservedRegistry
from gpucall.sqlite_store import SQLiteJobStore


class ChangingPresignObjectStore:
    def __init__(self) -> None:
        self.calls = 0

    def presign_get(self, request):
        self.calls += 1
        ref = request.data_ref.model_copy(
            update={
                "uri": f"https://storage.example/input.txt?X-Amz-Signature=secret-{self.calls}",
                "gateway_presigned": True,
            }
        )
        return PresignGetResponse(download_url=str(ref.uri), data_ref=ref)


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    root = tmp_path / "config"
    for path in source.rglob("*.yml"):
        target = root / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    for provider_path in (root / "providers").glob("*.yml"):
        if provider_path.name != "local-echo.yml":
            provider_path.unlink()
    for recipe_path in (root / "recipes").glob("*.yml"):
        if recipe_path.name not in {"smoke-text-small.yml", "text-infer-standard.yml"}:
            recipe_path.unlink()
    for recipe_name in ("text-infer-standard.yml",):
        path = root / "recipes" / recipe_name
        recipe = yaml.safe_load(path.read_text(encoding="utf-8"))
        recipe["timeout_seconds"] = 30
        recipe["lease_ttl_seconds"] = 120
        path.write_text(yaml.safe_dump(recipe, sort_keys=False), encoding="utf-8")
    return root


import pytest


@pytest.fixture(autouse=True)
def isolate_gateway_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GPUCALL_ALLOW_FAKE_AUTO_PROVIDERS", "1")
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
                        "bytes": 100000,
                        "content_type": "text/plain",
                    }
                ],
            },
        )

    payload = response.json()
    assert response.status_code == 422
    assert payload["code"] == "NO_AUTO_SELECTABLE_RECIPE"
    assert payload["context"]["required_model_len"] > payload["context"]["largest_auto_recipe_model_len"]


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
            provider_chain=["local-echo"],
            timeout_seconds=2,
            lease_ttl_seconds=10,
            tokenizer_family="qwen",
            token_budget=None,
            input_refs=[],
            inline_inputs={},
        )
    )

    await store.update(job.job_id, state=JobState.COMPLETED, result=ProviderResult(kind="inline", value="ok"))
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


def test_idempotency_replay_with_data_ref_uses_caller_body_hash(tmp_path) -> None:
    data_ref = {
        "uri": "s3://bucket/prompt.txt",
        "sha256": "a" * 64,
        "bytes": 100,
        "content_type": "text/plain",
    }
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
    assert "local_fallback_provider" in response.headers["x-gpucall-warning"]


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
        provider_chain=["modal-a10g"],
        timeout_seconds=30,
        lease_ttl_seconds=60,
        tokenizer_family="qwen",
        token_budget=None,
        input_refs=[],
        inline_inputs={},
    )

    assert warning_headers(plan)["X-GPUCall-Warning"] == "remote_worker_cold_start_possible"


def test_warning_header_announces_dataref_worker_fetch() -> None:
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="text-infer-standard",
        task="infer",
        mode=ExecutionMode.ASYNC,
        provider_chain=["modal-a10g"],
        timeout_seconds=30,
        lease_ttl_seconds=60,
        tokenizer_family="qwen",
        token_budget=None,
        input_refs=[DataRef(uri="s3://bucket/prompt.txt", sha256="a" * 64, bytes=32768, content_type="text/plain")],
        inline_inputs={},
    )

    assert warning_headers(plan)["X-GPUCall-Warning"] == "remote_worker_cold_start_possible, dataref_worker_fetch"


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


def test_public_task_endpoint_rejects_caller_recipe(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/tasks/sync", json={"task": "infer", "mode": "sync", "recipe": "text-infer-standard"})

    assert response.status_code == 400
    assert "caller-controlled routing is disabled" in response.json()["detail"]


def test_public_task_endpoint_rejects_requested_provider(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "requested_provider": "local-echo"},
        )

    assert response.status_code == 400
    assert "requested_provider" in response.json()["detail"]


def test_public_task_endpoint_rejects_requested_gpu(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "requested_gpu": "A100"},
        )

    assert response.status_code == 400
    assert "requested_gpu" in response.json()["detail"]


def test_debug_flag_allows_caller_routing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_ALLOW_CALLER_ROUTING", "1")
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "recipe": "text-infer-standard", "requested_provider": "local-echo"},
        )

    assert response.status_code == 200


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


def test_openai_chat_completions_facade_rejects_non_auto_model(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "provider:model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_model"


def test_openai_facade_prompt_does_not_add_user_prefix(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_execute_sync(plan):
        captured["prompt"] = plan.messages[-1].content
        return ProviderResult(kind="inline", value="ok")

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


def test_openai_facade_rejects_structured_message_content(tmp_path) -> None:
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
    assert "string message content only" in response.json()["detail"]


def test_provider_error_response_does_not_expose_internal_detail(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import ProviderError

        raise ProviderError(
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
    assert response.json()["detail"] == "provider execution failed (PROVIDER_UPSTREAM)"


def test_provider_error_response_does_not_expose_raw_output(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import ProviderError

        raise ProviderError(
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
    assert response.json()["detail"] == "provider execution failed (MALFORMED_OUTPUT)"


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

    assert response.status_code == 422
    assert "secret message content" not in response.text
    assert all("input" not in error for error in response.json()["detail"])


def test_async_failed_job_does_not_expose_raw_output(tmp_path, monkeypatch) -> None:
    async def fake_execute_sync(_plan):
        from gpucall.domain import ProviderError

        raise ProviderError(
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
    assert payload["gpucall"]["selected_provider"] == "local-echo"
    assert "selected_provider_model" in payload["gpucall"]
    assert payload["gpucall"]["governance_hash"]


def test_plan_with_worker_refs_rehashes_executable_plan() -> None:
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.SYNC,
        provider_chain=["p1"],
        timeout_seconds=1,
        lease_ttl_seconds=10,
        tokenizer_family="qwen",
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


def test_openai_chat_completions_rejects_stream_in_mvp(tmp_path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpucall:auto",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("stream is not supported")


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
        provider_chain=["local-echo"],
        timeout_seconds=1,
        lease_ttl_seconds=10,
        tokenizer_family="qwen",
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
    assert governance_status_code(GovernanceError("requested provider 'p1' is unavailable due to circuit breaker")) == 503
    assert governance_status_code(GovernanceError("no eligible provider after policy, recipe, and circuit constraints")) == 503
    assert governance_status_code(GovernanceError("unsupported task for v2.0 MVP: train")) == 400
