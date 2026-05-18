from __future__ import annotations

from io import BytesIO
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
    assert "\nfrom openai import OpenAI" not in helper
    assert "\nfrom gpucall_sdk import GPUCallClient" not in helper
    exec(helper, {})


def test_migrate_patch_rewrites_local_anthropic_import_without_global_dependency(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "vision.py"
    source.write_text(
        "def run():\n"
        "    import anthropic\n"
        "    client = anthropic.Anthropic(api_key='x')\n"
        "    return client\n",
        encoding="utf-8",
    )

    report = patch_suggestions(project, source="vision-app", apply=True)

    assert "vision.py" in report["changed_files"]
    text = source.read_text(encoding="utf-8")
    assert "from gpucall_migration import AnthropicCompat, AsyncAnthropicCompat" in text
    assert "client = AnthropicCompat(api_key='x')" in text


def test_migrate_patch_rewrites_combined_anthropic_import(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text(
        "from anthropic import Anthropic, AsyncAnthropic\n"
        "a = Anthropic()\n"
        "b = AsyncAnthropic()\n",
        encoding="utf-8",
    )

    report = patch_suggestions(project, source="combined-app", apply=True)

    assert "client.py" in report["changed_files"]
    text = source.read_text(encoding="utf-8")
    assert "from gpucall_migration import AnthropicCompat as Anthropic, AsyncAnthropicCompat as AsyncAnthropic" in text
    assert "from anthropic import" not in text


def test_migrate_patch_does_not_touch_model_literal_only_files(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "settings.py"
    source.write_text("MODEL = 'claude-haiku-4-5-20251001'\n", encoding="utf-8")

    report = patch_suggestions(project, source="literal-app", apply=True)

    assert "settings.py" not in report["changed_files"]
    assert "gpucall_migration.py" not in report["changed_files"]
    assert source.read_text(encoding="utf-8") == "MODEL = 'claude-haiku-4-5-20251001'\n"


def test_migrate_patch_adds_auth_headers_to_openai_compatible_httpx(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "llm_client.py"
    source.write_text(
        "def call():\n"
        "    import httpx\n"
        "    try:\n"
        "        resp = httpx.post(\n"
        "            f'{endpoint}/v1/chat/completions',\n"
        "            json={'messages': []},\n"
        "            timeout=30,\n"
        "        )\n"
        "    except Exception as e:\n"
        "        logger.warning(\"Local LLM failed, falling back to Anthropic: %s\", e)\n"
        "        return _call_anthropic('x')\n",
        encoding="utf-8",
    )

    report = patch_suggestions(project, source="httpx-app", apply=True)

    assert "llm_client.py" in report["changed_files"]
    text = source.read_text(encoding="utf-8")
    assert "gpucall_openai_headers" in text
    assert "headers=gpucall_openai_headers()," in text
    assert "gpucall_disable_hosted_fallback(e)" in text


def test_migrate_patch_routes_text_and_vision_through_correct_helpers(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "llm_client.py"
    source.write_text(
        "from pathlib import Path\n"
        "import base64\n\n"
        "_LOCAL_ENDPOINT = 'http://gateway/v1'\n"
        "_LOCAL_MODEL = None\n\n"
        "def _call_local(user_message, system_prompt, model, timeout, max_tokens):\n"
        "    import httpx\n"
        "    effective_model = _LOCAL_MODEL or model or 'local-model'\n"
        "    messages: list[dict] = []\n"
        "    payload = {\n"
        "        \"model\": effective_model,\n"
        "        \"messages\": messages,\n"
        "    }\n"
        "    return httpx.post(f'{_LOCAL_ENDPOINT}/v1/chat/completions', json=payload, timeout=timeout)\n\n"
        "def _call_local_vision(user_message, image_path, system_prompt, model, timeout, max_tokens):\n"
        "    import httpx\n"
        "    path = Path(image_path)\n"
        "    img_b64 = base64.b64encode(path.read_bytes()).decode()\n"
        "    effective_model = _LOCAL_MODEL or model or 'local-model'\n"
        "    messages: list[dict] = []\n"
        "    payload = {\n"
        "        \"model\": effective_model,\n"
        "        \"messages\": messages,\n"
        "    }\n"
        "    return httpx.post(f'{_LOCAL_ENDPOINT}/v1/chat/completions', json=payload, timeout=timeout)\n",
        encoding="utf-8",
    )

    patch_suggestions(project, source="news-system", apply=True)

    text = source.read_text(encoding="utf-8")
    text_function = text.split("def _call_local_vision", 1)[0]
    vision_function = "def _call_local_vision" + text.split("def _call_local_vision", 1)[1]
    assert "gpucall_infer_text(" in text_function
    assert "gpucall_vision_file(" not in text_function
    assert "gpucall_vision_file(" in vision_function
    assert "            image_path,\n" in vision_function
    assert "            path,\n" not in vision_function


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
    assert "\nfrom openai import OpenAI" not in helper
    assert "\nfrom gpucall_sdk import GPUCallClient" not in helper


def test_migrate_helper_fallback_rejects_stream_and_omits_none_anthropic_text(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")

    patch_suggestions(project, source="openai-app", apply=True)
    namespace: dict[str, object] = {}
    helper = (project / "gpucall_migration.py").read_text(encoding="utf-8").replace(
        "from openai import OpenAI  # type: ignore",
        "raise ModuleNotFoundError('openai')",
    )
    exec(helper, namespace)

    client = namespace["gpucall_openai_client"]()
    try:
        client.chat.completions.create(messages=[{"role": "user", "content": "x"}], stream=True)
    except RuntimeError as exc:
        assert "stream=True" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("stream=True should fail closed in stdlib fallback")
    prompt = namespace["_anthropic_prompt"]([{"content": [{"type": "text", "text": None}, {"type": "text", "text": "ok"}]}])
    assert prompt == "ok"


def test_migrate_helper_stream_compat_returns_final_text(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")

    patch_suggestions(project, source="openai-app", apply=True)
    namespace: dict[str, object] = {}
    exec((project / "gpucall_migration.py").read_text(encoding="utf-8"), namespace)
    content = namespace["_AnthropicContent"]("ok")
    message = namespace["_AnthropicMessage"]([content])
    with namespace["_AnthropicStreamCompat"](message) as stream:
        assert stream.get_final_text() == "ok"


def test_migrate_helper_prefers_rank_over_rss_when_prompt_contains_both(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")

    patch_suggestions(project, source="openai-app", apply=True)
    namespace: dict[str, object] = {}
    exec((project / "gpucall_migration.py").read_text(encoding="utf-8"), namespace)

    intent = namespace["gpucall_guess_intent"](
        "Rank these topics by importance. RSS items are included as sources.",
        "Return a global news ranking.",
    )
    assert intent == "rank_text_items"


def test_migrate_helper_prefers_rss_match_contract_over_rank_terms(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")

    patch_suggestions(project, source="openai-app", apply=True)
    namespace: dict[str, object] = {}
    exec((project / "gpucall_migration.py").read_text(encoding="utf-8"), namespace)

    intent = namespace["gpucall_guess_intent"](
        'RSS記事とVision抽出記事をsemantic matchし、{"matches":[{"rss_id":"r1","vision_rank":1,"confidence":0.9}]} だけ返す。',
        "Rank fields may appear in source layout metadata; do not rank topics.",
    )
    assert intent == "rss_semantic_match"


def test_migrate_helper_routes_vision_and_large_text_through_async(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")

    patch_suggestions(project, source="openai-app", apply=True)
    namespace: dict[str, object] = {}
    exec((project / "gpucall_migration.py").read_text(encoding="utf-8"), namespace)

    captured: list[tuple[str, str, dict]] = []

    def fake_json_request(method, url, payload, **_kwargs):
        captured.append((method, url, dict(payload)))
        if url.endswith("/v2/objects/presign-put"):
            return {
                "upload_url": "http://object-store.invalid/upload",
                "data_ref": {"uri": "s3://bucket/prompt.txt", "sha256": "0" * 64, "bytes": payload["bytes"], "content_type": payload["content_type"]},
            }
        if url.endswith("/v2/tasks/async"):
            return {"job_id": "j1", "state": "QUEUED", "status_url": "/v2/jobs/j1"}
        if url.endswith("/v2/jobs/j1"):
            return {"job_id": "j1", "state": "COMPLETED", "result": {"kind": "inline", "value": "ok"}}
        raise AssertionError(url)

    namespace["_json_request"] = fake_json_request
    namespace["urllib"].request.urlopen = lambda *_args, **_kwargs: type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda self, *_: None})()
    namespace["time"].sleep = lambda _seconds: None
    namespace["gpucall_infer_text"]("x" * 70000, intent="rank_text_items", timeout=1)
    assert any(url.endswith("/v2/tasks/async") and payload["mode"] == "async" for _method, url, payload in captured)

    captured.clear()
    image = project / "front.jpg"
    image.write_bytes(b"fake")
    namespace["_upload_file"] = lambda *_args, **_kwargs: {
        "uri": "s3://bucket/key",
        "sha256": "0" * 64,
        "bytes": 4,
        "content_type": "image/jpeg",
    }
    namespace["gpucall_vision_file"](image, prompt="extract articles", timeout=1)
    assert any(url.endswith("/v2/tasks/async") and payload["mode"] == "async" for _method, url, payload in captured)


def test_migrate_helper_retries_gateway_rate_limit_without_fallback(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")
    monkeypatch.setenv("GPUCALL_MIGRATION_MIN_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("GPUCALL_MIGRATION_RATE_LIMIT_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("GPUCALL_MIGRATION_HTTP_RETRIES", "1")

    patch_suggestions(project, source="openai-app", apply=True)
    namespace: dict[str, object] = {}
    exec((project / "gpucall_migration.py").read_text(encoding="utf-8"), namespace)

    calls: list[str] = []
    sleeps: list[float] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b'{"result":{"kind":"inline","value":"ok"}}'

    def fake_urlopen(request, **_kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise namespace["urllib"].error.HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {},
                BytesIO(b'{"error":{"code":"rate_limit_exceeded"}}'),
            )
        return Response()

    namespace["urllib"].request.urlopen = fake_urlopen
    namespace["time"].sleep = lambda seconds: sleeps.append(seconds)

    assert namespace["gpucall_infer_text"]("small prompt", timeout=1) == "ok"
    assert len(calls) == 2
    assert sleeps == [0.0]


def test_migrate_helper_retries_temporary_no_eligible_without_fallback(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "client.py"
    source.write_text("from openai import OpenAI\nclient = OpenAI()\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")
    monkeypatch.setenv("GPUCALL_MIGRATION_MIN_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("GPUCALL_MIGRATION_NO_ELIGIBLE_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("GPUCALL_MIGRATION_NO_ELIGIBLE_RETRIES", "1")

    patch_suggestions(project, source="openai-app", apply=True)
    namespace: dict[str, object] = {}
    exec((project / "gpucall_migration.py").read_text(encoding="utf-8"), namespace)

    calls: list[str] = []
    sleeps: list[float] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b'{"result":{"kind":"inline","value":"ok"}}'

    def fake_urlopen(request, **_kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise namespace["urllib"].error.HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                {},
                BytesIO(b'{"code":"NO_ELIGIBLE_TUPLE","detail":"no eligible tuple after policy"}'),
            )
        return Response()

    namespace["urllib"].request.urlopen = fake_urlopen
    namespace["time"].sleep = lambda seconds: sleeps.append(seconds)

    assert namespace["gpucall_infer_text"]("small prompt", timeout=1) == "ok"
    assert len(calls) == 2
    assert sleeps == [0.0]


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
