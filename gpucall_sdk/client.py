from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_CANDIDATES = [
    _HERE.parents[1] / "sdk" / "python" / "gpucall_sdk" / "client.py",
    Path("/app/sdk/python/gpucall_sdk/client.py"),
    Path("/opt/gpucall/sdk/python/gpucall_sdk/client.py"),
]
_CANONICAL_CLIENT = next((path for path in _CANDIDATES if path.exists()), _CANDIDATES[0])
_SPEC = importlib.util.spec_from_file_location("_gpucall_sdk_canonical_client", _CANONICAL_CLIENT)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
    raise ImportError(f"cannot load canonical gpucall SDK client from {_CANONICAL_CLIENT}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

__all__ = [name for name in vars(_MODULE) if not name.startswith("__")]

for _name in __all__:
    globals()[_name] = getattr(_MODULE, _name)
