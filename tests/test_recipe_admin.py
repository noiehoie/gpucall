from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from gpucall.config import load_config
from gpucall.compiler import GovernanceCompiler
from gpucall.domain import ChatMessage, ExecutionMode, RecipeAdminAutomationConfig, ResponseFormat, ResponseFormatType, TaskRequest
from gpucall.execution.contracts import official_contract
from gpucall.recipe_admin_files import move_submission
from gpucall.recipe_admin import (
    _auto_existing_tuple_report,
    _auto_promotion_report,
    _auto_select_shadowing,
    _config_hash,
    _git_commit,
    author_recipe_proposal,
    canonical_recipe_from_artifact,
    main,
    process_inbox,
    process_quality_inbox,
    quality_feedback_report,
    quality_feedback_status,
    recipe_request_status,
    review_artifact,
    validate_draft_artifact,
)
from gpucall.recipe_materialize import write_recipe_yaml
from gpucall.registry import ObservedRegistry
from gpucall.tuple_promotion import _tuple_from_candidate, _validation_mode, _write_split_tuple
from gpucall.recipe_request_index import RecipeRequestIndex
from gpucall.quality_feedback_index import QualityFeedbackIndex
from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.panopticon_provisioning import ProviderSupplyProvisioningApplyResult


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


def test_admin_materialize_rejects_missing_or_generic_intent() -> None:
    with pytest.raises(ValueError, match="explicit intent"):
        canonical_recipe_from_artifact({"sanitized_request": {"task": "infer"}})

    with pytest.raises(ValueError, match="must not equal task"):
        canonical_recipe_from_artifact({"sanitized_request": {"task": "infer", "intent": "infer"}})


def test_admin_materialize_rejects_unknown_workload_intent() -> None:
    intake = {
        "phase": "deterministic-contract-intake",
        "sanitized_request": {
            "task": "infer",
            "mode": "async",
            "intent": "unknown_workload_deadbeef",
            "expected_output": "plain_text",
            "error": {"context": {"context_budget_tokens": 131072}},
            "quality_contract": {"metrics": {"min_response_chars": 1000}},
            "draft_grammar": {
                "materialization_allowed": False,
                "blockers": ["unknown workload requires operator intent mapping before materialization"],
            },
        },
    }

    with pytest.raises(ValueError, match="unknown workload"):
        canonical_recipe_from_artifact(intake)


def test_admin_materialize_rejects_strict_intake_without_quality_contract() -> None:
    intake = {
        "phase": "deterministic-contract-intake",
        "sanitized_request": {
            "task": "infer",
            "mode": "async",
            "intent": "rss_semantic_match",
            "expected_output": "json_object",
            "error": {"context": {"context_budget_tokens": 131072}},
        },
    }

    with pytest.raises(ValueError, match="quality_contract"):
        canonical_recipe_from_artifact(intake)


def test_admin_validate_draft_rejects_weak_contract_before_materialization() -> None:
    intake = {
        "phase": "deterministic-contract-intake",
        "sanitized_request": {
            "task": "infer",
            "mode": "async",
            "intent": "rss_semantic_match",
            "expected_output": "json_object",
            "error": {"context": {"context_budget_tokens": 131072}},
            "draft_grammar": {"materialization_allowed": True, "blockers": []},
        },
        "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
    }

    report = validate_draft_artifact(intake)

    assert report["phase"] == "admin-draft-validation"
    assert report["decision"] == "REJECTED_DRAFT"
    assert report["accepted"] is False
    assert any("quality_contract" in item["reason"] for item in report["blockers"])


def test_admin_validate_draft_accepts_strict_contract_candidate() -> None:
    intake = {
        "phase": "deterministic-contract-intake",
        "sanitized_request": {
            "task": "infer",
            "mode": "async",
            "intent": "rss_semantic_match",
            "classification": "confidential",
            "expected_output": "json_object",
            "error": {"context": {"context_budget_tokens": 131072}},
            "quality_contract": {"metrics": {"min_rss_matches": 33, "max_http_422": 0}},
            "draft_grammar": {"materialization_allowed": True, "blockers": []},
        },
        "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
    }

    report = validate_draft_artifact(intake)

    assert report["decision"] == "ACCEPTED_DRAFT"
    assert report["accepted"] is True
    assert report["materialization_preview"]["intent"] == "rss_semantic_match"
    assert report["materialization_preview"]["context_budget_tokens"] == 131072


def test_admin_materialize_requires_accept_all(tmp_path) -> None:
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps({"sanitized_request": {"task": "infer"}}), encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing to materialize without --accept-all"):
        main(["materialize", "--input", str(intake_path), "--dry-run"])


def test_admin_cli_validate_draft_rejects_without_writing_recipe(tmp_path) -> None:
    intake_path = tmp_path / "weak.json"
    report_path = tmp_path / "validation.json"
    intake_path.write_text(
        json.dumps(
            {
                "phase": "deterministic-contract-intake",
                "sanitized_request": {
                    "task": "infer",
                    "mode": "async",
                    "intent": "rss_semantic_match",
                    "expected_output": "json_object",
                    "error": {"context": {"context_budget_tokens": 131072}},
                },
                "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
            }
        ),
        encoding="utf-8",
    )

    assert main(["validate-draft", "--input", str(intake_path), "--output", str(report_path)]) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["decision"] == "REJECTED_DRAFT"
    assert any("quality_contract" in item["reason"] for item in report["blockers"])


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


