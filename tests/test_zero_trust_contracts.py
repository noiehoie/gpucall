from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gpucall.artifacts import SQLiteArtifactRegistry
from gpucall.compiler import GovernanceCompiler
from gpucall.domain import (
    ArtifactManifest,
    DataClassification,
    ExecutionMode,
    Policy,
    ProviderPolicy,
    ProviderSpec,
    ProviderTrustProfile,
    Recipe,
    SecurityTier,
    TaskRequest,
)
from gpucall.registry import ObservedRegistry


def _compiler_for_security(provider: ProviderSpec, recipe: Recipe | None = None) -> GovernanceCompiler:
    policy = Policy(
        version="test",
        inline_bytes_limit=1024,
        default_lease_ttl_seconds=30,
        max_lease_ttl_seconds=60,
        max_timeout_seconds=30,
        providers=ProviderPolicy(allow=[provider.name], deny=[], max_data_classification=DataClassification.RESTRICTED),
    )
    if recipe is None:
        recipe = Recipe(
            name="restricted-infer",
            task="infer",
            data_classification=DataClassification.RESTRICTED,
            allowed_modes=[ExecutionMode.SYNC],
            min_vram_gb=24,
            max_model_len=4096,
            timeout_seconds=10,
            lease_ttl_seconds=20,
            tokenizer_family="qwen",
        )
    return GovernanceCompiler(
        policy=policy,
        recipes={recipe.name: recipe},
        providers={provider.name: provider},
        registry=ObservedRegistry(),
    )


def _provider(name: str, trust_profile: ProviderTrustProfile | None = None) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        adapter="modal",
        max_data_classification=DataClassification.RESTRICTED,
        trust_profile=trust_profile or ProviderTrustProfile(),
        gpu="H100",
        vram_gb=80,
        max_model_len=8192,
        cost_per_second=1,
        modes=[ExecutionMode.SYNC],
        target="app:fn",
        model="test-model",
    )


def test_restricted_workload_rejects_shared_gpu_provider() -> None:
    compiler = _compiler_for_security(_provider("shared"))

    with pytest.raises(ValueError, match="no eligible provider"):
        compiler.compile(TaskRequest(task="infer", mode="sync", recipe="restricted-infer"))


def test_restricted_workload_accepts_attested_confidential_tee_provider() -> None:
    compiler = _compiler_for_security(
        _provider(
            "tee",
            ProviderTrustProfile(
                security_tier=SecurityTier.CONFIDENTIAL_TEE,
                requires_attestation=True,
                supports_key_release=True,
                attestation_type="remote",
            ),
        )
    )

    plan = compiler.compile(TaskRequest(task="infer", mode="sync", recipe="restricted-infer"))

    assert plan.provider_chain == ["tee"]
    assert plan.attestations["compile_artifact"]["governance_hash"] == plan.attestations["governance_hash"]


def test_governance_hash_is_stable_across_plan_ids() -> None:
    provider = _provider("tee", ProviderTrustProfile(security_tier=SecurityTier.CONFIDENTIAL_TEE, requires_attestation=True))
    compiler = _compiler_for_security(provider)
    request = TaskRequest(task="infer", mode="sync", recipe="restricted-infer", max_tokens=8)

    first = compiler.compile(request)
    second = compiler.compile(request)

    assert first.plan_id != second.plan_id
    assert first.attestations["governance_hash"] == second.attestations["governance_hash"]


def test_artifact_registry_persists_append_only_manifests(tmp_path) -> None:
    registry = SQLiteArtifactRegistry(tmp_path / "artifacts.db")
    manifest = ArtifactManifest(
        artifact_id="artifact-1",
        artifact_chain_id="chain-1",
        version="0001",
        classification=DataClassification.RESTRICTED,
        ciphertext_uri="s3://bucket/gpucall/artifacts/artifact-1",
        ciphertext_sha256="a" * 64,
        key_id="tenant-kms-key",
        producer_plan_hash="b" * 64,
        attestation_evidence_ref="attestation-1",
        retention_until=datetime.now(timezone.utc) + timedelta(days=1),
    )

    registry.append(manifest)

    assert registry.get("artifact-1") == manifest
    assert list(registry.list_chain("chain-1")) == [manifest]
