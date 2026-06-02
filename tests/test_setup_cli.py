from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from gpucall import __version__
from gpucall.admin_automation import run_admin_automation_synthetic_dry_run
from gpucall.cli import main
from gpucall.cli_commands.setup import (
    _load_setup_plan,
    _modal_deploy_plan_hash,
    _start_admin_automation_service,
    _start_panopticon_background_service,
    apply_setup_plan,
    export_handoff_prompt,
    setup_next_text,
    setup_section_text,
    setup_status_text,
    write_starter_plan,
)
from gpucall.config import load_admin_automation, load_object_store
from gpucall.credentials import load_credentials, save_credentials
from gpucall.domain import ApiKeyHandoffMode, RecipeAdminAutomationConfig
from gpucall.panopticon import store_panopticon_evidence
from gpucall.provider_registry import load_provider_registry


def test_setup_status_starts_from_operator_dashboard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    text = setup_status_text(tmp_path / "config")

    assert "Profile: unselected" in text
    assert "[missing] config initialized" in text
    assert "GPU execution surfaces" in text
    assert "Choose section" not in text
    assert "gpucall setup next" in text
    assert "gpucall setup starter-plan --profile local-trial" in text


def test_setup_status_can_render_interactive_menu(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    text = setup_status_text(tmp_path / "config", include_menu=True)

    assert "Choose section" in text
    assert "External-system onboarding prompt" in text


def test_setup_next_points_to_first_missing_section(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    text = setup_next_text(tmp_path / "config")

    assert "Next required step: choose a starter plan" in text
    assert "gpucall setup starter-plan --profile local-trial" in text


def test_cli_version_is_available(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["gpucall", "--version"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert f"gpucall {__version__}" in capsys.readouterr().out


def test_setup_section_providers_is_dashboard_not_linear_wizard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    text = setup_section_text(tmp_path / "config", "providers")

    assert "GPU execution surfaces" in text
    assert "--provider modal" in text
    assert "--provider runpod" in text
    assert "Register controlled runtime" in text
    assert "local-trial" in text


def test_setup_starter_plan_makes_local_trial_unambiguous(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"

    report = write_starter_plan(plan, profile="local-trial", provider=None)
    dry_run = apply_setup_plan(config_dir, plan, dry_run=True, yes=True)
    applied = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    next_text = setup_next_text(config_dir)

    assert "Wrote starter setup plan" in report
    assert "providers:" not in plan.read_text(encoding="utf-8")
    assert "Setup plan: local-trial" in dry_run
    assert "Applied setup plan." in applied
    assert "Admin automation synthetic dry-run:" in applied
    assert (tmp_path / "state" / "gpucall" / "catalog" / "provider-panopticon.json").exists()
    assert (tmp_path / "state" / "gpucall" / "setup" / "control-plane-state.json").exists()
    assert "Local trial is complete." in next_text
    assert "--provider modal" in next_text
    assert "Modal token ID and token secret" in next_text
    status = setup_status_text(config_dir)
    assert "OOB readiness: local-trial-ready" in status
    assert "Panopticon: service-initialized" in status
    assert "TTL defaults: hot=300s price=3600s contract=86400s validation=604800s" in status
    assert "[ok] GPU execution surfaces: local smoke runtime" in status
    assert "cloud provider before external callers" in status
    assert "gateway URL and caller auth before external callers" in status


def test_setup_starter_plan_cloud_path_is_copy_pasteable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    plan = tmp_path / "gpucall.setup.yml"

    report = write_starter_plan(plan, profile="internal-team", provider="runpod")
    text = plan.read_text(encoding="utf-8")
    dry_run = apply_setup_plan(tmp_path / "config", plan, dry_run=True, yes=False)

    assert "gpucall setup apply --file" in report
    assert "profile: internal-team" in text
    assert "runpod:" in text
    assert "source: prompt" in text
    assert "endpoint_id is optional on first install" in text
    assert "trusted_bootstrap" in text
    assert "Add each external caller IP/CIDR" in text
    assert "127.0.0.1/32" in text
    assert "provider account: runpod (endpoint provisioning pending)" in dry_run


def test_setup_starter_plan_modal_is_oob_happy_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    plan = tmp_path / "gpucall.setup.yml"

    report = write_starter_plan(plan, profile="internal-team", provider="modal")
    text = plan.read_text(encoding="utf-8")
    dry_run = apply_setup_plan(tmp_path / "config", plan, dry_run=True, yes=False)

    assert "gpucall setup apply --file" in report
    assert "MODAL_TOKEN_ID" in report
    assert "--accept-plan-hash" in report
    assert "modal:" in text
    assert "deploy_worker: true" in text
    assert "Add each external caller IP/CIDR" in text
    assert "127.0.0.1/32" in text
    assert "auto_validate_existing_tuples: true" in text
    assert "auto_activate_existing_validated_recipe: true" in text
    assert "auto_billable_validation: true" in text
    assert "auto_validation_budget_usd: 0.10" in text
    assert "auto_activate_validated: true" in text
    assert "auto_set_auto_select: true" in text
    assert "auto_run_launch_check: true" in text
    assert "provider worker deployment: modal gpucall-worker-json" in dry_run
    assert "modal deploy requires provider-mutation consent plan_hash=" in dry_run
    assert "Interactive apply still asks for final confirmation" in dry_run
    assert "--yes alone is not provider mutation consent" in dry_run


def test_setup_plan_rejects_external_gateway_with_loopback_only_trusted_bootstrap(tmp_path) -> None:
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: http://203.0.113.10:18088
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 127.0.0.1/32
  allowed_hosts:
    - localhost
  recipe_inbox: /tmp/gpucall/recipe_requests/inbox
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        apply_setup_plan(tmp_path / "config", plan, dry_run=True, yes=False)

    assert "external gateway.base_url requires tenant_onboarding.allowed_cidrs" in str(exc.value)


def test_setup_plan_modal_deploy_worker_uses_gpucall_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test", "environment": "main"})
    calls = []

    def fake_run(command, *, capture_output, text, env, check, timeout):
        calls.append({"command": command, "env": env, "timeout": timeout})
        return types.SimpleNamespace(returncode=0, stdout="deployed", stderr="")

    monkeypatch.setattr("gpucall.cli_commands.setup.subprocess.run", fake_run)
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
    deploy_worker: true
""".lstrip(),
        encoding="utf-8",
    )

    setup_plan = _load_setup_plan(plan)
    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True, accept_plan_hash=_modal_deploy_plan_hash(setup_plan))
    registry = load_provider_registry()
    modal_record = registry["providers"]["modal"]

    assert "[ok] Modal worker deployed: gpucall-worker-json" in report
    assert calls[0]["command"][-4:] == ["modal", "deploy", "-m", "gpucall.worker_contracts.modal"]
    assert calls[0]["env"]["MODAL_TOKEN_ID"] == "ak-test"
    assert calls[0]["env"]["MODAL_TOKEN_SECRET"] == "as-test"
    assert calls[0]["env"]["MODAL_ENVIRONMENT"] == "main"
    assert modal_record["metadata"]["deployment_id"].startswith("modal:gpucall:gpucall-worker-json:")
    assert modal_record["metadata"]["ownership_tag"].startswith("gpucall-setup-")
    assert Path(modal_record["metadata"]["cleanup_manifest_path"]).exists()
    assert "environment" not in load_credentials()["modal"]


def test_setup_plan_prompt_credentials_can_use_environment_without_getpass_warning(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-env")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-env")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "dev")
    monkeypatch.setattr("gpucall.cli_commands.setup._confirm", lambda prompt: True)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"))
    monkeypatch.setattr("gpucall.cli_commands.setup.getpass.getpass", lambda prompt: pytest.fail(f"unexpected getpass: {prompt}"))
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.modal.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  modal:
    enabled: true
    credentials:
      source: prompt
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=False)
    credentials = load_credentials()["modal"]

    assert "Modal configured" in report
    assert credentials["token_id"] == "ak-env"
    assert credentials["token_secret"] == "as-env"
    assert load_provider_registry()["providers"]["modal"]["metadata"]["environment"] == "dev"


def test_setup_plan_gpucall_credentials_imports_modal_environment_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-env")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-env")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "main")
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.modal.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
""".lstrip(),
        encoding="utf-8",
    )

    dry_run = apply_setup_plan(config_dir, plan, dry_run=True, yes=True)
    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    status = setup_status_text(config_dir, profile="internal-team")
    credentials = load_credentials()["modal"]

    assert "modal credentials.source=gpucall_credentials but missing" not in dry_run
    assert "Modal configured" in report
    assert credentials["token_id"] == "ak-env"
    assert credentials["token_secret"] == "as-env"
    assert "[ok] GPU execution surfaces: Modal configured" in status


def test_admin_synthetic_dry_run_forces_materialize_only_automation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    observed: dict[str, RecipeAdminAutomationConfig] = {}

    def fake_process_inbox(**kwargs):
        automation = kwargs.get("automation_override")
        assert isinstance(automation, RecipeAdminAutomationConfig)
        observed["automation"] = automation
        recipe_path = Path(kwargs["output_dir"]) / "infer-summarize-text-draft.yml"
        report_path = Path(kwargs["report_dir"]) / "synthetic-oob-intake.report.json"
        recipe_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        recipe_path.write_text("name: infer-summarize-text-draft\n", encoding="utf-8")
        report_path.write_text("{}\n", encoding="utf-8")
        return [{"ok": True, "recipe": str(recipe_path), "report": str(report_path)}]

    monkeypatch.setattr("gpucall.admin_automation.process_inbox", fake_process_inbox)

    evidence = run_admin_automation_synthetic_dry_run(str(tmp_path / "recipe_requests" / "inbox"), config_dir=tmp_path / "config")
    automation = observed["automation"]

    assert evidence["status"] == "processed"
    assert automation.recipe_inbox_auto_materialize is True
    assert automation.recipe_inbox_auto_validate_existing_tuples is False
    assert automation.recipe_inbox_auto_promote_candidates is False
    assert automation.recipe_inbox_auto_provision_supply is False
    assert automation.recipe_inbox_auto_billable_validation is False
    assert automation.recipe_inbox_auto_activate_validated is False


def test_setup_sections_cover_recipe_inbox_and_external_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))

    recipe = setup_section_text(tmp_path / "config", "recipe-inbox")
    external = setup_section_text(tmp_path / "config", "external-system")

    assert "Auto materialize recipes" in recipe
    assert "Auto run billable validation" in recipe
    assert "gpucall-recipe-admin process-inbox" in recipe
    assert "export-handoff-prompt" in external
    assert "without embedding any API key" in external


def test_setup_next_and_provider_section_guide_modal_after_local_trial(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    write_starter_plan(plan, profile="local-trial", provider=None)
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    next_text = setup_next_text(config_dir)
    providers = setup_section_text(config_dir, "providers")
    gateway = setup_section_text(config_dir, "gateway")

    assert "Local trial is complete." in next_text
    assert "Recommended happy path: Modal." in next_text
    assert "gpucall setup apply --file gpucall.modal.setup.yml" in next_text
    assert "gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml" in next_text
    assert "Without provider credentials" in next_text
    assert providers.index("Modal happy path") < providers.index("RunPod advanced")
    assert "create a Modal account and token first" in providers
    assert "--provider modal" in gateway


def test_setup_status_reports_pending_handoff_when_external_system_is_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "setup.yml").write_text(
        """
setup_schema_version: 1
profile: internal-team
gateway_base_url: https://gpucall.example.internal
external_systems:
  - name: example/system
""".lstrip(),
        encoding="utf-8",
    )

    pending = setup_status_text(config_dir)
    handoff_dir = tmp_path / "data" / "gpucall" / "handoffs" / "example-system"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "caller-ai-onboarding-prompt.md").write_text("# prompt\n", encoding="utf-8")
    (handoff_dir / "CALLER_ENGINEER_README.md").write_text("# readme\n", encoding="utf-8")
    ready = setup_status_text(config_dir)

    assert "[warn] external-system handoff packages pending: example/system" in pending
    assert "[ok] external-system handoff packages generated" in ready


def test_setup_export_handoff_prompt_requires_concrete_values_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "gpucall",
            "setup",
            "export-handoff-prompt",
            "--config-dir",
            str(tmp_path / "config"),
            "--system-name",
            "example-caller",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code != 0
    assert "handoff package requires concrete values" in str(exc.value)
    assert "--allow-placeholders" in str(exc.value)


def test_setup_export_handoff_prompt_can_intentionally_emit_template(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "gpucall",
            "setup",
            "export-handoff-prompt",
            "--config-dir",
            str(tmp_path / "config"),
            "--system-name",
            "example-caller",
            "--allow-placeholders",
        ],
    )

    main()
    output = capsys.readouterr().out

    assert "GPUCALL_BASE_URL: `<GPUCALL_BASE_URL>`" in output
    assert "example-caller" in output


