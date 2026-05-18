import pytest
from gpucall.workload_contract import workload_profile_from_assessment, contract_to_recipe_intake, draft_workload_contract
from gpucall.recipe_materialize import canonical_recipe_from_artifact
from gpucall.recipe_intents import is_valid_production_intent

def test_rss_semantic_match_detection():
    assessment = {
        "findings": [
            {
                "path": "src/news_system.py",
                "symbol": "fetch_rss_and_match",
                "detail": "semantic match for feeds",
                "kind": "function"
            }
        ]
    }
    profile = workload_profile_from_assessment(assessment)
    workload = profile["workloads"][0]
    
    assert workload["intent"] == "rss_semantic_match"
    assert workload["input_profile"]["context_budget_tokens"] == 131072

def test_pairwise_match_detection():
    assessment = {
        "findings": [
            {
                "path": "src/matching.py",
                "symbol": "calculate_pairwise_similarity",
                "detail": "compare two items",
                "kind": "function"
            }
        ]
    }
    profile = workload_profile_from_assessment(assessment)
    workload = profile["workloads"][0]
    
    assert workload["intent"] == "pairwise_match"
    assert workload["input_profile"]["context_budget_tokens"] == 131072

def test_integrated_news_analysis_detection_wins_over_rss_words():
    assessment = {
        "findings": [
            {
                "path": "src/analyze/topic_engine.py",
                "symbol": "build_rankings",
                "detail": "RSS and Vision integrated analysis returns source_articles and east_west_gap importance ranking",
                "kind": "function",
            }
        ]
    }
    profile = workload_profile_from_assessment(assessment)
    workload = profile["workloads"][0]

    assert workload["intent"] == "rank_text_items"
    assert workload["input_profile"]["context_budget_tokens"] == 131072

def test_document_vision_contract_is_async_and_overdeclared():
    assessment = {
        "findings": [
            {
                "path": "src/analyze/overseas_vision.py",
                "symbol": "analyze_frontpage_image",
                "detail": "vision frontpage OCR article extraction",
                "kind": "function",
            }
        ]
    }
    profile = workload_profile_from_assessment(assessment)
    contract = draft_workload_contract(profile)
    workload = contract["workloads"][0]

    assert workload["intent"] == "understand_document_image"
    assert workload["modes"] == ["async"]
    assert workload["quality_contract"]["metrics"]["max_provider_temporary_failures"] == 0

def test_unknown_workload_fallback():
    # Assessment that doesn't match any deterministic rule
    assessment = {
        "findings": [
            {
                "path": "src/misc.py",
                "symbol": "do_something_generic",
                "detail": "generic processing",
                "kind": "function"
            }
        ]
    }
    profile = workload_profile_from_assessment(assessment)
    # _workload_seed should have defaulted to 'standard_text_inference' if it didn't match
    # but wait, let's look at _detected_workloads in workload_contract.py
    # if intent is None, it continues.
    # if no findings result in an intent, it falls back to [_workload_seed("infer", "standard_text_inference", evidence=[])]
    
    # Let's force an empty intent workload manually for the intake test
    contract = {
        "workloads": [
            {
                "id": "infer.generic",
                "task": "infer",
                "intent": "generic", # generic is invalid
                "input_profile": {"context_budget_tokens": 8192},
                "quality_contract": {}
            }
        ]
    }
    
    intake = contract_to_recipe_intake(contract)
    intent = intake["sanitized_request"]["intent"]
    assert intent.startswith("unknown_workload_")
    assert intake["sanitized_request"]["classification"] == "incomplete_draft"

def test_missing_quality_metrics_becomes_incomplete():
    contract = {
        "workloads": [
            {
                "id": "infer.summarize_text",
                "task": "infer",
                "intent": "summarize_text",
                "input_profile": {"context_budget_tokens": 8192},
                "quality_contract": {"missing_baseline_metrics": True}
            }
        ]
    }
    intake = contract_to_recipe_intake(contract)
    assert intake["sanitized_request"]["classification"] == "incomplete_draft"

def test_materialization_quality_floor_for_unknown():
    artifact = {
        "sanitized_request": {
            "task": "infer",
            "intent": "unknown_workload_1234abcd",
            "classification": "incomplete_draft",
            "expected_output": "plain_text",
            "error": {"context": {"context_budget_tokens": 8192}}
        }
    }
    with pytest.raises(ValueError, match="unknown workload intent requires operator mapping"):
        canonical_recipe_from_artifact(artifact)

def test_intent_validation_helper():
    assert is_valid_production_intent("summarize_text") is True
    assert is_valid_production_intent("rss_semantic_match") is True
    assert is_valid_production_intent("infer") is False
    assert is_valid_production_intent("generic") is False
    assert is_valid_production_intent("") is False
    assert is_valid_production_intent(None) is False
