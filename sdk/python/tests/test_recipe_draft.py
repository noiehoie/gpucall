from __future__ import annotations

import json
from pathlib import Path

from gpucall_recipe_draft.cli import main
from gpucall_recipe_draft.core import (
    DraftInputs,
    PreflightInputs,
    QualityFeedbackInputs,
    compare_preflight_to_failure,
    draft_from_intake,
    intake_from_error,
    intake_from_preflight,
    intake_from_quality_feedback,
)
from gpucall_recipe_draft.submit import build_submission_bundle, parse_remote_inbox, submit_bundle, submit_bundle_to_remote


def test_intake_redacts_sensitive_payload_and_keeps_metadata() -> None:
    error = {
        "detail": "no auto-selectable recipe for task 'vision': vision-image-standard: required capability is missing",
        "code": "NO_AUTO_SELECTABLE_RECIPE",
        "context": {
            "task": "vision",
            "mode": "sync",
            "context_budget_tokens": 9000,
            "largest_auto_recipe_context_budget_tokens": 512,
            "rejections": ["vision-image-standard: required capability is missing"],
        },
        "input_refs": [
            {
                "uri": "s3://secret-bucket/private.png",
                "content_type": "image/png",
                "bytes": 123456,
                "sha256": "a" * 64,
            }
        ],
        "inline_inputs": {"prompt": {"value": "secret prompt", "content_type": "text/plain"}},
        "upload_url": "https://example.com/upload?X-Amz-Signature=secret",
    }

    intake = intake_from_error(
        DraftInputs(
            error_payload=error,
            intent="understand_document_image",
            business_need="画像の内容に関する質問に答えたい",
            classification="confidential",
        )
    )

    sanitized = intake["sanitized_request"]
    assert sanitized["task"] == "vision"
    assert sanitized["desired_capabilities"] == [
        "document_understanding",
        "visual_question_answering",
        "instruction_following",
    ]
    assert sanitized["input_summary"]["content_types"] == ["image/png", "text/plain"]
    assert sanitized["input_summary"]["max_bytes"] == 123456
    assert intake["redaction_report"]["prompt_body_forwarded"] is False
    assert intake["redaction_report"]["data_ref_uri_forwarded"] is False
    assert intake["redacted_error_payload"]["input_refs"][0]["uri"]["redacted"] is True
    assert intake["redacted_error_payload"]["inline_inputs"]["prompt"]["redacted"] is True
    assert "value" in intake["redacted_error_payload"]["inline_inputs"]["prompt"]["keys"]
    assert intake["redacted_error_payload"]["upload_url"]["redacted"] is True


def test_intake_from_gateway_failure_artifact_prefers_safe_summary() -> None:
    intake = intake_from_error(
        DraftInputs(
            error_payload={
                "detail": "no auto-selectable recipe for task 'vision'",
                "code": "NO_AUTO_SELECTABLE_RECIPE",
                "failure_artifact": {
                    "failure_id": "gf-test",
                    "failure_kind": "no_recipe",
                    "caller_action": "run_gpucall_recipe_draft_intake",
                    "capability_gap": "unsupported_content_type",
                    "safe_request_summary": {
                        "task": "vision",
                        "mode": "sync",
                        "classification": "confidential",
                        "input_ref_count": 1,
                        "input_ref_content_types": ["image/png"],
                        "input_ref_max_bytes": 12345,
                    },
                    "rejection_matrix": {"recipes": {"vision-image-standard": "content_type 'image/png' is not allowed"}},
                    "redaction_guarantee": {"data_ref_uri_included": False},
                },
            },
            intent="understand_document_image",
        )
    )

    sanitized = intake["sanitized_request"]
    assert sanitized["task"] == "vision"
    assert sanitized["mode"] == "sync"
    assert sanitized["input_summary"]["content_types"] == ["image/png"]
    assert sanitized["input_summary"]["max_bytes"] == 12345
    assert sanitized["error"]["failure_id"] == "gf-test"
    assert sanitized["error"]["capability_gap"] == "unsupported_content_type"
    assert sanitized["error"]["rejections"] == ["vision-image-standard: content_type 'image/png' is not allowed"]


