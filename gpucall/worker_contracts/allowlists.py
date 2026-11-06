"""Worker model allowlists as pure data, importable without loading the Modal SDK.

Separated from modal.py so conformance checks (gpucall/provider_conformance.py)
can compare catalog declarations against what the worker will accept without
importing the heavy Modal app module.
"""
from __future__ import annotations

WORKER_ALLOWED_TEXT_MODELS = frozenset({
    "Qwen/Qwen2.5-0.5B-Instruct", "Qwen/Qwen2.5-1.5B-Instruct", "Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2.5-14B-Instruct", "Qwen/Qwen2.5-32B-Instruct", "Qwen/Qwen2.5-14B-Instruct-1M",
    "Qwen/Qwen3-32B", "Qwen/Qwen3-30B-A3B", "Qwen/Qwen3-235B-A22B-FP8",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B", "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B", "meta-llama/Llama-3.1-8B-Instruct",
    "microsoft/Phi-3.5-mini-instruct", "facebook/opt-125m",
})
WORKER_ALLOWED_VISION_MODELS = frozenset({
    "Salesforce/blip-image-captioning-base", "Salesforce/blip-vqa-base",
    "Qwen/Qwen2.5-VL-3B-Instruct", "Qwen/Qwen2.5-VL-7B-Instruct", "Qwen/Qwen2.5-VL-32B-Instruct",
    "microsoft/Florence-2-large-ft",
})
