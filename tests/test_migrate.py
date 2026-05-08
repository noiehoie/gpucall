from __future__ import annotations

import json

from gpucall.migrate import assess_project, build_preflight_requests, canary_project, main, patch_suggestions


def test_migrate_assess_detects_direct_provider_and_gpucall_paths(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "translator.py").write_text(
        "from anthropic import Anthropic\nmodel='claude-haiku-4-5-20251001'\ndef run(): call_claude_p('x')\n",
        encoding="utf-8",
    )
    (project / "client.py").write_text("from gpucall_sdk import GPUCallClient\nGPUCallClient('http://x')\n", encoding="utf-8")

    report = assess_project(project, source="news-system")

    assert report["summary"]["anthropic_direct"] >= 1
    assert report["summary"]["gpucall_path"] >= 1
    assert report["direct_provider_paths"]


def test_migrate_preflight_generates_translate_request(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "translate.py").write_text("call_claude_p(model='claude-haiku')\n", encoding="utf-8")

    report = assess_project(project, source="news-system")
    requests = build_preflight_requests(report, source="news-system")

    assert requests[0]["task"] == "infer"
    assert requests[0]["intent"] == "translate_text"
    assert "--required-model-len 32768" in requests[0]["command"]


def test_migrate_cli_writes_reports(tmp_path) -> None:
    project = tmp_path / "project"
    out = tmp_path / "out"
    project.mkdir()
    (project / "topic_engine.py").write_text("call_llm('summarize topic')\n", encoding="utf-8")

    assert main(["report", str(project), "--source", "news-system", "--output-dir", str(out)]) == 0

    data = json.loads((out / "migration-report.json").read_text(encoding="utf-8"))
    assert data["phase"] == "migration-assessment"
    assert data["preflight_requests"][0]["intent"] == "summarize_text"
    assert (out / "migration-report.md").exists()


def test_migrate_canary_runs_command(tmp_path) -> None:
    report = canary_project(tmp_path, command="printf 'NO_ELIGIBLE_TUPLE\\n'", source="test")

    assert report["ran"] is True
    assert report["returncode"] == 0
    assert report["error_codes"]["NO_ELIGIBLE_TUPLE"] == 1


def test_migrate_patch_apply_writes_helper_and_annotations(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "translate.py"
    source.write_text("from anthropic import Anthropic\nmodel='claude-haiku'\n", encoding="utf-8")

    report = patch_suggestions(project, source="news-system", apply=True)

    assert report["applied"] is True
    assert "gpucall_migration.py" in report["changed_files"]
    assert "translate.py" in report["changed_files"]
    assert "from gpucall_migration import AnthropicCompat as Anthropic" in source.read_text(encoding="utf-8")
    assert "direct provider path migrated" in source.read_text(encoding="utf-8")
    assert (project / ".gpucall-migration" / "applied-patch.json").exists()


def test_migrate_patch_apply_rewrites_openai_client_constructor(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")

    report = patch_suggestions(project, source="openai-app", apply=True)

    assert report["applied"] is True
    text = source.read_text(encoding="utf-8")
    assert "from gpucall_migration import gpucall_openai_client" in text
    assert "client = gpucall_openai_client()" in text
