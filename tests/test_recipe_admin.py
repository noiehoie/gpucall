from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from gpucall.config import load_config
from gpucall.execution.contracts import official_contract
from gpucall.recipe_admin import _config_hash, _git_commit, canonical_recipe_from_artifact, main, process_inbox, promote_production_tuple, recipe_request_status, review_artifact
from gpucall.tuple_promotion import _validation_mode
from gpucall.recipe_request_index import RecipeRequestIndex


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
    assert recipe["allowed_modes"] == ["async"]
    assert recipe["auto_select"] is False
    assert report["catalog_policy"]["requires_async"] is True
    assert report["policy"] == "accept-all"
    assert report["human_review_bypassed"] is True


def test_admin_materializer_uses_catalog_cold_start_to_force_async() -> None:
    intake = {
        "sanitized_request": {
            "task": "infer",
            "mode": "sync",
            "intent": "rank_text_items",
            "classification": "confidential",
            "desired_capabilities": ["summarization", "instruction_following"],
            "error": {"context": {"context_budget_tokens": 46000}},
        },
        "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
    }

    catalog = load_config(Path("gpucall/config_templates"))
    recipe = canonical_recipe_from_artifact(intake, catalog=catalog)

    assert recipe["allowed_modes"] == ["async"]
    assert recipe["context_budget_tokens"] == 65536
    assert recipe["resource_class"] == "large"
    assert recipe["auto_select"] is False


def test_admin_materializer_normalizes_legacy_topic_ranking_intent() -> None:
    intake = {
        "sanitized_request": {
            "task": "infer",
            "mode": "sync",
            "intent": "topic_ranking",
            "classification": "confidential",
            "error": {"context": {"context_budget_tokens": 46000}},
        },
        "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
    }

    recipe = canonical_recipe_from_artifact(intake, catalog=load_config(Path("gpucall/config_templates")))

    assert recipe["name"] == "infer-rank-text-items-draft"
    assert recipe["intent"] == "rank_text_items"
    assert recipe["allowed_modes"] == ["async"]


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
    record = RecipeRequestIndex(inbox / "recipe_requests.db").get("rr-test")
    assert record is not None
    assert record["status"] == "processed"
    assert record["task"] == "infer"
    assert record["intent"] == "summarize_text"
    assert record["original_path"] == str(inbox / "processed" / "rr-test.json")
    assert record["report_path"] == str(inbox / "reports" / "rr-test.report.json")
    assert len(record["original_sha256"]) == 64
    assert status["index_record"]["status"] == "processed"


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


def test_admin_process_inbox_reports_catalog_readiness_without_smoke(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text("recipe_inbox_auto_materialize: true\n", encoding="utf-8")
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

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, config_dir=config_dir, force=True)

    assert results[0]["ok"] is True
    report = json.loads((inbox / "reports" / "rr-test.report.json").read_text(encoding="utf-8"))
    assert "promotion" not in report
    assert report["catalog_readiness"]["phase"] == "recipe-catalog-readiness"
    assert report["catalog_readiness"]["static_config_valid"] is True
    assert report["catalog_readiness"]["eligible_tuples"]


