from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import types
import asyncio
import hashlib
import inspect
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gpucall.artifacts import SQLiteArtifactRegistry
from gpucall.domain import TenantSpec, TupleError
from gpucall.local_dataref_worker import _fetch_dataref_texts, _validate_dataref_fetch_uri, create_app, run_dataref_openai_request
from gpucall.postgres_store import PostgresIdempotencyStore
from gpucall.sqlite_store import SQLiteIdempotencyStore
from gpucall.tenant import PostgresTenantUsageLedger, TenantBudgetError, TenantUsageLedger, enforce_tenant_budget
from gpucall.worker_contracts.io import fetch_data_ref_bytes


def test_importing_cli_does_not_create_runtime_state(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(Path.cwd() / ".cache"),
        "GPUCALL_STATE_DIR": str(tmp_path / "state"),
    }
    code = """
from pathlib import Path
import os
root = Path(os.environ["GPUCALL_STATE_DIR"])
import gpucall.cli
print(sorted(p.name for p in root.glob("*")) if root.exists() else [])
"""
    completed = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, env=env)
    assert completed.stdout.strip() == "[]"


def test_s3_dataref_bytes_verify_declared_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    class Body:
        def __init__(self) -> None:
            self.done = False

        def read(self, _n: int) -> bytes:
            if self.done:
                return b""
            self.done = True
            return b"tampered"

    class Client:
        def get_object(self, Bucket: str, Key: str):
            return {"Body": Body()}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *args, **kwargs: Client()))

    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetch_data_ref_bytes(
            {
                "uri": "s3://bucket/key.txt",
                "bytes": 8,
                "sha256": "0" * 64,
                "allow_worker_s3_credentials": True,
            }
        )


def test_http_dataref_rejects_private_target_before_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPUCALL_WORKER_DATAREF_ALLOWED_HOSTS", "169.254.169.254")
    with pytest.raises(ValueError, match="non-public"):
        fetch_data_ref_bytes(
            {
                "uri": "http://169.254.169.254/latest/meta-data",
                "gateway_presigned": True,
                "bytes": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            }
        )


def test_dataref_rejects_negative_declared_bytes() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        fetch_data_ref_bytes(
            {
                "uri": "https://objects.example/ref.txt",
                "gateway_presigned": True,
                "bytes": -1,
                "sha256": hashlib.sha256(b"").hexdigest(),
            }
        )


def test_local_dataref_worker_requires_api_key() -> None:
    app = create_app(worker_api_key="")
    with TestClient(app) as client:
        response = client.post("/gpucall/local-dataref-openai/v1/chat", json={})
    assert response.status_code == 503
    assert "API key is not configured" in response.json()["detail"]


def test_local_dataref_worker_rejects_non_presigned_private_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TupleError, match="gateway-presigned"):
        _validate_dataref_fetch_uri("http://169.254.169.254/latest/meta-data", {"gateway_presigned": False})
    monkeypatch.setenv("GPUCALL_LOCAL_DATAREF_ALLOWED_HOSTS", "169.254.169.254")
    with pytest.raises(TupleError, match="non-public"):
        _validate_dataref_fetch_uri("http://169.254.169.254/latest/meta-data", {"gateway_presigned": True})


def test_local_dataref_worker_preserves_dataref_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPUCALL_LOCAL_DATAREF_ALLOWED_HOSTS", "objects.local")
    monkeypatch.setattr("gpucall.local_dataref_worker.socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))])
    transport = httpx.MockTransport(lambda _request: httpx.Response(429))

    async def run() -> None:
        with pytest.raises(TupleError) as exc_info:
            await _fetch_dataref_texts(
                [
                    {
                        "uri": "https://objects.local/ref.txt",
                        "gateway_presigned": True,
                        "bytes": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                        "content_type": "text/plain",
                    }
                ],
                max_dataref_bytes=1024,
                transport=transport,
            )
        assert exc_info.value.retryable is True
        assert exc_info.value.status_code == 429

    asyncio.run(run())


def test_local_dataref_worker_preserves_openai_rate_limit() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(429))

    async def run() -> None:
        with pytest.raises(TupleError) as exc_info:
            await run_dataref_openai_request(
                {},
                openai_base_url="http://local-openai.test/v1",
                model="local-model",
                api_key="secret",
                openai_transport=transport,
            )
        assert exc_info.value.retryable is True
        assert exc_info.value.status_code == 429

    asyncio.run(run())


