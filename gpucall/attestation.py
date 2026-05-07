from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from gpucall.domain import AttestationEvidence, KeyReleaseGrant, KeyReleaseRequirement, ExecutionTupleSpec, SecurityTier


def policy_hash(policy_payload: object) -> str:
    encoded = json.dumps(policy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AttestationVerifier:
    def verify(
        self,
        evidence: AttestationEvidence,
        *,
        tuple: ExecutionTupleSpec,
        expected_policy_hash: str,
        nonce: str,
        worker_image_digest: str | None = None,
    ) -> AttestationEvidence:
        if evidence.tuple != tuple.name:
            raise ValueError("attestation tuple does not match selected tuple")
        if evidence.security_tier != tuple.trust_profile.security_tier:
            raise ValueError("attestation security_tier does not match tuple trust_profile")
        if tuple.trust_profile.security_tier is SecurityTier.CONFIDENTIAL_TEE and not evidence.confidential_computing_mode:
            raise ValueError("confidential TEE attestation requires confidential_computing_mode")
        if not hmac.compare_digest(evidence.policy_hash, expected_policy_hash):
            raise ValueError("attestation policy_hash mismatch")
        if not hmac.compare_digest(evidence.nonce, nonce):
            raise ValueError("attestation nonce mismatch")
        if worker_image_digest is not None and evidence.worker_image_digest != worker_image_digest:
            raise ValueError("attestation worker_image_digest mismatch")
        return evidence.model_copy(update={"verified": True})


class KeyReleaseBroker:
    def release(
        self,
        requirement: KeyReleaseRequirement,
        *,
        evidence: AttestationEvidence,
        recipient: str,
        expires_at: datetime,
    ) -> KeyReleaseGrant:
        if requirement.gateway_may_generate_dek:
            raise ValueError("gateway-generated DEK is forbidden")
        if requirement.attestation_required and not evidence.verified:
            raise ValueError("verified attestation evidence is required for key release")
        if not hmac.compare_digest(requirement.policy_hash, evidence.policy_hash):
            raise ValueError("key release policy_hash mismatch")
        if expires_at <= datetime.now(timezone.utc):
            raise ValueError("key release expiry must be in the future")
        return KeyReleaseGrant(
            key_id=requirement.key_id,
            policy_hash=requirement.policy_hash,
            attestation_evidence_ref=evidence.evidence_ref or evidence.nonce,
            recipient=recipient,
            expires_at=expires_at,
        )


def attestation_audit_reference(evidence: AttestationEvidence) -> dict[str, Any]:
    return {
        "tuple": evidence.tuple,
        "security_tier": evidence.security_tier.value,
        "evidence_ref": evidence.evidence_ref,
        "verified": evidence.verified,
        "policy_hash": evidence.policy_hash,
        "nonce_observed_at": evidence.nonce_observed_at.isoformat(),
    }
