from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gpucall.cli import seed_liveness


class _Compiler:
    def __init__(self, estimated_cost: float) -> None:
        self.recipes = {"text-infer-standard": SimpleNamespace(name="text-infer-standard", task="infer")}
        self.estimated_cost = estimated_cost

    def compile(self, _request):
        return SimpleNamespace(attestations={"cost_estimate": {"estimated_cost_usd": self.estimated_cost}})


class _Dispatcher:
    def __init__(self) -> None:
        self.executions = 0

    async def execute_sync(self, _plan):
        self.executions += 1


def _runtime(estimated_cost: float) -> SimpleNamespace:
    return SimpleNamespace(compiler=_Compiler(estimated_cost), dispatcher=_Dispatcher())


@pytest.mark.asyncio
async def test_seed_liveness_fails_closed_on_zero_cost_estimate(monkeypatch) -> None:
    runtime = _runtime(0.0)
    monkeypatch.setattr("gpucall.cli.build_runtime", lambda _config_dir: runtime)

    with pytest.raises(SystemExit, match="zero-cost estimates"):
        await seed_liveness(Path("config"), "text-infer-standard", 1, budget_usd=1.0)

    assert runtime.dispatcher.executions == 0


@pytest.mark.asyncio
async def test_seed_liveness_enforces_budget_before_execution(monkeypatch) -> None:
    runtime = _runtime(0.25)
    monkeypatch.setattr("gpucall.cli.build_runtime", lambda _config_dir: runtime)

    with pytest.raises(SystemExit, match="budget exceeded"):
        await seed_liveness(Path("config"), "text-infer-standard", 2, budget_usd=0.25)

    assert runtime.dispatcher.executions == 1