def test_draft_uses_sanitized_intake_only() -> None:
    intake = {
        "sanitized_request": {
            "task": "vision",
            "mode": "sync",
            "intent": "understand_document_image",
            "classification": "confidential",
            "expected_output": "plain_text",
            "desired_capabilities": ["document_understanding", "visual_question_answering", "instruction_following"],
            "error": {"context": {"context_budget_tokens": 9000}},
        }
    }

    draft = draft_from_intake(intake)

    assert draft["human_review_required"] is True
    assert draft["source"] == "sanitized_request_only"
    assert draft["proposed_recipe"]["name"] == "vision-understand-document-image-draft"
    assert draft["proposed_recipe"]["recipe_schema_version"] == 3
    assert draft["proposed_recipe"]["context_budget_tokens"] == 32768
    assert draft["proposed_recipe"]["resource_class"] == "document_vision"
    assert draft["workload_contract"]["input_contracts"] == ["image", "data_refs", "text"]


def test_draft_does_not_select_provider_model_gpu_runtime_tuple_or_fallback() -> None:
    intake = {
        "sanitized_request": {
            "task": "infer",
            "mode": "sync",
            "intent": "summarize_text",
            "classification": "confidential",
            "desired_capabilities": ["summarization"],
            "runtime_selection": {
                "provider": "modal",
                "model": "qwen2.5",
                "gpu": "h100",
                "runtime": "vllm",
                "tuple": "modal-h100",
                "fallback": ["runpod-h100"],
            },
            "error": {
                "context": {
                    "provider": "modal",
                    "model": "qwen2.5",
                    "gpu": "h100",
                    "requested_tuple": "modal-h100",
                    "fallback": ["runpod-h100"],
                }
            },
        }
    }

    draft = draft_from_intake(intake)
    serialized = json.dumps(draft, sort_keys=True)

    assert draft["source"] == "sanitized_request_only"
    assert draft["proposed_recipe"]["name"] == "infer-summarize-text-draft"
    assert draft["proposed_recipe"]["auto_select"] is False
    for forbidden_key in ("runtime_selection", "provider", "model", "gpu", "runtime", "tuple", "requested_tuple", "fallback"):
        assert forbidden_key not in draft["proposed_recipe"]
        assert forbidden_key not in draft["workload_contract"]
    for forbidden_value in ("modal", "qwen2.5", "h100", "runpod", "vllm"):
        assert forbidden_value not in serialized