def test_setup_plan_dry_run_does_not_write_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key
providers:
  modal:
    enabled: true
    credentials:
      source: official_cli
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  recipe_inbox: admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=True, yes=True)

    assert "Setup plan: internal-team" in report
    assert "No changes written because --dry-run is set." in report
    assert not (config_dir / "admin.yml").exists()
    assert not (config_dir / "setup.yml").exists()


def test_setup_plan_apply_writes_admin_object_store_and_generated_gateway_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    save_credentials("runpod", {"api_key": "rk_test"})
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key
providers:
  runpod:
    enabled: true
    credentials:
      source: gpucall_credentials
    endpoint_id: rp-xxxxxxxxxxxx
object_store:
  provider: cloudflare_r2
  bucket: gpucall-data
  endpoint_url: https://example.r2.cloudflarestorage.com
  credentials:
    source: gpucall_credentials
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  allowed_hosts:
    - trusted-host
  recipe_inbox: admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox
recipe_automation:
  auto_materialize: true
  auto_validate_existing_tuples: true
  auto_activate_existing_validated_recipe: false
  auto_promote_candidates: true
  auto_billable_validation: true
  auto_activate_validated: false
  auto_set_auto_select: false
  promotion_work_dir: /srv/gpucall/state/recipe_requests/promotions
