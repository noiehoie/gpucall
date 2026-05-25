from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from gpucall.recipe_intents import CAPABILITY_BY_INTENT
from gpucall.release import GITHUB_RELEASE_TAG, SDK_WHEEL_URL


def test_tracked_files_do_not_contain_private_operator_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("public release tracked-file audit requires a git checkout")
    subprocess.run(["bash", "scripts/check_product_contamination.sh"], cwd=root, check=True)


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


def test_shipping_recipe_catalog_is_generic_starter_catalog() -> None:
    root = Path(__file__).resolve().parents[1]
    required_starter_intents = {
        "summarize_text",
        "extract_json",
        "translate_text",
        "rank_text_items",
        "understand_document_image",
    }
    prohibited_intents = {"rss_semantic_match"}
    prohibited_terms = ("news-system", "rss", "feed", "newspaper", "frontpage")

    for directory in (root / "config" / "recipes", root / "gpucall" / "config_templates" / "recipes"):
        recipes = []
        for path in directory.glob("*.yml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            recipes.append({"path": path, "data": data, "text": path.read_text(encoding="utf-8")})

        intents = {str(item["data"].get("intent")) for item in recipes if item["data"].get("intent")}
        assert required_starter_intents <= intents
        assert prohibited_intents.isdisjoint(intents)
        for item in recipes:
            serialized = item["text"].lower()
            assert not any(term in serialized for term in prohibited_terms), item["path"]


def test_external_onboarding_prompt_placeholders_are_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md").read_text(encoding="utf-8")

    assert "<admin-inbox>" not in text
    for placeholder in (
        "<system-name>",
        "<gpucall-base-url>",
        "<gpucall-api-key>",
        "<recipe-request-admin-inbox>",
        "<quality-feedback-admin-inbox>",
        "<canary-command>",
        "<gpucall-sdk-wheel-url>",
    ):
        assert placeholder in text

    assert "uv tool install <gpucall-sdk-wheel-url>" in text
    assert "--remote-inbox <recipe-request-admin-inbox>" in text
    assert 'export GPUCALL_QUALITY_FEEDBACK_INBOX="<quality-feedback-admin-inbox>"' in text


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
        assert data.get("recipe_inbox_auto_provision_supply", False) is False
        assert data.get("recipe_inbox_auto_apply_supply", False) is False
        assert data.get("recipe_inbox_auto_billable_validation", False) is False
        assert float(data.get("recipe_inbox_auto_validation_budget_usd", 0.10)) == 0.10
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


def test_sdk_release_urls_match_python_sdk_version() -> None:
    root = Path(__file__).resolve().parents[1]
    sdk_project = tomllib.loads((root / "sdk" / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    version = sdk_project["project"]["version"]
    wheel_name = f"gpucall_sdk-{version}-py3-none-any.whl"

    assert GITHUB_RELEASE_TAG == f"v{version}"
    assert SDK_WHEEL_URL == f"https://github.com/noiehoie/gpucall/releases/download/v{version}/{wheel_name}"
    for relative in (
        "README.md",
        "README.ja.md",
        "docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md",
        "docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md",
        "docs/GATEWAY_API_KEYS.md",
        "docs/PUBLIC_RELEASE_CHECKLIST.md",
        "docs/SDK_DISTRIBUTION.md",
        "sdk/python/README.md",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert wheel_name in text
