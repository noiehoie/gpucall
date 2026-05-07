from __future__ import annotations

from gpucall.domain import CompiledPlan, ProviderError
from gpucall.execution.base import RemoteHandle


class LifecycleOnlyMixin:
    async def wait(self, handle: RemoteHandle, plan: CompiledPlan):
        raise ProviderError(
            f"{handle.provider} lifecycle adapter started an official cloud resource, but gpucall worker result retrieval is not configured",
            retryable=False,
            status_code=501,
            code="PROVIDER_WORKER_BOOTSTRAP_NOT_CONFIGURED",
        )

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        raise ProviderError(
            f"{handle.provider} lifecycle adapter does not provide a token stream without a configured gpucall worker endpoint",
            retryable=False,
            status_code=501,
            code="PROVIDER_WORKER_BOOTSTRAP_NOT_CONFIGURED",
        )
        yield ""  # pragma: no cover
