from __future__ import annotations

from pathlib import Path

import yaml

from gpucall.openai_contract import (
    OPENAI_CHAT_COMPLETIONS_CONTRACT,
    OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS,
    OPENAI_CHAT_COMPLETIONS_FIELDS,
    OPENAI_CHAT_COMPLETIONS_SUPPORTED_FIELDS,
)


def test_generated_openai_chat_contract_matches_vendored_spec() -> None:
    spec = yaml.safe_load(Path("third_party/openai/openapi.documented.yml").read_text(encoding="utf-8"))
    fields, required = _collect_schema(spec, spec["components"]["schemas"]["CreateChatCompletionRequest"])

    assert OPENAI_CHAT_COMPLETIONS_CONTRACT["source"]["operation_id"] == "createChatCompletion"
    assert OPENAI_CHAT_COMPLETIONS_FIELDS == frozenset(fields)
    assert OPENAI_CHAT_COMPLETIONS_CONTRACT["request"]["required"] == required
    assert {"model", "messages", "tools", "response_format", "stream_options"} <= OPENAI_CHAT_COMPLETIONS_FIELDS
    assert OPENAI_CHAT_COMPLETIONS_CONTRACT["request"]["json_schema"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "$defs" in OPENAI_CHAT_COMPLETIONS_CONTRACT["request"]["json_schema"]
    assert "ChatCompletionRequestMessage" in OPENAI_CHAT_COMPLETIONS_CONTRACT["request"]["json_schema"]["$defs"]


def test_gpucall_openai_policy_classifies_every_official_request_field() -> None:
    classified = OPENAI_CHAT_COMPLETIONS_SUPPORTED_FIELDS | OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS

    assert classified == OPENAI_CHAT_COMPLETIONS_FIELDS
    assert not (OPENAI_CHAT_COMPLETIONS_SUPPORTED_FIELDS & OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS)
    assert "web_search_options" in OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS
    assert "modalities" in OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS
    assert "tool_choice" in OPENAI_CHAT_COMPLETIONS_SUPPORTED_FIELDS
    assert "n" in OPENAI_CHAT_COMPLETIONS_SUPPORTED_FIELDS
    assert "stream.response_format" not in OPENAI_CHAT_COMPLETIONS_CONTRACT["gpucall_policy"]["feature_gated_fields"]


def _collect_schema(spec: dict, schema: dict) -> tuple[list[str], list[str]]:
    properties, required = _collect_schema_properties(spec, schema)
    return sorted(properties), required


def _collect_schema_properties(spec: dict, schema: dict) -> tuple[dict, list[str]]:
    schema = _resolve_ref(spec, schema)
    properties: dict = {}
    required: list[str] = []
    for part in schema.get("allOf", []) or []:
        part_properties, part_required = _collect_schema_properties(spec, part)
        properties.update(part_properties)
        required.extend(part_required)
    properties.update(schema.get("properties", {}) or {})
    required.extend(schema.get("required", []) or [])
    return properties, sorted(set(required))


def _resolve_ref(spec: dict, schema: dict) -> dict:
    ref = schema.get("$ref")
    if not ref:
        return schema
    current = spec
    for part in ref.removeprefix("#/").split("/"):
        current = current[part]
    return current
