from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpucall.cli_commands.setup import apply_setup_plan, export_handoff_prompt, setup_next_text, setup_section_text, setup_status_text, write_starter_plan
from gpucall.config import load_admin_automation, load_object_store
from gpucall.credentials import load_credentials, save_credentials
from gpucall.domain import ApiKeyHandoffMode


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
    assert "All required setup checks are satisfied" in next_text
    assert "gateway URL and caller auth before external callers" in setup_status_text(config_dir)


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
    assert "provider account: runpod (endpoint provisioning pending)" in dry_run


def test_setup_sections_cover_recipe_inbox_and_external_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(tmp_path / "credentials.yml"))

    recipe = setup_section_text(tmp_path / "config", "recipe-inbox")
    external = setup_section_text(tmp_path / "config", "external-system")

    assert "Auto materialize recipes" in recipe
    assert "Auto run billable validation" in recipe
    assert "gpucall-recipe-admin process-inbox" in recipe
    assert "export-handoff-prompt" in external
    assert "without embedding any API key" in external


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

    report = apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
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
