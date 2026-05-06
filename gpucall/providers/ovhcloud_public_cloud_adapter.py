from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

from gpucall.domain import CompiledPlan, ProviderError
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.lifecycle_only import LifecycleOnlyMixin
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter


class OVHCloudPublicCloudInstanceAdapter(LifecycleOnlyMixin, ProviderAdapter):
    def __init__(
        self,
        *,
        name: str = "ovhcloud-public-cloud-instance",
        endpoint: str | None = None,
        service_name: str | None = None,
        region: str | None = None,
        flavor_id: str | None = None,
        image_id: str | None = None,
        ssh_key_id: str | None = None,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.endpoint = endpoint or os.getenv("OVH_ENDPOINT", "ovh-eu")
        self.service_name = service_name or os.getenv("OVH_CLOUD_PROJECT_SERVICE_NAME", "")
        self.region = region
        self.flavor_id = flavor_id
        self.image_id = image_id
        self.ssh_key_id = ssh_key_id
        self.application_key = application_key or os.getenv("OVH_APPLICATION_KEY")
        self.application_secret = application_secret or os.getenv("OVH_APPLICATION_SECRET")
        self.consumer_key = consumer_key or os.getenv("OVH_CONSUMER_KEY")
        self.params = params or {}

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        meta = await asyncio.to_thread(self._start_sync, plan)
        return RemoteHandle(
            provider=self.name,
            remote_id=meta["instance_id"],
            expires_at=plan.expires_at(),
            account_ref="ovhcloud",
            execution_surface="iaas_vm",
            resource_kind="vm",
            cleanup_required=True,
            reaper_eligible=True,
            meta=meta,
        )

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        await asyncio.to_thread(self._delete_sync, handle.remote_id)

    def _client(self) -> Any:
        try:
            import ovh
        except ImportError as exc:
            raise ProviderError("ovh is required for ovhcloud-public-cloud-instance", retryable=False, status_code=501) from exc
        kwargs: dict[str, str] = {"endpoint": self.endpoint}
        if self.application_key:
            kwargs["application_key"] = self.application_key
        if self.application_secret:
            kwargs["application_secret"] = self.application_secret
        if self.consumer_key:
            kwargs["consumer_key"] = self.consumer_key
        return ovh.Client(**kwargs)

    def _start_sync(self, plan: CompiledPlan) -> dict[str, Any]:
        required = {
            "service_name": self.service_name,
            "region": self.region,
            "flavor_id": self.flavor_id,
            "image_id": self.image_id,
            "ssh_key_id": self.ssh_key_id,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ProviderError(f"OVHcloud provider missing required fields: {', '.join(missing)}", retryable=False, status_code=400)
        instance_name = str(self.params.get("instance_name") or f"gpucall-{plan.plan_id[:12]}-{uuid4().hex[:6]}")
        body = {
            "name": instance_name,
            "region": self.region,
            "flavorId": self.flavor_id,
            "imageId": self.image_id,
            "sshKeyId": self.ssh_key_id,
        }
        body.update(self.params.get("create_overrides", {}))
        data = self._client().post(f"/cloud/project/{self.service_name}/instance", **body)
        instance_id = data.get("id") if isinstance(data, dict) else None
        if not instance_id:
            raise ProviderError("OVHcloud response did not include instance id", retryable=True, status_code=502)
        return {"instance_id": instance_id, "instance_name": instance_name, "service_name": self.service_name}

    def _delete_sync(self, instance_id: str) -> None:
        self._client().delete(f"/cloud/project/{self.service_name}/instance/{instance_id}")


@register_adapter(
    "ovhcloud-public-cloud-instance",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="ovhcloud-public-cloud-instance",
        output_contract="gpucall-provider-result",
        production_eligible=False,
        production_rejection_reason="OVHcloud Public Cloud adapter is lifecycle-only until worker bootstrap and result retrieval are configured",
        official_sources=(
            "https://github.com/ovh/python-ovh",
            "https://api.ovh.com/console/#/cloud/project/%7BserviceName%7D/instance#POST",
            "https://api.ovh.com/console/#/cloud/project/%7BserviceName%7D/instance/%7BinstanceId%7D#DELETE",
        ),
    ),
)
def build_ovhcloud_public_cloud_instance_adapter(spec, credentials):
    ovhcloud = credentials.get("ovhcloud", {})
    return OVHCloudPublicCloudInstanceAdapter(
        name=spec.name,
        endpoint=ovhcloud.get("endpoint"),
        service_name=spec.project_id or ovhcloud.get("service_name"),
        region=spec.region,
        flavor_id=spec.instance,
        image_id=spec.image,
        ssh_key_id=spec.key_name,
        application_key=ovhcloud.get("application_key"),
        application_secret=ovhcloud.get("application_secret"),
        consumer_key=ovhcloud.get("consumer_key"),
        params=spec.provider_params,
    )
