from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from gpucall_recipe_draft.core import llm_prompt_from_intake


DEFAULT_CONFIG_PATH = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "gpucall" / "recipe-draft.json"


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key_env: str | None = None
    timeout_seconds: float = 120.0
    temperature: float = 0.0


def default_config_template() -> dict[str, Any]:
    return {
        "provider": "openai-compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b-instruct",
        "api_key_env": None,
        "timeout_seconds": 120,
        "temperature": 0,
    }


def load_llm_config(path: str | Path | None = None) -> LLMConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("recipe draft LLM config must be a JSON object")
    return LLMConfig(
        provider=str(data.get("provider") or "openai-compatible"),
        base_url=str(data["base_url"]),
        model=str(data["model"]),
        api_key_env=str(data["api_key_env"]) if data.get("api_key_env") else None,
        timeout_seconds=float(data.get("timeout_seconds") or 120),
        temperature=float(data.get("temperature") or 0),
    )


def write_default_config(path: str | Path | None = None, *, force: bool = False) -> Path:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if config_path.exists() and not force:
        raise FileExistsError(f"config already exists: {config_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(default_config_template(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def draft_with_llm(
    intake: dict[str, Any],
    config: LLMConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if config.provider != "openai-compatible":
        raise ValueError("only provider='openai-compatible' is supported in this SDK helper")
    prompt = llm_prompt_from_intake(intake)
    content = call_openai_compatible(config, prompt, transport=transport)
    parsed: Any = None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None
    return {
        "schema_version": 1,
        "phase": "llm-draft",
        "source": "sanitized_request_only",
        "provider": config.provider,
        "base_url": _redact_base_url(config.base_url),
        "model": config.model,
        "human_review_required": True,
        "raw_text": content,
        "parsed_json": parsed,
    }


def call_openai_compatible(
    config: LLMConfig,
    prompt: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    headers = {"content-type": "application/json"}
    if config.api_key_env:
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise RuntimeError(f"configured API key env var is not set: {config.api_key_env}")
        headers["authorization"] = f"Bearer {api_key}"
    url = config.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "temperature": config.temperature,
        "messages": [
            {
                "role": "system",
                "content": "You draft gpucall recipe/provider requests from sanitized metadata only. Return JSON.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    with httpx.Client(timeout=config.timeout_seconds, transport=transport) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("OpenAI-compatible response did not contain choices[0].message.content") from exc


def _redact_base_url(value: str) -> str:
    return value.split("?", 1)[0]
