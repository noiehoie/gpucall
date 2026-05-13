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
        rows = conn.execute("SELECT tenant_id, estimated_cost_usd, tuple, recipe, plan_id, status FROM tenant_usage ORDER BY id").fetchall()

    assert {"tuple", "status"}.issubset(columns)
    assert rows == [
        ("tenant-a", 1.25, "legacy-tuple", "recipe-a", "plan-a", "committed"),
        ("tenant-a", 0.5, "new-tuple", "recipe-b", "plan-b", "reserved"),
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
        row = conn.execute("SELECT tuple, recipe, plan_id, status FROM tenant_usage").fetchone()

    assert {"tuple", "recipe", "plan_id", "status"}.issubset(columns)
    assert row == ("tuple-a", "recipe-a", "plan-a", "reserved")


def test_tenant_usage_release_excludes_reserved_plan_from_spend(tmp_path) -> None:
    from datetime import datetime, timezone

    ledger = TenantUsageLedger(tmp_path / "tenant_usage.db")
    ledger.reserve("tenant-a", 2.0, tuple="tuple-a", recipe="recipe-a", plan_id="plan-a")
    assert ledger.spend_since("tenant-a", datetime(2026, 1, 1, tzinfo=timezone.utc)) == 2.0

    ledger.release_plan("plan-a")

    assert ledger.spend_since("tenant-a", datetime(2026, 1, 1, tzinfo=timezone.utc)) == 0.0


def test_tenant_usage_commit_keeps_reserved_plan_in_spend(tmp_path) -> None:
    from datetime import datetime, timezone

    ledger = TenantUsageLedger(tmp_path / "tenant_usage.db")
    ledger.reserve("tenant-a", 2.0, tuple="tuple-a", recipe="recipe-a", plan_id="plan-a")
    ledger.commit_plan("plan-a")

    assert ledger.spend_since("tenant-a", datetime(2026, 1, 1, tzinfo=timezone.utc)) == 2.0
