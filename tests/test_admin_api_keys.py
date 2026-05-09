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
    assert oct(handoff.stat().st_mode & 0o777) == "0o600"
    assert (config_dir / "tenants" / "external-system.yml").exists()


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
