from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from gpucall.panopticon_provisioning import (
    ProviderSupplyProvisioningPlan,
    apply_provider_supply_provisioning_plan,
    build_provider_supply_provisioning_plan,
    dumps_provider_supply_provisioning_plan,
    _runpod_gpu_type_ids,
)


CONFIG_DIR = Path("config")
VISION_TUPLE = "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"
NOW = datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc)


def test_provision_plan_from_configured_tuple_creates_template_and_endpoint_actions() -> None:
    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        tuple_name=VISION_TUPLE,
        endpoint_name="gpucall-test-vl7b",
        template_name="gpucall-test-vl7b-template",
        now=NOW,
    )

    assert plan.phase == "provider-supply-provisioning-plan"
    assert plan.billable_generation_allowed is False
    assert [action.action for action in plan.actions] == ["create_runpod_template", "create_runpod_serverless_endpoint"]

    template = plan.actions[0]
    assert template.request["imageName"] == "runpod/worker-v1-vllm:v2.18.1"
    assert template.request["containerDiskInGb"] == 150
    assert template.request["isServerless"] is True
    assert template.request["env"]["MODEL_NAME"] == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert template.request["env"]["MAX_MODEL_LEN"] == "8192"

    endpoint = plan.actions[1]
    assert endpoint.request["name"] == "gpucall-test-vl7b"
    assert endpoint.request["computeType"] == "GPU"
    assert endpoint.request["gpuTypeIds"] == ["NVIDIA RTX A6000", "NVIDIA A40"]
    assert endpoint.request["workersMin"] == 0
    assert endpoint.request["workersMax"] == 1
    assert endpoint.parameters["template_id_from_action"] == template.action_id
    assert endpoint.post_apply_config_patch[0]["config_dir_relative_path"] == f"workers/{VISION_TUPLE}.yml"


def test_provision_plan_can_use_existing_template_id_without_creating_template() -> None:
    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        tuple_name=VISION_TUPLE,
        template_id="tm8l7oonfc",
        endpoint_name="gpucall-test-vl7b",
        now=NOW,
    )

    assert [action.action for action in plan.actions] == ["create_runpod_serverless_endpoint"]
    assert plan.actions[0].request["templateId"] == "tm8l7oonfc"
    assert plan.actions[0].depends_on == []


def test_provision_plan_can_select_candidate_from_recipe_admin_review(tmp_path) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps({"tuple_candidate_matches": [{"name": VISION_TUPLE}]}),
        encoding="utf-8",
    )

    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        review_path=review_path,
        template_id="tm8l7oonfc",
        endpoint_name="gpucall-test-vl7b",
        now=NOW,
    )

    assert plan.actions[0].source_kind == "candidate"
    assert "config/candidate_sources/runpod_serverless.yml" in str(plan.actions[0].source_path)
    assert plan.actions[0].request["templateId"] == "tm8l7oonfc"


def test_warm_workers_are_blocked_by_default_policy() -> None:
    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        tuple_name=VISION_TUPLE,
        template_id="tm8l7oonfc",
        workers_min=1,
        workers_max=1,
        endpoint_name="gpucall-test-vl7b",
        now=NOW,
    )

    endpoint = plan.actions[0]
    assert endpoint.apply_supported is False
    assert endpoint.apply_blocked_reasons == ["warm_workers_not_allowed_by_policy"]
    assert endpoint.request["workersMin"] == 1


def test_apply_dry_run_never_calls_provider(monkeypatch) -> None:
    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        tuple_name=VISION_TUPLE,
        template_id="tm8l7oonfc",
        endpoint_name="gpucall-test-vl7b",
        now=NOW,
    )

    def fail_create(*_args, **_kwargs):
        raise AssertionError("dry-run must not call RunPod")

    monkeypatch.setattr("gpucall.panopticon_provisioning._runpod_create_endpoint", fail_create)

    result = apply_provider_supply_provisioning_plan(plan, credentials={"runpod": {"api_key": "rk_test"}}, dry_run=True)

    assert result.applied_count == 0
    assert result.skipped_count == 1
    assert result.results[0]["status"] == "dry_run"


