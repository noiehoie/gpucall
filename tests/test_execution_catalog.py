from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gpucall.config import load_config
from gpucall.execution_catalog import build_resource_catalog_snapshot, generate_tuple_candidates


def test_execution_catalog_separates_accounts_surfaces_and_workers() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(config, config_dir=Path("config"))

    account_refs = {account.account_ref for account in snapshot.accounts}
    surfaces = {resource.execution_surface for resource in snapshot.resources}
    normalized_surfaces = {surface.execution_surface for surface in snapshot.execution_surfaces}

    assert {"hyperstack", "modal", "runpod"}.issubset(account_refs)
    assert {"iaas_vm", "function_runtime", "managed_endpoint"}.issubset(surfaces)
    assert {"iaas_vm", "function_runtime", "managed_endpoint"}.issubset(normalized_surfaces)
    assert all(resource.account_ref for resource in snapshot.resources)
    assert all(resource.worker_binding_ref for resource in snapshot.resources)
    assert all(worker.execution_surface for worker in snapshot.workers)
    assert all(worker.worker_binding_ref for worker in snapshot.workers)
    assert all(offering.account_ref for offering in snapshot.provider_offerings)
    assert all(offering.gpu_sku_ref.startswith("gpu:") for offering in snapshot.provider_offerings)
    assert all(claim.resource_ref and claim.worker_ref for claim in snapshot.capability_claims)
    assert all(claim.security_tier for claim in snapshot.capability_claims)
    assert all(rule.account_ref and rule.resource_ref for rule in snapshot.pricing_rules)
    assert all(overlay.resource_ref for overlay in snapshot.live_status_overlay)
    assert len(snapshot.snapshot_id) == 64
    assert len(snapshot.config_hash) == 64
    assert isinstance(snapshot.resources, tuple)
    assert isinstance(snapshot.workers, tuple)


def test_execution_catalog_normalizes_hardware_surface_pricing_and_network() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(config, config_dir=Path("config"))

    gpu_refs = {sku.sku_ref: sku for sku in snapshot.hardware_catalog}
    surfaces = {surface.execution_surface: surface for surface in snapshot.execution_surfaces}
    offerings = {offering.resource_ref: offering for offering in snapshot.provider_offerings}
    claims = {claim.resource_ref: claim for claim in snapshot.capability_claims}
    prices = {rule.resource_ref: rule for rule in snapshot.pricing_rules}

    assert "gpu:a100" in gpu_refs
    assert gpu_refs["gpu:a100"].architecture == "ampere"
    assert "gpu:h200x4" in gpu_refs
    assert gpu_refs["gpu:h200x4"].vram_gb == 564
    assert surfaces["iaas_vm"].cleanup_contract == "resource_lease_destroy_required"
    assert surfaces["function_runtime"].lifecycle_kind == "scale_to_zero_function"
    assert surfaces["managed_endpoint"].network_exposure == "provider_public_endpoint"
    assert offerings["active_tuple:hyperstack-a100:resource"].network_topology["ssh_remote_cidr"] == "203.0.113.10/32"
    assert claims["active_tuple:hyperstack-a100:resource"].security_tier == "encrypted_capsule"
    assert claims["active_tuple:hyperstack-a100:resource"].sovereign_jurisdiction == "CA"
    assert claims["active_tuple:hyperstack-a100:resource"].dedicated_gpu is True
    assert claims["active_tuple:hyperstack-a100:resource"].requires_attestation is False
    assert prices["active_tuple:hyperstack-a100:resource"].billing_granularity_seconds == 60
    with pytest.raises(TypeError):
        offerings["active_tuple:hyperstack-a100:resource"].network_topology["ssh_remote_cidr"] = "0.0.0.0/0"
    assert isinstance(claims["active_tuple:hyperstack-a100:resource"].required_input_contracts, tuple)
    worker = next(item for item in snapshot.workers if item.tuple_name == "hyperstack-a100")
    candidate = next(item for item in generate_tuple_candidates(snapshot, recipe=config.recipes["text-infer-standard"]) if item.tuple_name == "hyperstack-a100")
    assert isinstance(worker.modes, tuple)
    assert isinstance(worker.input_contracts, tuple)
    assert isinstance(candidate.modes, tuple)
    with pytest.raises(AttributeError):
        worker.modes.append("stream")
    with pytest.raises(TypeError):
        candidate.recipe_fit["fits"] = False


