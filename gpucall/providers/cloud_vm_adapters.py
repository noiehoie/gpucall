from __future__ import annotations

from gpucall.providers.azure_compute_vm_adapter import AzureComputeVMAdapter
from gpucall.providers.gcp_confidential_space_adapter import GCPConfidentialSpaceVMAdapter
from gpucall.providers.ovhcloud_public_cloud_adapter import OVHCloudPublicCloudInstanceAdapter
from gpucall.providers.scaleway_instance_adapter import ScalewayInstanceAdapter

__all__ = [
    "AzureComputeVMAdapter",
    "GCPConfidentialSpaceVMAdapter",
    "OVHCloudPublicCloudInstanceAdapter",
    "ScalewayInstanceAdapter",
]
