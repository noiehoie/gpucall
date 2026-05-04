from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gpucall.artifacts import SQLiteArtifactRegistry
from gpucall.attestation import AttestationVerifier, KeyReleaseBroker
from gpucall.compiler import GovernanceCompiler
from gpucall.domain import (
    ArtifactManifest,
    AttestationEvidence,
    DataClassification,
    DataRef,
    ExecutionMode,
    KeyReleaseRequirement,
    Policy,
    ProviderPolicy,
    ProviderSpec,
    ProviderTrustProfile,
    Recipe,
    SecurityTier,
    TaskRequest,
)
from gpucall.providers.payloads import plan_payload
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


def test_artifact_registry_latest_pointer_is_cas_only(tmp_path) -> None:
    registry = SQLiteArtifactRegistry(tmp_path / "artifacts.db")

    assert registry.compare_and_set_latest("chain-1", expected_version=None, new_version="0001") is True
    assert registry.latest_version("chain-1") == "0001"
    assert registry.compare_and_set_latest("chain-1", expected_version=None, new_version="0002") is False
    assert registry.compare_and_set_latest("chain-1", expected_version="0001", new_version="0002") is True


def test_train_plan_requires_explicit_artifact_export_and_key_release() -> None:
    provider = _provider(
        "tee",
        ProviderTrustProfile(
            security_tier=SecurityTier.CONFIDENTIAL_TEE,
            requires_attestation=True,
            supports_key_release=True,
        ),
    ).model_copy(update={"input_contracts": ["data_refs", "artifact_refs"], "output_contract": "artifact-manifest"})
    recipe = Recipe(
        name="lora-train",
        task="fine-tune",
        data_classification=DataClassification.RESTRICTED,
        allowed_modes=[ExecutionMode.SYNC],
        min_vram_gb=24,
        max_model_len=4096,
        timeout_seconds=10,
        lease_ttl_seconds=20,
        tokenizer_family="qwen",
        artifact_export=True,
        requires_key_release=True,
    )
    compiler = _compiler_for_security(provider, recipe)

    plan = compiler.compile(
        TaskRequest(
            task="fine-tune",
            mode="sync",
            recipe="lora-train",
            input_refs=[DataRef(uri="s3://bucket/train.jsonl", sha256="a" * 64, bytes=100, content_type="application/jsonl")],
            artifact_export={
                "artifact_chain_id": "chain-1",
                "version": "0001",
                "key_id": "tenant-key",
            },
        )
    )

    assert plan.artifact_export is not None
    assert plan.attestations["key_release_requirement"]["gateway_may_generate_dek"] is False
    assert plan_payload(plan)["artifact_export"]["artifact_chain_id"] == "chain-1"


def test_split_learning_plan_uses_activation_ref_without_inline_payload() -> None:
    provider = _provider("split", ProviderTrustProfile(security_tier=SecurityTier.SPLIT_LEARNING)).model_copy(
        update={"input_contracts": ["activation_refs"], "output_contract": "gpucall-provider-result"}
    )
    recipe = Recipe(
        name="split-infer",
        task="split-infer",
        data_classification=DataClassification.RESTRICTED,
        allowed_modes=[ExecutionMode.SYNC],
        min_vram_gb=24,
        max_model_len=4096,
        timeout_seconds=10,
        lease_ttl_seconds=20,
        tokenizer_family="activation",
    )
    compiler = _compiler_for_security(provider, recipe)

    plan = compiler.compile(
        TaskRequest(
            task="split-infer",
            mode="sync",
            recipe="split-infer",
            split_learning={
                "activation_ref": {"uri": "s3://bucket/activation.bin", "sha256": "a" * 64},
                "encoder_hash": "b" * 64,
                "dp_epsilon": 1.0,
                "irreversibility_claim": "empirical",
            },
        )
    )

    payload = plan_payload(plan)
    assert payload["split_learning"]["activation_ref"]["uri"] == "s3://bucket/activation.bin"
    assert payload["inline_inputs"] == {}


def test_attestation_verifier_and_key_release_broker_bind_policy_nonce_and_recipient() -> None:
    provider = _provider(
        "tee",
        ProviderTrustProfile(security_tier=SecurityTier.CONFIDENTIAL_TEE, requires_attestation=True, supports_key_release=True),
    )
    evidence = AttestationEvidence(
        provider="tee",
        security_tier=SecurityTier.CONFIDENTIAL_TEE,
        confidential_computing_mode="h100-cc",
        nonce="nonce-1",
        policy_hash="c" * 64,
        evidence_ref="attestation-1",
    )

    verified = AttestationVerifier().verify(evidence, provider=provider, expected_policy_hash="c" * 64, nonce="nonce-1")
    grant = KeyReleaseBroker().release(
        KeyReleaseRequirement(key_id="tenant-key", policy_hash="c" * 64),
        evidence=verified,
        recipient="worker-enclave",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    assert verified.verified is True
    assert grant.key_id == "tenant-key"
    assert grant.recipient == "worker-enclave"
