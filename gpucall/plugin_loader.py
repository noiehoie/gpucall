from __future__ import annotations

from importlib.metadata import entry_points
from threading import Lock


_LOADED_GROUPS: set[str] = set()
_LOCK = Lock()


def load_entry_point_group(group: str) -> None:
    with _LOCK:
        if group in _LOADED_GROUPS:
            return
        selected = entry_points().select(group=group)
        for entry_point in selected:
            entry_point.load()
        _LOADED_GROUPS.add(group)
