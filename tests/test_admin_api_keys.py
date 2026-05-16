from __future__ import annotations

import json

from gpucall.cli import admin_command, init_config
from gpucall.credentials import load_credentials


def test_admin_tenant_key_create_writes_credentials_and_lists_fingerprint(tmp_path, monkeypatch, capsys) -> None:
    credentials = tmp_path / "credentials.yml"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))

    init_config(config_dir)
    capsys.readouterr()
    admin_command(
        "tenant-create",
        config_dir,
        name="external-system",
        requests_per_minute=None,
        daily_budget_usd=None,
        monthly_budget_usd=None,
        max_request_estimated_cost_usd=None,
        object_prefix=None,
    )
    capsys.readouterr()

    admin_command(
        "tenant-key-create",
        config_dir,
        name="external-system",
        requests_per_minute=None,
        daily_budget_usd=None,
        monthly_budget_usd=None,
        max_request_estimated_cost_usd=None,
        object_prefix=None,
    )
    created = json.loads(capsys.readouterr().out)

    assert created["tenant"] == "external-system"
    assert created["api_key"].startswith("gpk_")
    assert created["handoff"]["GPUCALL_API_KEY"] == created["api_key"]
    assert load_credentials()["auth"]["tenant_keys"] == f"external-system:{created['api_key']}"

    admin_command(
        "tenant-key-list",
        config_dir,
        name=None,
        requests_per_minute=None,
        daily_budget_usd=None,
        monthly_budget_usd=None,
        max_request_estimated_cost_usd=None,
        object_prefix=None,
    )
    listed_raw = capsys.readouterr().out
    listed = json.loads(listed_raw)

    assert created["api_key"] not in listed_raw
    assert listed["tenant_keys"]["external-system"] == {
        "configured": True,
        "api_key_fingerprint": created["api_key_fingerprint"],
    }


def test_admin_tenant_key_create_requires_existing_tenant(tmp_path, monkeypatch) -> None:
    credentials = tmp_path / "credentials.yml"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))
    init_config(config_dir)

    try:
        admin_command(
            "tenant-key-create",
            config_dir,
            name="missing-system",
            requests_per_minute=None,
            daily_budget_usd=None,
            monthly_budget_usd=None,
            max_request_estimated_cost_usd=None,
            object_prefix=None,
        )
    except SystemExit as exc:
        assert "unknown tenant: missing-system" in str(exc)
    else:
        raise AssertionError("tenant-key-create should require an existing tenant")


def test_admin_tenant_onboard_creates_tenant_key_and_handoff_file(tmp_path, monkeypatch, capsys) -> None:
    credentials = tmp_path / "credentials.yml"
    config_dir = tmp_path / "config"
    handoff = tmp_path / "handoff" / "external-system.env"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))

    init_config(config_dir)
    (config_dir / "admin.yml").write_text("api_key_handoff_mode: handoff_file\n", encoding="utf-8")
    capsys.readouterr()
    admin_command(
        "tenant-onboard",
        config_dir,
        name="external-system",
        gateway_url="https://gpucall.example.internal",
        recipe_inbox="admin@gpucall.example.internal:/opt/gpucall/state/recipe_requests/inbox",
        output=handoff,
        output_format="env",
        requests_per_minute=30,
        daily_budget_usd=5.0,
        monthly_budget_usd=50.0,
        max_request_estimated_cost_usd=1.0,
        object_prefix=None,
    )
    report_raw = capsys.readouterr().out
    report = json.loads(report_raw)
    handoff_payload = handoff.read_text(encoding="utf-8")
    token = load_credentials()["auth"]["tenant_keys"].split(":", 1)[1]

    assert "api_key" not in report
    assert token not in report_raw
    assert "GPUCALL_API_KEY='gpk_" in handoff_payload
    assert "GPUCALL_BASE_URL='https://gpucall.example.internal'" in handoff_payload
    assert "GPUCALL_RECIPE_INBOX='admin@gpucall.example.internal:/opt/gpucall/state/recipe_requests/inbox'" in handoff_payload
    assert "GPUCALL_QUALITY_FEEDBACK_INBOX='admin@gpucall.example.internal:/opt/gpucall/state/quality_feedback/inbox'" in handoff_payload
    assert oct(handoff.stat().st_mode & 0o777) == "0o600"
    assert (config_dir / "tenants" / "external-system.yml").exists()


