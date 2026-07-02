from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gpucall.domain import CompiledPlan, ExecutionMode, ExecutionTupleSpec
from gpucall.execution.registry import build_registered_adapter, registered_adapter_names
from gpucall.provider_conformance import (
    registered_conformance_matrix,
    run_execution_cycle_conformance,
    run_provider_conformance,
)


def test_all_registered_adapters_pass_registry_conformance() -> None:
    report = run_provider_conformance()

    assert report["phase"] == "provider-conformance"
    assert report["non_generation_probe_only"] is True
    assert report["adapter_count"] == len(registered_adapter_names())
    failing = {
        name: [check["name"] for check in item["checks"] if not check["ok"]]
        for name, item in report["adapters"].items()
        if not item["passed"]
    }
    assert not failing, failing
    assert report["passed"] is True


def test_single_adapter_conformance() -> None:
    report = run_provider_conformance("echo")

    assert report["adapter_count"] == 1
    assert report["adapters"]["echo"]["vendor_family"] == "local"
    assert report["passed"] is True


def test_conformance_matrix_lists_every_adapter() -> None:
    matrix = registered_conformance_matrix()

    assert set(registered_adapter_names()) <= set(matrix)
    for name, summary in matrix.items():
        assert summary is not None, f"{name} has no descriptor"
        assert summary["execution_surface"], f"{name} has no execution surface"


def _echo_tuple_spec() -> ExecutionTupleSpec:
    return ExecutionTupleSpec(
        name="echo-conformance",
        adapter="echo",
        gpu="none",
        vram_gb=1,
        cost_per_second=0.0,
        max_model_len=8192,
    )


def _echo_plan() -> CompiledPlan:
    return CompiledPlan(
        policy_version="test",
        recipe_name="echo-recipe",
        task="infer",
        mode=ExecutionMode.SYNC,
        tuple_chain=["echo-conformance"],
        timeout_seconds=5,
        lease_ttl_seconds=60,
        token_estimation_profile="default",
        token_budget=None,
        input_refs=[],
        inline_inputs={},
    )


async def test_execution_cycle_conformance_on_owned_local_adapter() -> None:
    adapter = build_registered_adapter(_echo_tuple_spec(), {})
    report = await run_execution_cycle_conformance(adapter, _echo_plan())

    failing = [check["name"] for check in report["checks"] if not check["ok"]]
    assert not failing, failing
    assert report["passed"] is True