def test_admin_materializer_writes_mega_context_draft_contract() -> None:
    intake = {
        "sanitized_request": {
            "task": "infer",
            "mode": "async",
            "intent": "rank_text_items",
            "classification": "confidential",
            "desired_capabilities": ["instruction_following"],
            "error": {"context": {"context_budget_tokens": 6650439, "largest_auto_recipe_context_budget_tokens": 1010000}},
        },
        "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
    }

    recipe = canonical_recipe_from_artifact(intake, catalog=load_config(Path("gpucall/config_templates")))
    report = review_artifact(intake, config_dir="gpucall/config_templates")

    assert recipe["name"] == "infer-rank-text-items-mega-draft"
    assert recipe["allowed_modes"] == ["async"]
    assert recipe["context_budget_tokens"] == 8388608
    assert recipe["resource_class"] == "ultralong"
    assert recipe["auto_select"] is False
    assert report["decision"] == "CANDIDATE_ONLY"
    assert report["blockers"] == []
    assert report["required_execution_contract"]["min_model_len"] == 8388608
    assert report["required_execution_contract"]["context_budget_policy"]["requested_context_budget_tokens"] == 6650439
    assert report["required_execution_contract"]["context_budget_policy"]["materialized_context_budget_tokens"] == 8388608
    assert report["required_execution_contract"]["context_budget_policy"]["scale"] == "mega"
    assert report["required_execution_contract"]["context_budget_policy"]["requires_tuple_authoring"] is True


def test_admin_materializer_keeps_sync_when_catalog_has_sync_safe_tuple() -> None:
    intake = {
        "sanitized_request": {
            "task": "infer",
            "mode": "sync",
            "intent": "translate_text",
            "classification": "confidential",
            "desired_capabilities": ["translation"],
        },
        "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
    }

    catalog = load_config(Path("gpucall/config_templates"))
    recipe = canonical_recipe_from_artifact(intake, catalog=catalog)

    assert recipe["allowed_modes"] == ["sync"]
    assert recipe["context_budget_tokens"] == 8192


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


def test_admin_materializes_workload_contract_to_recipe() -> None:
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
                "input_profile": {
                    "content_types": ["text/plain"],
                    "max_bytes": 16000,
                    "input_count": 1,
                    "context_budget_tokens": 131072,
                },
                "output_profile": {"output_contract": "json_object"},
                "quality_contract": {
                    "gateway_may_infer_quality": False,
                    "metrics": {"min_topics": 12, "min_sources": 11, "min_response_chars": 20230},
                },
            }
        ],
    }

    recipe = canonical_recipe_from_artifact(contract)

    assert recipe["name"] == "infer-rank-text-items-draft"
    assert recipe["intent"] == "rank_text_items"
    assert recipe["allowed_modes"] == ["async"]
    assert recipe["context_budget_tokens"] == 131072
    assert recipe["output_contract"] == "json_object"


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


def test_admin_process_inbox_rejects_weak_draft_before_recipe_write(tmp_path) -> None:
    inbox = tmp_path / "inbox"
    output_dir = tmp_path / "recipes"
    inbox.mkdir()
    submission = {
        "kind": "gpucall.recipe_request_submission",
        "request_id": "rr-weak",
        "intake": {
            "phase": "deterministic-contract-intake",
            "sanitized_request": {
                "task": "infer",
                "mode": "async",
                "intent": "rss_semantic_match",
                "classification": "confidential",
                "expected_output": "json_object",
                "error": {"context": {"context_budget_tokens": 131072}},
                "draft_grammar": {"materialization_allowed": True, "blockers": []},
            },
            "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
        },
    }
    (inbox / "rr-weak.json").write_text(json.dumps(submission), encoding="utf-8")

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, accept_all=True)

    assert results[0]["ok"] is False
    assert "draft validation rejected" in results[0]["error"]
    assert not list(output_dir.glob("*.yml"))
    assert (inbox / "failed" / "rr-weak.json").exists()
    report = json.loads((inbox / "reports" / "rr-weak.report.json").read_text(encoding="utf-8"))
    assert report["decision"] == "REJECTED_DRAFT"
    assert report["draft_validation"]["decision"] == "REJECTED_DRAFT"
    status = recipe_request_status("rr-weak", inbox)
    assert status["state"] == "failed"
    assert status["report"]["decision"] == "REJECTED_DRAFT"


def test_admin_process_inbox_materializes_mega_context_submission(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    inbox = tmp_path / "inbox"
    output_dir = tmp_path / "recipes"
    inbox.mkdir()
    (inbox / "rr-mega.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-mega",
                "source": "example-caller-app",
                "intake": {
                    "phase": "deterministic-intake",
                    "sanitized_request": {
                        "task": "infer",
                        "mode": "async",
                        "intent": "rank_text_items",
                        "classification": "confidential",
                        "desired_capabilities": ["instruction_following"],
                        "error": {"context": {"context_budget_tokens": 6650439, "largest_auto_recipe_context_budget_tokens": 1010000}},
                    },
                    "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
                },
            }
        ),
        encoding="utf-8",
    )

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, config_dir=config_dir, accept_all=True)

    assert results[0]["ok"] is True
    recipe = yaml.safe_load((output_dir / "infer-rank-text-items-mega-draft.yml").read_text(encoding="utf-8"))
    report = json.loads((inbox / "reports" / "rr-mega.report.json").read_text(encoding="utf-8"))
    assert recipe["context_budget_tokens"] == 8388608
    assert recipe["allowed_modes"] == ["async"]
    assert report["context_budget_policy"]["scale"] == "mega"
    assert report["context_budget_policy"]["requested_context_budget_tokens"] == 6650439
    assert report["context_budget_policy"]["materialized_context_budget_tokens"] == 8388608
    assert report["admin_review"]["decision"] == "CANDIDATE_ONLY"
    assert report["admin_review"]["required_execution_contract"]["context_budget_policy"]["requires_tuple_authoring"] is True
    assert (inbox / "processed" / "rr-mega.json").exists()


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


def test_auto_select_shadowing_is_intent_scoped() -> None:
    config = load_config(Path("gpucall/config_templates"))
    candidate = config.recipes["admin-author-recipe-draft"].model_copy(update={"auto_select": True})

    shadowing = _auto_select_shadowing(candidate, config.recipes)

    assert shadowing["safe"] is True
    assert shadowing["existing_auto_select_recipes"] == []


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