def test_admin_automation_configure_trusted_bootstrap(tmp_path, capsys) -> None:
    config_dir = tmp_path / "config"
    init_config(config_dir)
    capsys.readouterr()

    admin_command(
        "automation-configure",
        config_dir,
        name=None,
        handoff_mode="trusted_bootstrap",
        bootstrap_allowed_cidrs=["10.0.0.42/32"],
        bootstrap_allowed_hosts=["trusted-host"],
        bootstrap_gateway_url="https://gpucall.example.internal",
        bootstrap_recipe_inbox="admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox",
        enable_recipe_auto_materialize=True,
        disable_recipe_auto_materialize=False,
        enable_recipe_auto_promote=True,
        disable_recipe_auto_promote=False,
        enable_recipe_auto_billable_validation=True,
        disable_recipe_auto_billable_validation=False,
        enable_recipe_auto_activate=False,
        disable_recipe_auto_activate=False,
        recipe_promotion_work_dir="/srv/gpucall/state/recipe_requests/promotions",
        onboarding_prompt_url="https://assets.example/docs/prompt.md",
        onboarding_manual_url="https://assets.example/docs/manual.md",
        caller_sdk_wheel_url="https://assets.example/sdk/gpucall_sdk-2.0.17-py3-none-any.whl",
        manifest=None,
        gateway_url=None,
        recipe_inbox=None,
        output=None,
        requests_per_minute=None,
        daily_budget_usd=None,
        monthly_budget_usd=None,
        max_request_estimated_cost_usd=None,
        object_prefix=None,
    )
    report = json.loads(capsys.readouterr().out)
    admin_yml = (config_dir / "admin.yml").read_text(encoding="utf-8")

    assert report["admin_automation"]["api_key_handoff_mode"] == "trusted_bootstrap"
    assert report["admin_automation"]["recipe_inbox_auto_materialize"] is True
    assert report["admin_automation"]["recipe_inbox_auto_promote_candidates"] is True
    assert report["admin_automation"]["recipe_inbox_auto_billable_validation"] is True
    assert report["admin_automation"]["recipe_inbox_auto_activate_validated"] is False
    assert report["admin_automation"]["recipe_inbox_promotion_work_dir"] == "/srv/gpucall/state/recipe_requests/promotions"
    assert report["admin_automation"]["trusted_bootstrap"]["allowed_cidrs"] == ["10.0.0.42/32"]
    assert report["admin_automation"]["trusted_bootstrap"]["allowed_hosts"] == ["trusted-host"]
    assert report["admin_automation"]["handoff_assets"]["onboarding_prompt_url"] == "https://assets.example/docs/prompt.md"
    assert report["admin_automation"]["handoff_assets"]["onboarding_manual_url"] == "https://assets.example/docs/manual.md"
    assert report["admin_automation"]["handoff_assets"]["caller_sdk_wheel_url"] == "https://assets.example/sdk/gpucall_sdk-2.0.17-py3-none-any.whl"
    assert "api_key_handoff_mode: trusted_bootstrap" in admin_yml
    assert "recipe_inbox_auto_promote_candidates: true" in admin_yml
    assert "recipe_inbox_auto_billable_validation: true" in admin_yml
    assert "api_key_bootstrap_gateway_url: https://gpucall.example.internal" in admin_yml


def test_admin_automation_configure_rejects_bootstrap_without_allowlist(tmp_path, capsys) -> None:
    config_dir = tmp_path / "config"
    init_config(config_dir)
    capsys.readouterr()

    try:
        admin_command(
            "automation-configure",
            config_dir,
            name=None,
            handoff_mode="trusted_bootstrap",
            manifest=None,
            gateway_url=None,
            recipe_inbox=None,
            output=None,
            requests_per_minute=None,
            daily_budget_usd=None,
            monthly_budget_usd=None,
            max_request_estimated_cost_usd=None,
            object_prefix=None,
        )
    except SystemExit as exc:
        assert "trusted_bootstrap requires at least one allowed CIDR or host" in str(exc)
    else:
        raise AssertionError("trusted bootstrap should require an allowlist")


def test_admin_automation_status_reports_non_secret_config(tmp_path, capsys) -> None:
    config_dir = tmp_path / "config"
    init_config(config_dir)
    capsys.readouterr()

    admin_command(
        "automation-status",
        config_dir,
        name=None,
        manifest=None,
        gateway_url=None,
        recipe_inbox=None,
        output=None,
        requests_per_minute=None,
        daily_budget_usd=None,
        monthly_budget_usd=None,
        max_request_estimated_cost_usd=None,
        object_prefix=None,
    )
    report = json.loads(capsys.readouterr().out)

    assert report["api_key_handoff_mode"] == "manual"
    assert report["trusted_bootstrap"]["enabled"] is False
    assert "gpk_" not in json.dumps(report)
    assert "credentials_path" not in report


