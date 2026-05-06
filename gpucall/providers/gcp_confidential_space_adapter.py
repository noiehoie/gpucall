from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

from gpucall.domain import CompiledPlan, ProviderError
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.lifecycle_only import LifecycleOnlyMixin
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter


class GCPConfidentialSpaceVMAdapter(LifecycleOnlyMixin, ProviderAdapter):
    def __init__(
        self,
        *,
        name: str = "gcp-confidential-space-vm",
        project_id: str | None = None,
        zone: str | None = None,
        machine_type: str | None = None,
        source_image: str | None = None,
        network: str | None = None,
        subnetwork: str | None = None,
        service_account: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        self.zone = zone
        self.machine_type = machine_type
        self.source_image = source_image
        self.network = network
        self.subnetwork = subnetwork
        self.service_account = service_account
        self.params = params or {}

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        meta = await asyncio.to_thread(self._start_sync, plan)
        return RemoteHandle(
            provider=self.name,
            remote_id=meta["instance_name"],
            expires_at=plan.expires_at(),
            account_ref="gcp",
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
            from google.cloud import compute_v1
        except ImportError as exc:
            raise ProviderError("google-cloud-compute is required for gcp-confidential-space-vm", retryable=False, status_code=501) from exc
        return compute_v1.InstancesClient()

    def _start_sync(self, plan: CompiledPlan) -> dict[str, Any]:
        required = {"project_id": self.project_id, "zone": self.zone, "machine_type": self.machine_type, "source_image": self.source_image}
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ProviderError(f"GCP provider missing required fields: {', '.join(missing)}", retryable=False, status_code=400)
        instance_name = str(self.params.get("instance_name") or f"gpucall-{plan.plan_id[:12]}-{uuid4().hex[:6]}")
        operation = self._client().insert(project=self.project_id, zone=self.zone, instance_resource=self._instance_resource(plan, instance_name))
        if self.params.get("wait_for_insert", False) and hasattr(operation, "result"):
            operation.result()
        return {"instance_name": instance_name, "project_id": self.project_id, "zone": self.zone}

    def _instance_resource(self, plan: CompiledPlan, instance_name: str) -> dict[str, Any]:
        network_interface: dict[str, Any] = {}
        if self.network:
            network_interface["network"] = self.network
        if self.subnetwork:
            network_interface["subnetwork"] = self.subnetwork
        metadata_items = [{"key": "gpucall-plan-id", "value": plan.plan_id}]
        metadata_items.extend(self.params.get("metadata_items", []))
        resource: dict[str, Any] = {
            "name": instance_name,
            "machine_type": self.machine_type,
            "disks": [
                {
                    "boot": True,
                    "auto_delete": True,
                    "initialize_params": {"source_image": self.source_image, "disk_size_gb": int(self.params.get("boot_disk_size_gb", 50))},
                }
            ],
            "network_interfaces": [network_interface],
            "confidential_instance_config": {
                "enable_confidential_compute": True,
                "confidential_instance_type": self.params.get("confidential_instance_type", "SEV"),
            },
            "shielded_instance_config": {"enable_secure_boot": True, "enable_vtpm": True, "enable_integrity_monitoring": True},
            "labels": {"gpucall-managed": "true"},
            "metadata": {"items": metadata_items},
        }
        if self.service_account:
            resource["service_accounts"] = [{"email": self.service_account, "scopes": self.params.get("scopes", ["https://www.googleapis.com/auth/cloud-platform"])}]
        return resource

    def _delete_sync(self, instance_name: str) -> None:
        if not self.project_id or not self.zone:
            raise ProviderError("GCP project_id and zone are required for delete", retryable=False, status_code=400)
        operation = self._client().delete(project=self.project_id, zone=self.zone, instance=instance_name)
        if self.params.get("wait_for_delete", False) and hasattr(operation, "result"):
            operation.result()


@register_adapter(
    "gcp-confidential-space-vm",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="gcp-confidential-space-vm",
        output_contract="gpucall-provider-result",
        production_eligible=False,
        production_rejection_reason="GCP Confidential Space VM adapter is lifecycle-only until worker bootstrap and result retrieval are configured",
        official_sources=(
            "https://cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient",
            "https://cloud.google.com/compute/docs/reference/rest/v1/instances/insert",
        ),
    ),
)
def build_gcp_confidential_space_vm_adapter(spec, credentials):
    gcp = credentials.get("gcp", {})
    return GCPConfidentialSpaceVMAdapter(
        name=spec.name,
        project_id=spec.project_id or gcp.get("project_id"),
        zone=spec.zone,
        machine_type=spec.instance,
        source_image=spec.image,
        network=spec.network,
        subnetwork=spec.subnet,
        service_account=spec.service_account,
        params=spec.provider_params,
    )
