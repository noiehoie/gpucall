from __future__ import annotations

from dataclasses import dataclass


CLOUD_PROVIDER_FAMILIES = ("modal", "runpod", "hyperstack")


@dataclass(frozen=True)
class ProviderSetupContract:
    family: str
    display_name: str
    setup_label: str
    credential_probe_sets: tuple[frozenset[str], ...]
    gpucall_credentials_required: frozenset[str]
    official_cli: str | None = None
    endpoint_id_supported: bool = False
    endpoint_pending_warning: str | None = None
    prompt_requires_ssh_key: bool = False

    def configured_by(self, configured: set[str]) -> bool:
        return any(probes.issubset(configured) for probes in self.credential_probe_sets)


PROVIDER_SETUP_CONTRACTS: dict[str, ProviderSetupContract] = {
    "modal": ProviderSetupContract(
        family="modal",
        display_name="Modal",
        setup_label="Modal serverless GPU",
        credential_probe_sets=(frozenset({"token_pair:modal"}), frozenset({"sdk_profile:modal"})),
        gpucall_credentials_required=frozenset({"token_pair:modal"}),
        official_cli="modal",
    ),
    "runpod": ProviderSetupContract(
        family="runpod",
        display_name="RunPod",
        setup_label="RunPod managed endpoint",
        credential_probe_sets=(frozenset({"api_key:runpod"}),),
        gpucall_credentials_required=frozenset({"api_key:runpod"}),
        official_cli="flash",
        endpoint_id_supported=True,
        endpoint_pending_warning="runpod endpoint_id omitted; provider account will be connected, endpoint provisioning remains pending",
    ),
    "hyperstack": ProviderSetupContract(
        family="hyperstack",
        display_name="Hyperstack",
        setup_label="Hyperstack VM",
        credential_probe_sets=(frozenset({"api_key:hyperstack", "ssh_key:hyperstack"}),),
        gpucall_credentials_required=frozenset({"api_key:hyperstack", "ssh_key:hyperstack"}),
        prompt_requires_ssh_key=True,
    ),
}
