from __future__ import annotations

from gpucall.execution.base import ProviderAdapter, RemoteHandle, ResourceLease
from gpucall.execution.factory import build_adapters
from gpucall.execution_surfaces.iaas_vm import AzureComputeVMAdapter
from gpucall.execution_surfaces.iaas_vm import GCPConfidentialSpaceVMAdapter
from gpucall.execution_surfaces.iaas_vm import HyperstackAdapter
from gpucall.execution_surfaces.iaas_vm import OVHCloudPublicCloudInstanceAdapter
from gpucall.execution_surfaces.iaas_vm import ScalewayInstanceAdapter
from gpucall.execution_surfaces.function_runtime import ModalAdapter
from gpucall.execution_surfaces.local_runtime import EchoProvider, LocalOllamaAdapter

__all__ = [
    "AzureComputeVMAdapter",
    "EchoProvider",
    "GCPConfidentialSpaceVMAdapter",
    "HyperstackAdapter",
    "LocalOllamaAdapter",
    "ModalAdapter",
    "OVHCloudPublicCloudInstanceAdapter",
    "ProviderAdapter",
    "RemoteHandle",
    "ResourceLease",
    "ScalewayInstanceAdapter",
    "build_adapters",
]