handoff_assets:
  onboarding_prompt_url: https://assets.example/gpucall/onboarding-prompt.md
  onboarding_manual_url: https://assets.example/gpucall/onboarding-manual.md
  caller_sdk_wheel_url: https://assets.example/gpucall/gpucall_sdk-2.0.17-py3-none-any.whl
external_systems:
  - name: example-system
    expected_workloads: [infer, vision]
launch:
  run_static_check: true
  require_object_store: true
  require_gateway_auth: true
""".lstrip(),
        encoding="utf-8",
    )

    setup_plan = _load_setup_plan(plan)
    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True, accept_plan_hash=_modal_deploy_plan_hash(setup_plan))
    automation = load_admin_automation(config_dir)
    object_store = load_object_store(config_dir)
    credentials = load_credentials()
    surface = (config_dir / "surfaces" / "runpod-vllm-serverless.yml").read_text(encoding="utf-8")
    worker = (config_dir / "workers" / "runpod-vllm-serverless.yml").read_text(encoding="utf-8")

    assert "Applied setup plan." in report
    assert "Post-apply checks:" in report
    assert "[ok] validate-config:" in report
    assert "[ok] security scan-secrets: 0 findings" in report
    assert automation.api_key_handoff_mode is ApiKeyHandoffMode.TRUSTED_BOOTSTRAP
    assert automation.api_key_bootstrap_allowed_cidrs == ("10.0.0.42/32",)
    assert automation.api_key_bootstrap_allowed_hosts == ("trusted-host",)
    assert automation.api_key_bootstrap_gateway_url == "https://gpucall.example.internal"
    assert automation.recipe_inbox_auto_materialize is True
    assert automation.recipe_inbox_auto_validate_existing_tuples is True
    assert automation.recipe_inbox_auto_activate_existing_validated_recipe is False
    assert automation.recipe_inbox_auto_promote_candidates is True
    assert automation.recipe_inbox_auto_billable_validation is True
    assert automation.recipe_inbox_auto_activate_validated is False
    assert automation.recipe_inbox_auto_set_auto_select is False
    assert automation.recipe_inbox_promotion_work_dir == "/srv/gpucall/state/recipe_requests/promotions"
    assert automation.onboarding_prompt_url == "https://assets.example/gpucall/onboarding-prompt.md"
    assert automation.onboarding_manual_url == "https://assets.example/gpucall/onboarding-manual.md"
    assert automation.caller_sdk_wheel_url == "https://assets.example/gpucall/gpucall_sdk-2.0.17-py3-none-any.whl"
    assert object_store is not None
    assert object_store.bucket == "gpucall-data"
    assert credentials["auth"]["api_keys"].startswith("gpk_")
    assert "target: rp-xxxxxxxxxxxx" in surface
    assert "target: rp-xxxxxxxxxxxx" in worker
    assert "RunPod managed endpoint ready" in report
    assert "profile: internal-team" in (config_dir / "setup.yml").read_text(encoding="utf-8")
    handoff_dir = tmp_path / "data" / "gpucall" / "handoffs" / "example-system"
    assert (handoff_dir / "caller-ai-onboarding-prompt.md").exists()
    assert "Caller handoff packages:" in report
    assert "example-system" in report
    assert "Caller handoff package generated." in report
    assert "give to caller-side AI CLI:" in report


def test_setup_plan_auto_writes_external_system_handoff_package(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})
    monkeypatch.setattr(
        "gpucall.cli_commands.setup._deploy_modal_worker",
        lambda: "[ok] Modal worker deployed: gpucall-worker-json",
    )
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
    deploy_worker: true
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_hosts:
    - caller.example.internal
  recipe_inbox: {recipe_inbox}
handoff_assets:
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.40-py3-none-any.whl
recipe_automation:
  auto_materialize: true
  auto_validate_existing_tuples: true
  auto_activate_existing_validated_recipe: true
  auto_promote_candidates: true
  auto_billable_validation: true
  auto_validation_budget_usd: 0.10
  auto_activate_validated: true
  auto_set_auto_select: true
external_systems:
  - name: example/system
    expected_workloads: [infer]
""".lstrip(),
        encoding="utf-8",
    )

    setup_plan = _load_setup_plan(plan)
    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True, accept_plan_hash=_modal_deploy_plan_hash(setup_plan))

    handoff_dir = tmp_path / "data" / "gpucall" / "handoffs" / "example-system"
    prompt = handoff_dir / "caller-ai-onboarding-prompt.md"
    assert prompt.exists()
    assert oct(handoff_dir.stat().st_mode & 0o777) == "0o700"
    assert "Caller handoff packages:" in report
    assert "[ok] example/system:" in report
    assert "Caller handoff package generated." in report
    assert "give to caller-side AI CLI:" in report
    assert "Setup is onboarding-ready-provisional." in report
    assert "Panopticon bootstrap refresh:" in report
    assert "caller-ai-onboarding-prompt.md" in report


