from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from statistics import median
from time import time

from gpucall.domain import TupleObservation, TupleScore


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    successes_to_close: int = 1
    recovery_timeout_seconds: float = 60.0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    open: bool = False
    opened_at: float | None = None

    def allow_request(self) -> bool:
        if not self.open:
            return True
        if self.opened_at is None:
            return False
        return time() - self.opened_at >= self.recovery_timeout_seconds

    def record(self, success: bool) -> None:
        if success:
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            if self.open and self.consecutive_successes >= self.successes_to_close:
                self.open = False
                self.opened_at = None
            return
        self.consecutive_successes = 0
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.open = True
            self.opened_at = time()


@dataclass
class ObservedRegistry:
    observations: dict[str, list[TupleObservation]] = field(default_factory=lambda: defaultdict(list))
    breakers: dict[str, CircuitBreaker] = field(default_factory=lambda: defaultdict(CircuitBreaker))
    path: Path | None = None
    max_observations_per_tuple: int = 1000

    def __post_init__(self) -> None:
        if self.path is None:
            return
        if self.path.suffix == ".db":
            self._init_db()
            self._migrate_jsonl_if_present()
            self._load_sqlite()
            return
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                observation = TupleObservation.model_validate_json(line)
                self.observations[observation.tuple].append(observation)
                self._trim_observations(observation.tuple)

    def record(self, observation: TupleObservation) -> None:
        self.observations[observation.tuple].append(observation)
        self._trim_observations(observation.tuple)
        self.breakers[observation.tuple].record(observation.success)
        if self.path is not None:
            if self.path.suffix == ".db":
                self._record_sqlite(observation)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(observation.model_dump_json())
                handle.write("\n")

    def is_available(self, tuple: str) -> bool:
        return self.breakers[tuple].allow_request()

    def score(self, tuple: str) -> TupleScore:
        rows = self.observations.get(tuple, [])
        if not rows:
            return TupleScore()
        successes = [row for row in rows if row.success]
        success_rate = len(successes) / len(rows)
        p50 = median([row.latency_ms for row in rows])
        cost_per_success = sum(row.cost for row in rows) / max(len(successes), 1)
        return TupleScore(
            success_rate=success_rate,
            p50_latency_ms=float(p50),
            cost_per_success=float(cost_per_success),
            samples=len(rows),
        )

    def rank(self, tuples: list[str]) -> list[str]:
        available = [item for item in tuples if self.is_available(item)]
        input_order = {item: index for index, item in enumerate(tuples)}

        def key(item: str) -> tuple[float, float, float, int]:
            score = self.score(item)
            latency = score.p50_latency_ms if score.p50_latency_ms is not None else 0.0
            cost = score.cost_per_success if score.cost_per_success is not None else 0.0
            return (-score.success_rate, latency, cost, input_order[item])

        return sorted(available, key=key)

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {
            item: self.score(item).model_dump(mode="json")
            for item in sorted(self.observations)
        }

    def _connect(self) -> sqlite3.Connection:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS observations (
                  provider TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  observed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS circuit_breakers (
                  provider TEXT PRIMARY KEY,
                  consecutive_failures INTEGER NOT NULL,
                  consecutive_successes INTEGER NOT NULL,
                  open INTEGER NOT NULL,
                  opened_at REAL
                )
                """
            )

    def _load_sqlite(self) -> None:
        with self._connect() as conn:
            for (payload,) in conn.execute(
                """
                SELECT payload
                FROM (
                  SELECT provider, payload, observed_at,
                         ROW_NUMBER() OVER (PARTITION BY provider ORDER BY observed_at DESC) AS rn
                  FROM observations
                )
                WHERE rn <= ?
                ORDER BY observed_at
                """,
                (self.max_observations_per_tuple,),
            ):
                observation = TupleObservation.model_validate_json(payload)
                self.observations[observation.tuple].append(observation)
                self._trim_observations(observation.tuple)
            for row in conn.execute(
                "SELECT provider, consecutive_failures, consecutive_successes, open, opened_at FROM circuit_breakers"
            ):
                tuple_name, failures, successes, opened, opened_at = row
                breaker = self.breakers[tuple_name]
                breaker.consecutive_failures = int(failures)
                breaker.consecutive_successes = int(successes)
                breaker.open = bool(opened)
                breaker.opened_at = float(opened_at) if opened_at is not None else None

    def _record_sqlite(self, observation: TupleObservation) -> None:
        breaker = self.breakers[observation.tuple]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO observations(provider, payload, observed_at) VALUES (?, ?, ?)",
                (observation.tuple, observation.model_dump_json(), observation.observed_at.isoformat()),
            )
            conn.execute(
                """
                DELETE FROM observations
                WHERE provider = ?
                  AND rowid NOT IN (
                    SELECT rowid
                    FROM observations
                    WHERE provider = ?
                    ORDER BY observed_at DESC
                    LIMIT ?
                  )
                """,
                (observation.tuple, observation.tuple, self.max_observations_per_tuple),
            )
            conn.execute(
                """
                INSERT INTO circuit_breakers(provider, consecutive_failures, consecutive_successes, open, opened_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                  consecutive_failures=excluded.consecutive_failures,
                  consecutive_successes=excluded.consecutive_successes,
                  open=excluded.open,
                  opened_at=excluded.opened_at
                """,
                (
                    observation.tuple,
                    breaker.consecutive_failures,
                    breaker.consecutive_successes,
                    int(breaker.open),
                    breaker.opened_at,
                ),
            )

    def _migrate_jsonl_if_present(self) -> None:
        assert self.path is not None
        legacy = self.path.with_suffix(".jsonl")
        if not legacy.exists():
            return
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            if count:
                return
            for line in legacy.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                observation = TupleObservation.model_validate_json(line)
                conn.execute(
                    "INSERT INTO observations(provider, payload, observed_at) VALUES (?, ?, ?)",
                    (observation.tuple, observation.model_dump_json(), observation.observed_at.isoformat()),
                )

    def _trim_observations(self, tuple: str) -> None:
        rows = self.observations[tuple]
        overflow = len(rows) - self.max_observations_per_tuple
        if overflow > 0:
            del rows[:overflow]
