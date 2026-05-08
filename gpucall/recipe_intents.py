from __future__ import annotations

CAPABILITY_BY_INTENT: dict[str, tuple[str, ...]] = {
    "answer_question_about_image": ("visual_question_answering", "instruction_following"),
    "caption_image": ("image_captioning",),
    "understand_document_image": ("document_understanding", "visual_question_answering", "instruction_following"),
    "transcribe_audio": ("speech_to_text",),
    "summarize_audio": ("speech_to_text", "summarization"),
    "summarize_video": ("video_understanding", "summarization"),
    "translate_text": ("translation",),
    "summarize_text": ("summarization",),
    "extract_json": ("structured_output",),
}

TASK_DEFAULT_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "infer": ("instruction_following",),
    "vision": ("visual_question_answering", "instruction_following"),
    "transcribe": ("speech_to_text",),
    "video": ("video_understanding",),
}


def capabilities_for(*, task: str, intent: str | None) -> list[str]:
    if intent and intent in CAPABILITY_BY_INTENT:
        return list(CAPABILITY_BY_INTENT[intent])
    return list(TASK_DEFAULT_CAPABILITIES.get(task, ("instruction_following",)))
