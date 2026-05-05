from __future__ import annotations

from typing import Any

from gpucall.domain import ProviderError

RUNPOD_API_BASE = "https://api.runpod.ai/v2"


def requests_session():
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:
        raise ProviderError("requests/urllib3 are required for RunPod", retryable=False, status_code=501) from exc
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=0)))
    return session


def json_or_error(response: Any, message: str) -> dict[str, Any]:
    if response.status_code in {200, 201, 202}:
        data = response.json()
        return data if isinstance(data, dict) else {"output": data}
    retryable = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    raise ProviderError(
        f"{message}: {response.status_code}",
        retryable=retryable,
        status_code=502 if response.status_code >= 500 else response.status_code,
    )