def test_admin_process_inbox_existing_tuple_validation_tries_next_eligible(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    attempts = []

    def fake_readiness_report(*, config_dir, recipe, validation_dir, live=False, **kwargs):
        assert live is True
        return {
            "recipes": [
                {
                    "recipe": recipe,
                    "live_ready_tuples": [],
                    "live_blocked_tuples": [
                        {"tuple": "modal-a10g", "live_reason": "validation_config_hash_mismatch"},
                        {"tuple": "modal-h200x4-qwen25-14b-1m", "live_reason": "validation_config_hash_mismatch"},
                    ],
                }
            ]
        }

    def fake_validation(*, tuple_name, recipe_name, config_dir, validation_dir, budget_usd=0.10):
        attempts.append(tuple_name)
        if tuple_name == "modal-a10g":
            return {"returncode": 1, "passed": False, "stderr": "not eligible"}
        return {"returncode": 0, "passed": True, "artifact_path": f"/tmp/{tuple_name}.json"}

    monkeypatch.setattr("gpucall.recipe_admin.build_readiness_report", fake_readiness_report)
    monkeypatch.setattr("gpucall.recipe_admin._run_existing_tuple_validation", fake_validation)

    activation = _auto_existing_tuple_report(
        {
            "canonical_recipe": {"name": "infer-rank-text-items-draft"},
            "eligible_tuples": ["modal-a10g", "modal-h200x4-qwen25-14b-1m"],
            "live_validation": {"matched": []},
        },
        request_id="rr-retry",
        automation=RecipeAdminAutomationConfig(
            recipe_inbox_auto_materialize=True,
            recipe_inbox_auto_validate_existing_tuples=True,
            recipe_inbox_auto_billable_validation=True,
        ),
        report_dir=report_dir,
        config_dir=config_dir,
        validation_dir=tmp_path / "tuple-validation",
        force=False,
    )

    assert attempts == ["modal-a10g", "modal-h200x4-qwen25-14b-1m"]
    assert activation["decision"] == "VALIDATED_READY_TO_ACTIVATE"
    assert activation["matched_validation"][0]["tuple"] == "modal-h200x4-qwen25-14b-1m"
    assert [item["tuple"] for item in activation["validation_attempts"]] == attempts


def test_existing_tuple_auto_validation_uses_only_readiness_ready_tuples(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    attempts = []

    def fake_readiness_report(*, config_dir, recipe, validation_dir, live=False, **kwargs):
        assert live is True
        return {
            "recipes": [
                {
                    "recipe": recipe,
                    "live_ready_tuples": [],
                    "live_blocked_tuples": [
                        {"tuple": "hyperstack-a100-nvlinkx1-qwen2-5-7b-instruct", "live_reason": "ssh_remote_cidr_not_configured"},
                        {"tuple": "modal-a10g", "live_reason": "missing_route_validation_evidence"},
                    ],
                }
            ]
        }

    def fake_validation(**kwargs):
        attempts.append(kwargs["tuple_name"])
        return {"returncode": 0, "passed": True, "artifact_path": str(tmp_path / "modal-validation.json")}

    monkeypatch.setattr("gpucall.recipe_admin.build_readiness_report", fake_readiness_report)
    monkeypatch.setattr("gpucall.recipe_admin._run_existing_tuple_validation", fake_validation)

    activation = _auto_existing_tuple_report(
        {
            "canonical_recipe": {"name": "infer-translate-text-draft"},
            "eligible_tuples": ["hyperstack-a100-nvlinkx1-qwen2-5-7b-instruct", "modal-a10g"],
            "live_validation": {"matched": []},
        },
        request_id="rr-modal-happy",
        automation=RecipeAdminAutomationConfig(
            recipe_inbox_auto_materialize=True,
            recipe_inbox_auto_validate_existing_tuples=True,
            recipe_inbox_auto_billable_validation=True,
        ),
        report_dir=report_dir,
        config_dir=config_dir,
        validation_dir=tmp_path / "tuple-validation",
        force=False,
    )

    assert attempts == ["modal-a10g"]
    assert activation["validation_gate"]["ready_tuples"] == ["modal-a10g"]
    assert activation["decision"] == "VALIDATED_READY_TO_ACTIVATE"


def test_existing_tuple_auto_validation_blocks_before_billable_when_readiness_is_stale(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    validation_calls = []

    def fake_readiness_report(*, config_dir, recipe, validation_dir, live=False, **kwargs):
        assert live is True
        return {
            "recipes": [
                {
                    "recipe": recipe,
                    "live_ready_tuples": [],
                    "live_blocked_tuples": [{"tuple": "runpod-vllm-h100-80gb-qwen2-5-7b-instruct", "live_reason": "models_probe_timeout"}],
                }
            ]
        }

    def fake_validation(**kwargs):
        validation_calls.append(kwargs)
        return {"returncode": 0, "passed": True}

    monkeypatch.setattr("gpucall.recipe_admin.build_readiness_report", fake_readiness_report)
    monkeypatch.setattr("gpucall.recipe_admin._run_existing_tuple_validation", fake_validation)

    report = _auto_existing_tuple_report(
        {
            "canonical_recipe": {"name": "infer-summarize-text-draft"},
            "eligible_tuples": ["runpod-vllm-h100-80gb-qwen2-5-7b-instruct"],
            "live_validation": {"matched": []},
        },
        request_id="rr-stale",
        automation=RecipeAdminAutomationConfig(
            recipe_inbox_auto_materialize=True,
            recipe_inbox_auto_validate_existing_tuples=True,
            recipe_inbox_auto_billable_validation=True,
            recipe_inbox_auto_validation_budget_usd=0.04,
        ),
        report_dir=report_dir,
        config_dir=config_dir,
        validation_dir=tmp_path / "tuple-validation",
        force=False,
    )

    assert report["decision"] == "WAITING_FOR_FRESH_READINESS"
    assert report["validation_gate"]["decision"] == "WAITING_FOR_FRESH_READINESS"
    assert "models_probe_timeout" in report["next_actions"][0]
    assert validation_calls == []
    assert (report_dir / "rr-stale.existing-tuple-readiness.json").exists()


def test_existing_tuple_auto_validation_uses_budget_after_validation_hash_mismatch_gate(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    validation_calls = []

    def fake_readiness_report(*, config_dir, recipe, validation_dir, live=False, **kwargs):
        assert live is True
        return {
            "recipes": [
                {
                    "recipe": recipe,
                    "live_ready_tuples": [],
                    "live_blocked_tuples": [{"tuple": "runpod-vllm-h100-80gb-qwen2-5-7b-instruct", "live_reason": "validation_config_hash_mismatch"}],
                }
            ]
        }

    def fake_validation(**kwargs):
        validation_calls.append(kwargs)
        return {"returncode": 0, "passed": True, "artifact_path": str(tmp_path / "validation-evidence.json")}

    monkeypatch.setattr("gpucall.recipe_admin.build_readiness_report", fake_readiness_report)
    monkeypatch.setattr("gpucall.recipe_admin._run_existing_tuple_validation", fake_validation)

    report = _auto_existing_tuple_report(
        {
            "canonical_recipe": {"name": "infer-summarize-text-draft"},
            "eligible_tuples": ["runpod-vllm-h100-80gb-qwen2-5-7b-instruct"],
            "live_validation": {"matched": []},
        },
        request_id="rr-hash",
        automation=RecipeAdminAutomationConfig(
            recipe_inbox_auto_materialize=True,
            recipe_inbox_auto_validate_existing_tuples=True,
            recipe_inbox_auto_billable_validation=True,
            recipe_inbox_auto_validation_budget_usd=0.04,
        ),
        report_dir=report_dir,
        config_dir=config_dir,
        validation_dir=tmp_path / "tuple-validation",
        force=False,
    )

    assert report["decision"] == "VALIDATED_READY_TO_ACTIVATE"
    assert report["validation_gate"]["decision"] == "READY_FOR_BILLABLE_VALIDATION"
    assert validation_calls[0]["budget_usd"] == 0.04


def test_admin_author_dry_run_bundle_redacts_prompt_text(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    report = {
        "recipe_path": "/config/recipes/infer-example-draft.yml",
        "canonical_recipe": {
            "name": "infer-example-draft",
            "task": "infer",
            "intent": "example",
            "system_prompt": "sensitive operator prompt body",
            "allowed_modes": ["sync"],
        },
        "admin_review": {"decision": "READY_FOR_VALIDATION", "canonical_recipe": {"name": "infer-example-draft", "task": "infer"}},
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    artifact = author_recipe_proposal(report_path=report_path, config_dir=config_dir, dry_run_bundle=True)

    recipe = artifact["bundle"]["materialization"]["canonical_recipe"]
    assert artifact["phase"] == "recipe-authoring-bundle"
    assert recipe["system_prompt"]["chars"] == len("sensitive operator prompt body")
    assert "sensitive operator prompt body" not in json.dumps(artifact, ensure_ascii=False)


def test_admin_author_returns_proposal_without_writing_config(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    report = {
        "recipe_path": str(config_dir / "recipes" / "infer-example-draft.yml"),
        "canonical_recipe": {"name": "infer-example-draft", "task": "infer", "intent": "example", "allowed_modes": ["sync"]},
        "admin_review": {"decision": "READY_FOR_VALIDATION", "canonical_recipe": {"name": "infer-example-draft", "task": "infer"}},
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    def fake_run_authoring_recipe(*, config_dir, authoring_recipe, bundle):
        return json.dumps(
            {
                "proposal_kind": "recipe_patch",
                "target_recipe": "infer-example-draft",
                "summary": "tighten JSON behavior",
                "patch": [{"op": "replace", "path": "/output_validation_attempts", "value": 2}],
                "validation_plan": ["gpucall validate-config --config-dir /config"],
                "risk_notes": ["requires smoke validation"],
            }
        )

    monkeypatch.setattr("gpucall.recipe_admin._run_authoring_recipe", fake_run_authoring_recipe)

    artifact = author_recipe_proposal(report_path=report_path, config_dir=config_dir)

    assert artifact["phase"] == "recipe-authoring-proposal"
    assert artifact["production_config_written"] is False
    assert artifact["proposal"]["patch"][0]["path"] == "/output_validation_attempts"
    assert not (config_dir / "recipes" / "infer-example-draft.yml").exists()


def test_admin_author_rejects_guarded_auto_select_patch(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"canonical_recipe": {"name": "infer-example-draft", "task": "infer"}, "admin_review": {}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "gpucall.recipe_admin._run_authoring_recipe",
        lambda **_: json.dumps(
            {
                "proposal_kind": "recipe_patch",
                "target_recipe": "infer-example-draft",
                "summary": "unsafe",
                "patch": [{"op": "replace", "path": "/auto_select", "value": True}],
                "validation_plan": [],
                "risk_notes": [],
            }
        ),
    )

    with pytest.raises(ValueError, match="guarded field /auto_select"):
        author_recipe_proposal(report_path=report_path, config_dir=config_dir)


def test_admin_author_recipe_prefers_configured_local_ds4_runtime(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    for kind in ("runtimes", "surfaces", "workers"):
        (config_dir / kind / "local-ds4.example").rename(config_dir / kind / "local-ds4.yml")
    config = load_config(config_dir)
    compiler = GovernanceCompiler(
        policy=config.policy,
        recipes=config.recipes,
        tuples=config.tuples,
        models=config.models,
        engines=config.engines,
        registry=ObservedRegistry(),
    )

    plan = compiler.compile(
        TaskRequest(
            task="infer",
            mode=ExecutionMode.ASYNC,
            recipe="admin-author-recipe-draft",
            messages=[ChatMessage(role="user", content="{}")],
            response_format=ResponseFormat(type=ResponseFormatType.JSON_OBJECT),
        )
    )

    assert plan.tuple_chain[0] == "local-ds4"


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
    assert report["admin_review"]["decision"] == "READY_FOR_VALIDATION"
    assert "modal-h100-qwen25-vl-7b" in report["admin_review"]["eligible_tuples"]
    assert report["promotion"]["decision"] == "SKIPPED_NO_TUPLE_CANDIDATE"
    assert not (inbox / "reports" / "rr-test.promotion.json").exists()


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
                        "desired_capabilities": ["summarization", "instruction_following", "reasoning"],
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
    report = json.loads((inbox / "reports" / "rr-rank.report.json").read_text(encoding="utf-8"))
    assert recipe["allowed_modes"] == ["sync", "async"]
    assert report["admin_review"]["decision"] == "READY_FOR_VALIDATION"
    assert "modal-h200x4-qwen25-14b-1m" in report["admin_review"]["eligible_tuples"]
    assert report["promotion"]["decision"] == "SKIPPED_NO_TUPLE_CANDIDATE"
    assert not (inbox / "reports" / "rr-rank.promotion.json").exists()


def test_admin_auto_promotion_plans_provider_supply_for_endpointless_candidate(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    inbox = tmp_path / "inbox"
    reports = inbox / "reports"
    reports.mkdir(parents=True)
    tuple_name = "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"
    candidate = next(row for row in load_tuple_candidate_payloads(config_dir) if row["name"] == tuple_name)
    recipe = yaml.safe_load((config_dir / "recipes" / "vision-image-standard.yml").read_text(encoding="utf-8"))
    recipe["name"] = "vision-understand-document-image-draft"
    recipe["auto_select"] = False
    recipe["quality_floor"] = "draft"
    automation = RecipeAdminAutomationConfig(
        recipe_inbox_auto_materialize=True,
        recipe_inbox_auto_promote_candidates=True,
        recipe_inbox_auto_provision_supply=True,
    )

    report = _auto_promotion_report(
        {
            "canonical_recipe": recipe,
            "tuple_candidate_matches": [{"name": tuple_name, "path": candidate["_path"]}],
        },
        request_id="rr-supply",
        automation=automation,
        inbox_dir=inbox,
        report_dir=reports,
        config_dir=config_dir,
        validation_dir=None,
        force=True,
    )

    supply = report["supply_provisioning"]
    assert report["decision"] == "READY_FOR_ENDPOINT_CONFIGURATION"
    assert supply["decision"] == "PROVISIONING_PLANNED"
    assert supply["provider_mutation_enabled"] is False
    assert supply["plan_source"] == "tuple_candidate_catalog"
    assert supply["plan_action_count"] == 2
    assert supply["apply_dry_run"] is True
    assert report["post_supply_workflow"]["decision"] == "WAITING_FOR_PROVIDER_SUPPLY_APPLY"
    assert (reports / "rr-supply.supply-provisioning-plan.json").exists()
    assert (reports / "rr-supply.supply-provisioning-apply.json").exists()
    assert (reports / "rr-supply.post-supply-workflow.json").exists()


def test_admin_auto_supply_apply_triggers_readiness_and_billable_validation(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    inbox = tmp_path / "inbox"
    reports = inbox / "reports"
    reports.mkdir(parents=True)
    tuple_name = "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"
    candidate = next(row for row in load_tuple_candidate_payloads(config_dir) if row["name"] == tuple_name)
    recipe = yaml.safe_load((config_dir / "recipes" / "vision-image-standard.yml").read_text(encoding="utf-8"))
    recipe["name"] = "vision-understand-document-image-draft"
    recipe["auto_select"] = False
    recipe["quality_floor"] = "draft"

    def fake_apply(plan, *, dry_run=True, now=None, credentials=None):
        assert dry_run is False
        return ProviderSupplyProvisioningApplyResult(
            generated_at="2026-05-23T00:00:00+00:00",
            dry_run=False,
            plan_action_count=plan.action_count,
            applied_count=1,
            skipped_count=0,
            failed_count=0,
            results=[
                {
                    "action_id": "endpoint-action",
                    "action": "create_runpod_serverless_endpoint",
                    "provider": "runpod",
                    "resource_type": "endpoint",
                    "resource_id": "endpoint-created",
                    "tuple": tuple_name,
                    "status": "applied",
                    "response": {"id": "endpoint-created"},
                    "materialized_config_patch": [
                        {
                            "kind": "worker_target",
                            "config_dir_relative_path": f"workers/{tuple_name}.yml",
                            "json_pointer": "/target",
                            "value": "endpoint-created",
                        }
                    ],
                }
            ],
        )

    def fake_readiness_report(*, config_dir, recipe, validation_dir, live=False, **kwargs):
        assert live is True
        return {
            "recipes": [
                {
                    "recipe": recipe,
                    "live_ready_tuples": [],
                    "live_blocked_tuples": [{"tuple": tuple_name, "live_reason": "missing_route_validation_evidence"}],
                }
            ]
        }

    commands = []

    def fake_admin_check(command, *, validation_dir=None, parse_json=False):
        commands.append(command)
        if "tuple-smoke" in command:
            return {
                "command": command,
                "returncode": 0,
                "stdout": json.dumps({"passed": True, "artifact_path": str(tmp_path / "validation-evidence.json")}),
                "stderr": "",
                "artifact": {"passed": True, "artifact_path": str(tmp_path / "validation-evidence.json")},
                "artifact_path": str(tmp_path / "validation-evidence.json"),
                "passed": True,
            }
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr("gpucall.recipe_admin.apply_provider_supply_provisioning_plan", fake_apply)
    monkeypatch.setattr("gpucall.recipe_admin.build_readiness_report", fake_readiness_report)
    monkeypatch.setattr("gpucall.recipe_admin._run_admin_check", fake_admin_check)

    report = _auto_promotion_report(
        {
            "canonical_recipe": recipe,
            "tuple_candidate_matches": [{"name": tuple_name, "path": candidate["_path"]}],
        },
        request_id="rr-supply-apply",
        automation=RecipeAdminAutomationConfig(
            recipe_inbox_auto_materialize=True,
            recipe_inbox_auto_promote_candidates=True,
            recipe_inbox_auto_provision_supply=True,
            recipe_inbox_auto_apply_supply=True,
            recipe_inbox_auto_billable_validation=True,
            recipe_inbox_auto_validation_budget_usd=0.07,
        ),
        inbox_dir=inbox,
        report_dir=reports,
        config_dir=config_dir,
        validation_dir=tmp_path / "tuple-validation",
        force=True,
    )

    workflow = report["post_supply_workflow"]
    worker = yaml.safe_load((Path(report["promotion_config_dir"]) / "workers" / f"{tuple_name}.yml").read_text(encoding="utf-8"))
    assert workflow["decision"] == "VALIDATED_READY_TO_ACTIVATE"
    assert worker["target"] == "endpoint-created"
    assert workflow["validation"]["passed"] is True
    assert any("tuple-smoke" in command and "0.07" in command for command in commands)
    assert (reports / "rr-supply-apply.post-supply-readiness.json").exists()
    assert (reports / "rr-supply-apply.post-supply-validation.json").exists()


def test_tuple_candidate_promotion_preserves_runtime_cost_estimates(tmp_path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    config = load_config(config_dir)
    candidate = next(
        row
        for row in load_tuple_candidate_payloads(config_dir)
        if row["adapter"] == "runpod-vllm-serverless"
        and row["gpu"] == "RUNPOD_H100_80GB"
        and row["model_ref"] == "qwen2.5-7b-instruct"
    )

    tuple_payload = _tuple_from_candidate(candidate, active_config=config)
    surface_path, _worker_path = _write_split_tuple(tmp_path / "out", tuple_payload, force=False)
    surface = yaml.safe_load(surface_path.read_text(encoding="utf-8"))

    assert tuple_payload["estimated_prefill_tokens_per_second"] == 1000
    assert surface["estimated_prefill_tokens_per_second"] == 1000
    assert surface["estimated_decode_tokens_per_second"] == 40
    assert surface["estimated_runtime_overhead_seconds"] == 45
    assert surface["runtime_estimate_safety_multiplier"] == 1.5


def test_admin_quality_feedback_report_is_not_production_materialization() -> None:
    report = quality_feedback_report(
        {
            "phase": "deterministic-quality-feedback-intake",
            "sanitized_request": {
                "task": "vision",
                "intent": "understand_document_image",
                "classification": "confidential",
                "expected_output": "headline_list",
                "runtime_selection": {
                    "observed_recipe": "vision-image-standard",
                    "observed_tuple": "modal-h100-qwen25-vl-7b",
                    "observed_tuple_model": "qwen25-vl-7b",
                    "output_validated": False,
                },
                "quality_feedback": {
                    "kind": "insufficient_ocr",
                    "observed_output_kind": "short_answer",
                    "reason": "redacted caller-side quality failure summary",
                },
            },
            "redaction_report": {
                "prompt_body_forwarded": False,
                "output_body_forwarded": False,
                "data_ref_uri_forwarded": False,
                "presigned_url_forwarded": False,
            },
        }
    )

    assert report["decision"] == "ACCEPT"
    assert report["phase"] == "quality-feedback-review"
    assert report["production_config_written"] is False
    assert "canonical_recipe" not in report
    assert "activation_paths" not in report
    assert report["next_actions"] == ["review observed tuple quality evidence before changing production routing"]


def test_submission_move_uses_unique_destination_without_overwrite(tmp_path, monkeypatch) -> None:
    source = tmp_path / "rr.json"
    processed = tmp_path / "processed"
    processed.mkdir()
    source.write_text("new", encoding="utf-8")
    (processed / "rr.json").write_text("existing", encoding="utf-8")
    (processed / "rr-123-1.json").write_text("collision", encoding="utf-8")
    monkeypatch.setattr("gpucall.recipe_admin_files.time.time_ns", lambda: 123)

    destination = move_submission(source, processed / "rr.json")

    assert destination == processed / "rr-123-2.json"
    assert destination.read_text(encoding="utf-8") == "new"
    assert (processed / "rr.json").read_text(encoding="utf-8") == "existing"
    assert (processed / "rr-123-1.json").read_text(encoding="utf-8") == "collision"


def test_process_inbox_moves_processed_submission_to_failed_if_index_update_fails(tmp_path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    output_dir = tmp_path / "recipes"
    inbox.mkdir()
    (inbox / "rr-index.json").write_text(
        json.dumps(
            {
                "sanitized_request": {
                    "task": "infer",
                    "mode": "sync",
                    "intent": "summarize_text",
                    "classification": "confidential",
                    "desired_capabilities": ["summarization"],
                },
                "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
            }
        ),
        encoding="utf-8",
    )

    def fail_mark_processed(self, *args, **kwargs):
        raise RuntimeError("index update failed")

    monkeypatch.setattr(RecipeRequestIndex, "mark_processed", fail_mark_processed)

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, accept_all=True)

    assert results == [{"submission": str(inbox / "failed" / "rr-index.json"), "ok": False, "error": "index update failed"}]
    assert not (inbox / "processed" / "rr-index.json").exists()
    assert (inbox / "failed" / "rr-index.json").exists()


def test_process_quality_inbox_moves_processed_submission_to_failed_if_index_update_fails(tmp_path, monkeypatch) -> None:
    inbox = tmp_path / "quality"
    inbox.mkdir()
    (inbox / "qf-index.json").write_text(
        json.dumps(
            {
                "phase": "deterministic-quality-feedback-intake",
                "sanitized_request": {
                    "task": "vision",
                    "intent": "understand_document_image",
                    "quality_feedback": {"kind": "insufficient_ocr", "reason": "redacted summary"},
                },
                "redaction_report": {
                    "prompt_body_forwarded": False,
                    "output_body_forwarded": False,
                    "data_ref_uri_forwarded": False,
                    "presigned_url_forwarded": False,
                },
            }
        ),
        encoding="utf-8",
    )

    def fail_mark_processed(self, *args, **kwargs):
        raise RuntimeError("quality index update failed")

    monkeypatch.setattr(QualityFeedbackIndex, "mark_processed", fail_mark_processed)

    results = process_quality_inbox(inbox_dir=inbox)

    assert results == [{"submission": str(inbox / "failed" / "qf-index.json"), "ok": False, "error": "quality index update failed"}]
    assert not (inbox / "processed" / "qf-index.json").exists()
    assert (inbox / "failed" / "qf-index.json").exists()


def test_existing_tuple_activation_checks_staged_config_before_active_write(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree("gpucall/config_templates", config_dir)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    recipe = load_config(config_dir).recipes["text-infer-standard"].model_dump(mode="json")
    recipe["name"] = "infer-staged-activation-draft"
    recipe["intent"] = "staged_activation"
    recipe["auto_select"] = False
    target = config_dir / "recipes" / "infer-staged-activation-draft.yml"

    def fail_check(command):
        return {"returncode": 1, "stdout": "", "stderr": "staged config rejected"}

    monkeypatch.setattr("gpucall.recipe_admin._run_admin_check", fail_check)

    report = _auto_existing_tuple_report(
        {
            "canonical_recipe": recipe,
            "eligible_tuples": ["modal-a10g"],
            "live_validation": {"matched": [{"tuple": "modal-a10g", "recipe": recipe["name"], "path": "/tmp/modal-a10g.json"}]},
        },
        request_id="rr-stage",
        automation=RecipeAdminAutomationConfig(
            recipe_inbox_auto_materialize=True,
            recipe_inbox_auto_validate_existing_tuples=True,
            recipe_inbox_auto_activate_existing_validated_recipe=True,
            recipe_inbox_auto_run_validate_config=True,
            recipe_inbox_auto_require_auto_select_safe=False,
        ),
        report_dir=report_dir,
        config_dir=config_dir,
        validation_dir=tmp_path / "tuple-validation",
        force=False,
    )

    assert report["decision"] == "VALIDATE_CONFIG_FAILED"
    assert report["activated"] is False
    assert "activation_paths" not in report
    assert "staged_activation_paths" in report
    assert not target.exists()


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


def test_write_recipe_yaml_rejects_contract_narrowing_with_force(tmp_path) -> None:
    output_dir = tmp_path / "recipes"
    existing = canonical_recipe_from_artifact(
        {
            "sanitized_request": {
                "task": "infer",
                "mode": "async",
                "intent": "rank_text_items",
                "desired_capabilities": ["instruction_following", "ranking"],
                "error": {"context": {"context_budget_tokens": 40000}},
            },
            "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
        }
    )
    existing["allowed_modes"] = ["sync", "async"]
    write_recipe_yaml(existing, output_dir)
    before = (output_dir / "infer-rank-text-items-draft.yml").read_text(encoding="utf-8")
    proposed = dict(existing)
    proposed["context_budget_tokens"] = 32768
    proposed["max_input_bytes"] = 65536
    proposed["allowed_modes"] = ["sync"]
    proposed["required_model_capabilities"] = ["instruction_following"]

    with pytest.raises(ValueError, match="refusing to narrow existing recipe contract"):
        write_recipe_yaml(proposed, output_dir, force=True)

    assert (output_dir / "infer-rank-text-items-draft.yml").read_text(encoding="utf-8") == before


def test_write_recipe_yaml_allows_explicit_contract_narrowing(tmp_path) -> None:
    output_dir = tmp_path / "recipes"
    existing = canonical_recipe_from_artifact(
        {
            "sanitized_request": {"task": "infer", "mode": "sync", "intent": "summarize_text", "error": {"context": {"context_budget_tokens": 40000}}},
            "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
        }
    )
    write_recipe_yaml(existing, output_dir)
    proposed = dict(existing)
    proposed["context_budget_tokens"] = 32768

    write_recipe_yaml(proposed, output_dir, force=True, allow_contract_narrowing=True)

    recipe = yaml.safe_load((output_dir / "infer-summarize-text-draft.yml").read_text(encoding="utf-8"))
    assert recipe["context_budget_tokens"] == 32768


def test_admin_process_inbox_force_rejects_existing_contract_narrowing(tmp_path) -> None:
    inbox = tmp_path / "inbox"
    output_dir = tmp_path / "recipes"
    inbox.mkdir()
    existing = canonical_recipe_from_artifact(
        {
            "sanitized_request": {"task": "infer", "mode": "async", "intent": "summarize_text", "error": {"context": {"context_budget_tokens": 700000}}},
            "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
        }
    )
    write_recipe_yaml(existing, output_dir)
    recipe_path = output_dir / "infer-summarize-text-draft.yml"
    before = recipe_path.read_text(encoding="utf-8")
    (inbox / "rr-old.json").write_text(
        json.dumps(
            {
                "kind": "gpucall.recipe_request_submission",
                "request_id": "rr-old",
                "intake": {
                    "sanitized_request": {
                        "task": "infer",
                        "mode": "sync",
                        "intent": "summarize_text",
                        "error": {"context": {"context_budget_tokens": 4096}},
                    },
                    "redaction_report": {"prompt_body_forwarded": False, "data_ref_uri_forwarded": False, "presigned_url_forwarded": False},
                },
            }
        ),
        encoding="utf-8",
    )

    results = process_inbox(inbox_dir=inbox, output_dir=output_dir, accept_all=True, force=True)

    assert results[0]["ok"] is True
    assert (inbox / "processed" / "rr-old.json").exists()
    assert recipe_path.read_text(encoding="utf-8") == before
    report = json.loads((inbox / "reports" / "rr-old.report.json").read_text(encoding="utf-8"))
    assert report["processing_action"] == "existing_recipe_retained"
    assert report["contract_narrowing_reasons"]


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
    assert "draft validation rejected submission" in record["error"]
    assert "missing redaction_report" in record["error"]
    report = json.loads((inbox / "reports" / "rr-bad.report.json").read_text(encoding="utf-8"))
    assert report["decision"] == "REJECTED_DRAFT"
    assert report["draft_validation"]["decision"] == "REJECTED_DRAFT"


def test_admin_process_quality_inbox_accepts_quality_feedback_without_recipe_materialization(tmp_path) -> None:
    inbox = tmp_path / "quality_feedback" / "inbox"
    inbox.mkdir(parents=True)
    submission = {
        "kind": "gpucall.recipe_request_submission",
        "request_id": "rr-quality",
        "source": "example-quality-caller",
        "intake": {
            "phase": "deterministic-quality-feedback-intake",
            "llm_safe": True,
            "sanitized_request": {
                "task": "vision",
                "mode": "sync",
                "intent": "understand_document_image",
                "classification": "newspaper_frontpage_article_extraction",
                "expected_output": "json_schema: articles array",
                "runtime_selection": {
                    "observed_recipe": "vision-understand-document-image-draft",
                    "observed_tuple": "modal-h100-qwen25-vl-3b",
                    "observed_tuple_model": "qwen25-vl-3b",
                    "output_validated": False,
                },
                "quality_feedback": {
                    "kind": "schema_noncompliance",
                    "reason": "21 of 27 caller schema checks failed",
                    "observed_output_kind": "schema_violation",
                    "output_contract_feedback": {
                        "response_format": "json_schema",
                        "expected_json_schema": {"type": "object"},
                        "observed_json_schema": {"type": "object"},
                        "schema_success_count": 6,
                        "schema_failure_count": 21,
                        "raw_output_forwarded": False,
                    },
                },
            },
            "redaction_report": {
                "prompt_body_forwarded": False,
                "message_content_forwarded": False,
                "data_ref_uri_forwarded": False,
                "presigned_url_forwarded": False,
                "output_body_forwarded": False,
            },
            "redacted_error_payload": {},
        },
        "draft": None,
    }
    (inbox / "rr-quality.json").write_text(json.dumps(submission), encoding="utf-8")

    results = process_quality_inbox(inbox_dir=inbox)

    assert results == [{"submission": str(inbox / "processed" / "rr-quality.json"), "report": str(inbox / "reports" / "rr-quality.report.json"), "ok": True}]
    assert (inbox / "processed" / "rr-quality.json").exists()
    assert not (tmp_path / "quality_feedback" / "recipes").exists()
    report = json.loads((inbox / "reports" / "rr-quality.report.json").read_text(encoding="utf-8"))
    assert report["decision"] == "ACCEPT"
    assert report["classification"] == "newspaper_frontpage_article_extraction"
    assert report["observed"]["tuple"] == "modal-h100-qwen25-vl-3b"
    assert report["output_contract_feedback"]["schema_failure_count"] == 21
    record = QualityFeedbackIndex(inbox / "quality_feedback.db").get("rr-quality")
    assert record is not None
    assert record["status"] == "processed"
    assert record["quality_kind"] == "schema_noncompliance"
    status = quality_feedback_status("rr-quality", inbox)
    assert status["state"] == "processed"
    assert status["report"]["decision"] == "ACCEPT"


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

    assert report["decision"] == "READY_FOR_VALIDATION"
    assert report["required_execution_contract"]["model_capabilities"] == [
        "document_understanding",
        "visual_question_answering",
        "instruction_following",
    ]
    assert report["required_execution_contract"]["live_validation_required"] is True
    assert report["required_execution_contract"]["quality_failure_to_correct"]["kind"] == "insufficient_ocr"
    assert "modal-h100-qwen25-vl-7b" in report["eligible_tuples"]
    assert report["tuple_candidate_matches"] == []


def test_admin_review_classifies_schema_mismatch_feedback() -> None:
    artifact = {
        "phase": "deterministic-quality-feedback-intake",
        "sanitized_request": {
            "task": "vision",
            "mode": "sync",
            "intent": "understand_document_image",
            "classification": "confidential",
            "expected_output": "articles_json",
            "desired_capabilities": ["document_understanding", "visual_question_answering", "instruction_following"],
            "quality_feedback": {
                "kind": "schema_mismatch",
                "observed_output_kind": "json_object_wrong_schema",
                "output_contract_feedback": {
                    "response_format": "json_object",
                    "expected_json_schema": {"type": "object", "required": ["articles"], "properties": {"articles": {"type": "array"}}},
                    "observed_json_schema": {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}},
                    "schema_success_count": 5,
                    "schema_failure_count": 16,
                    "raw_output_forwarded": False,
                },
            },
        },
        "redaction_report": {
            "prompt_body_forwarded": False,
            "output_body_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
        },
    }

    report = review_artifact(artifact, config_dir="gpucall/config_templates")
    contract = report["required_execution_contract"]

    assert contract["quality_failure_to_correct"]["kind"] == "schema_mismatch"
    assert contract["output_contract_feedback"]["response_format"] == "json_object"
    assert contract["output_contract_feedback"]["expected_json_schema_present"] is True
    assert contract["output_contract_feedback"]["schema_failure_count"] == 16
    assert contract["caller_action"].startswith("send response_format=json_schema")


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

    assert report["decision"] == "READY_FOR_VALIDATION"
    assert report["required_execution_contract"]["min_model_len"] == 1010000
    assert report["required_execution_contract"]["min_vram_gb"] == 320
    assert "modal-h200x4-qwen25-14b-1m" in report["eligible_tuples"]
    assert report["tuple_candidate_matches"] == []


def test_review_uses_existing_template_vision_tuple_before_candidate_promotion() -> None:
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

    assert review["decision"] == "READY_FOR_VALIDATION"
    assert "modal-h100-qwen25-vl-7b" in review["eligible_tuples"]
    assert review["tuple_candidate_matches"] == []


def test_promotion_validation_mode_follows_recipe_allowed_modes() -> None:
    assert _validation_mode("text-infer-standard", Path("gpucall/config_templates")) == "sync"
    assert _validation_mode("infer-rank-text-items-draft", Path("gpucall/config_templates")) == "sync"
