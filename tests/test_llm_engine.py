from __future__ import annotations

from gpucall.providers.llm_engine import messages_from_payload


def test_llm_engine_preserves_payload_messages_without_system_rewrite() -> None:
    payload = {
        "system_prompt": "recipe system",
        "messages": [
            {"role": "system", "content": "caller system"},
            {"role": "user", "content": "caller user"},
        ],
    }

    assert messages_from_payload(payload, "fallback prompt") == [
        {"role": "system", "content": "caller system"},
        {"role": "user", "content": "caller user"},
    ]


def test_llm_engine_uses_explicit_system_prompt_only_for_legacy_prompt_payload() -> None:
    payload = {"system_prompt": "recipe system"}

    assert messages_from_payload(payload, "legacy prompt") == [
        {"role": "system", "content": "recipe system"},
        {"role": "user", "content": "legacy prompt"},
    ]
