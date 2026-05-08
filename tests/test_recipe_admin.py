from __future__ import annotations

import json
import shutil

import pytest
import yaml

import gpucall.recipe_admin as recipe_admin
from gpucall.recipe_admin import canonical_recipe_from_artifact, main, process_inbox, promote_production_tuple, recipe_request_status, review_artifact


def test_admin_materializes_intake_to_canonical_recipe() -> None:
    intake = {
        "phase": "deterministic-intake",
        "sanitized_request": {
            "task": "vision",
            "mode": "sync",
            "intent": "understand_document_image",
            "classification": "confidential",
            "expected_output": "plain_text",
            "desired_capabilities": ["document_understanding", "visual_question_answering", "instruction_following"],
            "error": {"context": {"context_budget_tokens": 9000}},
        },
        "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
    }

    recipe = canonical_recipe_from_artifact(intake)

    assert recipe["name"] == "vision-understand-document-image-draft"
    assert recipe["task"] == "vision"
    assert recipe["recipe_schema_version"] == 3
    assert recipe["resource_class"] == "document_vision"
    assert recipe["context_budget_tokens"] == 32768
    assert recipe["allowed_mime_prefixes"] == ["image/"]
    assert recipe["allowed_inline_mime_prefixes"] == ["text/"]
    assert recipe["required_model_capabilities"] == ["document_understanding", "visual_question_answering", "instruction_following"]


def test_admin_materialize_requires_accept_all(tmp_path) -> None:
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps({"sanitized_request": {"task": "infer"}}), encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing to materialize without --accept-all"):
        main(["materialize", "--input", str(intake_path), "--dry-run"])


def test_admin_materialize_writes_yaml_and_report(tmp_path) -> None:
    intake_path = tmp_path / "intake.json"
    output_dir = tmp_path / "recipes"
    report_path = tmp_path / "report.json"
    intake_path.write_text(
        json.dumps(
            {
                "sanitized_request": {
                    "task": "infer",
                    "mode": "sync",
                    "intent": "summarize_text",
                    "classification": "confidential",
                    "desired_capabilities": ["summarization"],
                    "error": {"context": {"context_budget_tokens": 40000}},
                },
                "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "materialize",
                "--input",
                str(intake_path),
                "--output-dir",
                str(output_dir),
                "--report",
                str(report_path),
                "--accept-all",
            ]
        )
        == 0
    )

    recipe_path = output_dir / "infer-summarize-text-draft.yml"
    recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert recipe["name"] == "infer-summarize-text-draft"
    assert recipe["recipe_schema_version"] == 3
    assert recipe["context_budget_tokens"] == 65536
    assert report["policy"] == "accept-all"
    assert report["human_review_bypassed"] is True


def test_admin_process_inbox_materializes_submission(tmp_path) -> None:
    inbox = tmp_path / "inbox"
    output_dir = tmp_path / "recipes"
    inbox.mkdir()
    submission = {
        "kind": "gpucall.recipe_request_submission",
        "request_id": "rr-test",
            "intake": {
                "phase": "deterministic-intake",
                "sanitized_request": {
                "task": "infer",
                "mode": "sync",
                "intent": "summarize_text",
                "classification": "confidential",
                "desired_capabilities": ["summarization"],
                    "error": {"context": {"context_budget_tokens": 40000}},
                },
                "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
            },
            "draft": None,
    }
    (inbox / "rr-test.json").write_text(json.dumps(submission), encoding="utf-8")

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, accept_all=True)

    assert results[0]["ok"] is True
    assert (output_dir / "infer-summarize-text-draft.yml").exists()
    assert (inbox / "processed" / "rr-test.json").exists()
    assert (inbox / "reports" / "rr-test.report.json").exists()
    status = recipe_request_status("rr-test", inbox)
    assert status["state"] == "processed"
    assert status["report"]["policy"] == "accept-all"
    assert status["report"]["admin_review"]["decision"] in {"CANDIDATE_ONLY", "READY_FOR_VALIDATION", "READY_FOR_PRODUCTION", "AUTO_SELECT_SAFE"}


