from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from gpucall.recipe_intents import (
    capabilities_for,
    is_valid_production_intent,
    normalize_intent,
)


CONTRACT_SCHEMA_VERSION = 1
TRACE_SCHEMA_VERSION = 1
PROFILE_SCHEMA_VERSION = 1

QUALITY_TOPIC_RATIO = 0.8
QUALITY_SOURCE_RATIO = 0.8
QUALITY_RESPONSE_CHAR_RATIO = 0.5

INTENT_ORDER = (
    "rss_semantic_match",
    "pairwise_match",
    "rank_text_items",
    "summarize_text",
    "understand_document_image",
    "translate_text",
    "extract_json",
)


def parse_trace_text(
    text: str,
    *,
    source: str | None = None,
    backend: str | None = None,
    command: str | None = None,
    returncode: int | None = None,
    duration_seconds: float | None = None,
    log_path: str | None = None,
) -> dict[str, Any]:
    """Extract sanitized deterministic metrics from command output or logs.

    The trace intentionally stores only counts, lengths, hashes, and booleans.
    It never stores raw prompts, model output, article text, URLs, or log tails.
    """

    metrics = _empty_metrics()
    _merge_metrics(metrics, _regex_metrics(text))
    for payload in _iter_json_objects(text):
        _merge_metrics(metrics, _json_metrics(payload))
    metrics = _finalize_metrics(metrics)
    lines = text.splitlines()
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "phase": "workload-trace",
        "source": source,
        "backend": backend,
        "command": command,
        "returncode": returncode,
        "duration_seconds": round(float(duration_seconds), 3) if duration_seconds is not None else None,
        "log_path": log_path,
        "observed_at_unix": int(time.time()),
        "log_fingerprint": {
            "sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
            "line_count": len(lines),
            "byte_count": len(text.encode("utf-8", errors="replace")),
            "raw_forwarded": False,
        },
        "metrics": metrics,
        "workload_hints": _workload_hints_from_metrics(metrics),
        "redaction_report": {
            "raw_log_forwarded": False,
            "prompt_body_forwarded": False,
            "model_output_forwarded": False,
            "url_forwarded": False,
        },
    }


def workload_profile_from_assessment(
    assessment: Mapping[str, Any],
    *,
    traces: Iterable[Mapping[str, Any]] = (),
    source: str | None = None,
) -> dict[str, Any]:
    rows = [row for row in assessment.get("findings", []) if isinstance(row, Mapping)]
    trace_list = [dict(item) for item in traces]
    detected = _detected_workloads(rows)
    for workload in detected:
        _attach_trace_metrics(workload, trace_list)
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "phase": "workload-profile",
        "source": source or _str_or_none(assessment.get("source")),
        "project": _str_or_none(assessment.get("project")),
        "summary": dict(assessment.get("summary") or {}),
        "workloads": detected,
        "traces": [_trace_summary(item) for item in trace_list],
        "redaction_report": {
            "raw_prompt_forwarded": False,
            "raw_output_forwarded": False,
            "raw_log_forwarded": False,
        },
    }


def draft_workload_contract(profile: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    workloads = []
    for raw in profile.get("workloads", []) or []:
        if not isinstance(raw, Mapping):
            continue
        workload = _contract_workload(raw)
        workloads.append(workload)
    workloads.sort(key=lambda item: (INTENT_ORDER.index(item["intent"]) if item["intent"] in INTENT_ORDER else 99, item["id"]))
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "phase": "workload-contract",
        "source": source or _str_or_none(profile.get("source")),
        "project": _str_or_none(profile.get("project")),
        "primary_workload_id": workloads[0]["id"] if workloads else None,
        "workloads": workloads,
        "submission": {
            "llm_safe": True,
            "raw_prompt_forwarded": False,
            "raw_output_forwarded": False,
            "raw_log_forwarded": False,
        },
        "operator_notes": [
            "This contract is generated from deterministic source and trace metadata only.",
            "It declares caller-side success metrics; the gateway must not infer subjective quality.",
            "Production activation still requires admin review, config validation, and tuple validation evidence.",
        ],
    }


