from __future__ import annotations

import os
from typing import Any

from gpucall.providers.llm_engine import generate_text


def handler(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("input") or event
    if not isinstance(payload, dict):
        raise ValueError("RunPod event input must be an object")
    model = payload.get("model") or os.getenv("GPUCALL_RUNPOD_MODEL")
    max_model_len = payload.get("max_model_len") or os.getenv("GPUCALL_RUNPOD_MAX_MODEL_LEN")
    if not model or not max_model_len:
        raise ValueError("RunPod worker requires explicit model and max_model_len in payload or environment")
    model = str(model)
    max_model_len = int(max_model_len)
    return {"kind": "inline", "value": generate_text(payload, model=model, max_model_len=max_model_len)}


def start() -> None:
    try:
        import runpod
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("runpod is required in the RunPod Serverless worker image") from exc
    runpod.serverless.start({"handler": handler})


if __name__ == "__main__":  # pragma: no cover
    start()
