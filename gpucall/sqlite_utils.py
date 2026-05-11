from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def sqlite_timeout_seconds() -> float:
    raw = os.getenv("GPUCALL_SQLITE_TIMEOUT_SECONDS", "30")
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return 30.0


def connect_sqlite(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    timeout = sqlite_timeout_seconds()
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn
