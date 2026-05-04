# gpucall Python SDK

Private alpha package for gpucall v2.0 integrations.

Install from a private wheel:

```bash
uv add "gpucall-sdk @ file:///path/to/gpucall_sdk-2.0.0a2-py3-none-any.whl"
```

Configure:

```bash
export GPUCALL_API_KEY="<gateway token>"
export GPUCALL_BASE_URL="http://gpucall.example.internal:18088"
```

Use:

```python
from gpucall_sdk import GPUCallClient

client = GPUCallClient(
    os.environ["GPUCALL_BASE_URL"],
    api_key=os.environ["GPUCALL_API_KEY"],
)

response = client.chat.completions.create(
    model="gpucall:auto",
    messages=[{"role": "user", "content": "Return exactly {\"answer\":2}"}],
    response_format={"type": "json_object"},
    temperature=0,
    max_tokens=64,
    parse_json=True,
)

print(response["parsed"])
```

The SDK automatically uploads large prompts through the gpucall object-store
DataRef path. It does not depend on provider libraries such as Modal, RunPod, or
boto3.
