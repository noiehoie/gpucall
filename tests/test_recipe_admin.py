from __future__ import annotations

import json

import pytest
import yaml

from gpucall.recipe_admin import canonical_recipe_from_artifact, main


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
            "error": {"context": {"required_model_len": 9000}},
        },
    }

    recipe = canonical_recipe_from_artifact(intake)

    assert recipe["name"] == "vision-understand-document-image-draft"
    assert recipe["task"] == "vision"
    assert recipe["min_vram_gb"] == 80
    assert recipe["max_model_len"] == 32768
    assert recipe["allowed_mime_prefixes"] == ["image/"]
    assert recipe["allowed_inline_mime_prefixes"] == ["text/"]
    assert "required_model_capabilities" not in recipe


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
                    "error": {"context": {"required_model_len": 40000}},
                }
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
    assert recipe["max_model_len"] == 65536
    assert report["policy"] == "accept-all"
    assert report["human_review_bypassed"] is True
