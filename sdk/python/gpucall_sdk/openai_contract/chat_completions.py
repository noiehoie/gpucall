from __future__ import annotations

import json
from importlib import resources
from typing import Any, Mapping


def _load_contract() -> dict[str, Any]:
    with resources.files(__package__).joinpath("chat_completions.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


OPENAI_CHAT_COMPLETIONS_CONTRACT = _load_contract()
OPENAI_CHAT_COMPLETIONS_FIELDS = frozenset(OPENAI_CHAT_COMPLETIONS_CONTRACT["request"]["fields"])
OPENAI_CHAT_COMPLETIONS_SUPPORTED_FIELDS = frozenset(OPENAI_CHAT_COMPLETIONS_CONTRACT["gpucall_policy"]["supported_fields"])
OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS = frozenset(OPENAI_CHAT_COMPLETIONS_CONTRACT["gpucall_policy"]["fail_closed_fields"])
OPENAI_CHAT_COMPLETIONS_FEATURE_GATED_FIELDS = frozenset(OPENAI_CHAT_COMPLETIONS_CONTRACT["gpucall_policy"]["feature_gated_fields"])
OPENAI_CHAT_COMPLETIONS_REQUEST_SCHEMA = OPENAI_CHAT_COMPLETIONS_CONTRACT["request"]["json_schema"]
OPENAI_CHAT_COMPLETIONS_RESPONSE_SCHEMA = OPENAI_CHAT_COMPLETIONS_CONTRACT["response"]["json_schema"]
OPENAI_CHAT_COMPLETIONS_STREAM_RESPONSE_SCHEMA = OPENAI_CHAT_COMPLETIONS_CONTRACT["stream_response"]["json_schema"]


def openai_chat_completions_unknown_fields(payload: Mapping[str, Any]) -> list[str]:
    return sorted(str(key) for key in payload if str(key) not in OPENAI_CHAT_COMPLETIONS_FIELDS)
