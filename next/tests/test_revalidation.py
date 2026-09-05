from gpucall.fixtures import FixtureProvider, fixture_bundle
import pytest
from pydantic import ValidationError

from gpucall.models import Profile, Request
from gpucall.runtime import Runtime
from gpucall.store import Store

NOW = 1_800_000_000_000


def test_profile_cannot_exceed_actual_worker_byte_limits():
    for field, limit in (("max_input_bytes", 16*1024*1024), ("max_output_bytes", 1024*1024)):
        with pytest.raises(ValidationError):
            Profile(id="oversized", task="infer", revision=1, **{field: limit+1})


def test_callers_can_discover_contracts_without_provider_access(tmp_path):
    from fastapi.testclient import TestClient
    from gpucall.api import create_app
    bundle = fixture_bundle(NOW)
    store = Store(tmp_path, 1000)
    runtime = Runtime(store, bundle, {})
    client = TestClient(create_app(store, bundle, runtime, token="t"*32))
    assert client.get("/v1/schema").status_code == 401
    headers = {"Authorization": "Bearer " + "t"*32}
    schema = client.get("/v1/schema", headers=headers).json()
    assert "budget_microusd" in schema["required"]
    assert "execution" not in schema["properties"]
    profiles = client.get("/v1/profiles", headers=headers).json()["profiles"]
    assert {p["task"] for p in profiles} == {"infer", "vision"}


def test_failure_arriving_after_admission_blocks_gpu_start(tmp_path):
    bundle = fixture_bundle(NOW)
    store = Store(tmp_path, 1000)
    provider = FixtureProvider(tmp_path / "remote")
    runtime = Runtime(store, bundle, {"fixture": provider})
    request = Request(key="late-failure", profile="extract", instruction="hello",
                      deadline_ms=NOW+60000, budget_microusd=100)
    job = store.submit(request, bundle, NOW)
    runtime.tick(NOW)
    assert store.get(job["id"])["status"] == "starting"
    failed = bundle.snapshot.evidence[0].model_copy(update={"passed": False, "tested_ms": NOW+1})
    runtime.bundle = bundle.model_copy(update={"snapshot": bundle.snapshot.model_copy(update={
        "evidence": (*bundle.snapshot.evidence, failed)})})
    runtime.tick(NOW+2)
    assert store.get(job["id"])["status"] == "failed"
    assert provider.starts == 0
    assert store.account()["reserved_microusd"] == 0


def test_price_change_after_admission_cannot_spend_under_old_reservation(tmp_path):
    bundle = fixture_bundle(NOW)
    store = Store(tmp_path, 1000)
    provider = FixtureProvider(tmp_path / "remote")
    runtime = Runtime(store, bundle, {"fixture": provider})
    request = Request(key="new-price", profile="extract", instruction="hello",
                      deadline_ms=NOW+60000, budget_microusd=100)
    job = store.submit(request, bundle, NOW)
    new_offer = bundle.snapshot.offers[0].model_copy(update={"rate_microusd_per_second": 100})
    runtime.bundle = bundle.model_copy(update={"snapshot": bundle.snapshot.model_copy(update={"offers": (new_offer,)})})
    runtime.tick(NOW)
    assert store.get(job["id"])["error"] == "execution_terms_changed"
    assert provider.starts == 0
    assert store.account()["reserved_microusd"] == 0