def test_admin_process_inbox_can_activate_existing_validated_recipe(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text(
        "\n".join(
            [
                "recipe_inbox_auto_materialize: true",
                "recipe_inbox_auto_validate_existing_tuples: true",
                "recipe_inbox_auto_activate_existing_validated_recipe: true",
                "recipe_inbox_auto_set_auto_select: true",
                "recipe_inbox_auto_require_auto_select_safe: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    inbox = tmp_path / "inbox"
    output_dir = config_dir / "recipes"
    validation_dir = tmp_path / "tuple-validation"
    inbox.mkdir()
    validation_dir.mkdir()
    intake = {
        "kind": "gpucall.recipe_request_submission",
        "request_id": "rr-existing",
        "intake": {
            "sanitized_request": {"task": "infer", "mode": "sync", "intent": "smoke_test", "classification": "internal", "desired_capabilities": []},
            "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
        },
    }
    recipe = canonical_recipe_from_artifact(intake["intake"], catalog=load_config(config_dir))
    (output_dir / f"{recipe['name']}.yml").write_text(yaml.safe_dump(recipe, sort_keys=False), encoding="utf-8")
    config = load_config(config_dir)
    tuple_spec = config.tuples["modal-a10g"]
    (validation_dir / "modal-a10g-smoke.json").write_text(
        json.dumps(
            {
                "validation_schema_version": 1,
                "passed": True,
                "tuple": "modal-a10g",
                "recipe": recipe["name"],
                "mode": "sync",
                "model_ref": tuple_spec.model_ref,
                "engine_ref": tuple_spec.engine_ref,
                "official_contract": official_contract(tuple_spec),
                "config_hash": _config_hash(config_dir),
                "commit": _git_commit(Path.cwd()),
            }
        ),
        encoding="utf-8",
    )
    (inbox / "rr-existing.json").write_text(json.dumps(intake), encoding="utf-8")

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, config_dir=config_dir, validation_dir=validation_dir)

    assert results[0]["ok"] is True
    report = json.loads((inbox / "reports" / "rr-existing.report.json").read_text(encoding="utf-8"))
    activated = json.loads((inbox / "reports" / "rr-existing.existing-tuple-activation.json").read_text(encoding="utf-8"))
    active_recipe = yaml.safe_load((output_dir / f"{recipe['name']}.yml").read_text(encoding="utf-8"))
    assert report["existing_tuple_activation"]["decision"] == "ACTIVATED"
    assert activated["matched_validation"]
    assert active_recipe["auto_select"] is True
    assert active_recipe["quality_floor"] == "standard"


def test_admin_process_inbox_existing_tuple_waits_for_validation_when_not_billable(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text(
        "recipe_inbox_auto_materialize: true\nrecipe_inbox_auto_validate_existing_tuples: true\n",
        encoding="utf-8",
    )
    inbox = tmp_path / "inbox"
    output_dir = config_dir / "recipes"
    inbox.mkdir()
    (inbox / "rr-wait.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-wait",
                "intake": {
                    "sanitized_request": {"task": "infer", "mode": "sync", "intent": "smoke_test", "classification": "internal", "desired_capabilities": []},
                    "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
                },
            }
        ),
        encoding="utf-8",
    )

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, config_dir=config_dir, force=True)

    assert results[0]["ok"] is True
    report = json.loads((inbox / "reports" / "rr-wait.report.json").read_text(encoding="utf-8"))
    assert report["existing_tuple_activation"]["decision"] == "READY_FOR_BILLABLE_VALIDATION"


def test_admin_process_inbox_can_auto_promote_candidate_without_validation(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text(
        "recipe_inbox_auto_materialize: true\nrecipe_inbox_auto_promote_candidates: true\n",
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
                },
            }
        ),
        encoding="utf-8",
    )

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, config_dir=config_dir, force=True)

    assert results[0]["ok"] is True
    report = json.loads((inbox / "reports" / "rr-test.report.json").read_text(encoding="utf-8"))
    promotion = json.loads((inbox / "reports" / "rr-test.promotion.json").read_text(encoding="utf-8"))
    assert report["promotion"]["decision"] == "READY_FOR_BILLABLE_VALIDATION"
    assert promotion["candidate"]["name"] in {match["name"] for match in report["admin_review"]["tuple_candidate_matches"]}
    assert (inbox / "promotions" / "rr-test" / "config" / "tuples" / f"{promotion['candidate']['name']}.yml").exists()


