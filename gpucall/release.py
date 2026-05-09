from __future__ import annotations

GITHUB_RELEASE_TAG = "v2.0.8"
GITHUB_RELEASE_BASE = f"https://github.com/noiehoie/gpucall/releases/download/{GITHUB_RELEASE_TAG}"
GITHUB_RAW_TAG_BASE = f"https://raw.githubusercontent.com/noiehoie/gpucall/{GITHUB_RELEASE_TAG}"

ONBOARDING_PROMPT_URL = f"{GITHUB_RAW_TAG_BASE}/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md"
ONBOARDING_MANUAL_URL = f"{GITHUB_RAW_TAG_BASE}/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md"
SDK_WHEEL_URL = f"{GITHUB_RELEASE_BASE}/gpucall_sdk-2.0.8-py3-none-any.whl"