def test_admin_tenant_onboard_refuses_existing_key(tmp_path, monkeypatch, capsys) -> None:
    credentials = tmp_path / "credentials.yml"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))

    init_config(config_dir)
    (config_dir / "admin.yml").write_text("api_key_handoff_mode: handoff_file\n", encoding="utf-8")
    capsys.readouterr()
    kwargs = {
        "config_dir": config_dir,
        "name": "external-system",
        "gateway_url": "https://gpucall.example.internal",
        "recipe_inbox": "admin@gpucall.example.internal:/opt/gpucall/state/recipe_requests/inbox",
        "output": tmp_path / "external-system.env",
        "output_format": "json",
        "requests_per_minute": None,
        "daily_budget_usd": None,
        "monthly_budget_usd": None,
        "max_request_estimated_cost_usd": None,
        "object_prefix": None,
    }
    admin_command("tenant-onboard", **kwargs)
    capsys.readouterr()
    kwargs["output"] = tmp_path / "external-system-2.env"

    try:
        admin_command("tenant-onboard", **kwargs)
    except SystemExit as exc:
        assert "tenant key already exists for external-system" in str(exc)
    else:
        raise AssertionError("tenant-onboard should not reprint an existing key")


def test_admin_tenant_onboard_requires_config_opt_in(tmp_path, monkeypatch, capsys) -> None:
    credentials = tmp_path / "credentials.yml"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))

    init_config(config_dir)
    capsys.readouterr()
    try:
        admin_command(
            "tenant-onboard",
            config_dir,
            name="external-system",
            gateway_url="https://gpucall.example.internal",
            recipe_inbox="admin@gpucall.example.internal:/opt/gpucall/state/recipe_requests/inbox",
            output=tmp_path / "external-system.env",
            output_format="env",
            requests_per_minute=None,
            daily_budget_usd=None,
            monthly_budget_usd=None,
            max_request_estimated_cost_usd=None,
            object_prefix=None,
        )
    except SystemExit as exc:
        assert "api_key_handoff_mode: handoff_file" in str(exc)
    else:
        raise AssertionError("tenant-onboard should require config opt-in")


def test_admin_tenant_onboard_batch_creates_isolated_handoffs(tmp_path, monkeypatch, capsys) -> None:
    credentials = tmp_path / "credentials.yml"
    config_dir = tmp_path / "config"
    manifest = tmp_path / "systems.yml"
    handoff_dir = tmp_path / "handoffs"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))

    init_config(config_dir)
    (config_dir / "admin.yml").write_text("api_key_handoff_mode: handoff_file\n", encoding="utf-8")
    manifest.write_text(
        """
systems:
  - name: example-news
    daily_budget_usd: 10
  - name: example-analysis
    requests_per_minute: 30
""".lstrip(),
        encoding="utf-8",
    )
    capsys.readouterr()

    admin_command(
        "tenant-onboard-batch",
        config_dir,
        name=None,
        manifest=manifest,
        gateway_url="https://gpucall.example.internal",
        recipe_inbox="admin@gpucall.example.internal:/opt/gpucall/state/recipe_requests/inbox",
        output=handoff_dir,
        output_format="env",
        requests_per_minute=None,
        daily_budget_usd=None,
        monthly_budget_usd=None,
        max_request_estimated_cost_usd=None,
        object_prefix=None,
    )
    report_raw = capsys.readouterr().out
    report = json.loads(report_raw)
    credentials_payload = load_credentials()["auth"]["tenant_keys"]

    assert report["count"] == 2
    assert (handoff_dir / "example-news.gpucall.env").exists()
    assert (handoff_dir / "example-analysis.gpucall.env").exists()
    assert oct(handoff_dir.stat().st_mode & 0o777) == "0o700"
    assert oct((handoff_dir / "example-news.gpucall.env").stat().st_mode & 0o777) == "0o600"
    assert "example-news:gpk_" in credentials_payload
    assert "example-analysis:gpk_" in credentials_payload
    for item in credentials_payload.split(","):
        _tenant, token = item.split(":", 1)
        assert token not in report_raw


def test_admin_tenant_onboard_batch_rejects_duplicate_system_names(tmp_path, monkeypatch, capsys) -> None:
    credentials = tmp_path / "credentials.yml"
    config_dir = tmp_path / "config"
    manifest = tmp_path / "systems.yml"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))

    init_config(config_dir)
    (config_dir / "admin.yml").write_text("api_key_handoff_mode: handoff_file\n", encoding="utf-8")
    manifest.write_text(
        """
systems:
  - name: same-system
  - name: same-system
""".lstrip(),
        encoding="utf-8",
    )
    capsys.readouterr()

    try:
        admin_command(
            "tenant-onboard-batch",
            config_dir,
            name=None,
            manifest=manifest,
            gateway_url="https://gpucall.example.internal",
            recipe_inbox="admin@gpucall.example.internal:/opt/gpucall/state/recipe_requests/inbox",
            output=tmp_path / "handoffs",
            output_format="env",
            requests_per_minute=None,
            daily_budget_usd=None,
            monthly_budget_usd=None,
            max_request_estimated_cost_usd=None,
            object_prefix=None,
        )
    except SystemExit as exc:
        assert "duplicate tenant name: same-system" in str(exc)
    else:
        raise AssertionError("tenant-onboard-batch should reject duplicate names")
