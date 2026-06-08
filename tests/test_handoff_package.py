from __future__ import annotations

import getpass
import json
import sys

from gpucall.cli_commands.setup import apply_setup_plan
from gpucall.handoff_package import build_handoff_package, human_readme_quality_blockers, prompt_quality_blockers, write_handoff_package


def test_handoff_package_bundles_caller_ai_prompt_with_concrete_values(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GPUCALL_HANDOFF_SSH_USER", raising=False)
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  recipe_inbox: {recipe_inbox}
handoff_assets:
  onboarding_prompt_url: https://assets.example/docs/prompt.md
  onboarding_manual_url: https://assets.example/docs/manual.md
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.28-py3-none-any.whl
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    package = build_handoff_package(config_dir, "example-caller")

    assert package["phase"] == "gpucall-caller-handoff-package"
    assert package["checklist"]["prompt_quality"]["go"] is True
    assert package["checklist"]["human_readme_quality"]["go"] is True
    prompt = package["prompt"]
    readme = package["human_readme"]
    remote_recipe_inbox = f"{getpass.getuser()}@gpucall.example.internal:{recipe_inbox}"
    assert "https://gpucall.example.internal" in prompt
    assert remote_recipe_inbox in prompt
    assert "GPUCALL_API_KEY_HANDOFF_MODE: `trusted_bootstrap`" in prompt
    assert "request the key exactly once from `GPUCALL_BOOTSTRAP_ENDPOINT`" in prompt
    assert 'uv tool install --force "$GPUCALL_SDK_WHEEL_URL"' in prompt
    assert "gpucall-migrate assess" in prompt
    assert "gpucall-recipe-draft" in prompt
    assert "No-Go: gpucall-admin must configure object_store before image/file canary" in prompt
    assert '"/readyz/details"' in prompt
    assert 'headers["Authorization"] = "Bearer " + api_key' in prompt
    assert "gateway canary while the recipe request is still `pending`." in prompt
    assert 'state != "processed"' in prompt
    assert "existing_tuple_activation_decision" in prompt
    assert "PENDING_BUDGET_APPROVAL" in prompt
    assert "waiting for explicit validation budget approval" in prompt
    assert "<caller baseline command>" not in prompt
    assert "First identify the smallest representative baseline command" in prompt
    assert "$CALLER_BASELINE_COMMAND" in prompt
    assert "Do not clone, install, modify, vendor, or import the gpucall gateway repository." in prompt
    assert prompt_quality_blockers(prompt, package["contract"]) == []
    assert "Responsibility Boundary" in readme
    assert "caller-ai-onboarding-prompt.md" in readme
    assert "This package does not include API keys, provider credentials" in readme
    assert "Do not choose providers, GPUs, endpoint IDs, model IDs, recipes, tuples, or fallback order in caller code." in readme
    assert "Go / No-Go Rule" in readme
    assert "https://gpucall.example.internal" in readme
    assert remote_recipe_inbox in readme
    assert human_readme_quality_blockers(readme, package["contract"]) == []


def test_handoff_package_keeps_same_machine_local_inbox_path(tmp_path) -> None:
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: http://localhost:18088
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 127.0.0.1/32
  recipe_inbox: {recipe_inbox}
handoff_assets:
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.58-py3-none-any.whl
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    package = build_handoff_package(config_dir, "same-machine-caller")

    assert package["contract"]["inboxes"]["recipe"] == str(recipe_inbox)
    assert f"@localhost:{recipe_inbox}" not in package["prompt"]


def test_handoff_package_converts_local_inbox_path_for_remote_gateway(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_HANDOFF_SSH_USER", "operator")
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: http://192.0.2.10:18088
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 192.0.2.20/32
  recipe_inbox: {recipe_inbox}
handoff_assets:
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.58-py3-none-any.whl
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)

    package = build_handoff_package(config_dir, "remote-caller")

    remote_recipe_inbox = f"operator@192.0.2.10:{recipe_inbox}"
    assert package["contract"]["inboxes"]["recipe"] == remote_recipe_inbox
    assert package["contract"]["inboxes"]["quality_feedback"] == f"operator@192.0.2.10:{recipe_inbox.parent.parent}/quality_feedback/inbox"
    assert remote_recipe_inbox in package["prompt"]
    assert "Use `--inbox-dir` only when the caller repository is on the same host" in package["prompt"]


def test_prompt_quality_does_not_reject_marker_text_inside_concrete_values(tmp_path) -> None:
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  recipe_inbox: {recipe_inbox}
handoff_assets:
  caller_sdk_wheel_url: https://assets.example/TODO/gpucall_sdk-2.0.28-py3-none-any.whl
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    package = build_handoff_package(config_dir, "example-caller")

    assert package["checklist"]["prompt_quality"]["go"] is True
    assert package["checklist"]["human_readme_quality"]["go"] is True
    assert prompt_quality_blockers(package["prompt"] + "\nTODO: fill this later\n", package["contract"]) == [
        "prompt_contains_unresolved_marker"
    ]
    assert human_readme_quality_blockers(package["human_readme"] + "\nTODO: fill this later\n", package["contract"]) == [
        "readme_contains_unresolved_marker"
    ]


def test_handoff_package_write_creates_manifest_and_0600_files(tmp_path) -> None:
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  recipe_inbox: {recipe_inbox}
handoff_assets:
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.58-py3-none-any.whl
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    output = tmp_path / "handoff" / "example-caller"

    report = write_handoff_package(config_dir, "example-caller", output)

    assert report["files"] == [
        "CALLER_ENGINEER_README.md",
        "MANIFEST.json",
        "acceptance-checklist.json",
        "caller-ai-onboarding-prompt.md",
        "gpucall-handoff.json",
    ]
    assert oct(output.stat().st_mode & 0o777) == "0o700"
    for name in report["files"]:
        assert (output / name).exists()
        assert oct((output / name).stat().st_mode & 0o777) == "0o600"
    manifest = json.loads((output / "MANIFEST.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {
        "gpucall-handoff.json",
        "CALLER_ENGINEER_README.md",
        "caller-ai-onboarding-prompt.md",
        "acceptance-checklist.json",
    }
    assert report["human_readme_quality"]["go"] is True


def test_setup_cli_exports_handoff_package(tmp_path, monkeypatch, capsys) -> None:
    from gpucall.cli import main

    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  recipe_inbox: {recipe_inbox}
handoff_assets:
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.58-py3-none-any.whl
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    output = tmp_path / "handoff" / "example-caller"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gpucall",
            "setup",
            "export-handoff-package",
            "--config-dir",
            str(config_dir),
            "--system-name",
            "example-caller",
            "--output-dir",
            str(output),
        ],
    )

    main()
    stdout = capsys.readouterr().out

    assert "phase: gpucall-caller-handoff-package-write" in stdout
    assert "status: generated" in stdout
    assert "Caller handoff package generated." in stdout
    assert "caller_ai_onboarding_prompt_path:" in stdout
    assert "next_action:" in stdout
    assert (output / "caller-ai-onboarding-prompt.md").exists()
    assert (output / "CALLER_ENGINEER_README.md").exists()


def test_handoff_package_refuses_unpublished_default_sdk_release(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    recipe_inbox = tmp_path / "state" / "recipe_requests" / "inbox"
    plan = tmp_path / "gpucall.setup.yml"
    plan.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: https://gpucall.example.internal
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.42/32
  recipe_inbox: {recipe_inbox}
""".lstrip(),
        encoding="utf-8",
    )
    apply_setup_plan(config_dir, plan, dry_run=False, yes=True)
    monkeypatch.setattr("gpucall.handoff_package._default_sdk_wheel_url_available", lambda url: False)

    try:
        write_handoff_package(config_dir, "example-caller", tmp_path / "handoff")
    except ValueError as exc:
        assert "default caller SDK wheel URL is not reachable" in str(exc)
    else:
        raise AssertionError("handoff package should reject an unreachable default SDK wheel URL")
