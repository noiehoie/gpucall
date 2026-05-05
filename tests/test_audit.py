from __future__ import annotations

from gpucall.audit import AuditTrail, redacted_plan_for_audit
from gpucall.domain import CompiledPlan, DataRef, ExecutionMode, InlineValue


def test_audit_trail_is_hash_chained(tmp_path) -> None:
    audit = AuditTrail(tmp_path / "trail.jsonl")
    audit.append("one", {"a": 1})
    audit.append("two", {"b": 2})

    assert audit.verify()


def test_audit_trail_redacts_inline_inputs_and_signed_urls(tmp_path) -> None:
    audit = AuditTrail(tmp_path / "trail.jsonl")
    audit.append(
        "plan.accepted",
        {
            "inline_inputs": {"prompt": {"value": "top secret prompt", "content_type": "text/plain"}},
            "input_refs": [{"uri": "https://example.com/object?signature=secret", "sha256": "a" * 64}],
        },
    )

    raw = (tmp_path / "trail.jsonl").read_text(encoding="utf-8")

    assert "top secret prompt" not in raw
    assert "signature=secret" not in raw
    assert '"redacted":true' in raw


def test_audit_trail_redacts_provider_error_text(tmp_path) -> None:
    audit = AuditTrail(tmp_path / "trail.jsonl")
    audit.append(
        "provider.failed",
        {
            "provider": "storage",
            "error": (
                "PUT https://bucket.example/object?"
                "X-Amz-Credential=cred&X-Amz-Signature=sig with Authorization: Bearer token123"
            ),
        },
    )

    raw = (tmp_path / "trail.jsonl").read_text(encoding="utf-8")

    assert "X-Amz-Credential=cred" not in raw
    assert "X-Amz-Signature=sig" not in raw
    assert "token123" not in raw
    assert "X-Amz-Credential=<redacted>" in raw
    assert "X-Amz-Signature=<redacted>" in raw


def test_audit_trail_coordinates_multiple_writers(tmp_path) -> None:
    path = tmp_path / "trail.jsonl"
    first = AuditTrail(path)
    second = AuditTrail(path)

    first.append("one", {"a": 1})
    second.append("two", {"b": 2})
    first.append("three", {"c": 3})

    assert AuditTrail(path).verify()


def test_audit_rotation_preserves_hash_chain_continuity(tmp_path) -> None:
    path = tmp_path / "trail.jsonl"
    audit = AuditTrail(path)
    audit.append("one", {"a": 1})
    rotated = audit.rotate_if_needed(1)
    assert rotated is not None

    audit.append("two", {"b": 2})

    assert AuditTrail(path).verify()


def test_redacted_plan_for_audit_is_allowlisted() -> None:
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.SYNC,
        provider_chain=["modal-a10g"],
        timeout_seconds=2,
        lease_ttl_seconds=10,
        tokenizer_family="qwen",
        token_budget=128,
        max_tokens=64,
        input_refs=[
            DataRef(
                uri="https://r2.example/object.txt?X-Amz-Signature=secret",
                sha256="a" * 64,
                bytes=123,
                content_type="text/plain",
                endpoint_url="https://r2.example",
            )
        ],
        inline_inputs={"prompt": InlineValue(value="secret prompt", content_type="text/plain")},
        attestations={
            "governance_hash": "abc123",
            "recipe_snapshot": {"name": "r1", "system_prompt": "stable recipe prompt"},
        },
    )

    redacted = redacted_plan_for_audit(plan)
    raw = str(redacted)

    assert redacted["plan_id"] == plan.plan_id
    assert redacted["provider_chain"] == ["modal-a10g"]
    assert redacted["inline_inputs"]["prompt"]["bytes"] == len("secret prompt")
    assert redacted["input_refs"][0]["bytes"] == 123
    assert redacted["input_refs"][0]["sha256_prefix"] == "a" * 12
    assert redacted["attestations"]["governance_hash"] == "abc123"
    assert redacted["attestations"]["recipe_snapshot"]["name"]["redacted"] is True
    assert redacted["attestations"]["recipe_snapshot"]["system_prompt"]["redacted"] is True
    assert "secret prompt" not in raw
    assert "stable recipe prompt" not in raw
    assert "X-Amz" not in raw
    assert "Signature" not in raw
    assert "r2.example" not in raw
