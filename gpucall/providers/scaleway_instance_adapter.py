from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

from gpucall.domain import CompiledPlan, ProviderError
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.lifecycle_only import LifecycleOnlyMixin
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter


class ScalewayInstanceAdapter(LifecycleOnlyMixin, ProviderAdapter):
    def __init__(
        self,
        *,
        name: str = "scaleway-instance",
        secret_key: str | None = None,
        project_id: str | None = None,
        zone: str | None = None,
        commercial_type: str | None = None,
        image: str | None = None,
        base_url: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.secret_key = secret_key or os.getenv("SCW_SECRET_KEY", "")
        self.project_id = project_id or os.getenv("SCW_PROJECT_ID", "")
        self.zone = zone or os.getenv("SCW_DEFAULT_ZONE", "")
        self.commercial_type = commercial_type
        self.image = image
        self.base_url = (base_url or "https://api.scaleway.com").rstrip("/")
        self.params = params or {}

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        meta = await asyncio.to_thread(self._start_sync, plan)
        return RemoteHandle(provider=self.name, remote_id=meta["server_id"], expires_at=plan.expires_at(), meta=meta)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        await asyncio.to_thread(self._delete_sync, handle.remote_id)

    def _session(self) -> Any:
        if not self.secret_key:
            raise ProviderError("Scaleway SCW_SECRET_KEY is not configured", retryable=False, status_code=401)
        try:
            import requests
        except ImportError as exc:
            raise ProviderError("requests is required for scaleway-instance", retryable=False, status_code=501) from exc
        session = requests.Session()
        session.headers.update({"X-Auth-Token": self.secret_key, "Content-Type": "application/json"})
        return session

    def _start_sync(self, plan: CompiledPlan) -> dict[str, Any]:
        required = {"project_id": self.project_id, "zone": self.zone, "commercial_type": self.commercial_type, "image": self.image}
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ProviderError(f"Scaleway provider missing required fields: {', '.join(missing)}", retryable=False, status_code=400)
        name = str(self.params.get("server_name") or f"gpucall-{plan.plan_id[:12]}-{uuid4().hex[:6]}")
        body = {
            "name": name,
            "project": self.project_id,
            "commercial_type": self.commercial_type,
            "image": self.image,
            "enable_ipv6": bool(self.params.get("enable_ipv6", False)),
            "tags": ["gpucall-managed", f"gpucall-plan-{plan.plan_id[:12]}"],
        }
        body.update(self.params.get("create_overrides", {}))
        response = self._session().post(f"{self.base_url}/instance/v1/zones/{self.zone}/servers", json=body, timeout=15)
        if response.status_code not in {200, 201, 202}:
            raise ProviderError(f"Scaleway create instance failed: {response.status_code}", retryable=response.status_code >= 500, status_code=502)
        data = response.json()
        server = data.get("server") if isinstance(data, dict) else None
        server_id = (server or {}).get("id") or data.get("id")
        if not server_id:
            raise ProviderError("Scaleway response did not include server id", retryable=True, status_code=502)
        return {"server_id": server_id, "server_name": name, "zone": self.zone}

    def _delete_sync(self, server_id: str) -> None:
        response = self._session().delete(f"{self.base_url}/instance/v1/zones/{self.zone}/servers/{server_id}", timeout=15)
        if response.status_code not in {200, 202, 204, 404}:
            raise ProviderError(f"Scaleway delete instance failed: {response.status_code}", retryable=response.status_code >= 500, status_code=502)


@register_adapter(
    "scaleway-instance",
    descriptor=ProviderAdapterDescriptor(endpoint_contract="scaleway-instance", output_contract="gpucall-provider-result"),
)
def build_scaleway_instance_adapter(spec, credentials):
    scaleway = credentials.get("scaleway", {})
    return ScalewayInstanceAdapter(
        name=spec.name,
        secret_key=scaleway.get("secret_key"),
        project_id=spec.project_id or scaleway.get("project_id"),
        zone=spec.zone,
        commercial_type=spec.instance,
        image=spec.image,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        params=spec.provider_params,
    )
