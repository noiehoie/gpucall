from __future__ import annotations

import ipaddress
from typing import Any


_EMPTY_TARGETS = {"", "none", "null", "nil", "changeme", "change-me", "todo", "tbd"}
_DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
    )
)


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


def is_configured_cidr(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if text.lower() in _EMPTY_TARGETS:
        return False
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return False
    if network.prefixlen == 0:
        return False
    if any(network.subnet_of(doc) for doc in _DOCUMENTATION_NETWORKS):
        return False
    return True