def test_setup_status_requires_object_store_for_external_vision_workload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})
    monkeypatch.setattr(
        "gpucall.cli_commands.setup._deploy_modal_worker",
        lambda: "[ok] Modal worker deployed: gpucall-worker-json",
    )
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
    deploy_worker: true
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_hosts:
    - caller.example.internal
  recipe_inbox: {recipe_inbox}
handoff_assets:
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.40-py3-none-any.whl
recipe_automation:
  auto_materialize: true
  auto_validate_existing_tuples: true
  auto_activate_existing_validated_recipe: true
  auto_promote_candidates: true
  auto_billable_validation: true
  auto_validation_budget_usd: 0.10
  auto_activate_validated: true
  auto_set_auto_select: true
external_systems:
  - name: example/vision-system
    expected_workloads: [vision]
""".lstrip(),
        encoding="utf-8",
    )

    setup_plan = _load_setup_plan(plan)
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True, accept_plan_hash=_modal_deploy_plan_hash(setup_plan))
    status = setup_status_text(config_dir)

    assert "OOB readiness: onboarding-blocked" in status
    assert "[missing] object store / DataRef storage for external image/file workflows" in status


def test_setup_internal_team_reaches_onboarding_ready_with_fresh_panopticon_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("GPUCALL_SETUP_LIVE_PROVIDER_PROBES", "1")
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})

    def fake_refresh(*, config_dir, panopticon_path, **kwargs):
        store_panopticon_evidence(
            {
                "modal-a10g": {
                    "tuple": "modal-a10g",
                    "adapter": "modal",
                    "status": "live_revalidated",
                    "checked": True,
                    "findings": [
                        {
                            "tuple": "modal-a10g",
                            "adapter": "modal",
                            "dimension": "health",
                            "severity": "info",
                            "source": "modal",
                        }
                    ],
                }
            },
            panopticon_path,
        )
        return {"phase": "provider-panopticon-refresh", "preflight": {"status": "ok"}, "observed_count": 1, "snapshot_count": 1}

    monkeypatch.setattr("gpucall.cli_commands.setup.refresh_panopticon", fake_refresh)
    monkeypatch.setattr(
        "gpucall.cli_commands.setup._start_panopticon_background_service",
        lambda config_dir, plan: {"status": "service-running", "health_url": "http://127.0.0.1:18090/healthz", "pid": 1234},
    )
    monkeypatch.setattr(
        "gpucall.cli_commands.setup._start_admin_automation_service",
        lambda config_dir, plan: {"status": "service-running", "inbox_dir": str(tmp_path / "state" / "recipe_requests" / "inbox")},
    )
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_hosts:
    - caller.example.internal
  recipe_inbox: {recipe_inbox}
recipe_automation:
  auto_materialize: true
  auto_validate_existing_tuples: true
  auto_activate_existing_validated_recipe: true
  auto_promote_candidates: true
  auto_billable_validation: true
  auto_validation_budget_usd: 0.10
  auto_activate_validated: true
  auto_set_auto_select: true
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    assert "OOB readiness: onboarding-ready" in report
    assert "Panopticon: service-running" in report
    assert "Admin automation: service-running" in report
    assert "Panopticon evidence: evidence-fresh" in report


def test_setup_status_blocks_internal_team_without_validation_activation_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_hosts:
    - caller.example.internal
  recipe_inbox: {recipe_inbox}
recipe_automation:
  auto_materialize: true
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    assert "OOB readiness: onboarding-blocked" in report
    assert "[missing] recipe inbox billable validation budget" in report
    assert "[missing] validated route activation automation" in report


def test_setup_partial_panopticon_refresh_keeps_service_health_separate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("GPUCALL_SETUP_LIVE_PROVIDER_PROBES", "1")
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})

    def fake_refresh(*, config_dir, panopticon_path, **kwargs):
        store_panopticon_evidence(
            {
                "modal-a10g": {
                    "tuple": "modal-a10g",
                    "adapter": "modal",
                    "status": "live_revalidated",
                    "checked": True,
                    "findings": [
                        {
                            "tuple": "modal-a10g",
                            "adapter": "modal",
                            "dimension": "health",
                            "severity": "info",
                            "source": "modal",
                        }
                    ],
                }
            },
            panopticon_path,
        )
        return {
            "phase": "provider-panopticon-refresh",
            "status": "partial",
            "snapshot_path": str(panopticon_path),
            "observed_count": 1,
            "snapshot_count": 1,
            "preflight": {
                "status": "partial",
                "probe_tuple_count": 1,
                "skipped_tuple_count": 1,
                "skipped_tuples": {"runpod-missing-target"},
                "blockers": [{"code": "PROVIDER_ENDPOINT_TARGET_MISSING", "provider": "runpod"}],
            },
        }

    monkeypatch.setattr("gpucall.cli_commands.setup.refresh_panopticon", fake_refresh)
    monkeypatch.setattr(
        "gpucall.cli_commands.setup._start_panopticon_background_service",
        lambda config_dir, plan: {"status": "service-running", "health_url": "http://127.0.0.1:18090/healthz", "pid": 1234},
    )
    monkeypatch.setattr(
        "gpucall.cli_commands.setup._start_admin_automation_service",
        lambda config_dir, plan: {"status": "service-running", "inbox_dir": str(tmp_path / "state" / "recipe_requests" / "inbox")},
    )
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_hosts:
    - caller.example.internal
  recipe_inbox: {recipe_inbox}
recipe_automation:
  auto_materialize: true
  auto_validate_existing_tuples: true
  auto_activate_existing_validated_recipe: true
  auto_promote_candidates: true
  auto_billable_validation: true
  auto_validation_budget_usd: 0.10
  auto_activate_validated: true
  auto_set_auto_select: true
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    next_text = setup_next_text(config_dir)

    assert "OOB readiness: onboarding-ready" in report
    assert "Panopticon: service-running" in report
    assert "Panopticon bootstrap: processed" in report
    assert "provider panopticon bootstrap refresh completed with provider-level warnings" in report
    assert "service-error" not in report
    assert "Now you are good to go for an internal gateway setup." in next_text
    bootstrap = json.loads((tmp_path / "state" / "gpucall" / "setup" / "panopticon-bootstrap-refresh.json").read_text(encoding="utf-8"))
    assert bootstrap["panopticon_report"]["preflight"]["skipped_tuples"] == ["runpod-missing-target"]


def test_setup_compose_service_mode_is_bounded_without_compose_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_SETUP_START_SERVICES", "1")
    monkeypatch.setenv("GPUCALL_SETUP_SERVICE_MODE", "docker-compose-service")
    monkeypatch.delenv("GPUCALL_SETUP_COMPOSE_FILE", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
recipe_automation:
  auto_materialize: true
tenant_onboarding:
  recipe_inbox: /tmp/gpucall-recipe-inbox
""".lstrip(),
        encoding="utf-8",
    )

    setup_plan = _load_setup_plan(plan)
    panopticon = _start_panopticon_background_service(tmp_path / "config", setup_plan)
    admin = _start_admin_automation_service(tmp_path / "config", setup_plan)

    assert panopticon["status"] == "service-initialized"
    assert panopticon["service_mode"] == "docker-compose-service"
    assert "GPUCALL_SETUP_COMPOSE_FILE" in panopticon["reason"]
    assert admin["status"] == "service-initialized"
    assert admin["service_mode"] == "docker-compose-service"
    assert "GPUCALL_SETUP_COMPOSE_FILE" in admin["reason"]


