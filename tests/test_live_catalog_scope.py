from types import SimpleNamespace

from gpucall.domain import ExecutionTupleSpec
from gpucall.live_catalog_scope import live_catalog_scope


def _tuple(name: str, target: str) -> ExecutionTupleSpec:
    return ExecutionTupleSpec(
        name=name,
        adapter="runpod-vllm-serverless",
        gpu="H100",
        vram_gb=80,
        max_model_len=8192,
        cost_per_second=0.001,
        target=target,
    )


def test_candidate_scope_does_not_override_active_tuple(monkeypatch, tmp_path) -> None:
    active = _tuple("runpod-prod", "real-endpoint-id")
    placeholder = _tuple("runpod-prod", "RUNPOD_ENDPOINT_ID_PLACEHOLDER")
    config = SimpleNamespace(tuples={"runpod-prod": active})

    monkeypatch.setattr("gpucall.live_catalog_scope.load_tuple_candidate_payloads", lambda _config_dir: [{"name": "runpod-prod"}])
    monkeypatch.setattr("gpucall.live_catalog_scope._tuple_from_candidate", lambda _candidate, _config: placeholder)

    scope = live_catalog_scope(config, tmp_path)

    assert scope["runpod-prod"].target == "real-endpoint-id"
