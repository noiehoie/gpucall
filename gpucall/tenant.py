from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpucall.credentials import load_credentials
from gpucall.domain import TenantSpec


class TenantBudgetError(RuntimeError):
    def __init__(self, message: str, *, code: str = "TENANT_BUDGET_EXCEEDED", status_code: int = 402) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TenantUsageLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    estimated_cost_usd REAL NOT NULL,
                    tuple TEXT,
                    recipe TEXT,
                    plan_id TEXT,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    recorded_at TEXT NOT NULL
                )
                """
            )
            self._migrate_schema(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tenant_usage_time ON tenant_usage(tenant_id, recorded_at)")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tenant_usage)").fetchall()}
        required = {
            "tuple": "TEXT",
            "recipe": "TEXT",
            "plan_id": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'committed'",
        }
        for column, declaration in required.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE tenant_usage ADD COLUMN {column} {declaration}")
                columns.add(column)
        if "provider" in columns:
            conn.execute("UPDATE tenant_usage SET tuple = provider WHERE tuple IS NULL AND provider IS NOT NULL")

    def reserve(self, tenant_id: str, estimated_cost_usd: float, *, tuple: str | None, recipe: str | None, plan_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tenant_usage (tenant_id, estimated_cost_usd, tuple, recipe, plan_id, status, recorded_at)
                VALUES (?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (tenant_id, estimated_cost_usd, tuple, recipe, plan_id, datetime.now(timezone.utc).isoformat()),
            )

    def reserve_with_budget(
        self,
        tenant_id: str,
        estimated_cost_usd: float,
        *,
        tenant: TenantSpec | None,
        tuple: str | None,
        recipe: str | None,
        plan_id: str | None,
    ) -> None:
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if tenant is not None:
                if tenant.max_request_estimated_cost_usd is not None and estimated_cost_usd > float(tenant.max_request_estimated_cost_usd):
                    raise TenantBudgetError(
                        f"estimated request cost {estimated_cost_usd:.4f} exceeds tenant max_request_estimated_cost_usd "
                        f"{float(tenant.max_request_estimated_cost_usd):.4f}"
                    )
                if tenant.daily_budget_usd is not None:
                    projected = self._spend_since_conn(conn, tenant_id, day_start) + estimated_cost_usd
                    if projected > float(tenant.daily_budget_usd):
                        raise TenantBudgetError(
                            f"tenant daily budget exceeded: projected {projected:.4f} > {float(tenant.daily_budget_usd):.4f}"
                        )
                if tenant.monthly_budget_usd is not None:
                    projected = self._spend_since_conn(conn, tenant_id, month_start) + estimated_cost_usd
                    if projected > float(tenant.monthly_budget_usd):
                        raise TenantBudgetError(
                            f"tenant monthly budget exceeded: projected {projected:.4f} > {float(tenant.monthly_budget_usd):.4f}"
                        )
            conn.execute(
                """
                INSERT INTO tenant_usage (tenant_id, estimated_cost_usd, tuple, recipe, plan_id, status, recorded_at)
                VALUES (?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (tenant_id, estimated_cost_usd, tuple, recipe, plan_id, now.isoformat()),
            )

    def release_plan(self, plan_id: str | None) -> None:
        if not plan_id:
            return
        with self._connect() as conn:
            conn.execute("UPDATE tenant_usage SET status = 'released' WHERE plan_id = ? AND status = 'reserved'", (plan_id,))

    def commit_plan(self, plan_id: str | None) -> None:
        if not plan_id:
            return
        with self._connect() as conn:
            conn.execute("UPDATE tenant_usage SET status = 'committed' WHERE plan_id = ? AND status = 'reserved'", (plan_id,))

    def spend_since(self, tenant_id: str, since: datetime) -> float:
        with self._connect() as conn:
            row = self._spend_since_row(conn, tenant_id, since)
        return float(row[0] or 0)

    @staticmethod
    def _spend_since_row(conn: sqlite3.Connection, tenant_id: str, since: datetime):
        return conn.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM tenant_usage WHERE tenant_id = ? AND recorded_at >= ? AND status IN ('reserved', 'committed')",
            (tenant_id, since.isoformat()),
        ).fetchone()

    @classmethod
    def _spend_since_conn(cls, conn: sqlite3.Connection, tenant_id: str, since: datetime) -> float:
        row = cls._spend_since_row(conn, tenant_id, since)
        return float(row[0] or 0)

    def summary(self, tenants: dict[str, TenantSpec]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows: dict[str, Any] = {}
        for name, tenant in sorted(tenants.items()):
            rows[name] = {
                "daily_budget_usd": tenant.daily_budget_usd,
                "monthly_budget_usd": tenant.monthly_budget_usd,
                "daily_estimated_spend_usd": self.spend_since(name, day_start),
                "monthly_estimated_spend_usd": self.spend_since(name, month_start),
            }
        return rows


class PostgresTenantUsageLedger:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gpucall_tenant_usage (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    estimated_cost_usd DOUBLE PRECISION NOT NULL,
                    tuple TEXT,
                    recipe TEXT,
                    plan_id TEXT,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    recorded_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS gpucall_tenant_usage_time_idx ON gpucall_tenant_usage(tenant_id, recorded_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS gpucall_tenant_usage_plan_idx ON gpucall_tenant_usage(plan_id)")
        self._conn.commit()

    def reserve(self, tenant_id: str, estimated_cost_usd: float, *, tuple: str | None, recipe: str | None, plan_id: str | None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gpucall_tenant_usage (tenant_id, estimated_cost_usd, tuple, recipe, plan_id, status, recorded_at)
                VALUES (%s, %s, %s, %s, %s, 'reserved', %s)
                """,
                (tenant_id, estimated_cost_usd, tuple, recipe, plan_id, datetime.now(timezone.utc)),
            )
        self._conn.commit()

    def reserve_with_budget(
        self,
        tenant_id: str,
        estimated_cost_usd: float,
        *,
        tenant: TenantSpec | None,
        tuple: str | None,
        recipe: str | None,
        plan_id: str | None,
    ) -> None:
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with self._conn.cursor() as cur:
            lock_key = int(hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:15], 16)
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            if tenant is not None:
                if tenant.max_request_estimated_cost_usd is not None and estimated_cost_usd > float(tenant.max_request_estimated_cost_usd):
                    self._conn.rollback()
                    raise TenantBudgetError(
                        f"estimated request cost {estimated_cost_usd:.4f} exceeds tenant max_request_estimated_cost_usd "
                        f"{float(tenant.max_request_estimated_cost_usd):.4f}"
                    )
                if tenant.daily_budget_usd is not None:
                    projected = self._spend_since_cur(cur, tenant_id, day_start) + estimated_cost_usd
                    if projected > float(tenant.daily_budget_usd):
                        self._conn.rollback()
                        raise TenantBudgetError(f"tenant daily budget exceeded: projected {projected:.4f} > {float(tenant.daily_budget_usd):.4f}")
                if tenant.monthly_budget_usd is not None:
                    projected = self._spend_since_cur(cur, tenant_id, month_start) + estimated_cost_usd
                    if projected > float(tenant.monthly_budget_usd):
                        self._conn.rollback()
                        raise TenantBudgetError(
                            f"tenant monthly budget exceeded: projected {projected:.4f} > {float(tenant.monthly_budget_usd):.4f}"
                        )
            cur.execute(
                """
                INSERT INTO gpucall_tenant_usage (tenant_id, estimated_cost_usd, tuple, recipe, plan_id, status, recorded_at)
                VALUES (%s, %s, %s, %s, %s, 'reserved', %s)
                """,
                (tenant_id, estimated_cost_usd, tuple, recipe, plan_id, now),
            )
        self._conn.commit()

    def release_plan(self, plan_id: str | None) -> None:
        if not plan_id:
            return
        with self._conn.cursor() as cur:
            cur.execute("UPDATE gpucall_tenant_usage SET status = 'released' WHERE plan_id = %s AND status = 'reserved'", (plan_id,))
        self._conn.commit()

    def commit_plan(self, plan_id: str | None) -> None:
        if not plan_id:
            return
        with self._conn.cursor() as cur:
            cur.execute("UPDATE gpucall_tenant_usage SET status = 'committed' WHERE plan_id = %s AND status = 'reserved'", (plan_id,))
        self._conn.commit()

    def spend_since(self, tenant_id: str, since: datetime) -> float:
        with self._conn.cursor() as cur:
            return self._spend_since_cur(cur, tenant_id, since)

    @staticmethod
    def _spend_since_cur(cur: Any, tenant_id: str, since: datetime) -> float:
        cur.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_usd), 0)
            FROM gpucall_tenant_usage
            WHERE tenant_id = %s AND recorded_at >= %s AND status IN ('reserved', 'committed')
            """,
            (tenant_id, since),
        )
        row = cur.fetchone()
        return float(row[0] or 0)

    def summary(self, tenants: dict[str, TenantSpec]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            name: {
                "daily_budget_usd": tenant.daily_budget_usd,
                "monthly_budget_usd": tenant.monthly_budget_usd,
                "daily_estimated_spend_usd": self.spend_since(name, day_start),
                "monthly_estimated_spend_usd": self.spend_since(name, month_start),
            }
            for name, tenant in sorted(tenants.items())
        }

    def close(self) -> None:
        self._conn.close()


def build_tenant_usage_ledger(state_dir: Path) -> TenantUsageLedger | PostgresTenantUsageLedger:
    database_url = os.getenv("GPUCALL_DATABASE_URL") or os.getenv("DATABASE_URL")
    if database_url and database_url.startswith(("postgres://", "postgresql://")):
        return PostgresTenantUsageLedger(database_url)
    return TenantUsageLedger(state_dir / "tenant_usage.db")


def tenant_key_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in _tenant_key_sources():
        for item in raw.split(","):
            if not item.strip() or ":" not in item:
                continue
            tenant_id, key = item.split(":", 1)
            tenant_id = tenant_id.strip()
            key = key.strip()
            if tenant_id and key:
                mapping[key] = tenant_id
    return mapping


def legacy_api_keys() -> list[str]:
    configured = load_credentials().get("auth", {}).get("api_keys", "")
    raw = os.getenv("GPUCALL_API_KEYS") or configured
    return [key.strip() for key in raw.split(",") if key.strip()]


def tenant_for_api_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    mapped = tenant_key_map().get(api_key)
    if mapped:
        return mapped
    if api_key in legacy_api_keys():
        return "default"
    return None


def tenant_identity(tenant_id: str | None, api_key: str | None) -> str:
    if tenant_id:
        return tenant_id
    if api_key:
        return "key-" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    return "anonymous"


def enforce_tenant_budget(
    *,
    tenant_id: str | None,
    tenant: TenantSpec | None,
    ledger: TenantUsageLedger,
    estimated_cost_usd: float,
    tuple: str | None,
    recipe: str | None,
    plan_id: str | None,
) -> None:
    resolved = tenant_identity(tenant_id, None)
    ledger.reserve_with_budget(resolved, estimated_cost_usd, tenant=tenant, tuple=tuple, recipe=recipe, plan_id=plan_id)


def _tenant_key_sources() -> list[str]:
    creds = load_credentials().get("auth", {})
    return [os.getenv("GPUCALL_TENANT_API_KEYS", ""), creds.get("tenant_keys", "")]
