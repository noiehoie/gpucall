from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gpucall.agent_surface import failure_taxonomy
from gpucall.app import create_app
from gpucall.domain import ProviderErrorCode


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


def test_failure_taxonomy_covers_all_provider_error_codes() -> None:
    taxonomy = failure_taxonomy()

    assert taxonomy["schema_version"] == 1
    assert set(taxonomy["provider_errors"]) == {item.value for item in ProviderErrorCode}
    for entry in taxonomy["provider_errors"].values():
        assert entry["retryable"] is True
        assert entry["caller_action"]
    assert set(taxonomy["governance_failures"]) == {
        "no_recipe",
        "no_tuple",
        "policy_denied",
        "input_contract",
        "tenant_budget",
    }
    for entry in taxonomy["governance_failures"].values():
        assert entry["caller_action"]
        assert entry["owner"]
        assert isinstance(entry["retryable_without_change"], bool)


def test_failure_taxonomy_endpoint(tmp_path: Path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.get("/v2/failure-taxonomy")

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "failure-taxonomy"
    assert "PROVIDER_CAPACITY_UNAVAILABLE" in payload["provider_errors"]
    assert payload["retry_semantics"]["idempotency"]


def test_estimate_endpoint_returns_plan_and_cost_without_execution(tmp_path: Path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post("/v2/estimate", json={"task": "infer", "mode": "sync"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "estimate"
    assert payload["billable"] is False
    assert payload["budget_reserved"] is False
    assert payload["plan"]["recipe_name"]
    assert payload["plan"]["tuple_chain"]
    assert isinstance(payload["budget_reservation_usd"], (int, float))
    assert payload["next_action"]


def test_estimate_endpoint_fails_closed_for_unknown_workload(tmp_path: Path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/estimate",
            json={"task": "infer", "mode": "sync", "metadata": {"intent": "definitely_not_a_recipe_intent"}},
        )
        sync_response = client.post(
            "/v2/tasks/sync",
            json={"task": "infer", "mode": "sync", "metadata": {"intent": "definitely_not_a_recipe_intent"}},
        )

    # Estimate must classify exactly like the billable path, not weaker.
    assert response.status_code == sync_response.status_code


def test_estimate_endpoint_rejects_caller_routing_selectors(tmp_path: Path) -> None:
    with TestClient(create_app(copy_config(tmp_path))) as client:
        response = client.post(
            "/v2/estimate",
            json={"task": "infer", "mode": "sync", "recipe": "echo-recipe"},
        )

    assert response.status_code in (400, 403, 422)