def test_admin_automation_service_disabled_is_not_onboarding_ready(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("GPUCALL_SETUP_LIVE_PROVIDER_PROBES", "1")
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})

    def fake_refresh(*, config_dir, panopticon_path, **kwargs):
        store_panopticon_evidence(
            {
                "modal-a10g": {
                    "tuple": "modal-a10g",
                    "adapter": "modal",
                    "status": "live_revalidated",
                    "checked": True,
                    "findings": [
                        {
                            "tuple": "modal-a10g",
                            "adapter": "modal",
                            "dimension": "health",
                            "severity": "info",
                            "source": "modal",
                        }
                    ],
                }
            },
            panopticon_path,
        )
        return {"phase": "provider-panopticon-refresh", "preflight": {"status": "ok"}, "observed_count": 1, "snapshot_count": 1}

    monkeypatch.setattr("gpucall.cli_commands.setup.refresh_panopticon", fake_refresh)
    monkeypatch.setattr(
        "gpucall.cli_commands.setup._start_panopticon_background_service",
        lambda config_dir, plan: {"status": "service-running", "health_url": "http://127.0.0.1:18090/healthz", "pid": 1234},
    )
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_hosts:
    - caller.example.internal
  recipe_inbox: {recipe_inbox}
recipe_automation:
  auto_materialize: true
  auto_validate_existing_tuples: true
  auto_activate_existing_validated_recipe: true
  auto_promote_candidates: true
  auto_billable_validation: true
  auto_validation_budget_usd: 0.10
  auto_activate_validated: true
  auto_set_auto_select: true
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    assert "OOB readiness: onboarding-ready-provisional" in report
    assert "Panopticon: service-running" in report
    assert "Admin automation: service-initialized" in report


