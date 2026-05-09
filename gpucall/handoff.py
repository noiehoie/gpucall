from __future__ import annotations

import json

from gpucall.release import ONBOARDING_MANUAL_URL, ONBOARDING_PROMPT_URL, SDK_WHEEL_URL


def handoff_payload(*, tenant: str, token: str, gateway_url: str, recipe_inbox: str) -> dict[str, str]:
    return {
        "GPUCALL_TENANT": tenant,
        "GPUCALL_BASE_URL": gateway_url,
        "GPUCALL_API_KEY": token,
        "GPUCALL_RECIPE_INBOX": recipe_inbox,
        "GPUCALL_ONBOARDING_PROMPT_URL": ONBOARDING_PROMPT_URL,
        "GPUCALL_ONBOARDING_MANUAL_URL": ONBOARDING_MANUAL_URL,
        "GPUCALL_SDK_WHEEL_URL": SDK_WHEEL_URL,
    }


def render_handoff(payload: dict[str, str], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output_format == "env":
        return "".join(f"{key}={_shell_quote(value)}\n" for key, value in payload.items())
    raise SystemExit(f"unknown handoff format: {output_format}")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
