from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from gpucall.app_helpers import enforce_request_budget, safe_request_summary
from gpucall.domain import ChatMessage, ExecutionMode, TaskRequest
from gpucall.tenant import TenantUsageLedger


@pytest.mark.asyncio
async def test_tenant_budget_ledger_reserves_request_budget_not_fixed_cost(tmp_path) -> None:
    ledger = TenantUsageLedger(tmp_path / "tenant_usage.db")
    runtime = SimpleNamespace(
        tenants={
            "tenant-a": SimpleNamespace(
                name="tenant-a",
                max_request_estimated_cost_usd=0.1,
                daily_budget_usd=0.1,
                monthly_budget_usd=0.1,
            )
        },
        tenant_usage=ledger,
    )
    request = SimpleNamespace(state=SimpleNamespace(api_key=None, tenant_id="tenant-a"))
    plan = SimpleNamespace(
        attestations={
            "cost_estimate": {
                "estimated_cost_usd": 1.2665,
                "fixed_cost_usd": 1.224,
                "marginal_cost_usd": 0.0425,
                "budget_reservation_usd": 0.0425,
            }
        },
        tuple_chain=["runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"],
        recipe_name="vision-understand-document-image-draft",
        plan_id="plan-a",
    )

    await enforce_request_budget(runtime, request, plan)

    spent = ledger.spend_since("tenant-a", datetime.fromtimestamp(0, timezone.utc))
    assert spent == pytest.approx(0.0425)


def test_safe_request_summary_accepts_openai_multimodal_and_tool_messages() -> None:
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        messages=[
            ChatMessage(role="user", content=[{"type": "text", "text": "read this"}]),
            ChatMessage(role="assistant", tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "noop", "arguments": "{}"}}]),
        ],
    )

    summary = safe_request_summary(request)

    assert summary["message_count"] == 2
    assert summary["message_total_bytes"] > 0


def test_every_registered_adapter_has_explicit_billing_model() -> None:
    # Freeze the _billing_model coverage: any production-eligible adapter must map
    # to an explicit billing model, never fall through to generic_request_runtime.
    from gpucall.costing import _billing_model
    from gpucall.domain import ExecutionTupleSpec
    from gpucall.execution.registry import adapter_descriptor, registered_adapter_names

    for adapter in registered_adapter_names():
        descriptor = adapter_descriptor(adapter)
        if descriptor is None or not descriptor.production_eligible:
            continue
        spec = ExecutionTupleSpec(
            name=f"probe-{adapter}",
            adapter=adapter,
            execution_surface=descriptor.execution_surface,
            gpu="L4",
            vram_gb=24,
            max_model_len=1024,
            cost_per_second=0.001,
        )
        assert _billing_model(spec) != "generic_request_runtime", (
            f"adapter {adapter!r} is production_eligible but _billing_model falls through "
            "to generic_request_runtime; teach _billing_model about it"
        )
