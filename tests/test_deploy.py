from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_helm_chart_mounts_config_and_persistent_state() -> None:
    deployment = (ROOT / "deploy" / "helm" / "gpucall" / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    pvc = (ROOT / "deploy" / "helm" / "gpucall" / "templates" / "pvc.yaml").read_text(encoding="utf-8")

    assert "mountPath: {{ .Values.config.mountPath }}" in deployment
    assert "persistentVolumeClaim:" in deployment
    assert "readinessProbe:" in deployment
    assert "livenessProbe:" in deployment
    assert "PersistentVolumeClaim" in pvc


def test_systemd_unit_uses_installed_binary_and_hardening() -> None:
    unit = (ROOT / "deploy" / "systemd" / "gpucall.service").read_text(encoding="utf-8")

    assert "uv run" not in unit
    assert "ExecStart=/usr/local/bin/gpucall serve" in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=" in unit


def test_postgres_schema_matches_runtime_payload_contract() -> None:
    schema = (ROOT / "deploy" / "postgres" / "001_init.sql").read_text(encoding="utf-8")

    assert "payload JSONB NOT NULL" in schema
    assert "gpucall_idempotency" in schema
    assert "created_at DOUBLE PRECISION NOT NULL" in schema
    assert "status INTEGER NOT NULL" in schema
    assert "content JSONB NOT NULL" in schema
    assert "headers JSONB NOT NULL" in schema
    assert "status_code" not in schema
    assert "response_json" not in schema
    assert "headers_json" not in schema
