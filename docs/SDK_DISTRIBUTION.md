# gpucall SDK Distribution

The Python SDK is distributed separately from the gateway. Public releases use
GitHub Release assets; private deployments may mirror the same files in an
internal artifact store.

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
gpucall-sdk 2.0.8 -> gpucall Gateway 2.0.x
```

Runtime dependency:

```text
httpx
```

Provider libraries such as Modal, RunPod, Hyperstack, boto3, FastAPI, and
uvicorn are intentionally not SDK dependencies.

## Install From Public Release Asset

Example:

```bash
uv tool install https://github.com/noiehoie/gpucall/releases/download/v2.0.8/gpucall_sdk-2.0.8-py3-none-any.whl
```

Download and verify the release `SHA256SUMS` before wiring the wheel URL into
production automation.

## Install From Private Mirror

Example:

```bash
uv add "gpucall-sdk @ file:///opt/gpucall/artifacts/sdk/python/gpucall_sdk-2.0.8-py3-none-any.whl"
```

For another host, copy the wheel first:

```bash
scp gateway.example.internal:/opt/gpucall/artifacts/sdk/python/gpucall_sdk-2.0.8-py3-none-any.whl ./vendor/
uv add "gpucall-sdk @ file://${PWD}/vendor/gpucall_sdk-2.0.8-py3-none-any.whl"
```

## Configure

```bash
export GPUCALL_BASE_URL="https://gpucall-gateway.example.internal"
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

The gateway package `gpucall` and client package `gpucall-sdk` are separate artifacts. Installing the gateway wheel does not install `gpucall_sdk`; install `gpucall-sdk` explicitly in client applications.
