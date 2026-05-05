from __future__ import annotations

import json

from gpucall_recipe_draft.cli import main
from gpucall_recipe_draft.core import DraftInputs, draft_from_intake, intake_from_error


def test_intake_redacts_sensitive_payload_and_keeps_metadata() -> None:
    error = {
        "detail": "no auto-selectable recipe for task 'vision': vision-image-standard: required capability is missing",
        "code": "NO_AUTO_SELECTABLE_RECIPE",
        "context": {
            "task": "vision",
            "mode": "sync",
            "required_model_len": 9000,
            "largest_auto_recipe_model_len": 512,
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


def test_draft_uses_sanitized_intake_only() -> None:
    intake = {
        "sanitized_request": {
            "task": "vision",
            "mode": "sync",
            "intent": "understand_document_image",
            "classification": "confidential",
            "expected_output": "plain_text",
            "desired_capabilities": ["document_understanding", "visual_question_answering", "instruction_following"],
            "error": {"context": {"required_model_len": 9000}},
        }
    }

    draft = draft_from_intake(intake)

    assert draft["human_review_required"] is True
    assert draft["source"] == "sanitized_request_only"
    assert draft["proposed_recipe"]["name"] == "vision-understand-document-image-draft"
    assert draft["proposed_recipe"]["max_model_len"] == 32768
    assert draft["proposed_recipe"]["min_vram_gb"] == 80
    assert draft["provider_requirements"]["input_contracts"] == ["image", "data_refs", "text"]


def test_recipe_draft_cli_intake_and_draft(tmp_path, capsys) -> None:
    error_path = tmp_path / "error.json"
    intake_path = tmp_path / "intake.json"
    error_path.write_text(
        json.dumps(
            {
                "detail": "no auto-selectable recipe for task 'infer': text-infer-standard: required model length 40000 exceeds max_model_len 32768",
                "context": {"task": "infer", "mode": "sync", "required_model_len": 40000},
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
    assert output["proposed_recipe"]["max_model_len"] == 65536