def compare_trace_to_contract(contract: Mapping[str, Any], trace: Mapping[str, Any]) -> dict[str, Any]:
    metrics = _metrics(trace)
    results = []
    for workload in contract.get("workloads", []) or []:
        if not isinstance(workload, Mapping):
            continue
        results.append(_compare_workload(workload, metrics))
    violations = [violation for result in results for violation in result["violations"]]
    return {
        "schema_version": 1,
        "phase": "workload-contract-comparison",
        "source": contract.get("source") or trace.get("source"),
        "ok": not violations,
        "contract_phase": contract.get("phase"),
        "trace_phase": trace.get("phase"),
        "workload_results": results,
        "violations": violations,
        "caller_action": "submit_contract_feedback_to_gpucall_admin" if violations else "none",
    }


def merge_traces(traces: Iterable[Mapping[str, Any]], *, source: str | None = None, backend: str | None = None) -> dict[str, Any]:
    trace_list = [trace for trace in traces if isinstance(trace, Mapping)]
    metrics = _empty_metrics()
    hints: set[str] = set()
    fingerprints = []
    returncodes: list[int] = []
    duration = 0.0
    for trace in trace_list:
        _merge_metrics(metrics, _metrics(trace))
        hints.update(str(item) for item in trace.get("workload_hints") or [])
        fingerprint = trace.get("log_fingerprint")
        if isinstance(fingerprint, Mapping):
            fingerprints.append(dict(fingerprint))
        if isinstance(trace.get("returncode"), int):
            returncodes.append(int(trace["returncode"]))
        if isinstance(trace.get("duration_seconds"), (int, float)):
            duration += float(trace["duration_seconds"])
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "phase": "workload-trace",
        "source": source,
        "backend": backend,
        "merged": True,
        "trace_count": len(trace_list),
        "returncode": max(returncodes) if returncodes else None,
        "duration_seconds": round(duration, 3) if duration else None,
        "metrics": _finalize_metrics(metrics),
        "workload_hints": sorted(hints) or _workload_hints_from_metrics(metrics),
        "log_fingerprints": fingerprints,
        "redaction_report": {
            "raw_log_forwarded": False,
            "prompt_body_forwarded": False,
            "model_output_forwarded": False,
            "url_forwarded": False,
        },
    }


def contract_to_recipe_intake(contract: Mapping[str, Any], *, workload_id: str | None = None) -> dict[str, Any]:
    workload = _select_workload(contract, workload_id)
    if workload is None:
        raise ValueError("workload contract has no workloads")
    input_profile = _mapping(workload.get("input_profile"))
    quality = _mapping(workload.get("quality_contract"))
    output_contract = str(_mapping(workload.get("output_profile")).get("output_contract") or quality.get("output_contract") or "plain_text")
    task = str(workload.get("task") or "infer")

    raw_intent = _str_or_none(workload.get("intent"))
    if is_valid_production_intent(raw_intent):
        intent = str(normalize_intent(raw_intent))
    else:
        intent = _unknown_intent_for_text(json.dumps(dict(workload), sort_keys=True, default=str))

    grammar_blockers = _intake_grammar_blockers(workload=workload, intent=intent, output_contract=output_contract, quality=quality)
    is_incomplete = bool(grammar_blockers)

    mode = _first_mode(workload.get("modes"))
    return {
        "schema_version": 1,
        "phase": "deterministic-contract-intake",
        "llm_safe": True,
        "sanitized_request": {
            "task": task,
            "mode": mode,
            "intent": intent,
            "business_need": f"materialized from workload contract {workload.get('id')}",
            "classification": "incomplete_draft" if is_incomplete else str(workload.get("classification") or "confidential"),
            "expected_output": output_contract,
            "error": {
                "code": None,
                "detail_kind": "workload_contract",
                "context": {
                    "context_budget_tokens": _positive_int(input_profile.get("context_budget_tokens"), default=32768),
                    "largest_auto_recipe_context_budget_tokens": None,
                },
                "rejections": [],
            },
            "input_summary": {
                "content_types": list(input_profile.get("content_types") or []),
                "max_bytes": input_profile.get("max_bytes"),
                "input_count": input_profile.get("input_count"),
                "prompt_lengths": [],
            },
            "desired_capabilities": capabilities_for(task=task, intent=intent),
            "quality_contract": quality,
            "draft_grammar": {
                "materialization_allowed": not is_incomplete,
                "blockers": grammar_blockers,
                "caller_bias": "overdeclare_when_uncertain",
                "admin_policy": "reject_or_narrow_deterministically",
            },
        },
        "workload_contract": dict(workload),
        "redaction_report": {
            "removed_fields": [],
            "prompt_body_forwarded": False,
            "message_content_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
            "output_body_forwarded": False,
            "raw_log_forwarded": False,
        },
        "redacted_error_payload": {},
    }


