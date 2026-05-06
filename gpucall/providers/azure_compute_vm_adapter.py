from __future__ import annotations

import asyncio
import base64
import os
from typing import Any
from uuid import uuid4

from gpucall.domain import CompiledPlan, ProviderError
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.lifecycle_only import LifecycleOnlyMixin
from gpucall.providers.payloads import plan_payload
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter


class AzureComputeVMAdapter(LifecycleOnlyMixin, ProviderAdapter):
    def __init__(
        self,
        *,
        name: str = "azure-compute-vm",
        subscription_id: str | None = None,
        resource_group: str | None = None,
        location: str | None = None,
        vm_size: str | None = None,
        image_reference: dict[str, Any] | None = None,
        network_interface_id: str | None = None,
        admin_username: str | None = None,
        ssh_public_key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.subscription_id = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", "")
        self.resource_group = resource_group
        self.location = location
        self.vm_size = vm_size
        self.image_reference = image_reference or {}
        self.network_interface_id = network_interface_id
        self.admin_username = admin_username
        self.ssh_public_key = ssh_public_key
        self.params = params or {}

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        meta = await asyncio.to_thread(self._start_sync, plan)
        return RemoteHandle(
            provider=self.name,
            remote_id=meta["vm_name"],
            expires_at=plan.expires_at(),
            account_ref="azure",
            execution_surface="iaas_vm",
            resource_kind="vm",
            cleanup_required=True,
            reaper_eligible=True,
            meta=meta,
        )

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        await asyncio.to_thread(self._delete_sync, handle.remote_id)

    def _client(self) -> Any:
        if not self.subscription_id:
            raise ProviderError("Azure subscription_id is not configured", retryable=False, status_code=401)
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.compute import ComputeManagementClient
        except ImportError as exc:
            raise ProviderError(
                "azure-identity and azure-mgmt-compute are required for azure-compute-vm",
                retryable=False,
                status_code=501,
            ) from exc
        return ComputeManagementClient(DefaultAzureCredential(), self.subscription_id)

    def _start_sync(self, plan: CompiledPlan) -> dict[str, Any]:
        required = {
            "resource_group": self.resource_group,
            "location": self.location,
            "vm_size": self.vm_size,
            "network_interface_id": self.network_interface_id,
            "admin_username": self.admin_username,
            "ssh_public_key": self.ssh_public_key,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ProviderError(f"Azure provider missing required fields: {', '.join(missing)}", retryable=False, status_code=400)
        vm_name = str(self.params.get("vm_name") or f"gpucall-{plan.plan_id[:12]}-{uuid4().hex[:6]}")
        poller = self._client().virtual_machines.begin_create_or_update(self.resource_group, vm_name, self._vm_parameters(plan, vm_name))
        if self.params.get("wait_for_create", True):
            poller.result()
        return {"vm_name": vm_name, "resource_group": self.resource_group}

    def _vm_parameters(self, plan: CompiledPlan, vm_name: str) -> dict[str, Any]:
        custom_data = self.params.get("custom_data_b64")
        if not custom_data and self.params.get("embed_gpucall_payload") is True:
            custom_data = base64.b64encode(str(plan_payload(plan)).encode("utf-8")).decode("ascii")
        image_reference = self.image_reference or self.params.get("image_reference")
        if not image_reference:
            raise ProviderError("Azure provider requires image_reference", retryable=False, status_code=400)
        return {
            "location": self.location,
            "hardware_profile": {"vm_size": self.vm_size},
            "storage_profile": {"image_reference": image_reference},
            "os_profile": {
                "computer_name": vm_name,
                "admin_username": self.admin_username,
                "custom_data": custom_data,
                "linux_configuration": {
                    "disable_password_authentication": True,
                    "ssh": {"public_keys": [{"path": f"/home/{self.admin_username}/.ssh/authorized_keys", "key_data": self.ssh_public_key}]},
                },
            },
            "network_profile": {"network_interfaces": [{"id": self.network_interface_id, "primary": True}]},
            "security_profile": {
                "security_type": self.params.get("security_type", "ConfidentialVM"),
                "uefi_settings": {"secure_boot_enabled": True, "v_tpm_enabled": True},
            },
            "tags": {"gpucall-managed": "true", "gpucall-plan-id": plan.plan_id[:32]},
        }

    def _delete_sync(self, vm_name: str) -> None:
        if not self.resource_group:
            raise ProviderError("Azure resource_group is not configured", retryable=False, status_code=400)
        poller = self._client().virtual_machines.begin_delete(self.resource_group, vm_name)
        if self.params.get("wait_for_delete", False):
            poller.result()


@register_adapter(
    "azure-compute-vm",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="azure-compute-vm",
        output_contract="gpucall-provider-result",
        production_eligible=False,
        production_rejection_reason="Azure VM adapter is lifecycle-only until worker bootstrap and result retrieval are configured",
        official_sources=(
            "https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/azure.mgmt.compute.operations.virtualmachinesoperations",
            "https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential",
        ),
    ),
)
def build_azure_compute_vm_adapter(spec, credentials):
    azure = credentials.get("azure", {})
    image_reference = spec.provider_params.get("image_reference")
    return AzureComputeVMAdapter(
        name=spec.name,
        subscription_id=azure.get("subscription_id"),
        resource_group=spec.resource_group,
        location=spec.region,
        vm_size=spec.instance,
        image_reference=image_reference if isinstance(image_reference, dict) else None,
        network_interface_id=spec.network,
        admin_username=spec.provider_params.get("admin_username"),
        ssh_public_key=spec.provider_params.get("ssh_public_key"),
        params=spec.provider_params,
    )
