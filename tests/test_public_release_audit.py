from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from gpucall.recipe_intents import CAPABILITY_BY_INTENT


def test_tracked_files_do_not_contain_private_operator_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("public release tracked-file audit requires a git checkout")
    files = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()
    ignored = {
        "docs/PUBLIC_RELEASE_CHECKLIST.md",
        "scripts/public_release_audit.sh",
        "tests/test_public_release_audit.py",
    }
    patterns = [
        r"100\.([6-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}",
        r"api\.runpod\.ai/v2/[a-z0-9]{12,}",
        r"vllm-[a-z0-9]{12,}",
        r"^\s*ssh_remote_cidr:\s+(?!(203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|\"\"|null))([0-9]{1,3}\.){3}[0-9]{1,3}",
        r"\broot@",
        "news-" + "system",
        "/Users/" + "tamotsu",
        "PRIVATE KEY",
        r"sk-[A-Za-z0-9]",
        r"AKIA[0-9A-Z]{16}",
        r"no eligible provider after policy, recipe, and circuit constraints",
        r"provider-smoke",
        r"sdk/python/dist/.*\.whl",
    ]
    compiled = re.compile("|".join(patterns))

    findings: list[str] = []
    for relative in files:
        if relative in ignored:
            continue
        path = root / relative
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if compiled.search(line):
                findings.append(f"{relative}:{lineno}:{line}")

    assert findings == []


def test_recipe_intents_are_registered() -> None:
    root = Path(__file__).resolve().parents[1]
    intents: set[str] = set()
    for directory in (root / "config" / "recipes", root / "gpucall" / "config_templates" / "recipes"):
        for path in directory.glob("*.yml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            intent = data.get("intent")
            if intent:
                intents.add(str(intent))

    assert sorted(intent for intent in intents if intent not in CAPABILITY_BY_INTENT) == []


def test_admin_automation_defaults_are_closed() -> None:
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("public release tracked-file audit requires a git checkout")
    for relative in ("config/admin.yml", "config/admin.yml.example", "gpucall/config_templates/admin.yml.example"):
        data = yaml.safe_load((root / relative).read_text(encoding="utf-8")) or {}
        assert data.get("recipe_inbox_auto_materialize") is False
        assert data.get("recipe_inbox_auto_validate_existing_tuples", False) is False
        assert data.get("recipe_inbox_auto_activate_existing_validated_recipe", False) is False
        assert data.get("recipe_inbox_auto_promote_candidates", False) is False
        assert data.get("recipe_inbox_auto_billable_validation", False) is False
        assert data.get("recipe_inbox_auto_activate_validated", False) is False
        assert data.get("recipe_inbox_auto_set_auto_select", False) is False
        assert data.get("recipe_inbox_auto_run_launch_check", False) is False
        assert data.get("api_key_handoff_mode") == "manual"


def test_runpod_flash_is_optional_provider_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project_deps = text.split("[project.optional-dependencies]", 1)[0]
    assert "runpod-flash" not in project_deps
    assert "runpod-flash" in text


def test_public_repo_uses_canonical_release_checklist_only() -> None:
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("public release tracked-file audit requires a git checkout")
    assert not (root / "RELEASE_CHECKLIST.md").exists()
    assert (root / "docs" / "PUBLIC_RELEASE_CHECKLIST.md").exists()