def test_apply_yes_creates_template_then_endpoint(monkeypatch) -> None:
    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        tuple_name=VISION_TUPLE,
        endpoint_name="gpucall-test-vl7b",
        template_name="gpucall-test-vl7b-template",
        now=NOW,
    )
    calls: list[tuple[str, dict[str, object], str]] = []

    def fake_create_template(request, api_key):
        calls.append(("template", dict(request), api_key))
        return {"id": "tpl-created", "name": request["name"]}

    def fake_create_endpoint(request, api_key):
        calls.append(("endpoint", dict(request), api_key))
        return {"id": "endpoint-created", "name": request["name"], "workersMin": request["workersMin"]}

    monkeypatch.setattr("gpucall.panopticon_provisioning._runpod_create_template", fake_create_template)
    monkeypatch.setattr("gpucall.panopticon_provisioning._runpod_create_endpoint", fake_create_endpoint)

    result = apply_provider_supply_provisioning_plan(plan, credentials={"runpod": {"api_key": "rk_test"}}, dry_run=False)

    assert result.applied_count == 2
    assert calls[0][0] == "template"
    assert calls[1] == (
        "endpoint",
        {
            "computeType": "GPU",
            "gpuCount": 1,
            "gpuTypeIds": ["NVIDIA RTX A6000", "NVIDIA A40"],
            "idleTimeout": 5,
            "name": "gpucall-test-vl7b",
            "scalerType": "QUEUE_DELAY",
            "scalerValue": 4,
            "templateId": "tpl-created",
            "workersMax": 1,
            "workersMin": 0,
        },
        "rk_test",
    )
    assert result.results[1]["materialized_config_patch"][0]["value"] == "endpoint-created"


def test_apply_fails_if_endpoint_response_omits_id(monkeypatch) -> None:
    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        tuple_name=VISION_TUPLE,
        template_id="tm8l7oonfc",
        endpoint_name="gpucall-test-vl7b",
        now=NOW,
    )

    def fake_create_endpoint(request, api_key):
        return {"name": request["name"]}

    monkeypatch.setattr("gpucall.panopticon_provisioning._runpod_create_endpoint", fake_create_endpoint)

    result = apply_provider_supply_provisioning_plan(plan, credentials={"runpod": {"api_key": "rk_test"}}, dry_run=False)

    assert result.failed_count == 1
    assert "provider response missing resource id" in result.results[0]["reason"]


def test_runpod_gpu_type_mapping_accepts_multi_gpu_suffix() -> None:
    assert _runpod_gpu_type_ids("AMPERE_48x2") == ["NVIDIA RTX A6000", "NVIDIA A40"]
    assert _runpod_gpu_type_ids("AMPERE_48 x2") == ["NVIDIA RTX A6000", "NVIDIA A40"]


def test_apply_runpod_endpoint_uses_official_rest_post(monkeypatch) -> None:
    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        tuple_name=VISION_TUPLE,
        template_id="tm8l7oonfc",
        endpoint_name="gpucall-test-vl7b",
        now=NOW,
    )
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200
        text = '{"id":"endpoint-created"}'

        def json(self) -> dict[str, object]:
            return {"id": "endpoint-created"}

    class Session:
        def post(self, url: str, **kwargs: object) -> Response:
            calls.append({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr("gpucall.panopticon_provisioning.requests_session", lambda: Session())

    result = apply_provider_supply_provisioning_plan(plan, credentials={"runpod": {"api_key": "rk_test"}}, dry_run=False)

    assert result.applied_count == 1
    assert calls == [
        {
            "url": "https://rest.runpod.io/v1/endpoints",
            "headers": {"Authorization": "Bearer rk_test", "content-type": "application/json", "accept": "application/json"},
            "json": {
                "computeType": "GPU",
                "gpuCount": 1,
                "gpuTypeIds": ["NVIDIA RTX A6000", "NVIDIA A40"],
                "idleTimeout": 5,
                "name": "gpucall-test-vl7b",
                "scalerType": "QUEUE_DELAY",
                "scalerValue": 4,
                "templateId": "tm8l7oonfc",
                "workersMax": 1,
                "workersMin": 0,
            },
            "timeout": 30,
        }
    ]


def test_provision_plan_round_trip_is_strict_json() -> None:
    plan = build_provider_supply_provisioning_plan(
        config_dir=CONFIG_DIR,
        tuple_name=VISION_TUPLE,
        template_id="tm8l7oonfc",
        endpoint_name="gpucall-test-vl7b",
        now=NOW,
    )
    payload = json.loads(dumps_provider_supply_provisioning_plan(plan))

    assert ProviderSupplyProvisioningPlan.model_validate(payload).action_count == 1


def test_panopticon_provision_cli_round_trip(tmp_path, monkeypatch, capsys) -> None:
    from gpucall.cli import main

    plan_path = tmp_path / "supply-plan.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gpucall",
            "panopticon",
            "provision-plan",
            "--config-dir",
            str(CONFIG_DIR),
            "--tuple",
            VISION_TUPLE,
            "--template-id",
            "tm8l7oonfc",
            "--endpoint-name",
            "gpucall-test-vl7b",
            "--output-json",
            str(plan_path),
        ],
    )

    main()
    stdout_plan = json.loads(capsys.readouterr().out)

    assert stdout_plan["phase"] == "provider-supply-provisioning-plan"
    assert plan_path.exists()
    assert stdout_plan["actions"][0]["action"] == "create_runpod_serverless_endpoint"

    monkeypatch.setattr(sys, "argv", ["gpucall", "panopticon", "provision-apply", str(plan_path)])
    main()
    apply_stdout = json.loads(capsys.readouterr().out)
    assert apply_stdout["dry_run"] is True
    assert apply_stdout["results"][0]["status"] == "dry_run"
