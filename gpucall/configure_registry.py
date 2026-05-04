from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gpucall.plugin_loader import load_entry_point_group


@dataclass(frozen=True)
class ConfigureTarget:
    name: str
    label: str
    run: Callable[[Path], bool]
    success_message: str | None = None


_TARGETS: dict[str, ConfigureTarget] = {}


def register_configure_target(
    name: str,
    *,
    label: str | None = None,
    success_message: str | None = None,
) -> Callable[[Callable[[Path], bool]], Callable[[Path], bool]]:
    def decorator(run: Callable[[Path], bool]) -> Callable[[Path], bool]:
        _TARGETS[name] = ConfigureTarget(
            name=name,
            label=label or name,
            run=run,
            success_message=success_message,
        )
        return run

    return decorator


def configure_targets() -> list[ConfigureTarget]:
    load_entry_point_group("gpucall.configure_targets")
    return list(_TARGETS.values())


def configure_target_labels() -> dict[str, str]:
    load_entry_point_group("gpucall.configure_targets")
    return {name: target.label for name, target in _TARGETS.items()}


def configure_target_names() -> list[str]:
    load_entry_point_group("gpucall.configure_targets")
    return list(_TARGETS)


def configure_target(name: str) -> ConfigureTarget | None:
    load_entry_point_group("gpucall.configure_targets")
    return _TARGETS.get(name)