def test_recipe_draft_cli_intake_and_draft(tmp_path, capsys) -> None:
    error_path = tmp_path / "error.json"
    intake_path = tmp_path / "intake.json"
    error_path.write_text(
        json.dumps(
            {
                "detail": "no auto-selectable recipe for task 'infer': text-infer-standard: required context budget 40000 exceeds 32768",
                "context": {"task": "infer", "mode": "sync", "context_budget_tokens": 40000},
                "inline_inputs": {"prompt": {"value": "secret text", "content_type": "text/plain"}},
            }
        ),
        encoding="utf-8",
    )

    assert main(["intake", "--error", str(error_path), "--intent", "summarize_text", "--output", str(intake_path)]) == 0
    assert main(["draft", "--input", str(intake_path)]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["proposed_recipe"]["task"] == "infer"
    assert output["proposed_recipe"]["required_model_capabilities"] == ["summarization"]
    assert output["proposed_recipe"]["context_budget_tokens"] == 65536


def test_submit_writes_file_based_bundle(tmp_path) -> None:
    bundle = build_submission_bundle(
        intake={"phase": "deterministic-intake", "sanitized_request": {"task": "infer"}},
        draft={"phase": "draft", "proposed_recipe": {"name": "infer-draft", "task": "infer"}},
        source="example-caller-app",
    )

    path = submit_bundle(bundle, tmp_path / "inbox")
    data = json.loads(path.read_text(encoding="utf-8"))

    assert path.name.startswith("rr-")
    assert data["kind"] == "gpucall.recipe_request_submission"
    assert data["source"] == "example-caller-app"
    assert data["intake"]["sanitized_request"]["task"] == "infer"


def test_recipe_draft_cli_submit(tmp_path, capsys) -> None:
    intake = tmp_path / "intake.json"
    draft = tmp_path / "draft.json"
    inbox = tmp_path / "inbox"
    intake.write_text(json.dumps({"phase": "deterministic-intake", "sanitized_request": {"task": "infer"}}), encoding="utf-8")
    draft.write_text(json.dumps({"phase": "draft", "proposed_recipe": {"name": "infer-draft", "task": "infer"}}), encoding="utf-8")

    assert main(["submit", "--intake", str(intake), "--draft", str(draft), "--inbox-dir", str(inbox), "--source", "caller"]) == 0
    output_path = capsys.readouterr().out.strip()

    assert output_path
    assert Path(output_path).exists()
    assert json.loads(Path(output_path).read_text(encoding="utf-8"))["source"] == "caller"


def test_recipe_draft_cli_recipe_status_reads_report(tmp_path, capsys) -> None:
    request_id = "rr-test-recipe"
    reports = tmp_path / "inbox" / "reports"
    reports.mkdir(parents=True)
    (reports / f"{request_id}.report.json").write_text(
        json.dumps(
            {
                "phase": "recipe-materialization",
                "decision": "READY_FOR_VALIDATION",
                "task": "infer",
                "intent": "summarize_text",
                "next_actions": ["run validation"],
                "warnings": [{"check": "tuple_fit", "reason": "validation required", "secret": "drop"}],
            }
        ),
        encoding="utf-8",
    )

    assert main(["status", "--pipeline", "recipe", "--request-id", request_id, "--inbox-dir", str(tmp_path / "inbox")]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output == {
        "pipeline": "recipe",
        "request_id": request_id,
        "status": "processed",
        "report_available": True,
        "decision": "READY_FOR_VALIDATION",
        "task": "infer",
        "intent": "summarize_text",
        "phase": "recipe-materialization",
        "next_actions": ["run validation"],
        "warnings": [{"check": "tuple_fit", "reason": "validation required"}],
    }


def test_recipe_draft_cli_quality_status_reads_report(tmp_path, capsys) -> None:
    request_id = "rr-test-quality"
    reports = tmp_path / "quality" / "reports"
    reports.mkdir(parents=True)
    (reports / f"{request_id}.report.json").write_text(
        json.dumps(
            {
                "phase": "quality-feedback-review",
                "decision": "ACCEPT",
                "task": "vision",
                "intent": "understand_document_image",
                "quality_feedback": {"kind": "schema_noncompliance", "reason": "redacted summary only"},
                "observed": {"tuple": "modal-h100-qwen25-vl-3b", "tuple_model": "qwen25-vl-3b"},
                "next_actions": ["review structured-output schema adherence for the observed tuple"],
            }
        ),
        encoding="utf-8",
    )

    assert main(["quality-status", "--request-id", request_id, "--quality-inbox-dir", str(tmp_path / "quality")]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["pipeline"] == "quality"
    assert output["request_id"] == request_id
    assert output["status"] == "processed"
    assert output["decision"] == "ACCEPT"
    assert output["quality_kind"] == "schema_noncompliance"
    assert output["observed_tuple"] == "modal-h100-qwen25-vl-3b"
    assert output["observed_tuple_model"] == "qwen25-vl-3b"


def test_recipe_draft_cli_status_reads_remote_quality_inbox(monkeypatch, capsys) -> None:
    calls = []

    def fake_status(*, request_id, remote_inbox, pipeline):
        calls.append({"request_id": request_id, "remote_inbox": remote_inbox, "pipeline": pipeline})
        return {"pipeline": pipeline, "request_id": request_id, "status": "processed", "report_available": True}

    monkeypatch.setattr("gpucall_recipe_draft.cli.get_remote_submission_status", fake_status)

    assert (
        main(
            [
                "status",
                "--pipeline",
                "quality",
                "--request-id",
                "rr-test",
                "--remote-quality-inbox",
                "operator@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert calls == [
        {
            "request_id": "rr-test",
            "remote_inbox": "operator@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox",
            "pipeline": "quality",
        }
    ]
    assert output["status"] == "processed"


def test_parse_remote_inbox_requires_absolute_path() -> None:
    target = parse_remote_inbox("operator@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox")

    assert target.host == "operator@gateway.example.internal"
    assert target.inbox_dir == "/opt/gpucall/state/recipe_requests/inbox"


def test_submit_bundle_to_remote_uses_ssh_atomic_write(monkeypatch) -> None:
    calls = []

    def fake_run(args, *, input, stdout, stderr, check):
        calls.append(
            {
                "args": args,
                "input": input.decode("utf-8"),
                "stdout": stdout,
                "stderr": stderr,
                "check": check,
            }
        )

    monkeypatch.setattr("gpucall_recipe_draft.submit.subprocess.run", fake_run)
    bundle = build_submission_bundle(
        intake={"phase": "deterministic-intake", "sanitized_request": {"task": "infer"}},
        source="example-caller-app",
    )
    request_id = bundle["request_id"]

    result = submit_bundle_to_remote(bundle, "operator@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox")

    assert result == f"operator@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox/{request_id}.json"
    assert calls[0]["args"][0:2] == ["ssh", "operator@gateway.example.internal"]
    assert "mkdir -p" in calls[0]["args"][2]
    assert f".{request_id}.tmp" in calls[0]["args"][2]
    assert f"{request_id}.json" in calls[0]["args"][2]
    assert "chmod 0644" in calls[0]["args"][2]
    assert json.loads(calls[0]["input"])["source"] == "example-caller-app"


def test_recipe_draft_cli_quality_can_submit_to_remote(monkeypatch, capsys) -> None:
    submitted = []

    def fake_submit(bundle, remote_inbox):
        submitted.append({"bundle": bundle, "remote_inbox": remote_inbox})
        return f"{remote_inbox}/rr-test.json"

    monkeypatch.setattr("gpucall_recipe_draft.cli.submit_bundle_to_remote", fake_submit)

    assert (
        main(
            [
                "quality",
                "--task",
                "vision",
                "--intent",
                "understand_document_image",
                "--content-type",
                "image/jpeg",
                "--bytes",
                "1136521",
                "--quality-failure-kind",
                "insufficient_ocr",
                "--remote-inbox",
                "operator@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox",
                "--source",
                "example-caller-app",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert submitted[0]["remote_inbox"] == "operator@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox"
    assert submitted[0]["bundle"]["source"] == "example-caller-app"
    assert submitted[0]["bundle"]["intake"]["phase"] == "deterministic-quality-feedback-intake"
    assert "operator@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox/rr-test.json" in captured.err


def test_recipe_draft_cli_quality_prefers_remote_quality_inbox(monkeypatch, capsys) -> None:
    submitted = []

    def fake_submit(bundle, remote_inbox):
        submitted.append({"bundle": bundle, "remote_inbox": remote_inbox})
        return f"{remote_inbox}/rr-quality.json"

    monkeypatch.setattr("gpucall_recipe_draft.cli.submit_bundle_to_remote", fake_submit)

    assert (
        main(
            [
                "quality",
                "--task",
                "vision",
                "--intent",
                "understand_document_image",
                "--quality-failure-kind",
                "schema_noncompliance",
                "--remote-inbox",
                "operator@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox",
                "--remote-quality-inbox",
                "operator@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox",
                "--source",
                "example-caller-app",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert submitted[0]["remote_inbox"] == "operator@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox"
    assert submitted[0]["bundle"]["intake"]["phase"] == "deterministic-quality-feedback-intake"
    assert "operator@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox/rr-quality.json" in captured.err


def test_preflight_intake_is_sanitized_metadata_only() -> None:
    intake = intake_from_preflight(
        PreflightInputs(
            task="vision",
            mode="sync",
            intent="understand_document_image",
            classification="confidential",
            content_types=("image/png",),
            byte_values=(123456,),
            context_budget_tokens=9000,
        )
    )

    sanitized = intake["sanitized_request"]
    assert intake["phase"] == "deterministic-preflight-intake"
    assert sanitized["task"] == "vision"
    assert sanitized["desired_capabilities"] == [
        "document_understanding",
        "visual_question_answering",
        "instruction_following",
    ]
    assert sanitized["input_summary"]["content_types"] == ["image/png"]
    assert sanitized["input_summary"]["max_bytes"] == 123456
    assert intake["redaction_report"]["prompt_body_forwarded"] is False


def test_quality_feedback_intake_is_sanitized_metadata_only() -> None:
    intake = intake_from_quality_feedback(
        QualityFeedbackInputs(
            task="vision",
            mode="sync",
            intent="understand_document_image",
            classification="confidential",
            expected_output="headline_list",
            content_types=("image/jpeg",),
            byte_values=(1136521,),
            dimensions=("1200x2287",),
            observed_recipe="vision-image-standard",
            observed_tuple="modal-vision-a10g",
            observed_tuple_model="Salesforce/blip-vqa-base",
            output_validated=None,
            quality_failure_kind="insufficient_ocr",
            quality_failure_reason="short answer only; expected top headlines, not raw page text",
            observed_output_kind="short_answer",
        )
    )

    sanitized = intake["sanitized_request"]
    assert intake["phase"] == "deterministic-quality-feedback-intake"
    assert sanitized["error"]["code"] == "LOW_QUALITY_SUCCESS"
    assert sanitized["error"]["capability_gap"] == "model_or_recipe_capability_mismatch"
    assert sanitized["runtime_selection"]["observed_tuple_model"] == "Salesforce/blip-vqa-base"
    assert sanitized["quality_feedback"]["kind"] == "insufficient_ocr"
    assert sanitized["input_summary"]["dimensions"] == ["1200x2287"]
    assert intake["redaction_report"]["prompt_body_forwarded"] is False
    assert intake["redaction_report"]["output_body_forwarded"] is False


def test_quality_feedback_carries_schema_mismatch_metadata_only() -> None:
    intake = intake_from_quality_feedback(
        QualityFeedbackInputs(
            task="vision",
            mode="sync",
            intent="understand_document_image",
            expected_output="articles_json",
            quality_failure_kind="schema_mismatch",
            quality_failure_reason="caller schema rejected output",
            observed_output_kind="json_object_wrong_schema",
            response_format="json_object",
            expected_json_schema={
                "type": "object",
                "required": ["articles"],
                "properties": {
                    "articles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["headline_original", "rank"],
                            "properties": {
                                "headline_original": {"type": "string", "description": "raw headline text must not be included here"},
                                "rank": {"type": "integer"},
                            },
                        },
                    }
                },
                "description": "must be dropped",
            },
            observed_json_schema={
                "type": "object",
                "required": ["contains_text", "dominant_color", "summary"],
                "properties": {"contains_text": {"type": "boolean"}, "summary": {"type": "string"}},
            },
            schema_success_count=5,
            schema_failure_count=16,
        )
    )

    feedback = intake["sanitized_request"]["quality_feedback"]
    contract = feedback["output_contract_feedback"]
    assert feedback["kind"] == "schema_mismatch"
    assert intake["sanitized_request"]["error"]["capability_gap"] == "output_contract_insufficient"
    assert contract["response_format"] == "json_object"
    assert contract["schema_success_count"] == 5
    assert contract["schema_failure_count"] == 16
    assert contract["expected_json_schema"]["required"] == ["articles"]
    assert "description" not in contract["expected_json_schema"]
    assert "description" not in contract["expected_json_schema"]["properties"]["articles"]["items"]["properties"]["headline_original"]
    assert contract["observed_json_schema"]["required"] == ["contains_text", "dominant_color", "summary"]
    assert contract["raw_output_forwarded"] is False


def test_recipe_draft_cli_quality_accepts_schema_files(tmp_path) -> None:
    expected = tmp_path / "expected.json"
    observed = tmp_path / "observed.json"
    output = tmp_path / "quality.json"
    expected.write_text(json.dumps({"type": "object", "required": ["articles"], "properties": {"articles": {"type": "array"}}}), encoding="utf-8")
    observed.write_text(json.dumps({"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}), encoding="utf-8")

    assert (
        main(
            [
                "quality",
                "--task",
                "vision",
                "--intent",
                "understand_document_image",
                "--quality-failure-kind",
                "schema_mismatch",
                "--response-format",
                "json_object",
                "--expected-json-schema",
                str(expected),
                "--observed-json-schema",
                str(observed),
                "--schema-success-count",
                "5",
                "--schema-failure-count",
                "16",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    contract = data["sanitized_request"]["quality_feedback"]["output_contract_feedback"]
    assert contract["expected_json_schema"]["required"] == ["articles"]
    assert contract["observed_json_schema"]["required"] == ["summary"]


def test_compare_preflight_to_failure_detects_workload_drift() -> None:
    preflight = intake_from_preflight(
        PreflightInputs(task="infer", intent="summarize_text", content_types=("text/plain",), context_budget_tokens=40000)
    )
    failure = intake_from_preflight(
        PreflightInputs(task="vision", intent="understand_document_image", content_types=("image/png",), context_budget_tokens=9000)
    )

    report = compare_preflight_to_failure(preflight, failure)

    assert report["preflight_matched_actual"] is False
    assert report["classification"] == "workload_drift"
    assert {item["field"] for item in report["differences"]} >= {"task", "content_types", "desired_capabilities"}


def test_compare_preflight_to_failure_matches() -> None:
    preflight = intake_from_preflight(
        PreflightInputs(task="infer", intent="summarize_text", content_types=("text/plain",), context_budget_tokens=40000)
    )
    failure = intake_from_preflight(
        PreflightInputs(task="infer", intent="summarize_text", content_types=("text/plain",), context_budget_tokens=40000)
    )

    report = compare_preflight_to_failure(preflight, failure)

    assert report["preflight_matched_actual"] is True
    assert report["classification"] == "preflight_matched_runtime_failure"
    assert report["differences"] == []


def test_preflight_normalizes_legacy_topic_ranking_intent() -> None:
    intake = intake_from_preflight(
        PreflightInputs(task="infer", intent="topic_ranking", content_types=("text/plain",), context_budget_tokens=46000)
    )
    draft = draft_from_intake(intake)

    assert intake["sanitized_request"]["intent"] == "rank_text_items"
    assert intake["sanitized_request"]["desired_capabilities"] == ["instruction_following", "reasoning"]
    assert draft["proposed_recipe"]["name"] == "infer-rank-text-items-draft"


def test_recipe_draft_cli_preflight_and_compare(tmp_path, capsys) -> None:
    preflight_path = tmp_path / "preflight.json"
    failure_path = tmp_path / "failure.json"
    assert (
        main(
            [
                "preflight",
                "--task",
                "infer",
                "--intent",
                "summarize_text",
                "--content-type",
                "text/plain",
                "--context-budget-tokens",
                "40000",
                "--output",
                str(preflight_path),
            ]
        )
        == 0
    )
    failure_path.write_text(
        json.dumps(
            {
                "sanitized_request": {
                    "task": "infer",
                    "mode": "sync",
                    "intent": "summarize_text",
                    "classification": "confidential",
                    "expected_output": "plain_text",
                    "desired_capabilities": ["summarization"],
                    "error": {"context": {"context_budget_tokens": 40000}},
                    "input_summary": {"content_types": ["text/plain"], "max_bytes": None},
                }
            }
        ),
        encoding="utf-8",
    )

    assert main(["compare", "--preflight", str(preflight_path), "--failure", str(failure_path)]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["preflight_matched_actual"] is True


def test_recipe_draft_cli_intake_can_auto_submit(tmp_path, capsys) -> None:
    error_path = tmp_path / "error.json"
    inbox = tmp_path / "inbox"
    error_path.write_text(
        json.dumps({"detail": "no auto-selectable recipe for task 'infer'", "context": {"task": "infer", "mode": "sync"}}),
        encoding="utf-8",
    )

    assert main(["intake", "--error", str(error_path), "--intent", "summarize_text", "--inbox-dir", str(inbox), "--source", "caller"]) == 0
    captured = capsys.readouterr()
    submitted = Path(captured.err.strip())

    assert submitted.exists()
    bundle = json.loads(submitted.read_text(encoding="utf-8"))
    assert bundle["source"] == "caller"
    assert bundle["intake"]["sanitized_request"]["intent"] == "summarize_text"


def test_recipe_draft_cli_quality_can_auto_submit(tmp_path, capsys) -> None:
    inbox = tmp_path / "inbox"

    assert (
        main(
            [
                "quality",
                "--task",
                "vision",
                "--intent",
                "understand_document_image",
                "--content-type",
                "image/jpeg",
                "--bytes",
                "1136521",
                "--dimension",
                "1200x2287",
                "--observed-recipe",
                "vision-image-standard",
                "--reported-tuple",
                "modal-vision-a10g",
                "--reported-tuple-model",
                "Salesforce/blip-vqa-base",
                "--quality-failure-kind",
                "insufficient_ocr",
                "--quality-failure-reason",
                "short answer only",
                "--inbox-dir",
                str(inbox),
                "--source",
                "example-caller-app",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    submitted = Path(captured.err.strip())

    assert submitted.exists()
    intake = json.loads(submitted.read_text(encoding="utf-8"))["intake"]
    assert intake["phase"] == "deterministic-quality-feedback-intake"
    assert intake["sanitized_request"]["quality_feedback"]["kind"] == "insufficient_ocr"