def test_setup_plan_yes_alone_does_not_consent_to_modal_provider_mutation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
    deploy_worker: true
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        apply_setup_plan(tmp_path / "config", plan, dry_run=False, yes=True)

    assert "requires explicit provider-mutation consent" in str(exc.value)
    assert "--yes alone is not consent" in str(exc.value)


def test_setup_plan_accepts_runpod_credentials_without_endpoint_for_first_install(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    save_credentials("runpod", {"api_key": "rk_test"})
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  runpod:
    enabled: true
    credentials:
      source: gpucall_credentials
recipe_automation:
  auto_materialize: true
  auto_promote_candidates: true
  auto_provision_supply: true
  auto_apply_supply: false
""".lstrip(),
        encoding="utf-8",
    )

    dry_run = apply_setup_plan(config_dir, plan, dry_run=True, yes=True)
    assert "Setup plan: internal-team" in dry_run
    assert "provider account: runpod (endpoint provisioning pending)" in dry_run
    assert "runpod endpoint_id omitted; provider account will be connected" in dry_run
    assert "runpod requires endpoint_id" not in dry_run

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    assert "Applied setup plan." in report
    assert "RunPod account connected; endpoint provisioning pending" in report
    surface = (config_dir / "surfaces" / "runpod-vllm-serverless.yml").read_text(encoding="utf-8")
    assert "target:" not in surface
    assert "endpoint: null" in surface

    providers = setup_section_text(config_dir, "providers")
    assert "[partial] RunPod account connected; endpoint provisioning pending" in providers
    assert "[ok] RunPod managed endpoint" not in providers


def test_setup_plan_accepts_modal_gpucall_credentials_without_cli_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
""".lstrip(),
        encoding="utf-8",
    )

    dry_run = apply_setup_plan(config_dir, plan, dry_run=True, yes=True)
    assert "Setup plan: internal-team" in dry_run
    assert "modal requires credentials.source: official_cli" not in dry_run
    assert "modal credentials.source=gpucall_credentials but missing" not in dry_run

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    assert "Applied setup plan." in report
    assert "Modal configured" in report