def test_admin_process_inbox_can_auto_promote_long_text_candidate_without_validation(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text(
        "recipe_inbox_auto_materialize: true\nrecipe_inbox_auto_promote_candidates: true\n",
        encoding="utf-8",
    )
    inbox = tmp_path / "inbox"
    output_dir = config_dir / "recipes"
    inbox.mkdir()
    (inbox / "rr-rank.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-rank",
                "intake": {
                    "phase": "deterministic-intake",
                    "sanitized_request": {
                        "task": "infer",
                        "mode": "sync",
                        "intent": "rank_text_items",
                        "classification": "confidential",
                        "desired_capabilities": ["summarization", "instruction_following"],
                        "error": {"context": {"context_budget_tokens": 46000}},
                    },
                    "redaction_report": {
                        "prompt_body_forwarded": False,
                        "data_ref_uri_forwarded": False,
                        "presigned_url_forwarded": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, config_dir=config_dir, force=True)

    assert results[0]["ok"] is True
    recipe = yaml.safe_load((output_dir / "infer-rank-text-items-draft.yml").read_text(encoding="utf-8"))
    promotion = json.loads((inbox / "reports" / "rr-rank.promotion.json").read_text(encoding="utf-8"))
    worker = yaml.safe_load(
        (inbox / "promotions" / "rr-rank" / "config" / "workers" / f"{promotion['candidate']['name']}.yml").read_text(
            encoding="utf-8"
        )
    )
    surface = yaml.safe_load(
        (inbox / "promotions" / "rr-rank" / "config" / "surfaces" / f"{promotion['candidate']['name']}.yml").read_text(
            encoding="utf-8"
        )
    )
    assert recipe["allowed_modes"] == ["async"]
    assert promotion["decision"] == "READY_FOR_BILLABLE_VALIDATION"
    assert worker["target"] == "gpucall-worker-json:run_inference_on_modal"
    assert surface["configured_price_ttl_seconds"] == 604800


def test_admin_process_inbox_links_existing_recipe_without_overwrite(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text("recipe_inbox_auto_materialize: true\n", encoding="utf-8")
    inbox = tmp_path / "inbox"
    output_dir = config_dir / "recipes"
    inbox.mkdir()
    recipe_path = output_dir / "infer-summarize-text-draft.yml"
    recipe_path.write_text(
        yaml.safe_dump(
            canonical_recipe_from_artifact(
                {
                    "sanitized_request": {"task": "infer", "mode": "sync", "intent": "summarize_text"},
                    "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
                }
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    before = recipe_path.read_text(encoding="utf-8")
    (inbox / "rr-test.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-test",
                "intake": {
                    "sanitized_request": {"task": "infer", "mode": "sync", "intent": "summarize_text"},
                    "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
                },
            }
        ),
        encoding="utf-8",
    )

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, config_dir=config_dir)

    assert results[0]["ok"] is True
    report = json.loads((inbox / "reports" / "rr-test.report.json").read_text(encoding="utf-8"))
    assert report["processing_action"] == "existing_recipe_linked"
    assert recipe_path.read_text(encoding="utf-8") == before


def test_admin_process_inbox_indexes_failed_submission(tmp_path) -> None:
    inbox = tmp_path / "inbox"
    output_dir = tmp_path / "recipes"
    inbox.mkdir()
    (inbox / "rr-bad.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-bad",
                "intake": {
                    "sanitized_request": {"task": "infer", "intent": "summarize_text"},
                },
            }
        ),
        encoding="utf-8",
    )

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, accept_all=True)

    assert results[0]["ok"] is False
    assert (inbox / "failed" / "rr-bad.json").exists()
    record = RecipeRequestIndex(inbox / "recipe_requests.db").get("rr-bad")
    assert record is not None
    assert record["status"] == "failed"
    assert record["task"] == "infer"
    assert record["intent"] == "summarize_text"
    assert record["original_path"] == str(inbox / "failed" / "rr-bad.json")
    assert "admin review rejected submission" in record["error"]


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


def test_admin_inbox_list_and_readiness(tmp_path, capsys) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    (config_dir / "admin.yml").write_text("recipe_inbox_auto_materialize: true\n", encoding="utf-8")
    inbox = tmp_path / "inbox"
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
                "inbox",
                "materialize",
                "--inbox-dir",
                str(inbox),
                "--output-dir",
                str(config_dir / "recipes"),
                "--config-dir",
                str(config_dir),
                "--force",
            ]
        )
        == 0
    )
    materialize_output = json.loads(capsys.readouterr().out)
    assert materialize_output["processed"][0]["ok"] is True

    assert main(["inbox", "list", "--inbox-dir", str(inbox)]) == 0
    list_output = json.loads(capsys.readouterr().out)
    assert list_output["requests"][0]["status"] == "processed"

    assert main(["inbox", "readiness", "--inbox-dir", str(inbox), "--config-dir", str(config_dir)]) == 0
    readiness_output = json.loads(capsys.readouterr().out)
    assert readiness_output["phase"] == "recipe-inbox-readiness"
    assert readiness_output["readiness"][0]["recipes"][0]["recipe"] == "infer-summarize-text-draft"


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


def test_promotion_validation_mode_follows_recipe_allowed_modes() -> None:
    assert _validation_mode("text-infer-standard", Path("gpucall/config_templates")) == "sync"
    assert _validation_mode("infer-rank-text-items-draft", Path("gpucall/config_templates")) == "async"