def test_execution_catalog_generates_snapshot_pinned_tuple_candidates() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(config, config_dir=Path("config"))
    candidates = generate_tuple_candidates(snapshot, recipe=config.recipes["text-infer-standard"])

    assert any(candidate.tuple_name == "hyperstack-a100" and candidate.execution_surface == "iaas_vm" for candidate in candidates)
    assert any(candidate.tuple_name == "modal-a10g" and candidate.execution_surface == "function_runtime" for candidate in candidates)
    assert any(
        candidate.tuple_name.startswith("runpod-vllm-")
        and candidate.execution_surface == "managed_endpoint"
        and candidate.production_state == "candidate_draft"
        for candidate in candidates
    )
    assert any(
        candidate.tuple_name.startswith("runpod-native-")
        and candidate.execution_surface == "managed_endpoint"
        and candidate.production_state == "candidate_draft"
        for candidate in candidates
    )
    assert all(candidate.snapshot_id == snapshot.snapshot_id for candidate in candidates)
    assert all(candidate.snapshot_pinned is True for candidate in candidates)
    assert all(candidate.recipe_fit is not None for candidate in candidates)


def test_execution_catalog_recipe_fit_respects_worker_contracts() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(config, config_dir=Path("config"))
    vision_candidates = generate_tuple_candidates(snapshot, recipe=config.recipes["vision-image-standard"])
    hyperstack = next(item for item in vision_candidates if item.tuple_name == "hyperstack-a100")
    modal_vision = next(item for item in vision_candidates if item.tuple_name == "modal-vision-a10g")

    assert hyperstack.recipe_fit is not None
    assert hyperstack.recipe_fit["eligible"] is False
    assert "worker input_contracts do not declare image support" in hyperstack.recipe_fit["reasons"]
    assert modal_vision.recipe_fit is not None
    assert modal_vision.recipe_fit["eligible"] is True


def test_execution_catalog_uses_live_price_and_stock_evidence() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(
        config,
        config_dir=Path("config"),
        live_catalog_evidence={
            "modal-a10g": {
                "status": "live_revalidated",
                "checked": True,
                "findings": [
                    {
                        "tuple": "modal-a10g",
                        "severity": "info",
                        "dimension": "price",
                        "live_price_per_second": 0.00031,
                        "live_price_source": "test-live-price",
                    },
                    {
                        "tuple": "modal-a10g",
                        "severity": "info",
                        "dimension": "stock",
                        "live_stock_state": "available",
                    },
                ],
            }
        },
    )
    candidates = generate_tuple_candidates(snapshot, recipe=config.recipes["text-infer-standard"])
    modal = next(item for item in candidates if item.tuple_name == "modal-a10g")

    assert modal.configured_price_per_second == 0.00035
    assert modal.price_per_second == 0.00031
    assert modal.live_price_per_second == 0.00031
    assert modal.live_stock_state == "available"
    resource = next(item for item in snapshot.resources if item.tuple_name == "modal-a10g")
    overlay = next(item for item in snapshot.live_status_overlay if item.resource_ref == resource.resource_ref)
    assert isinstance(overlay.dimensions, tuple)
    assert isinstance(resource.live_catalog_findings, tuple)
    with pytest.raises(TypeError):
        resource.live_catalog_findings[0]["severity"] = "critical"


def test_execution_catalog_cli_outputs_candidates() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "execution-catalog",
            "candidates",
            "--config-dir",
            "config",
            "--recipe",
            "text-infer-standard",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert any(item["execution_surface"] == "iaas_vm" for item in payload)
    assert any(item["execution_surface"] == "function_runtime" for item in payload)
    assert any(item["source"] == "tuple_candidate" for item in payload)