def _intake_grammar_blockers(*, workload: Mapping[str, Any], intent: str, output_contract: str, quality: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    task = str(workload.get("task") or "")
    if not intent or intent == task:
        blockers.append("intent must be explicit and must not fall back to task")
    if not is_valid_production_intent(intent):
        blockers.append(f"intent is not in the production intent registry: {intent}")
    if intent.startswith("unknown_workload_"):
        blockers.append("unknown workload requires operator intent mapping before materialization")
    if output_contract not in {"plain_text", "plain-text", "json_object", "json_schema"}:
        blockers.append(f"unsupported output_contract: {output_contract}")
    if not quality:
        blockers.append("quality_contract is required")
    if quality.get("missing_baseline_metrics"):
        blockers.append("quality_contract requires baseline metrics before recipe materialization")
    if not _mapping(quality.get("metrics")):
        blockers.append("quality_contract.metrics must not be empty")
    context_budget = _positive_int(_mapping(workload.get("input_profile")).get("context_budget_tokens"), default=0)
    if context_budget <= 0:
        blockers.append("input_profile.context_budget_tokens is required")
    return blockers


def _empty_metrics() -> dict[str, Any]:
    return {
        "input_chars": None,
        "estimated_input_tokens": None,
        "input_candidates": None,
        "selected_items": None,
        "selection_cap": None,
        "response_chars": None,
        "topics_count": None,
        "source_count": None,
        "articles_count": None,
        "schema_success": None,
        "elapsed_seconds": None,
        "no_auto_selectable_recipe_count": 0,
        "http_422_count": 0,
        "json_extract_failures": 0,
        "rss_match_total": None,
        "rss_match_matched": None,
        "commentary_count": None,
    }


def _regex_metrics(text: str) -> dict[str, Any]:
    metrics = _empty_metrics()
    patterns = {
        "response_chars": [
            r"\bresponse_len\s*[=:]\s*(\d+)\b",
            r"\bresponse_chars\s*[=:]\s*(\d+)\b",
        ],
        "topics_count": [
            r"Analysis complete:\s*(\d+)\s+topics?\s+ranked",
            r"\btopics?_count\s*[=:]\s*(\d+)\b",
            r"\btopics?\s*[=:]\s*(\d+)\b",
        ],
        "source_count": [
            r"\bsource_count\s*[=:]\s*(\d+)\b",
            r"\bsources_count\s*[=:]\s*(\d+)\b",
            r"\bsource\s+count\s*[=:]\s*(\d+)\b",
        ],
        "articles_count": [
            r"\barticles\s*[=:]\s*(\d+)\b",
            r"\barticles_count\s*[=:]\s*(\d+)\b",
        ],
        "elapsed_seconds": [
            r"\belapsed\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)s?\b",
        ],
    }
    for key, regexes in patterns.items():
        values: list[int | float] = []
        for pattern in regexes:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                raw = match.group(1)
                values.append(float(raw) if "." in raw else int(raw))
        if values:
            metrics[key] = max(values)
    compression = re.search(
        r"candidates=(\d+)\s+selected=(\d+)\s+cap=(\d+)\s+chars=(\d+)\s+est_tokens=(\d+)",
        text,
    )
    if compression:
        metrics["input_candidates"] = int(compression.group(1))
        metrics["selected_items"] = int(compression.group(2))
        metrics["selection_cap"] = int(compression.group(3))
        metrics["input_chars"] = int(compression.group(4))
        metrics["estimated_input_tokens"] = int(compression.group(5))
    schema = re.findall(r"\bschema_(?:parse|success)\s*[=:]\s*(true|false|yes|no|0|1)\b", text, flags=re.IGNORECASE)
    if schema:
        metrics["schema_success"] = _parse_bool(schema[-1])
    metrics["no_auto_selectable_recipe_count"] = text.count("NO_AUTO_SELECTABLE_RECIPE")
    metrics["http_422_count"] = len(re.findall(r"/v2/tasks/[^ ]+\s+\"HTTP/1\.1 422", text))
    metrics["json_extract_failures"] = text.count("Could not extract JSON")
    match = re.search(r"\[OverseasVision/RSSマッチ\]\s+全体:\s*(\d+)/(\d+)", text)
    if match:
        metrics["rss_match_matched"] = int(match.group(1))
        metrics["rss_match_total"] = int(match.group(2))
    commentary = re.search(r"\[E/コメント\]\s+完了:\s*(\d+)件", text)
    if commentary:
        metrics["commentary_count"] = int(commentary.group(1))
    return metrics


def _iter_json_objects(text: str) -> Iterable[Mapping[str, Any]]:
    stripped_text = text.strip()
    if stripped_text.startswith("{") and stripped_text.endswith("}"):
        try:
            payload = json.loads(stripped_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            yield payload
            if "\n" not in stripped_text:
                return
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            yield payload


def _json_metrics(payload: Mapping[str, Any]) -> dict[str, Any]:
    metrics = _empty_metrics()
    _walk_json_metrics(payload, metrics)
    return metrics


def _walk_json_metrics(value: Any, metrics: dict[str, Any]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in {"response_len", "response_chars", "chars"}:
                metrics["response_chars"] = _max_int(metrics["response_chars"], item)
            elif normalized in {"topic_count", "topics_count"}:
                metrics["topics_count"] = _max_int(metrics["topics_count"], item)
            elif normalized in {"topics", "rankings"} and isinstance(item, list):
                metrics["topics_count"] = _max_int(metrics["topics_count"], len(item))
            elif normalized in {"source_count", "sources_count"}:
                metrics["source_count"] = _max_int(metrics["source_count"], item)
            elif normalized in {"sources", "source_articles"} and isinstance(item, list):
                metrics["source_count"] = _max_int(metrics["source_count"], len(item))
            elif normalized in {"articles_count", "article_count"}:
                metrics["articles_count"] = _max_int(metrics["articles_count"], item)
            elif normalized == "articles" and isinstance(item, list):
                metrics["articles_count"] = _max_int(metrics["articles_count"], len(item))
            elif normalized in {"schema_success", "schema_parse", "schema_valid"}:
                parsed = _parse_bool(item)
                if parsed is not None:
                    metrics["schema_success"] = parsed
            elif normalized in {"elapsed", "elapsed_seconds", "duration_seconds"}:
                metrics["elapsed_seconds"] = _max_float(metrics["elapsed_seconds"], item)
            elif normalized in {"estimated_input_tokens", "est_tokens"}:
                metrics["estimated_input_tokens"] = _max_int(metrics["estimated_input_tokens"], item)
            elif normalized in {"input_chars", "prompt_chars"}:
                metrics["input_chars"] = _max_int(metrics["input_chars"], item)
            _walk_json_metrics(item, metrics)
    elif isinstance(value, list):
        for item in value:
            _walk_json_metrics(item, metrics)


def _detected_workloads(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        task, intent, context_budget = _guess_from_finding(row)
        if intent is None:
            continue
        key = (task, intent)
        grouped.setdefault(key, _workload_seed(task, intent, evidence=[]))
        grouped[key]["evidence"].append(
            {
                "path": row.get("path"),
                "line": row.get("line"),
                "kind": row.get("kind"),
                "symbol": row.get("symbol"),
            }
        )
        grouped[key]["input_profile"]["context_budget_tokens"] = max(
            _positive_int(grouped[key]["input_profile"].get("context_budget_tokens"), default=0),
            context_budget,
        )
    return list(grouped.values())


def _guess_from_finding(row: Mapping[str, Any]) -> tuple[str, str | None, int]:
    text = " ".join(str(row.get(key, "")) for key in ("path", "symbol", "detail")).lower()
    if "translate" in text or "translation" in text:
        return "infer", "translate_text", 32768
    if "vision" in text or "image" in text or "ocr" in text or "frontpage" in text:
        return "vision", "understand_document_image", 8192
    if "rss" in text or "feed" in text or "semantic" in text:
        return "infer", "rss_semantic_match", 131072
    if "pair" in text or "match" in text:
        if "topic" in text or "rank" in text:
            return "infer", "rank_text_items", 131072
        return "infer", "pairwise_match", 131072
    if "topic" in text or "rank" in text or "ranking" in text or "score" in text:
        return "infer", "rank_text_items", 131072
    if "summary" in text or "summarize" in text:
        return "infer", "summarize_text", 65536
    if "json" in text or "schema" in text or "extract" in text:
        return "infer", "extract_json", 32768
    return "infer", _unknown_intent_for_text(text), 131072


def _workload_seed(task: str, intent: str, *, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_intent = normalize_intent(intent) or intent
    context_budget = 8192 if task == "vision" else 32768
    if normalized_intent in {"rank_text_items", "rss_semantic_match", "pairwise_match", "summarize_text"}:
        context_budget = 65536 if normalized_intent == "summarize_text" else 131072
    max_bytes = 2_000_000 if task == "vision" else max(16_384, context_budget * 8)
    return {
        "id": f"{task}.{normalized_intent}",
        "task": task,
        "intent": normalized_intent,
        "classification": "confidential",
        "modes": ["sync", "async"],
        "input_profile": {
            "content_types": ["image/"] if task == "vision" else ["text/plain"],
            "max_bytes": max_bytes,
            "input_count": None,
            "context_budget_tokens": context_budget,
        },
        "output_profile": {
            "output_contract": "json_object"
            if normalized_intent in {"rank_text_items", "rss_semantic_match", "pairwise_match", "extract_json", "understand_document_image"}
            else "plain_text",
        },
        "evidence": evidence,
        "trace_metrics": {},
    }


def _attach_trace_metrics(workload: dict[str, Any], traces: list[Mapping[str, Any]]) -> None:
    intent = str(workload.get("intent") or "")
    merged = _empty_metrics()
    for trace in traces:
        metrics = _metrics(trace)
        hints = set(str(item) for item in trace.get("workload_hints") or [])
        matched = intent in hints if hints else _metrics_match_intent(metrics, intent)
        if matched:
            _merge_metrics(merged, metrics)
    workload["trace_metrics"] = _finalize_metrics(merged)
    input_profile = _mapping(workload.get("input_profile"))
    if workload["trace_metrics"].get("input_chars"):
        input_profile["observed_input_chars"] = workload["trace_metrics"]["input_chars"]
    if workload["trace_metrics"].get("estimated_input_tokens"):
        input_profile["context_budget_tokens"] = max(
            _positive_int(input_profile.get("context_budget_tokens"), default=32768),
            _round_context_budget(int(workload["trace_metrics"]["estimated_input_tokens"]) * 2),
        )
    workload["input_profile"] = dict(input_profile)


def _contract_workload(workload: Mapping[str, Any]) -> dict[str, Any]:
    task = str(workload.get("task") or "infer")
    intent = normalize_intent(_str_or_none(workload.get("intent"))) or _unknown_intent_for_text(str(workload.get("id") or task))
    input_profile = dict(_mapping(workload.get("input_profile")))
    trace_metrics = _mapping(workload.get("trace_metrics"))
    output_profile = dict(_mapping(workload.get("output_profile")))
    output_profile.update(_output_profile_from_metrics(intent, trace_metrics))
    quality = _quality_contract(intent, trace_metrics, output_profile)
    modes = [str(item) for item in workload.get("modes") or ["sync"]]
    return {
        "id": str(workload.get("id") or f"{task}.{intent}"),
        "task": task,
        "intent": intent,
        "classification": str(workload.get("classification") or "confidential"),
        "modes": modes,
        "required_capabilities": capabilities_for(task=task, intent=intent),
        "input_profile": input_profile,
        "output_profile": output_profile,
        "quality_contract": quality,
        "budget_contract": {
            "max_request_usd": None,
            "requires_operator_budget": True,
            "reservation_must_separate_standing_and_runtime": True,
        },
        "latency_contract": {
            "recommended_mode": "async" if _positive_int(input_profile.get("context_budget_tokens"), default=0) > 32768 else modes[0],
            "timeout_seconds": 1800 if task == "vision" else (900 if _positive_int(input_profile.get("context_budget_tokens"), default=0) > 32768 else 180),
        },
        "evidence": list(workload.get("evidence") or [])[:20],
    }


def _output_profile_from_metrics(intent: str, metrics: Mapping[str, Any]) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    if intent == "rank_text_items":
        profile["observed_topics_count"] = metrics.get("topics_count")
        profile["observed_source_count"] = metrics.get("source_count")
        profile["observed_response_chars"] = metrics.get("response_chars")
        profile.setdefault("output_contract", "json_object")
    elif intent in {"rss_semantic_match", "pairwise_match"}:
        profile["observed_response_chars"] = metrics.get("response_chars")
        profile["observed_rss_match_total"] = metrics.get("rss_match_total")
        profile["observed_rss_match_matched"] = metrics.get("rss_match_matched")
        profile.setdefault("output_contract", "json_object")
    elif intent == "understand_document_image":
        profile["observed_articles_count"] = metrics.get("articles_count")
        profile["observed_schema_success"] = metrics.get("schema_success")
        profile["observed_response_chars"] = metrics.get("response_chars")
        profile.setdefault("output_contract", "json_object")
    else:
        profile["observed_response_chars"] = metrics.get("response_chars")
    return profile


def _quality_contract(intent: str, metrics: Mapping[str, Any], output_profile: Mapping[str, Any]) -> dict[str, Any]:
    quality: dict[str, Any] = {
        "contract_kind": "deterministic_metrics",
        "gateway_may_infer_quality": False,
        "raw_output_required": False,
        "output_contract": output_profile.get("output_contract") or "plain_text",
        "metrics": {},
    }
    metric_contract = quality["metrics"]
    metric_contract["max_no_auto_selectable_recipe"] = 0
    metric_contract["max_http_422"] = 0
    metric_contract["max_json_extract_failures"] = 0
    response_chars = _optional_int(metrics.get("response_chars"))
    if response_chars is not None and response_chars > 0:
        metric_contract["min_response_chars"] = max(1, math.floor(response_chars * QUALITY_RESPONSE_CHAR_RATIO))
    if intent == "rank_text_items":
        topics = _optional_int(metrics.get("topics_count"))
        sources = _optional_int(metrics.get("source_count"))
        if topics is not None and topics > 0:
            metric_contract["min_topics"] = max(1, math.floor(topics * QUALITY_TOPIC_RATIO))
        if sources is not None and sources > 0:
            metric_contract["min_sources"] = max(1, math.floor(sources * QUALITY_SOURCE_RATIO))
    if intent in {"rss_semantic_match", "pairwise_match"}:
        matched = _optional_int(metrics.get("rss_match_matched"))
        total = _optional_int(metrics.get("rss_match_total"))
        if matched is not None and matched > 0:
            metric_contract["min_rss_matches"] = max(1, math.floor(matched * QUALITY_SOURCE_RATIO))
        if total is not None and total > 0:
            metric_contract["min_rss_match_total"] = total
    if intent == "understand_document_image":
        articles = _optional_int(metrics.get("articles_count"))
        if articles is not None and articles > 0:
            metric_contract["min_articles"] = max(1, math.floor(articles * QUALITY_TOPIC_RATIO))
        if metrics.get("schema_success") is True:
            metric_contract["require_schema_success"] = True
    elif metrics.get("schema_success") is True:
        metric_contract["require_schema_success"] = True
    if set(metric_contract) == {"max_no_auto_selectable_recipe", "max_http_422", "max_json_extract_failures"}:
        quality["missing_baseline_metrics"] = True
    return quality


def _compare_workload(workload: Mapping[str, Any], metrics: Mapping[str, Any]) -> dict[str, Any]:
    quality = _mapping(workload.get("quality_contract"))
    required = _mapping(quality.get("metrics"))
    violations: list[dict[str, Any]] = []
    checks = {
        "min_response_chars": ("response_chars", "response chars below caller contract"),
        "min_topics": ("topics_count", "topic count below caller contract"),
        "min_sources": ("source_count", "source count below caller contract"),
        "min_articles": ("articles_count", "article count below caller contract"),
        "min_rss_matches": ("rss_match_matched", "RSS semantic match count below caller contract"),
        "min_rss_match_total": ("rss_match_total", "RSS semantic match total below caller contract"),
    }
    for requirement, (metric_name, reason) in checks.items():
        if requirement not in required:
            continue
        observed = _optional_int(metrics.get(metric_name))
        minimum = _positive_int(required.get(requirement), default=1)
        if observed is None or observed < minimum:
            violations.append(
                {
                    "workload_id": workload.get("id"),
                    "metric": metric_name,
                    "required": minimum,
                    "observed": observed,
                    "reason": reason,
                }
            )
    maximum_checks = {
        "max_no_auto_selectable_recipe": ("no_auto_selectable_recipe_count", "recipe routing failures are not allowed in a successful canary"),
        "max_http_422": ("http_422_count", "HTTP 422 routing/admission failures are not allowed in a successful canary"),
        "max_json_extract_failures": ("json_extract_failures", "JSON extraction failures are not allowed in a successful canary"),
    }
    for requirement, (metric_name, reason) in maximum_checks.items():
        if requirement not in required:
            continue
        observed = _optional_int(metrics.get(metric_name)) or 0
        maximum = _optional_int(required.get(requirement))
        if maximum is not None and observed > maximum:
            violations.append(
                {
                    "workload_id": workload.get("id"),
                    "metric": metric_name,
                    "required": maximum,
                    "observed": observed,
                    "reason": reason,
                }
            )
    if required.get("require_schema_success") is True and metrics.get("schema_success") is not True:
        violations.append(
            {
                "workload_id": workload.get("id"),
                "metric": "schema_success",
                "required": True,
                "observed": metrics.get("schema_success"),
                "reason": "schema validation did not pass",
            }
        )
    return {
        "workload_id": workload.get("id"),
        "task": workload.get("task"),
        "intent": workload.get("intent"),
        "ok": not violations,
        "violations": violations,
    }


def _workload_hints_from_metrics(metrics: Mapping[str, Any]) -> list[str]:
    hints: list[str] = []
    if metrics.get("rss_match_total") is not None:
        hints.append("rss_semantic_match")
    if metrics.get("topics_count") is not None or metrics.get("source_count") is not None:
        hints.append("rank_text_items")
    if metrics.get("articles_count") is not None:
        hints.append("understand_document_image")
    if metrics.get("response_chars") is not None and not hints:
        hints.append("summarize_text")
    return hints


def _metrics_match_intent(metrics: Mapping[str, Any], intent: str) -> bool:
    if intent == "rss_semantic_match":
        return metrics.get("rss_match_total") is not None
    if intent == "pairwise_match":
        return metrics.get("response_chars") is not None
    if intent == "rank_text_items":
        return metrics.get("topics_count") is not None or metrics.get("source_count") is not None
    if intent == "understand_document_image":
        return metrics.get("articles_count") is not None
    if intent in {"summarize_text", "translate_text", "extract_json"}:
        return metrics.get("response_chars") is not None
    return False


def _unknown_intent_for_text(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"unknown_workload_{digest}"


def _trace_summary(trace: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": trace.get("source"),
        "backend": trace.get("backend"),
        "returncode": trace.get("returncode"),
        "duration_seconds": trace.get("duration_seconds"),
        "log_fingerprint": trace.get("log_fingerprint"),
        "metrics": trace.get("metrics"),
        "workload_hints": trace.get("workload_hints"),
    }


def _select_workload(contract: Mapping[str, Any], workload_id: str | None) -> Mapping[str, Any] | None:
    workloads = [item for item in contract.get("workloads", []) or [] if isinstance(item, Mapping)]
    if workload_id is None:
        workload_id = _str_or_none(contract.get("primary_workload_id"))
    for workload in workloads:
        if workload_id is None or workload.get("id") == workload_id:
            return workload
    return workloads[0] if workloads else None


def _metrics(trace: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = trace.get("metrics")
    return metrics if isinstance(metrics, Mapping) else {}


def _merge_metrics(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if value is None:
            continue
        if isinstance(value, bool):
            target[key] = value if target.get(key) is None else bool(target[key]) and value
        elif isinstance(value, (int, float)):
            current = target.get(key)
            target[key] = value if current is None else max(current, value)


def _finalize_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if value is not None}


def _round_context_budget(value: int) -> int:
    tiers = (8192, 32768, 65536, 131072, 262144, 524288, 1010000)
    required = max(1, int(value))
    for candidate in tiers:
        if required <= candidate:
            return candidate
    return 1 << (required - 1).bit_length()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _positive_int(value: Any, *, default: int) -> int:
    number = _optional_int(value)
    if number is None:
        return default
    return max(1, number)


def _max_int(current: Any, value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return current if isinstance(current, int) else None
    if current is None:
        return number
    try:
        return max(int(current), number)
    except (TypeError, ValueError):
        return number


def _max_float(current: Any, value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return current if isinstance(current, float) else None
    if current is None:
        return number
    try:
        return max(float(current), number)
    except (TypeError, ValueError):
        return number


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def _first_mode(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    return "sync"


def load_json_file(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def read_trace_log(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")
