from fastapi.testclient import TestClient

from gpucall.api import create_app
from gpucall.fixtures import FixtureProvider, fixture_bundle
from gpucall.runtime import Runtime
from gpucall.store import Store

NOW = 1_800_000_000_000
TOKEN = "test-only-owner-token-" + "x" * 32


def client(tmp_path):
    bundle = fixture_bundle(NOW)
    store = Store(tmp_path / "state", 1000)
    runtime = Runtime(store, bundle, {"fixture": FixtureProvider(tmp_path/"remote")})
    app = create_app(store, bundle, runtime, token=TOKEN, clock=lambda: NOW)
    c = TestClient(app)
    c.headers["Authorization"] = "Bearer " + TOKEN
    return c, runtime


def request():
    return {"key": "api-job", "profile": "extract", "instruction": "秘密をログに出さない",
            "deadline_ms": NOW+60000, "budget_microusd": 100}


def test_authenticated_api_full_cycle(tmp_path):
    c, runtime = client(tmp_path)
    assert c.post("/v1/estimate", json=request()).status_code == 200
    response = c.post("/v1/jobs", json=request())
    assert response.status_code == 202
    job = response.json()["id"]
    assert c.get(f"/v1/jobs/{job}/result").status_code == 409
    for _ in range(8):
        runtime.tick(NOW)
    assert c.get(f"/v1/jobs/{job}").json()["status"] == "succeeded"
    assert c.get(f"/v1/jobs/{job}/replay").json()["matches"]
    assert c.get(f"/v1/jobs/{job}/result").status_code == 200
    assert c.get("/v1/audit").json()["valid"]
    assert c.get("/v1/account").json()["reserved_microusd"] == 0


def test_authentication_and_validation_redaction(tmp_path):
    c, _ = client(tmp_path)
    c.headers.clear()
    assert c.post("/v1/jobs", json=request()).status_code == 401
    c.headers["Authorization"] = "Bearer " + TOKEN
    result = c.post("/v1/jobs", json={**request(), "secret_unknown_field": "do-not-echo-me"})
    assert result.status_code == 422
    assert "do-not-echo-me" not in result.text


def test_artifacts_and_missing_ids(tmp_path):
    c, _ = client(tmp_path)
    response = c.post("/v1/artifacts", content=b"private data")
    sha = response.json()["sha256"]
    assert c.get(f"/v1/artifacts/{sha}").content == b"private data"
    assert c.get("/v1/jobs/nonexistent").status_code == 404


def test_request_body_limit_and_no_validation_bypass(tmp_path):
    c, _ = client(tmp_path)
    assert c.post("/v1/jobs", content=b"x"*140000).status_code == 413
    assert c.post("/v1/jobs", json={**request(), "validation_execution": "fixture"}).status_code == 422