def test_admin_cli_process_inbox_requires_accept_all(tmp_path) -> None:
    with pytest.raises(SystemExit, match="refusing to process inbox without --accept-all"):
        main(["process-inbox", "--inbox-dir", str(tmp_path / "inbox"), "--output-dir", str(tmp_path / "recipes")])


def test_admin_cli_process_inbox_allows_configured_auto_materialize(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text("recipe_inbox_auto_materialize: true\n", encoding="utf-8")
    inbox = tmp_path / "inbox"
    output_dir = tmp_path / "recipes"
    inbox.mkdir()
    (inbox / "rr-test.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-test",
                "intake": {
                    "sanitized_request": {"task": "infer", "intent": "summarize_text"},
                    "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
                },
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "process-inbox",
                "--inbox-dir",
                str(inbox),
                "--output-dir",
                str(output_dir),
                "--config-dir",
                str(config_dir),
            ]
        )
        == 0
    )

    assert (output_dir / "infer-summarize-text-draft.yml").exists()


def test_admin_process_inbox_can_auto_promote_from_config(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text(
        "\n".join(
            [
                "recipe_inbox_auto_materialize: true",
                "recipe_inbox_auto_promote: true",
                "recipe_inbox_auto_run_validation: true",
                "recipe_inbox_auto_activate: true",
                f"recipe_inbox_promotion_work_dir: {tmp_path / 'promotions'}",
                f"recipe_inbox_validation_dir: {tmp_path / 'validation'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    inbox = tmp_path / "inbox"
    output_dir = config_dir / "recipes"
    inbox.mkdir()
    (inbox / "rr-test.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-test",
                "intake": {
                    "sanitized_request": {"task": "infer", "intent": "summarize_text"},
                    "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_promote_production_tuple(**kwargs):
        calls.append(kwargs)
        return {"decision": "ACTIVATED", "activated": True}

    monkeypatch.setattr(recipe_admin, "promote_production_tuple", fake_promote_production_tuple)
    monkeypatch.setattr(
        recipe_admin,
        "_run_existing_tuple_validation",
        lambda *args, **kwargs: {"returncode": 0, "passed": True},
    )

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, config_dir=config_dir)

    assert results[0]["ok"] is True
    report = json.loads((inbox / "reports" / "rr-test.report.json").read_text(encoding="utf-8"))
    assert report["promotion"]["decision"] == "ACTIVATED"
    assert report["promotion"]["validation"] == {"returncode": 0, "passed": True}


def test_admin_cli_watch_one_iteration(tmp_path, capsys) -> None:
    inbox = tmp_path / "inbox"
    output_dir = tmp_path / "recipes"
    inbox.mkdir()
    (inbox / "rr-test.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-test",
                "intake": {
                    "sanitized_request": {"task": "infer", "intent": "summarize_text"},
                    "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
                },
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "watch",
                "--inbox-dir",
                str(inbox),
                "--output-dir",
                str(output_dir),
                "--accept-all",
                "--max-iterations",
                "1",
                "--interval-seconds",
                "0",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert output["processed"][0]["ok"] is True


def test_admin_cli_status(tmp_path, capsys) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "rr-pending.json").write_text(json.dumps({"request_id": "rr-pending"}), encoding="utf-8")

    assert main(["status", "--request-id", "rr-pending", "--inbox-dir", str(inbox)]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["state"] == "pending"


def test_admin_review_rejects_missing_redaction_report() -> None:
    report = review_artifact({"sanitized_request": {"task": "infer", "intent": "summarize_text"}})

    assert report["decision"] == "REJECT"
    assert report["blockers"][0]["check"] == "redaction_report"


def test_admin_review_outputs_provider_contract_when_existing_providers_are_insufficient(tmp_path) -> None:
    artifact = {
        "phase": "deterministic-quality-feedback-intake",
        "sanitized_request": {
            "task": "vision",
            "mode": "sync",
            "intent": "understand_document_image",
            "classification": "confidential",
            "expected_output": "headline_list",
            "desired_capabilities": ["document_understanding", "visual_question_answering", "instruction_following"],
            "quality_feedback": {"kind": "insufficient_ocr", "observed_output_kind": "short_answer"},
        },
        "redaction_report": {
            "prompt_body_forwarded": False,
            "output_body_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
        },
    }

    report = review_artifact(artifact, config_dir="gpucall/config_templates")

    assert report["decision"] == "CANDIDATE_ONLY"
    assert report["required_execution_contract"]["model_capabilities"] == [
        "document_understanding",
        "visual_question_answering",
        "instruction_following",
    ]
    assert report["required_execution_contract"]["live_validation_required"] is True
    assert report["required_execution_contract"]["quality_failure_to_correct"]["kind"] == "insufficient_ocr"
    assert any(match["name"] == "modal-h100-qwen25-vl-7b" for match in report["tuple_candidate_matches"])
    assert report["tuple_candidate_matches"] == report["tuple_candidate_matches"]
    assert all(match["eligible"] is True for match in report["tuple_candidate_matches"])
    assert all(match["execution_surface"] == "function_runtime" for match in report["tuple_candidate_matches"])


def test_admin_review_matches_long_context_tuple_candidates() -> None:
    artifact = {
        "phase": "deterministic-intake",
        "sanitized_request": {
            "task": "infer",
            "mode": "sync",
            "intent": "summarize_text",
            "classification": "confidential",
            "expected_output": "plain_text",
            "desired_capabilities": ["summarization"],
            "error": {"context": {"context_budget_tokens": 938000}},
        },
        "redaction_report": {
            "prompt_body_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
        },
    }

    report = review_artifact(artifact, config_dir="gpucall/config_templates")

    assert report["decision"] == "CANDIDATE_ONLY"
    assert report["required_execution_contract"]["min_model_len"] == 1010000
    assert report["required_execution_contract"]["min_vram_gb"] == 320
    names = {match["name"] for match in report["tuple_candidate_matches"]}
    assert "modal-h200x4-qwen25-14b-1m" in names
    assert "modal-h200-qwen25-14b-1m" not in names
    assert "runpod-vllm-h200-qwen25-14b-1m" not in names
    assert all("run gpucall tuple-smoke" in " ".join(match["promotion_actions"]) for match in report["tuple_candidate_matches"])


def test_promote_production_tuple_writes_isolated_config_without_activation(tmp_path) -> None:
    artifact = {
        "phase": "deterministic-quality-feedback-intake",
        "sanitized_request": {
            "task": "vision",
            "mode": "sync",
            "intent": "understand_document_image",
            "classification": "confidential",
            "expected_output": "headline_list",
            "desired_capabilities": ["document_understanding", "visual_question_answering", "instruction_following"],
            "quality_feedback": {"kind": "insufficient_ocr", "observed_output_kind": "short_answer"},
        },
        "redaction_report": {
            "prompt_body_forwarded": False,
            "output_body_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
        },
    }
    review = review_artifact(artifact, config_dir="gpucall/config_templates")

    report = promote_production_tuple(
        review=review,
        candidate_name="modal-h100-qwen25-vl-7b",
        config_dir="gpucall/config_templates",
        work_dir=tmp_path / "promotion",
        run_validation=False,
        activate=False,
    )

    assert report["decision"] == "READY_FOR_BILLABLE_VALIDATION"
    assert report["config_valid"] is True
    assert (tmp_path / "promotion" / "config" / "tuples" / "modal-h100-qwen25-vl-7b.yml").exists()
    assert (tmp_path / "promotion" / "config" / "recipes" / "vision-understand-document-image-draft.yml").exists()
    tuple = yaml.safe_load((tmp_path / "promotion" / "config" / "tuples" / "modal-h100-qwen25-vl-7b.yml").read_text())
    assert tuple["model"] == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert tuple["model_ref"] == "qwen2.5-vl-7b-instruct"
    assert report["activated"] is False
