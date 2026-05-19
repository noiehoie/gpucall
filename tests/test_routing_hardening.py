import pytest
import os
from gpucall.compiler import GovernanceCompiler, GovernanceError
from gpucall.domain import (
    ExecutionMode,
    ExecutionSurface,
    Policy,
    ExecutionTupleSpec,
    Recipe,
    TaskRequest,
    TuplePolicy,
)
from gpucall.registry import ObservedRegistry

@pytest.fixture
def base_policy():
    return Policy(
        version="1.0",
        inline_bytes_limit=1024,
        default_lease_ttl_seconds=300,
        max_lease_ttl_seconds=3600,
        max_timeout_seconds=600,
        local_large_context_threshold=1000,  # Small threshold for testing
        tuples=TuplePolicy(allow=["cloud-h100", "local-ollama"]),
    )

@pytest.fixture
def cloud_tuple():
    return ExecutionTupleSpec(
        name="cloud-h100",
        adapter="runpod-vllm-serverless",
        execution_surface=ExecutionSurface.MANAGED_ENDPOINT,
        gpu="H100",
        vram_gb=80,
        max_model_len=131072,
        cost_per_second=0.001,
        target="endpoint-123",
        model="gemma3",
        endpoint_contract="openai-chat-completions",
        input_contracts=["text", "chat_messages", "data_refs"],
        output_contract="openai-chat-completions",
        modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
        provider_params={
            "worker_env": {
                "MODEL_NAME": "gemma3",
                "MAX_MODEL_LEN": 131072,
                "GPU_MEMORY_UTILIZATION": 0.9,
                "MAX_CONCURRENCY": 1,
            },
            "model_storage": {
                "storage_kind": "container_ephemeral"
            }
        },
        image="runpod/worker-v1-vllm:latest",
    )

@pytest.fixture
def local_tuple():
    return ExecutionTupleSpec(
        name="local-ollama",
        adapter="local-ollama",
        execution_surface=ExecutionSurface.LOCAL_RUNTIME,
        gpu="RTX4090",
        vram_gb=24,
        max_model_len=131072,
        cost_per_second=0.0,
        endpoint="http://localhost:11434",
        model="gemma3",
        endpoint_contract="ollama-generate",
        input_contracts=["text", "chat_messages"],
        output_contract="ollama-generate",
        modes=[ExecutionMode.SYNC],
        controlled_runtime_ref="ollama-runtime", # Bypass VRAM gate
    )

@pytest.fixture
def standard_recipe():
    return Recipe(
        name="standard-infer",
        task="infer",
        allowed_modes=[ExecutionMode.SYNC],
        context_budget_tokens=131072,
        timeout_seconds=60,
        lease_ttl_seconds=300,
        auto_select=True,
    )

def test_routing_prefers_local_for_small_workload(base_policy, cloud_tuple, local_tuple, standard_recipe):
    registry = ObservedRegistry()
    compiler = GovernanceCompiler(
        policy=base_policy,
        recipes={standard_recipe.name: standard_recipe},
        tuples={cloud_tuple.name: cloud_tuple, local_tuple.name: local_tuple},
        registry=registry,
    )
    
    # Small workload (context length < threshold=1000)
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
    )
    
    plan = compiler.compile(request)
    assert plan.tuple_chain[0] == "local-ollama"

def test_routing_prefers_cloud_for_large_workload(base_policy, cloud_tuple, local_tuple, standard_recipe):
    registry = ObservedRegistry()
    compiler = GovernanceCompiler(
        policy=base_policy,
        recipes={standard_recipe.name: standard_recipe},
        tuples={cloud_tuple.name: cloud_tuple, local_tuple.name: local_tuple},
        registry=registry,
    )
    
    # Large workload (context length > threshold=1000)
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        messages=[{"role": "user", "content": "x" * 2000}],
        max_tokens=100,
    )
    
    plan = compiler.compile(request)
    assert plan.tuple_chain[0] == "cloud-h100"

def test_routing_fails_closed_for_large_workload_when_no_cloud_available(base_policy, local_tuple, standard_recipe):
    registry = ObservedRegistry()
    compiler = GovernanceCompiler(
        policy=base_policy,
        recipes={standard_recipe.name: standard_recipe},
        tuples={local_tuple.name: local_tuple},
        registry=registry,
    )
    
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        messages=[{"role": "user", "content": "x" * 2000}],
        max_tokens=100,
    )
    
    with pytest.raises(GovernanceError) as excinfo:
        compiler.compile(request)
    
    assert excinfo.value.code == "NO_ELIGIBLE_TUPLE"
    # Rejection reason should be in the context rejections
    rejections = excinfo.value.context.get("tuple_rejections", {})
    assert any("local execution is disabled for large context workloads" in reason for reason in rejections.values())

def test_routing_allows_local_large_workload_when_explicitly_enabled_in_recipe(base_policy, local_tuple, standard_recipe):
    # Enable allow_local_large_context in recipe
    standard_recipe.allow_local_large_context = True
    
    registry = ObservedRegistry()
    compiler = GovernanceCompiler(
        policy=base_policy,
        recipes={standard_recipe.name: standard_recipe},
        tuples={local_tuple.name: local_tuple},
        registry=registry,
    )
    
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        messages=[{"role": "user", "content": "x" * 2000}],
        max_tokens=100,
    )
    
    plan = compiler.compile(request)
    assert plan.tuple_chain[0] == "local-ollama"

def test_routing_prefers_cloud_over_local_even_when_explicitly_allowed(base_policy, cloud_tuple, local_tuple, standard_recipe):
    # Enable allow_local_large_context in recipe
    standard_recipe.allow_local_large_context = True
    
    registry = ObservedRegistry()
    compiler = GovernanceCompiler(
        policy=base_policy,
        recipes={standard_recipe.name: standard_recipe},
        tuples={cloud_tuple.name: cloud_tuple, local_tuple.name: local_tuple},
        registry=registry,
    )
    
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        messages=[{"role": "user", "content": "x" * 2000}],
        max_tokens=100,
    )
    
    plan = compiler.compile(request)
    assert plan.tuple_chain[0] == "cloud-h100"
