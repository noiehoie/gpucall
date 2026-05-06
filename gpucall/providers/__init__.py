from __future__ import annotations

from typing import Any

from gpucall.providers.base import ProviderAdapter, RemoteHandle, ResourceLease

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
    "RunpodServerlessAdapter",
    "RunpodVllmServerlessAdapter",
    "ScalewayInstanceAdapter",
    "build_adapters",
]


def __getattr__(name: str) -> Any:
    if name == "build_adapters":
        from gpucall.providers.factory import build_adapters

        return build_adapters
    if name == "EchoProvider":
        from gpucall.execution_surfaces.local_runtime import EchoProvider

        return EchoProvider
    if name == "LocalOllamaAdapter":
        from gpucall.execution_surfaces.local_runtime import LocalOllamaAdapter

        return LocalOllamaAdapter
    if name == "ModalAdapter":
        from gpucall.execution_surfaces.function_runtime import ModalAdapter

        return ModalAdapter
    if name == "HyperstackAdapter":
        from gpucall.execution_surfaces.iaas_vm import HyperstackAdapter

        return HyperstackAdapter
    if name in {"RunpodServerlessAdapter", "RunpodVllmServerlessAdapter"}:
        from gpucall.execution_surfaces.managed_endpoint import RunpodServerlessAdapter
        from gpucall.execution_surfaces.managed_endpoint import RunpodVllmServerlessAdapter

        return {"RunpodServerlessAdapter": RunpodServerlessAdapter, "RunpodVllmServerlessAdapter": RunpodVllmServerlessAdapter}[name]
    if name in {
        "AzureComputeVMAdapter",
        "GCPConfidentialSpaceVMAdapter",
        "OVHCloudPublicCloudInstanceAdapter",
        "ScalewayInstanceAdapter",
    }:
        from gpucall.execution_surfaces.iaas_vm import AzureComputeVMAdapter
        from gpucall.execution_surfaces.iaas_vm import GCPConfidentialSpaceVMAdapter
        from gpucall.execution_surfaces.iaas_vm import OVHCloudPublicCloudInstanceAdapter
        from gpucall.execution_surfaces.iaas_vm import ScalewayInstanceAdapter

        return {
            "AzureComputeVMAdapter": AzureComputeVMAdapter,
            "GCPConfidentialSpaceVMAdapter": GCPConfidentialSpaceVMAdapter,
            "OVHCloudPublicCloudInstanceAdapter": OVHCloudPublicCloudInstanceAdapter,
            "ScalewayInstanceAdapter": ScalewayInstanceAdapter,
        }[name]
    raise AttributeError(name)
