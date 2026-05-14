from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "third_party" / "openai" / "openapi.documented.yml"
GATEWAY_OUT = ROOT / "gpucall" / "openai_contract" / "chat_completions.json"
SDK_OUT = ROOT / "sdk" / "python" / "gpucall_sdk" / "openai_contract" / "chat_completions.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate gpucall's OpenAI Chat Completions contract snapshot.")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--gateway-out", type=Path, default=GATEWAY_OUT)
    parser.add_argument("--sdk-out", type=Path, default=SDK_OUT)
    args = parser.parse_args()

    spec_bytes = args.spec.read_bytes()
    spec = yaml.safe_load(spec_bytes)
    schemas = spec["components"]["schemas"]
    request_props, request_required = _collect_schema(spec, schemas["CreateChatCompletionRequest"])
    response_props, response_required = _collect_schema(spec, schemas["CreateChatCompletionResponse"])
    stream_props, stream_required = _collect_schema(spec, schemas["CreateChatCompletionStreamResponse"])
    request_json_schema = _json_schema_document(spec, "CreateChatCompletionRequest")
    response_json_schema = _json_schema_document(spec, "CreateChatCompletionResponse")
    stream_json_schema = _json_schema_document(spec, "CreateChatCompletionStreamResponse")

    request_fields = sorted(request_props)
    contract = {
        "source": {
            "name": "openai-openapi documented OpenAPI spec",
            "url": "https://app.stainless.com/api/spec/documented/openai/openapi.documented.yml",
            "license": "MIT",
            "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
            "operation_id": "createChatCompletion",
            "path": "/chat/completions",
        },
        "request": {
            "schema": "CreateChatCompletionRequest",
            "required": request_required,
            "fields": request_fields,
            "json_schema": request_json_schema,
        },
        "response": {
            "schema": "CreateChatCompletionResponse",
            "required": response_required,
            "fields": sorted(response_props),
            "json_schema": response_json_schema,
        },
        "stream_response": {
            "schema": "CreateChatCompletionStreamResponse",
            "required": stream_required,
            "fields": sorted(stream_props),
            "json_schema": stream_json_schema,
        },
        "gpucall_policy": {
            "supported_fields": sorted(
                {
                    "model",
                    "messages",
                    "metadata",
                    "temperature",
                    "max_tokens",
                    "max_completion_tokens",
                    "top_p",
                    "stop",
                    "seed",
                    "tools",
                    "tool_choice",
                    "functions",
                    "function_call",
                    "user",
                    "presence_penalty",
                    "frequency_penalty",
                    "response_format",
                    "stream",
                    "stream_options",
                    "n",
                }
            ),
            "fail_closed_fields": sorted(
                {
                    "audio",
                    "logit_bias",
                    "logprobs",
                    "modalities",
                    "parallel_tool_calls",
                    "prediction",
                    "prompt_cache_key",
                    "prompt_cache_retention",
                    "reasoning_effort",
                    "safety_identifier",
                    "service_tier",
                    "store",
                    "top_logprobs",
                    "verbosity",
                    "web_search_options",
                }
            ),
            "feature_gated_fields": sorted(
                {
                    "stream_options.include_obfuscation",
                }
            ),
        },
    }

    _validate_policy_subset(contract)
    for path in (args.gateway_out, args.sdk_out):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _collect_schema(spec: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    schema = _resolve_ref(spec, schema)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for part in schema.get("allOf", []) or []:
        part_properties, part_required = _collect_schema(spec, part)
        properties.update(part_properties)
        required.extend(part_required)
    properties.update(schema.get("properties", {}) or {})
    required.extend(schema.get("required", []) or [])
    return properties, sorted(set(required))


def _resolve_ref(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    current: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        current = current[part]
    return current


def _validate_policy_subset(contract: dict[str, Any]) -> None:
    request_fields = set(contract["request"]["fields"])
    policy_fields = set(contract["gpucall_policy"]["supported_fields"]) | set(contract["gpucall_policy"]["fail_closed_fields"])
    missing = sorted(policy_fields - request_fields)
    if missing:
        raise SystemExit(f"policy contains fields absent from OpenAI spec: {missing}")
    unclassified = sorted(request_fields - policy_fields)
    if unclassified:
        raise SystemExit(f"OpenAI request fields are not classified by gpucall policy: {unclassified}")


def _json_schema_document(spec: dict[str, Any], schema_name: str) -> dict[str, Any]:
    """Return a compact JSON Schema document with the referenced OpenAI schema closure.

    The vendored OpenAI document is OpenAPI-flavoured. Runtime validation uses
    jsonschema, so the generated contract translates local component refs to
    `$defs` and normalizes OpenAPI `nullable: true` into JSON Schema types.
    """
    refs = _schema_ref_closure(spec, schema_name)
    defs = {
        name: _normalize_json_schema(copy.deepcopy(spec["components"]["schemas"][name]))
        for name in sorted(refs)
        if name != schema_name
    }
    root = _normalize_json_schema(copy.deepcopy(spec["components"]["schemas"][schema_name]))
    root["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    if defs:
        root["$defs"] = defs
    return root


def _schema_ref_closure(spec: dict[str, Any], schema_name: str) -> set[str]:
    seen: set[str] = set()
    pending = [schema_name]
    schemas = spec["components"]["schemas"]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for ref in _iter_local_component_refs(schemas[current]):
            if ref not in seen:
                pending.append(ref)
    return seen


def _iter_local_component_refs(value: Any):
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            yield ref.rsplit("/", 1)[-1]
        for item in value.values():
            yield from _iter_local_component_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_local_component_refs(item)


def _normalize_json_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "nullable":
            continue
        if key == "$ref" and isinstance(item, str) and item.startswith("#/components/schemas/"):
            normalized[key] = "#/$defs/" + item.rsplit("/", 1)[-1]
            continue
        normalized[key] = _normalize_json_schema(item)

    if value.get("nullable") is True:
        existing_type = normalized.get("type")
        if isinstance(existing_type, str):
            normalized["type"] = sorted({existing_type, "null"})
        elif isinstance(existing_type, list):
            normalized["type"] = sorted({str(item) for item in existing_type} | {"null"})
        elif "$ref" in normalized:
            ref = normalized.pop("$ref")
            normalized["anyOf"] = [{"$ref": ref}, {"type": "null"}]
        elif "oneOf" in normalized:
            normalized["oneOf"] = [*normalized["oneOf"], {"type": "null"}]
        elif "anyOf" in normalized:
            normalized["anyOf"] = [*normalized["anyOf"], {"type": "null"}]
    return normalized


if __name__ == "__main__":
    main()
