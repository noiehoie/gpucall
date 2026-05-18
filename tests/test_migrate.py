from __future__ import annotations

import json

from gpucall.migrate import (
    assess_project,
    build_preflight_requests,
    canary_project,
    compare_project,
    draft_contract_project,
    main,
    patch_suggestions,
    profile_project,
    trace_project,
)
from gpucall.workload_contract import compare_trace_to_contract, contract_to_recipe_intake


def test_migrate_assess_detects_direct_provider_and_gpucall_paths(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "translator.py").write_text(
        "from anthropic import Anthropic\nmodel='claude-haiku-4-5-20251001'\ndef run(): call_claude_p('x')\n",
        encoding="utf-8",
    )
    (project / "client.py").write_text("from gpucall_sdk import GPUCallClient\nGPUCallClient('http://x')\n", encoding="utf-8")

    report = assess_project(project, source="example-caller-app")

    assert report["summary"]["anthropic_direct"] >= 1
    assert report["summary"]["gpucall_path"] >= 1
    assert report["direct_provider_paths"]


def test_migrate_preflight_generates_translate_request(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "translate.py").write_text("call_claude_p(model='claude-haiku')\n", encoding="utf-8")

    report = assess_project(project, source="example-caller-app")
    requests = build_preflight_requests(report, source="example-caller-app")

    assert requests[0]["task"] == "infer"
    assert requests[0]["intent"] == "translate_text"
    assert "--required-model-len 32768" in requests[0]["command"]


def test_migrate_preflight_overdeclares_rss_semantic_match(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "rss_match.py").write_text("def run():\n    call_llm('semantic RSS match')\n", encoding="utf-8")

    report = assess_project(project, source="news-system")
    requests = build_preflight_requests(report, source="news-system")

    assert requests[0]["task"] == "infer"
    assert requests[0]["intent"] == "rss_semantic_match"
    assert "--required-model-len 131072" in requests[0]["command"]


def test_migrate_preflight_overdeclares_pairwise_match(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "matcher.py").write_text("def run():\n    call_llm('pairwise similarity')\n", encoding="utf-8")

    report = assess_project(project, source="news-system")
    requests = build_preflight_requests(report, source="news-system")

    assert requests[0]["task"] == "infer"
    assert requests[0]["intent"] == "pairwise_match"
    assert "--required-model-len 131072" in requests[0]["command"]


def test_migrate_cli_writes_reports(tmp_path) -> None:
    project = tmp_path / "project"
    out = tmp_path / "out"
    project.mkdir()
    (project / "topic_engine.py").write_text("call_llm('summarize topic')\n", encoding="utf-8")

    assert main(["report", str(project), "--source", "example-caller-app", "--output-dir", str(out)]) == 0

    data = json.loads((out / "migration-report.json").read_text(encoding="utf-8"))
    assert data["phase"] == "migration-assessment"
    assert data["preflight_requests"][0]["intent"] == "rank_text_items"
    assert (out / "migration-report.md").exists()


def test_migrate_canary_runs_command(tmp_path) -> None:
    report = canary_project(tmp_path, command="printf 'NO_ELIGIBLE_TUPLE\\n'", source="test")

    assert report["ran"] is True
    assert report["returncode"] == 0
    assert report["error_codes"]["NO_ELIGIBLE_TUPLE"] == 1


def test_migrate_trace_timeout_returns_bounded_report(tmp_path) -> None:
    report = trace_project(
        tmp_path,
        command="python -c 'import time; time.sleep(2)'",
        source="test",
        timeout_seconds=0.1,
    )

    assert report["ran"] is True
    assert report["timed_out"] is True
    assert report["returncode"] is None
    assert report["redaction_report"]["raw_log_forwarded"] is False


