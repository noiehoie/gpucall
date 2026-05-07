from __future__ import annotations

from typing import Any

from gpucall.execution.base import TupleAdapter, RemoteHandle, ResourceLease

__all__ = [
    "AzureComputeVMAdapter",
    "EchoTuple",
    "GCPConfidentialSpaceVMAdapter",
    "HyperstackAdapter",
    "LocalOllamaAdapter",
    "ModalAdapter",
    "OVHCloudPublicCloudInstanceAdapter",
    "TupleAdapter",
    "RemoteHandle",
    "ResourceLease",
    "ScalewayInstanceAdapter",
    "build_adapters",
]


def __getattr__(name: str) -> Any:
    if name == "build_adapters":
        from gpucall.execution.factory import build_adapters

        return build_adapters
    if name in {
        "AzureComputeVMAdapter",
        "GCPConfidentialSpaceVMAdapter",
        "HyperstackAdapter",
        "OVHCloudPublicCloudInstanceAdapter",
        "ScalewayInstanceAdapter",
    }:
        from gpucall.execution_surfaces.iaas_vm import AzureComputeVMAdapter
        from gpucall.execution_surfaces.iaas_vm import GCPConfidentialSpaceVMAdapter
        from gpucall.execution_surfaces.iaas_vm import HyperstackAdapter
        from gpucall.execution_surfaces.iaas_vm import OVHCloudPublicCloudInstanceAdapter
        from gpucall.execution_surfaces.iaas_vm import ScalewayInstanceAdapter

        return {
            "AzureComputeVMAdapter": AzureComputeVMAdapter,
            "GCPConfidentialSpaceVMAdapter": GCPConfidentialSpaceVMAdapter,
            "HyperstackAdapter": HyperstackAdapter,
            "OVHCloudPublicCloudInstanceAdapter": OVHCloudPublicCloudInstanceAdapter,
            "ScalewayInstanceAdapter": ScalewayInstanceAdapter,
        }[name]
    if name == "ModalAdapter":
        from gpucall.execution_surfaces.function_runtime import ModalAdapter

        return ModalAdapter
    if name in {"EchoTuple", "LocalOllamaAdapter"}:
        from gpucall.execution_surfaces.local_runtime import EchoTuple, LocalOllamaAdapter

        return {"EchoTuple": EchoTuple, "LocalOllamaAdapter": LocalOllamaAdapter}[name]
    raise AttributeError(name)
