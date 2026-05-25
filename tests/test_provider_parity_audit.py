from __future__ import annotations

import subprocess
import sys

from gpucall.provider_contracts import CLOUD_PROVIDER_FAMILIES, PROVIDER_SETUP_CONTRACTS


def test_provider_setup_contracts_cover_all_cloud_families() -> None:
    assert set(PROVIDER_SETUP_CONTRACTS) == set(CLOUD_PROVIDER_FAMILIES) == {"modal", "runpod", "hyperstack"}
    for name, contract in PROVIDER_SETUP_CONTRACTS.items():
        assert contract.family == name
        assert contract.credential_probe_sets
        assert contract.gpucall_credentials_required


def test_provider_parity_guard_passes() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_provider_parity.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "provider parity guard ok" in result.stdout
