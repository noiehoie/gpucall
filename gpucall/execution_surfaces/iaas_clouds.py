from __future__ import annotations


import asyncio
import base64
import os
from typing import Any
from uuid import uuid4

from gpucall.domain import CompiledPlan, TupleError
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.lifecycle import LifecycleOnlyMixin
from gpucall.execution.payloads import plan_payload
from gpucall.execution.registry import TupleAdapterDescriptor, register_adapter


class AzureComputeVMAdapter(LifecycleOnlyMixin, TupleAdapter):
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
            tuple=self.name,
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
            raise TupleError("Azure subscription_id is not configured", retryable=False, status_code=401)
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.compute import ComputeManagementClient
        except ImportError as exc:
            raise TupleError(
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
            raise TupleError(f"Azure tuple missing required fields: {', '.join(missing)}", retryable=False, status_code=400)
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
            raise TupleError("Azure tuple requires image_reference", retryable=False, status_code=400)
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
            raise TupleError("Azure resource_group is not configured", retryable=False, status_code=400)
        poller = self._client().virtual_machines.begin_delete(self.resource_group, vm_name)
        if self.params.get("wait_for_delete", False):
            poller.result()


@register_adapter(
    "azure-compute-vm",
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="azure-compute-vm",
        output_contract="gpucall-tuple-result",
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


import asyncio
import os
from typing import Any
from uuid import uuid4

from gpucall.domain import CompiledPlan, TupleError
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.lifecycle import LifecycleOnlyMixin
from gpucall.execution.registry import TupleAdapterDescriptor, register_adapter


class GCPConfidentialSpaceVMAdapter(LifecycleOnlyMixin, TupleAdapter):
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
            tuple=self.name,
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
            raise TupleError("google-cloud-compute is required for gcp-confidential-space-vm", retryable=False, status_code=501) from exc
        return compute_v1.InstancesClient()

    def _start_sync(self, plan: CompiledPlan) -> dict[str, Any]:
        required = {"project_id": self.project_id, "zone": self.zone, "machine_type": self.machine_type, "source_image": self.source_image}
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise TupleError(f"GCP tuple missing required fields: {', '.join(missing)}", retryable=False, status_code=400)
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
            raise TupleError("GCP project_id and zone are required for delete", retryable=False, status_code=400)
        operation = self._client().delete(project=self.project_id, zone=self.zone, instance=instance_name)
        if self.params.get("wait_for_delete", False) and hasattr(operation, "result"):
            operation.result()


@register_adapter(
    "gcp-confidential-space-vm",
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="gcp-confidential-space-vm",
        output_contract="gpucall-tuple-result",
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


from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.lifecycle import LifecycleOnlyMixin
from gpucall.execution.registry import TupleAdapterDescriptor, register_adapter


class OVHCloudPublicCloudInstanceAdapter(LifecycleOnlyMixin, TupleAdapter):
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
            tuple=self.name,
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
            raise TupleError("ovh is required for ovhcloud-public-cloud-instance", retryable=False, status_code=501) from exc
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
            raise TupleError(f"OVHcloud tuple missing required fields: {', '.join(missing)}", retryable=False, status_code=400)
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
            raise TupleError("OVHcloud response did not include instance id", retryable=True, status_code=502)
        return {"instance_id": instance_id, "instance_name": instance_name, "service_name": self.service_name}

    def _delete_sync(self, instance_id: str) -> None:
        self._client().delete(f"/cloud/project/{self.service_name}/instance/{instance_id}")


@register_adapter(
    "ovhcloud-public-cloud-instance",
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="ovhcloud-public-cloud-instance",
        output_contract="gpucall-tuple-result",
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


import asyncio
import os
from typing import Any
from uuid import uuid4

from gpucall.domain import CompiledPlan, TupleError
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.lifecycle import LifecycleOnlyMixin
from gpucall.execution.registry import TupleAdapterDescriptor, register_adapter


class ScalewayInstanceAdapter(LifecycleOnlyMixin, TupleAdapter):
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
        return RemoteHandle(
            tuple=self.name,
            remote_id=meta["server_id"],
            expires_at=plan.expires_at(),
            account_ref="scaleway",
            execution_surface="iaas_vm",
            resource_kind="vm",
            cleanup_required=True,
            reaper_eligible=True,
            meta=meta,
        )

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        await asyncio.to_thread(self._delete_sync, handle.remote_id)

    def _session(self) -> Any:
        if not self.secret_key:
            raise TupleError("Scaleway SCW_SECRET_KEY is not configured", retryable=False, status_code=401)
        try:
            import requests
        except ImportError as exc:
            raise TupleError("requests is required for scaleway-instance", retryable=False, status_code=501) from exc
        session = requests.Session()
        session.headers.update({"X-Auth-Token": self.secret_key, "Content-Type": "application/json"})
        return session

    def _start_sync(self, plan: CompiledPlan) -> dict[str, Any]:
        required = {"project_id": self.project_id, "zone": self.zone, "commercial_type": self.commercial_type, "image": self.image}
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise TupleError(f"Scaleway tuple missing required fields: {', '.join(missing)}", retryable=False, status_code=400)
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
            raise TupleError(f"Scaleway create instance failed: {response.status_code}", retryable=response.status_code >= 500, status_code=502)
        data = response.json()
        server = data.get("server") if isinstance(data, dict) else None
        server_id = (server or {}).get("id") or data.get("id")
        if not server_id:
            raise TupleError("Scaleway response did not include server id", retryable=True, status_code=502)
        return {"server_id": server_id, "server_name": name, "zone": self.zone}

    def _delete_sync(self, server_id: str) -> None:
        response = self._session().delete(f"{self.base_url}/instance/v1/zones/{self.zone}/servers/{server_id}", timeout=15)
        if response.status_code not in {200, 202, 204, 404}:
            raise TupleError(f"Scaleway delete instance failed: {response.status_code}", retryable=response.status_code >= 500, status_code=502)


@register_adapter(
    "scaleway-instance",
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="scaleway-instance",
        output_contract="gpucall-tuple-result",
        production_eligible=False,
        production_rejection_reason="Scaleway Instance adapter is lifecycle-only until worker bootstrap and result retrieval are configured",
        official_sources=(
            "https://www.scaleway.com/en/developer-api/",
            "https://www.scaleway.com/en/developers/api/instances/",
        ),
    ),
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
