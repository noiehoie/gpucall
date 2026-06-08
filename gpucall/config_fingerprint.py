from __future__ import annotations

import hashlib
from pathlib import Path


ROUTE_VALIDATION_HASH_EXCLUDED_PREFIXES = ("tenants/",)
ROUTE_VALIDATION_HASH_EXCLUDED_PATHS = {"credentials.yml"}


def route_validation_config_hash(config_dir: Path | None) -> str | None:
    if config_dir is None or not config_dir.exists():
        return None
    digest = hashlib.sha256()
    for path in sorted(config_dir.rglob("*.yml")):
        if not path.is_file():
            continue
        relative = path.relative_to(config_dir).as_posix()
        if _route_validation_hash_excluded(relative):
            continue
        try:
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            return None
    return digest.hexdigest()


def _route_validation_hash_excluded(relative_path: str) -> bool:
    return relative_path in ROUTE_VALIDATION_HASH_EXCLUDED_PATHS or any(
        relative_path.startswith(prefix) for prefix in ROUTE_VALIDATION_HASH_EXCLUDED_PREFIXES
    )
