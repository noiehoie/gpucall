# Execution Surface Conformance

gpucall execution-surface adapters are isolated per cloud API surface. An
adapter may own lifecycle operations only when it can call the vendor's official
API or SDK. It must not report production inference success unless a gpucall
worker bootstrap and result retrieval path is configured and verified.

Each execution surface module registers its own builder and descriptor.
Router core code consumes descriptors for endpoint/output contract validation,
production route eligibility, local execution tagging, stream preconditions, and
optional live catalog checks. Vendor-specific names must not be hardcoded in
`config.py`, `routing.py`, `tuple_catalog.py`, `compiler.py`, `dispatcher.py`,
or `tuples/factory.py`.

## Implemented Lifecycle Adapters

- `azure-compute-vm`: uses Azure SDK for Python `ComputeManagementClient.virtual_machines.begin_create_or_update` and `begin_delete`. Confidential VM intent is expressed through the VM `security_profile`.
- `gcp-confidential-space-vm`: uses Google Cloud `compute_v1.InstancesClient.insert` and `delete`. Confidential execution intent is expressed through `confidential_instance_config` and a Confidential Space image reference.
- `scaleway-instance`: uses Scaleway Instance REST API `POST /instance/v1/zones/{zone}/servers` and `DELETE /instance/v1/zones/{zone}/servers/{server_id}`.
- `ovhcloud-public-cloud-instance`: uses the official `ovh` Python wrapper against `POST /cloud/project/{serviceName}/instance` and `DELETE /cloud/project/{serviceName}/instance/{instanceId}`.

## Non-Negotiable Behavior

- Lifecycle-only adapters raise `PROVIDER_WORKER_BOOTSTRAP_NOT_CONFIGURED` with HTTP `501` from `wait` and `stream`.
- Lifecycle-only adapters are not production-eligible for deterministic routing until worker bootstrap, result retrieval, and billable live validation are implemented.
- Credentials are loaded from environment variables or `credentials.yml`; secrets do not belong in tuple YAML.
- Tuple YAML examples declare resource shape and routing metadata only.

## Official References

- Azure SDK VM example: https://learn.microsoft.com/en-us/azure/developer/python/sdk/examples/azure-sdk-example-virtual-machines
- Azure `SecurityProfile`: https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/azure.mgmt.compute.models.securityprofile
- Google Compute `InstancesClient`: https://cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient
- Google Confidential Space overview: https://cloud.google.com/confidential-computing/confidential-space/docs/confidential-space-overview
- Scaleway Instance API: https://www.scaleway.com/en/developers/api/instances/
- OVHcloud API first steps: https://support.us.ovhcloud.com/hc/en-us/articles/360018130839-First-Steps-with-the-OVHcloud-API
- OVH official Python wrapper: https://github.com/ovh/python-ovh
