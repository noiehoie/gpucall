from pathlib import Path

_HERE = Path(__file__).resolve()
for _package_path in (
    _HERE.parents[1] / "sdk" / "python" / "gpucall_sdk",
    Path("/app/sdk/python/gpucall_sdk"),
    Path("/opt/gpucall/sdk/python/gpucall_sdk"),
):
    if _package_path.exists():
        __path__.append(str(_package_path))

from gpucall_sdk.client import (
    AsyncGPUCallClient,
    GPUCallCallerRoutingError,
    GPUCallCircuitBreaker,
    GPUCallCircuitOpenError,
    GPUCallCircuitScope,
    GPUCallColdStartTimeout,
    GPUCallClient,
    GPUCallEmptyOutputError,
    GPUCallHTTPError,
    GPUCallJSONParseError,
    GPUCallNoEligibleTupleError,
    GPUCallNoRecipeError,
    GPUCallProviderRuntimeError,
    GPUCallWarning,
)

__all__ = [
    "AsyncGPUCallClient",
    "GPUCallCallerRoutingError",
    "GPUCallCircuitBreaker",
    "GPUCallCircuitOpenError",
    "GPUCallCircuitScope",
    "GPUCallColdStartTimeout",
    "GPUCallClient",
    "GPUCallEmptyOutputError",
    "GPUCallHTTPError",
    "GPUCallJSONParseError",
    "GPUCallNoEligibleTupleError",
    "GPUCallNoRecipeError",
    "GPUCallProviderRuntimeError",
    "GPUCallWarning",
]
