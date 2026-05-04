# gpucall SDK Distribution

The Python SDK is distributed privately during the v2.0 alpha.

## Artifact

Package:

```text
gpucall-sdk
```

Import:

```python
from gpucall_sdk import GPUCallClient
```

Compatibility:

```text
gpucall-sdk 2.0.0a1 -> gpucall Gateway 2.0.x
```

Runtime dependency:

```text
httpx
```

Provider libraries such as Modal, RunPod, Hyperstack, boto3, FastAPI, and
uvicorn are intentionally not SDK dependencies.

## Install From Private Wheel

Example:

```bash
uv add "gpucall-sdk @ file:///opt/gpucall/artifacts/sdk/python/gpucall_sdk-2.0.0a1-py3-none-any.whl"
```

For another host, copy the wheel first:

```bash
scp netcup:/opt/gpucall/artifacts/sdk/python/gpucall_sdk-2.0.0a1-py3-none-any.whl ./vendor/
uv add "gpucall-sdk @ file://${PWD}/vendor/gpucall_sdk-2.0.0a1-py3-none-any.whl"
```

## Configure

```bash
export GPUCALL_BASE_URL="http://gpucall.example.internal:18088"
export GPUCALL_API_KEY="<gateway token>"
```

Do not log API keys, prompt bodies, presigned URLs, or DataRef URIs.

## Smoke

```bash
python scripts/smoke_sdk.py
```

Expected shape:

```text
{'ok': True, 'output_validated': True, 'parsed': {'answer': 2}}
```

## Interface Rules

- Use `/v1/chat/completions` or OpenAI SDK compatibility for lightweight text.
- Use `gpucall-sdk` for large prompts, files, images, or DataRef automation.
- Do not send provider, GPU, or recipe. The gateway rejects caller-controlled routing on public task endpoints.
- Use `response_format` and `temperature=0` for JSON-required tasks.
- Validate parsed JSON against the caller's business schema.
