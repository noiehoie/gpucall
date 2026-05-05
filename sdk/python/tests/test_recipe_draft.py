from __future__ import annotations

import json

import httpx

from gpucall_recipe_draft.cli import main
from gpucall_recipe_draft.core import DraftInputs, draft_from_intake, intake_from_error, llm_prompt_from_intake
from gpucall_recipe_draft.llm import LLMConfig, call_openai_compatible, draft_with_llm, load_llm_config, write_default_config


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


def test_llm_prompt_uses_sanitized_intake_only() -> None:
    intake = {
        "sanitized_request": {
            "task": "vision",
            "intent": "understand_document_image",
            "desired_capabilities": ["document_understanding"],
            "business_need": "画像の内容に関する質問に答えたい",
        },
        "redacted_error_payload": {
            "inline_inputs": {"prompt": {"redacted": True, "type": "object", "keys": ["value"]}},
            "input_refs": [{"uri": {"redacted": True, "type": "str", "utf8_bytes": 22}}],
        },
    }

    prompt = llm_prompt_from_intake(intake)

    assert "sanitized_request" in prompt
    assert "understand_document_image" in prompt
    assert "Do not infer from missing prompt text" in prompt
    assert "redacted_error_payload" not in prompt
    assert "s3://" not in prompt


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


def test_recipe_draft_cli_llm_prompt(tmp_path, capsys) -> None:
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(
        json.dumps({"sanitized_request": {"task": "vision", "intent": "answer_question_about_image"}}),
        encoding="utf-8",
    )

    assert main(["llm-prompt", "--input", str(intake_path)]) == 0
    output = capsys.readouterr().out

    assert "answer_question_about_image" in output
    assert "Sanitized gpucall request metadata" in output


def test_llm_config_template_contains_no_secret(tmp_path) -> None:
    path = write_default_config(tmp_path / "recipe-draft.json")
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["provider"] == "openai-compatible"
    assert "api_key" not in data
    assert data["api_key_env"] is None
    assert load_llm_config(path).base_url == data["base_url"]


def test_openai_compatible_call_uses_user_config_and_env_key(monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"ok\": true}"}}]})

    monkeypatch.setenv("DRAFT_API_KEY", "secret-key")
    config = LLMConfig(
        provider="openai-compatible",
        base_url="https://llm.example/v1",
        model="draft-model",
        api_key_env="DRAFT_API_KEY",
    )

    text = call_openai_compatible(config, "safe prompt", transport=httpx.MockTransport(handler))

    assert text == "{\"ok\": true}"
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["payload"]["model"] == "draft-model"
    assert seen["payload"]["messages"][1]["content"] == "safe prompt"


def test_draft_with_llm_returns_review_artifact() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"proposed_recipe\": {\"name\": \"x\"}}"}}]})

    result = draft_with_llm(
        {"sanitized_request": {"task": "infer", "intent": "summarize_text"}},
        LLMConfig(provider="openai-compatible", base_url="http://local/v1", model="local-model"),
        transport=httpx.MockTransport(handler),
    )

    assert result["phase"] == "llm-draft"
    assert result["source"] == "sanitized_request_only"
    assert result["human_review_required"] is True
    assert result["parsed_json"] == {"proposed_recipe": {"name": "x"}}
