# gpucall Python SDK

Private alpha package for gpucall v2.0 integrations.

Install from a private wheel:

```bash
uv add "gpucall-sdk @ file:///path/to/gpucall_sdk-2.0.0a2-py3-none-any.whl"
```

Verify that the SDK console script is installed:

```bash
uv run gpucall-recipe-draft --help
```

When running directly from a source checkout instead of an installed wheel, use:

```bash
uv run python -m gpucall_recipe_draft.cli --help
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

The SDK distribution also includes `gpucall-recipe-draft`, the caller-side helper in the three-part gpucall product. The other two parts are the gateway runtime and the administrator-side `gpucall-recipe-admin` helper.

Use `gpucall-recipe-draft` for workloads that the gateway cannot route with its current recipe catalog and production tuples, and for `200 OK` outputs that fail caller-side business quality checks.

It does not change gateway routing and it does not bypass policy. It also does not choose providers, GPUs, models, engines, or tuples. The `intake` phase is deterministic and strips prompt bodies, DataRef URIs, presigned URLs, and secrets before any draft is produced.

```bash
gpucall-recipe-draft preflight \
  --task vision \
  --intent understand_document_image \
  --content-type image/png \
  --bytes 2000000 \
  --output preflight-intake.json

gpucall-recipe-draft intake \
  --error gpucall-error.json \
  --task vision \
  --intent understand_document_image \
  --classification confidential \
  --output intake.json \
  --remote-inbox admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox \
  --source news-system

gpucall-recipe-draft quality \
  --task vision \
  --intent understand_document_image \
  --content-type image/jpeg \
  --bytes 1136521 \
  --dimension 1200x2287 \
  --observed-recipe vision-image-standard \
  --observed-tuple modal-vision-a10g \
  --observed-tuple-model Salesforce/blip-vqa-base \
  --quality-failure-kind insufficient_ocr \
  --quality-failure-reason "short answer only; expected top headlines" \
  --remote-inbox admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox \
  --source news-system

gpucall-recipe-draft draft --input intake.json --output recipe-draft.json

gpucall-recipe-draft compare \
  --preflight preflight-intake.json \
  --failure intake.json \
  --output drift-report.json

gpucall-recipe-draft submit \
  --intake intake.json \
  --draft recipe-draft.json \
  --remote-inbox admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox \
  --source news-system
```

The caller-side helper does not call an LLM. It creates deterministic intake material for gpucall administrators. If LLM-assisted recipe authoring is needed, it should run on the gpucall administrator side as an audited admin workflow.

Generated drafts are review artifacts, not production config. `submit` writes a JSON bundle to a file-based inbox; it does not call the gpucall gateway API. `preflight`, `intake`, and `quality` can also write to the inbox directly with `--inbox-dir` or over SSH with `--remote-inbox USER@HOST:/absolute/path`.
