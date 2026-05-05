from __future__ import annotations

from typing import Any

from gpucall.providers.base import ProviderAdapter, RemoteHandle

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
    "RunpodFlashAdapter",
    "RunpodServerlessAdapter",
    "ScalewayInstanceAdapter",
    "build_adapters",
]


def __getattr__(name: str) -> Any:
    if name == "build_adapters":
        from gpucall.providers.factory import build_adapters

        return build_adapters
    if name == "EchoProvider":
        from gpucall.providers.echo import EchoProvider

        return EchoProvider
    if name == "LocalOllamaAdapter":
        from gpucall.providers.local_adapter import LocalOllamaAdapter

        return LocalOllamaAdapter
    if name == "ModalAdapter":
        from gpucall.providers.modal_adapter import ModalAdapter

        return ModalAdapter
    if name == "HyperstackAdapter":
        from gpucall.providers.hyperstack_adapter import HyperstackAdapter

        return HyperstackAdapter
    if name in {"RunpodFlashAdapter", "RunpodServerlessAdapter"}:
        from gpucall.providers.runpod_flash_adapter import RunpodFlashAdapter
        from gpucall.providers.runpod_serverless_adapter import RunpodServerlessAdapter

        return {"RunpodFlashAdapter": RunpodFlashAdapter, "RunpodServerlessAdapter": RunpodServerlessAdapter}[name]
    if name in {
        "AzureComputeVMAdapter",
        "GCPConfidentialSpaceVMAdapter",
        "OVHCloudPublicCloudInstanceAdapter",
        "ScalewayInstanceAdapter",
    }:
        from gpucall.providers.azure_compute_vm_adapter import AzureComputeVMAdapter
        from gpucall.providers.gcp_confidential_space_adapter import GCPConfidentialSpaceVMAdapter
        from gpucall.providers.ovhcloud_public_cloud_adapter import OVHCloudPublicCloudInstanceAdapter
        from gpucall.providers.scaleway_instance_adapter import ScalewayInstanceAdapter

        return {
            "AzureComputeVMAdapter": AzureComputeVMAdapter,
            "GCPConfidentialSpaceVMAdapter": GCPConfidentialSpaceVMAdapter,
            "OVHCloudPublicCloudInstanceAdapter": OVHCloudPublicCloudInstanceAdapter,
            "ScalewayInstanceAdapter": ScalewayInstanceAdapter,
        }[name]
    raise AttributeError(name)
