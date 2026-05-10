from __future__ import annotations

from typing import Any


_EMPTY_TARGETS = {"", "none", "null", "nil", "changeme", "change-me", "todo", "tbd"}


def is_configured_target(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if text.lower() in _EMPTY_TARGETS:
        return False
    lowered = text.lower()
    if "placeholder" in lowered:
        return False
    if "xxxxxxxx" in lowered:
        return False
    if text.startswith("<") and text.endswith(">"):
        return False
    return True


def has_configured_endpoint_or_target(endpoint: Any, target: Any) -> bool:
    return endpoint is not None or is_configured_target(target)
