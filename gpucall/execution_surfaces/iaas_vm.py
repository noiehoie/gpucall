from __future__ import annotations

from gpucall.execution_surfaces.iaas_clouds import AzureComputeVMAdapter
from gpucall.execution_surfaces.iaas_clouds import GCPConfidentialSpaceVMAdapter
from gpucall.execution_surfaces.iaas_clouds import OVHCloudPublicCloudInstanceAdapter
from gpucall.execution_surfaces.iaas_clouds import ScalewayInstanceAdapter
from gpucall.execution_surfaces.hyperstack_vm import DEFAULT_HYPERSTACK_IMAGE
from gpucall.execution_surfaces.hyperstack_vm import HyperstackAdapter
from gpucall.execution_surfaces.hyperstack_vm import hyperstack_catalog_findings
from gpucall.execution_surfaces.hyperstack_vm import hyperstack_config_findings
from gpucall.execution_surfaces.hyperstack_vm import region_from_hyperstack_environment

__all__ = [
    "AzureComputeVMAdapter",
    "DEFAULT_HYPERSTACK_IMAGE",
    "GCPConfidentialSpaceVMAdapter",
    "HyperstackAdapter",
    "OVHCloudPublicCloudInstanceAdapter",
    "ScalewayInstanceAdapter",
    "hyperstack_catalog_findings",
    "hyperstack_config_findings",
    "region_from_hyperstack_environment",
]
