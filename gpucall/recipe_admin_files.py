from __future__ import annotations

import shutil
import time
from pathlib import Path


def move_submission(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = _unique_destination(destination)
    shutil.move(str(source), str(target))
    return target


def _unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    for attempt in range(1, 10_000):
        candidate = destination.with_name(f"{destination.stem}-{time.time_ns()}-{attempt}{destination.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not find unique destination for {destination}")