def test_local_dataref_worker_converts_dataref_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPUCALL_LOCAL_DATAREF_ALLOWED_HOSTS", "objects.local")
    monkeypatch.setattr("gpucall.local_dataref_worker.socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))])

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot connect", request=request)

    transport = httpx.MockTransport(handler)

    async def run() -> None:
        with pytest.raises(TupleError) as exc_info:
            await _fetch_dataref_texts(
                [
                    {
                        "uri": "https://objects.local/ref.txt",
                        "gateway_presigned": True,
                        "bytes": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                        "content_type": "text/plain",
                    }
                ],
                max_dataref_bytes=1024,
                transport=transport,
            )
        assert exc_info.value.retryable is True
        assert exc_info.value.status_code == 502

    asyncio.run(run())


def test_tenant_budget_reservation_is_atomic(tmp_path: Path) -> None:
    ledger = TenantUsageLedger(tmp_path / "tenant_usage.db")
    tenant = TenantSpec(name="tenant-a", daily_budget_usd=1.0)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def reserve(plan_id: str) -> None:
        barrier.wait()
        try:
            enforce_tenant_budget(
                tenant_id="tenant-a",
                tenant=tenant,
                ledger=ledger,
                estimated_cost_usd=0.75,
                tuple="tuple-a",
                recipe="recipe-a",
                plan_id=plan_id,
            )
            results.append("ok")
        except TenantBudgetError:
            results.append("budget")

    threads = [threading.Thread(target=reserve, args=(f"plan-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["budget", "ok"]
    assert ledger.spend_since("tenant-a", datetime.fromtimestamp(0, timezone.utc)) == 0.75


def test_artifact_latest_compare_and_set_rejects_stale_expected_version(tmp_path: Path) -> None:
    registry = SQLiteArtifactRegistry(tmp_path / "artifacts.db")

    assert registry.compare_and_set_latest("chain-a", expected_version=None, new_version="v1") is True
    assert registry.compare_and_set_latest("chain-a", expected_version=None, new_version="v2") is False
    assert registry.compare_and_set_latest("chain-a", expected_version="missing", new_version="v2") is False
    assert registry.latest_version("chain-a") == "v1"


def test_idempotency_store_does_not_overwrite_existing_key(tmp_path: Path) -> None:
    store = SQLiteIdempotencyStore(tmp_path / "idempotency.db")
    try:
        store.set(
            "idem-key",
            request_hash="first-hash",
            status=200,
            content={"result": "first"},
            headers={"x-first": "1"},
            max_entries=100,
        )
        store.set(
            "idem-key",
            request_hash="second-hash",
            status=500,
            content={"result": "second"},
            headers={"x-second": "1"},
            max_entries=100,
        )

        assert store.get("idem-key", ttl_seconds=3600, max_entries=100) == (
            "first-hash",
            200,
            {"result": "first"},
            {"x-first": "1"},
            "completed",
        )
    finally:
        store.close()


def test_idempotency_store_pending_reservation_blocks_duplicates(tmp_path: Path) -> None:
    store = SQLiteIdempotencyStore(tmp_path / "idempotency.db")
    try:
        assert store.reserve("idem-key", request_hash="first-hash", max_entries=100) is True
        assert store.reserve("idem-key", request_hash="first-hash", max_entries=100) is False
        assert store.get("idem-key", ttl_seconds=3600, max_entries=100) == (
            "first-hash",
            0,
            {},
            {},
            "pending",
        )

        store.set(
            "idem-key",
            request_hash="first-hash",
            status=200,
            content={"result": "done"},
            headers={},
            max_entries=100,
        )
        assert store.get("idem-key", ttl_seconds=3600, max_entries=100) == (
            "first-hash",
            200,
            {"result": "done"},
            {},
            "completed",
        )
    finally:
        store.close()


def test_idempotency_store_releases_pending_on_failure(tmp_path: Path) -> None:
    store = SQLiteIdempotencyStore(tmp_path / "idempotency.db")
    try:
        assert store.reserve("idem-key", request_hash="first-hash", max_entries=100) is True
        store.release("idem-key", request_hash="first-hash")
        assert store.reserve("idem-key", request_hash="first-hash", max_entries=100) is True
    finally:
        store.close()


def test_sqlite_idempotency_migrates_legacy_not_null_schema(tmp_path: Path) -> None:
    path = tmp_path / "idempotency.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE idempotency_entries (
              key TEXT PRIMARY KEY,
              created_at REAL NOT NULL,
              request_hash TEXT NOT NULL,
              status INTEGER NOT NULL,
              content TEXT NOT NULL,
              headers TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO idempotency_entries(key, created_at, request_hash, status, content, headers) VALUES (?, ?, ?, ?, ?, ?)",
            ("old", 1.0, "old-hash", 200, "{}", "{}"),
        )
    store = SQLiteIdempotencyStore(path)
    try:
        assert store.reserve("new", request_hash="new-hash", max_entries=100) is True
        assert store.get("old", ttl_seconds=float("inf"), max_entries=100) == ("old-hash", 200, {}, {}, "completed")
    finally:
        store.close()


def test_postgres_idempotency_reserve_uses_atomic_insert_contract() -> None:
    source = inspect.getsource(PostgresIdempotencyStore.reserve)
    assert "ON CONFLICT(key) DO NOTHING" in source
    assert "rowcount == 1" in source


def test_postgres_idempotency_completion_requires_matching_pending_hash() -> None:
    source = inspect.getsource(PostgresIdempotencyStore.set)
    assert "idempotency_status = 'pending'" in source
    assert "request_hash = excluded.request_hash" in source


def test_postgres_tenant_budget_uses_transaction_lock_before_insert() -> None:
    source = inspect.getsource(PostgresTenantUsageLedger.reserve_with_budget)
    assert "pg_advisory_xact_lock" in source
    assert "INSERT INTO gpucall_tenant_usage" in source
    assert source.index("pg_advisory_xact_lock") < source.index("INSERT INTO gpucall_tenant_usage")