def test_migrate_patch_apply_writes_helper_and_annotations(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "translate.py"
    source.write_text("from anthropic import Anthropic\nmodel='claude-haiku'\n", encoding="utf-8")

    report = patch_suggestions(project, source="example-caller-app", apply=True)

    assert report["applied"] is True
    assert "gpucall_migration.py" in report["changed_files"]
    assert "translate.py" in report["changed_files"]
    assert "from gpucall_migration import AnthropicCompat as Anthropic" in source.read_text(encoding="utf-8")
    assert "direct provider path migrated" in source.read_text(encoding="utf-8")
    assert (project / ".gpucall-migration" / "applied-patch.json").exists()
    helper = (project / "gpucall_migration.py").read_text(encoding="utf-8")
    assert "class _AsyncAnthropicMessagesCompat" in helper
    assert "async def create" in helper


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


def test_migrate_helper_fails_closed_without_gpucall_env(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")

    patch_suggestions(project, source="openai-app", apply=True)
    helper = (project / "gpucall_migration.py").read_text(encoding="utf-8")

    assert '_required_env("GPUCALL_BASE_URL")' in helper
    assert '_required_env("GPUCALL_API_KEY")' in helper
    assert 'os.environ.get("GPUCALL_API_KEY", "gpucall")' not in helper
    assert 'os.environ.get("GPUCALL_BASE_URL", "http://127.0.0.1:18088")' not in helper


def test_migrate_trace_parses_news_class_metrics_without_raw_log(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    log = tmp_path / "baseline.log"
    log.write_text(
        "\n".join(
            [
                "[input-compress] candidates=136 selected=56 cap=56 chars=11674 est_tokens=2918",
                "response_len=40461",
                "source count: 14",
                "Analysis complete: 15 topics ranked",
                '{"schema_success": true, "articles": [{"rank": 1}, {"rank": 2}]}',
            ]
        ),
        encoding="utf-8",
    )

    trace = trace_project(project, log_file=log, source="fixture", backend="anthropic")

    assert trace["metrics"]["response_chars"] == 40461
    assert trace["metrics"]["topics_count"] == 15
    assert trace["metrics"]["source_count"] == 14
    assert trace["metrics"]["articles_count"] == 2
    assert trace["metrics"]["schema_success"] is True
    assert trace["log_fingerprint"]["raw_forwarded"] is False
    assert "response_len=40461" not in json.dumps(trace)


def test_migrate_trace_parses_pretty_phase_json_metrics(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    phase = tmp_path / "phase2_analysis.json"
    phase.write_text(
        json.dumps(
            {
                "analysis": {
                    "rankings": [
                        {"topic": "redacted", "source_articles": [{"paper": "a"}, {"paper": "b"}]},
                        {"topic": "redacted", "source_articles": [{"paper": "c"}]},
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    trace = trace_project(project, log_file=phase, source="fixture", backend="baseline")

    assert trace["metrics"]["topics_count"] == 2
    assert trace["metrics"]["source_count"] == 2


def test_migrate_profile_and_contract_generate_deterministic_quality_metrics(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "topic_engine.py").write_text("def run():\n    call_llm('rank topics')\n", encoding="utf-8")
    (project / "overseas_vision.py").write_text("def run():\n    analyze_vision_image()\n", encoding="utf-8")
    trace_path = tmp_path / "trace.json"
    trace = trace_project(
        project,
        log_file=_write_log(
            tmp_path,
            "baseline.log",
            "response_len=40461\nsource_count=14\nAnalysis complete: 15 topics ranked\nschema_success=true\n",
        ),
        source="fixture",
        backend="baseline",
    )
    trace_path.write_text(json.dumps(trace), encoding="utf-8")

    profile = profile_project(project, trace_paths=[trace_path], source="fixture")
    contract = draft_contract_project(project, profile_path=None, trace_paths=[trace_path], source="fixture")

    assert profile["phase"] == "workload-profile"
    ranking = next(item for item in contract["workloads"] if item["intent"] == "rank_text_items")
    metrics = ranking["quality_contract"]["metrics"]
    assert metrics["min_topics"] == 12
    assert metrics["min_sources"] == 11
    assert metrics["min_response_chars"] == 20230
    assert ranking["quality_contract"]["gateway_may_infer_quality"] is False
    assert contract["submission"]["raw_output_forwarded"] is False


def test_migrate_contract_models_rss_match_as_materializable_intent(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "rss_match.py").write_text("def run():\n    call_llm('semantic RSS match')\n", encoding="utf-8")
    trace = trace_project(
        project,
        log_file=_write_log(tmp_path, "baseline.log", "[OverseasVision/RSSマッチ] 全体: 42/63 (66%) マッチ成功\nresponse_len=1200\n"),
        source="fixture",
        backend="baseline",
    )

    profile = profile_project(project, trace_paths=[_write_json(tmp_path, "trace.json", trace)], source="fixture")
    contract = draft_contract_project(project, profile_path=_write_json(tmp_path, "profile.json", profile), source="fixture")
    workload = next(item for item in contract["workloads"] if item["intent"] == "rss_semantic_match")
    intake = contract_to_recipe_intake(contract, workload_id=workload["id"])

    assert workload["input_profile"]["context_budget_tokens"] == 131072
    assert workload["quality_contract"]["metrics"]["min_rss_matches"] == 33
    assert intake["sanitized_request"]["intent"] == "rss_semantic_match"
    assert intake["sanitized_request"]["draft_grammar"]["materialization_allowed"] is True


def test_migrate_compare_detects_low_quality_success_as_contract_violation(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "topic_engine.py").write_text("call_llm('rank topics')\n", encoding="utf-8")
    baseline = trace_project(
        project,
        log_file=_write_log(tmp_path, "baseline.log", "response_len=40461\nsource_count=14\nAnalysis complete: 15 topics ranked\n"),
        source="fixture",
    )
    candidate = trace_project(
        project,
        log_file=_write_log(tmp_path, "candidate.log", "response_len=3997\nsource_count=8\nAnalysis complete: 8 topics ranked\n"),
        source="fixture",
        backend="gpucall",
    )
    profile = profile_project(project, trace_paths=[_write_json(tmp_path, "baseline.json", baseline)], source="fixture")
    contract = draft_contract_project(project, profile_path=_write_json(tmp_path, "profile.json", profile), source="fixture")

    comparison = compare_trace_to_contract(contract, candidate)

    assert comparison["ok"] is False
    fields = {item["metric"] for item in comparison["violations"]}
    assert {"response_chars", "topics_count", "source_count"}.issubset(fields)
    assert comparison["caller_action"] == "submit_contract_feedback_to_gpucall_admin"


def test_migrate_compare_rejects_recipe_routing_failures_even_when_exit_zero(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "rss_match.py").write_text("call_llm('semantic RSS match')\n", encoding="utf-8")
    baseline = trace_project(
        project,
        log_file=_write_log(tmp_path, "baseline.log", "[OverseasVision/RSSマッチ] 全体: 42/63 (66%) マッチ成功\nresponse_len=1200\n"),
        source="fixture",
    )
    candidate = trace_project(
        project,
        log_file=_write_log(
            tmp_path,
            "candidate.log",
            '[OverseasVision/RSSマッチ] 全体: 22/63 (35%) マッチ成功\n/v2/tasks/sync "HTTP/1.1 422 Unprocessable Entity"\nNO_AUTO_SELECTABLE_RECIPE\n',
        ),
        source="fixture",
        backend="gpucall",
    )
    profile = profile_project(project, trace_paths=[_write_json(tmp_path, "baseline.json", baseline)], source="fixture")
    contract = draft_contract_project(project, profile_path=_write_json(tmp_path, "profile.json", profile), source="fixture")

    comparison = compare_trace_to_contract(contract, candidate)

    assert comparison["ok"] is False
    fields = {item["metric"] for item in comparison["violations"]}
    assert {"rss_match_matched", "no_auto_selectable_recipe_count", "http_422_count"}.issubset(fields)


def test_migrate_compare_cli_can_merge_multiple_candidate_traces(tmp_path) -> None:
    project = tmp_path / "project"
    out = tmp_path / "out"
    project.mkdir()
    contract = {
        "phase": "workload-contract",
        "workloads": [
            {
                "id": "infer.rank_text_items",
                "task": "infer",
                "intent": "rank_text_items",
                "quality_contract": {"metrics": {"min_response_chars": 1000, "min_topics": 2, "min_sources": 2}},
            }
        ],
    }
    contract_path = _write_json(tmp_path, "contract.json", contract)
    trace_a = _write_json(tmp_path, "trace-a.json", trace_project(project, log_file=_write_log(tmp_path, "a.log", "response_len=1200\nAnalysis complete: 2 topics ranked\n")))
    trace_b = _write_json(tmp_path, "trace-b.json", trace_project(project, log_file=_write_log(tmp_path, "b.log", '{"source_count": 2}\n')))

    assert (
        main(
            [
                "compare",
                str(project),
                "--contract",
                str(contract_path),
                "--trace",
                str(trace_a),
                "--trace",
                str(trace_b),
                "--output-dir",
                str(out),
            ]
        )
        == 0
    )

    comparison = json.loads((out / "contract-comparison.json").read_text(encoding="utf-8"))
    assert comparison["ok"] is True
    assert comparison["violations"] == []


def test_migrate_cli_onboard_writes_contract_and_recipe_intake(tmp_path) -> None:
    project = tmp_path / "project"
    output = tmp_path / "out"
    project.mkdir()
    (project / "topic_engine.py").write_text("call_llm('rank topics')\n", encoding="utf-8")

    assert (
        main(
            [
                "onboard",
                str(project),
                "--source",
                "fixture",
                "--output-dir",
                str(output),
                "--command",
                "printf response_len=40461\\\\nsource_count=14\\\\nAnalysis\\ complete:\\ 15\\ topics\\ ranked\\\\n",
            ]
        )
        == 0
    )

    contract = json.loads((output / "workload-contract.json").read_text(encoding="utf-8"))
    intake = json.loads((output / "recipe-intake.json").read_text(encoding="utf-8"))
    assert contract["phase"] == "workload-contract"
    assert intake["phase"] == "deterministic-contract-intake"
    assert intake["sanitized_request"]["intent"] == "rank_text_items"


def test_migrate_cli_onboard_accepts_existing_log_files(tmp_path) -> None:
    project = tmp_path / "project"
    output = tmp_path / "out"
    project.mkdir()
    (project / "topic_engine.py").write_text("call_llm('rank topics')\n", encoding="utf-8")
    log = _write_log(tmp_path, "baseline.log", "response_len=40461\nAnalysis complete: 15 topics ranked\n")
    phase = _write_log(tmp_path, "phase.json", '{"source_count": 9}\n')

    assert (
        main(
            [
                "onboard",
                str(project),
                "--source",
                "fixture",
                "--output-dir",
                str(output),
                "--log-file",
                str(log),
                "--log-file",
                str(phase),
            ]
        )
        == 0
    )

    contract = json.loads((output / "workload-contract.json").read_text(encoding="utf-8"))
    ranking = next(item for item in contract["workloads"] if item["intent"] == "rank_text_items")
    assert ranking["quality_contract"]["metrics"]["min_topics"] == 12
    assert ranking["quality_contract"]["metrics"]["min_sources"] == 7


def test_contract_to_recipe_intake_preserves_contract_metadata() -> None:
    contract = {
        "phase": "workload-contract",
        "primary_workload_id": "infer.rank_text_items",
        "workloads": [
            {
                "id": "infer.rank_text_items",
                "task": "infer",
                "intent": "rank_text_items",
                "classification": "confidential",
                "modes": ["async"],
                "input_profile": {"content_types": ["text/plain"], "max_bytes": 16000, "input_count": 1, "context_budget_tokens": 131072},
                "output_profile": {"output_contract": "json_object"},
                "quality_contract": {"metrics": {"min_topics": 12}, "gateway_may_infer_quality": False},
            }
        ],
    }

    intake = contract_to_recipe_intake(contract)

    assert intake["sanitized_request"]["task"] == "infer"
    assert intake["sanitized_request"]["mode"] == "async"
    assert intake["sanitized_request"]["intent"] == "rank_text_items"
    assert intake["sanitized_request"]["quality_contract"]["metrics"]["min_topics"] == 12


def test_contract_to_recipe_intake_marks_unknown_workload_non_materializable() -> None:
    contract = {
        "phase": "workload-contract",
        "primary_workload_id": "infer.unknown",
        "workloads": [
            {
                "id": "infer.unknown",
                "task": "infer",
                "intent": "unknown_workload_deadbeef",
                "classification": "confidential",
                "modes": ["async"],
                "input_profile": {"content_types": ["text/plain"], "context_budget_tokens": 131072},
                "output_profile": {"output_contract": "plain_text"},
                "quality_contract": {"missing_baseline_metrics": True, "metrics": {}},
            }
        ],
    }

    intake = contract_to_recipe_intake(contract)

    assert intake["sanitized_request"]["intent"].startswith("unknown_workload_")
    assert intake["sanitized_request"]["draft_grammar"]["materialization_allowed"] is False
    assert any("operator intent mapping" in item for item in intake["sanitized_request"]["draft_grammar"]["blockers"])


def _write_log(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(tmp_path, name: str, payload: dict):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
