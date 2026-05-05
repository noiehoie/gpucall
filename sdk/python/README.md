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

## Recipe Draft Helper

The SDK distribution also includes `gpucall-recipe-draft`, an operator-assist CLI for workloads that the gateway cannot route with its current recipes/providers.

It does not change gateway routing and it does not bypass policy. The `intake` phase is deterministic and strips prompt bodies, DataRef URIs, presigned URLs, and secrets before any draft is produced.

```bash
gpucall-recipe-draft intake \
  --error gpucall-error.json \
  --task vision \
  --intent understand_document_image \
  --classification confidential \
  --output intake.json

gpucall-recipe-draft draft --input intake.json --output recipe-draft.json

gpucall-recipe-draft llm-prompt --input intake.json --output llm-prompt.txt
```

`gpucall-recipe-draft` does not choose an LLM for you. If you want the helper to call an LLM directly, create a user-controlled config. Secrets stay in environment variables, not in the config file.

```bash
gpucall-recipe-draft init-config

# Edit ~/.config/gpucall/recipe-draft.json:
# {
#   "provider": "openai-compatible",
#   "base_url": "http://127.0.0.1:11434/v1",
#   "model": "qwen2.5:7b-instruct",
#   "api_key_env": null
# }

gpucall-recipe-draft draft-llm --input intake.json --output llm-draft.json
```

Generated drafts are review artifacts for gpucall administrators, not production config.
