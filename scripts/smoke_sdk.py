from __future__ import annotations

import os
import sys

from gpucall_sdk import GPUCallClient


def main() -> int:
    base_url = os.getenv("GPUCALL_BASE_URL", "http://gpucall.example.internal:18088")
    api_key = os.getenv("GPUCALL_API_KEY")
    if not api_key:
        print("GPUCALL_API_KEY is required", file=sys.stderr)
        return 2

    with GPUCallClient(base_url, api_key=api_key) as client:
        response = client.chat.completions.create(
            model="gpucall:auto",
            messages=[
                {
                    "role": "user",
                    "content": "Return exactly this JSON object and no prose: {\"answer\":2}",
                }
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=64,
            parse_json=True,
        )
        print(
            {
                "ok": True,
                "output_validated": response.get("output_validated"),
                "parsed": response.get("parsed"),
            }
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
