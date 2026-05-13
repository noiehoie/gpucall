from __future__ import annotations

import argparse
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
        },
        "response": {
            "schema": "CreateChatCompletionResponse",
            "required": response_required,
            "fields": sorted(response_props),
        },
        "stream_response": {
            "schema": "CreateChatCompletionStreamResponse",
            "required": stream_required,
            "fields": sorted(stream_props),
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
                    "n",
                    "stream_options.include_obfuscation",
                    "stream_options.include_usage",
                    "stream.response_format",
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


if __name__ == "__main__":
    main()