def test_setup_apply_writes_profile_before_bounded_panopticon_bootstrap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.delenv("GPUCALL_SETUP_LIVE_PROVIDER_PROBES", raising=False)
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})
    observed: dict[str, str] = {}

    def fake_bootstrap(config_dir, plan):
        observed["setup_state"] = (config_dir / "setup.yml").read_text(encoding="utf-8")
        return {
            "schema_version": 1,
            "phase": "provider-panopticon-bootstrap-refresh",
            "status": "processed",
            "mode": "preflight-only",
            "reason": "inline live probes skipped; Provider Panopticon service will refresh provider evidence in the background",
        }

    monkeypatch.setattr("gpucall.cli_commands.setup._run_panopticon_bootstrap_refresh", fake_bootstrap)
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.modal.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    assert "profile: internal-team" in observed["setup_state"]
    assert "Profile: internal-team" in report
    assert "inline live probes skipped" in report


def test_setup_preflight_only_bootstrap_writes_panopticon_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("GPUCALL_SETUP_START_SERVICES", "0")
    monkeypatch.delenv("GPUCALL_SETUP_LIVE_PROVIDER_PROBES", raising=False)
    save_credentials("modal", {"token_id": "ak-test", "token_secret": "as-test"})
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.modal.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  modal:
    enabled: true
    credentials:
      source: gpucall_credentials
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    snapshot = json.loads((tmp_path / "state" / "gpucall" / "catalog" / "provider-panopticon.json").read_text(encoding="utf-8"))

    assert "setup preflight evidence written" in report
    assert "Panopticon evidence: evidence-fresh" in report
    assert snapshot["tuples"]["modal-setup-bootstrap"]["adapter"] == "modal"
    assert snapshot["tuples"]["modal-setup-bootstrap"]["status"] == "unknown"
    assert snapshot["tuples"]["modal-setup-bootstrap"]["checked"] is False


