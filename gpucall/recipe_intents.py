from __future__ import annotations

CAPABILITY_BY_INTENT: dict[str, tuple[str, ...]] = {
    "answer_question_about_image": ("visual_question_answering", "instruction_following"),
    "caption_image": ("image_captioning",),
    "convert_document": ("document_conversion",),
    "extra_large_context_text_inference": ("instruction_following",),
    "extract_json": ("structured_output",),
    "fine_tune_lora": ("lora_training",),
    "large_context_text_inference": ("instruction_following",),
    "rank_text_items": ("instruction_following",),
    "short_text_inference": ("instruction_following",),
    "smoke_test": ("instruction_following",),
    "split_infer_activation": ("split_inference",),
    "standard_text_inference": ("instruction_following",),
    "summarize_audio": ("speech_to_text", "summarization"),
    "summarize_text": ("summarization",),
    "summarize_video": ("video_understanding", "summarization"),
    "train_lora": ("lora_training",),
    "transcribe_audio": ("speech_to_text",),
    "translate_text": ("translation",),
    "ultralong_text_inference": ("instruction_following",),
    "understand_document_image": ("document_understanding", "visual_question_answering", "instruction_following"),
    "understand_image": ("visual_question_answering", "image_captioning"),
}

INTENT_ALIASES: dict[str, str] = {
    "topic_ranking": "rank_text_items",
    "rank_topics": "rank_text_items",
}

TASK_DEFAULT_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "infer": ("instruction_following",),
    "vision": ("visual_question_answering", "instruction_following"),
    "transcribe": ("speech_to_text",),
    "video": ("video_understanding",),
}


def normalize_intent(intent: str | None) -> str | None:
    if intent is None:
        return None
    normalized = intent.strip().lower()
    if not normalized:
        return None
    return INTENT_ALIASES.get(normalized, normalized)


def capabilities_for(*, task: str, intent: str | None) -> list[str]:
    normalized = normalize_intent(intent)
    if normalized and normalized in CAPABILITY_BY_INTENT:
        return list(CAPABILITY_BY_INTENT[normalized])
    return list(TASK_DEFAULT_CAPABILITIES.get(task, ("instruction_following",)))
