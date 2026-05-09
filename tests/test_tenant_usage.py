from __future__ import annotations

import sqlite3

from gpucall.tenant import TenantUsageLedger


def test_tenant_usage_ledger_migrates_legacy_provider_column(tmp_path) -> None:
    path = tmp_path / "tenant_usage.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE tenant_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                provider TEXT,
                recipe TEXT,
                plan_id TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tenant_usage (tenant_id, estimated_cost_usd, provider, recipe, plan_id, recorded_at)
            VALUES ('tenant-a', 1.25, 'legacy-tuple', 'recipe-a', 'plan-a', '2026-05-01T00:00:00+00:00')
            """
        )

    ledger = TenantUsageLedger(path)
    ledger.reserve("tenant-a", 0.5, tuple="new-tuple", recipe="recipe-b", plan_id="plan-b")

    with sqlite3.connect(path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tenant_usage)").fetchall()}
        rows = conn.execute("SELECT tenant_id, estimated_cost_usd, tuple, recipe, plan_id FROM tenant_usage ORDER BY id").fetchall()

    assert "tuple" in columns
    assert rows == [
        ("tenant-a", 1.25, "legacy-tuple", "recipe-a", "plan-a"),
        ("tenant-a", 0.5, "new-tuple", "recipe-b", "plan-b"),
    ]


def test_tenant_usage_ledger_adds_missing_optional_columns(tmp_path) -> None:
    path = tmp_path / "tenant_usage.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE tenant_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )

    ledger = TenantUsageLedger(path)
    ledger.reserve("tenant-a", 0.5, tuple="tuple-a", recipe="recipe-a", plan_id="plan-a")

    with sqlite3.connect(path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tenant_usage)").fetchall()}
        row = conn.execute("SELECT tuple, recipe, plan_id FROM tenant_usage").fetchone()

    assert {"tuple", "recipe", "plan_id"}.issubset(columns)
    assert row == ("tuple-a", "recipe-a", "plan-a")
