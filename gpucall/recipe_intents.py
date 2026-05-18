from __future__ import annotations

CAPABILITY_BY_INTENT: dict[str, tuple[str, ...]] = {
    "answer_question_about_image": ("visual_question_answering", "instruction_following"),
    "author_recipe": ("instruction_following",),
    "caption_image": ("image_captioning",),
    "convert_document": ("document_conversion",),
    "extra_large_context_text_inference": ("instruction_following",),
    "extract_json": ("structured_output",),
    "fine_tune_lora": ("lora_training",),
    "large_context_text_inference": ("instruction_following",),
    "pairwise_match": ("instruction_following", "reasoning", "structured_output"),
    "rank_text_items": ("instruction_following", "reasoning", "structured_output"),
    "rss_semantic_match": ("instruction_following", "reasoning", "structured_output"),
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
    "article_match": "rss_semantic_match",
    "pair_match": "pairwise_match",
    "pairwise_similarity": "pairwise_match",
    "rss_match": "rss_semantic_match",
    "semantic_match": "rss_semantic_match",
    "semantic_rss_match": "rss_semantic_match",
    "short_pairwise_similarity": "pairwise_match",
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


def is_valid_production_intent(intent: str | None) -> bool:
    normalized = normalize_intent(intent)
    if normalized is None:
        return False
    if normalized in {"infer", "vision", "task", "draft", "generic", "unknown"}:
        return False
    return normalized in CAPABILITY_BY_INTENT


def is_known_intent(intent: str | None) -> bool:
    return is_valid_production_intent(intent)


def is_unknown_workload_intent(intent: str | None) -> bool:
    normalized = normalize_intent(intent)
    return bool(normalized and normalized.startswith("unknown_workload_"))


def capabilities_for(*, task: str, intent: str | None) -> list[str]:
    normalized = normalize_intent(intent)
    if normalized and normalized in CAPABILITY_BY_INTENT:
        return list(CAPABILITY_BY_INTENT[normalized])
    return list(TASK_DEFAULT_CAPABILITIES.get(task, ("instruction_following",)))