def test_setup_plan_keeps_hyperstack_ssh_path_out_of_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    save_credentials("hyperstack", {"api_key": "hs-test"})
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  hyperstack:
    enabled: true
    credentials:
      source: gpucall_credentials
    ssh_key_path: ~/.ssh/gpucall_hyperstack_ed25519
""".lstrip(),
        encoding="utf-8",
    )

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    credentials = load_credentials()
    registry = load_provider_registry()

    assert "Applied setup plan." in report
    assert credentials["hyperstack"] == {"api_key": "hs-test"}
    assert registry["providers"]["hyperstack"]["metadata"]["ssh_key_path"] == "~/.ssh/gpucall_hyperstack_ed25519"


def test_setup_plan_rejects_invalid_recipe_automation_chain(tmp_path) -> None:
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
recipe_automation:
  auto_billable_validation: true
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        apply_setup_plan(tmp_path / "config", plan, dry_run=True, yes=True)

    assert "auto_billable_validation requires auto_promote_candidates or auto_validate_existing_tuples" in str(exc.value)


def test_setup_plan_rejects_raw_env_style_credentials(tmp_path) -> None:
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  runpod:
    enabled: true
    api_key_env: RUNPOD_API_KEY
    endpoint_id: rp-xxxxxxxxxxxx
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        apply_setup_plan(tmp_path / "config", plan, dry_run=True, yes=True)

    assert "Extra inputs are not permitted" in str(exc.value)


def test_setup_plan_yes_rejects_interactive_prompt_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
providers:
  runpod:
    enabled: true
    credentials:
      source: prompt
    endpoint_id: rp-xxxxxxxxxxxx
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        apply_setup_plan(tmp_path / "config", plan, dry_run=False, yes=True)

    assert "setup apply --yes cannot use credentials.source: prompt" in str(exc.value)


def test_setup_handoff_prompt_does_not_include_api_key(tmp_path) -> None:
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  recipe_inbox: admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    prompt = export_handoff_prompt(config_dir, "example-system")

    assert "System name: example-system" in prompt
    assert "https://gpucall.example.internal/v2/bootstrap/tenant-key" in prompt
    assert "GPUCALL_API_KEY_HANDOFF_MODE" in prompt
    assert "GPUCALL_API_KEY: `[redacted-key]`" not in prompt
    assert 'export GPUCALL_API_KEY="' not in prompt


def test_setup_handoff_prompt_uses_operator_asset_urls(tmp_path) -> None:
    config_dir = tmp_path / "config"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        """
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  recipe_inbox: admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox
handoff_assets:
  onboarding_prompt_url: https://assets.example/docs/prompt.md
  onboarding_manual_url: https://assets.example/docs/manual.md
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.17-py3-none-any.whl
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    prompt = export_handoff_prompt(config_dir, "example-system")

    assert "https://assets.example/docs/prompt.md" in prompt
    assert "https://assets.example/docs/manual.md" in prompt
    assert "https://assets.example/sdk/gpucall_sdk-2.0.17-py3-none-any.whl" in prompt
