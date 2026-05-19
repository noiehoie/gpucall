# Provider Panopticon Communication Contract

Provider Panopticon is the out-of-execution-path monitor for provider endpoint,
stock, price, and readiness evidence. It is not the product north star; it is a
control-plane component that helps gpucall move toward the north star: making GPU
compute feel like electricity to callers.

## Fetch Contract

gpucall reads Panopticon evidence through `gpucall.panopticon_client`.

Supported sources:

- `file`: default. Reads the strict snapshot at
  `$XDG_STATE_HOME/gpucall/catalog/provider-panopticon.json`.
- `http`: enabled only when `GPUCALL_PANOPTICON_URL` or
  `GPUCALL_PANOPTICON_SOURCE=http` is configured. Reads `GET /v1/snapshot`.

Environment:

- `GPUCALL_PANOPTICON_SOURCE=file|http`
- `GPUCALL_PANOPTICON_PATH=/path/to/provider-panopticon.json`
- `GPUCALL_PANOPTICON_URL=http://127.0.0.1:18090`
- `GPUCALL_PANOPTICON_TIMEOUT_SECONDS=2`
- `GPUCALL_PANOPTICON_FAIL_CLOSED_ON_MISSING=1`
- `GPUCALL_PANOPTICON_FAIL_CLOSED_ON_UNREACHABLE=1`
- `GPUCALL_PANOPTICON_FAIL_CLOSED_ON_INVALID=1`

HTTP URLs may point either at the service root or directly at `/v1/snapshot`.

## Fetch Report

The client returns a strict communication report:

```json
{
  "schema_version": 1,
  "phase": "provider-panopticon-fetch",
  "source_kind": "file",
  "snapshot_path": "/path/to/provider-panopticon.json",
  "snapshot_url": null,
  "fetched_at": "2026-05-20T00:00:00+00:00",
  "status": "ok",
  "fail_closed": false,
  "snapshot_hash": "sha256:...",
  "tuple_count": 1,
  "stale_tuple_count": 0,
  "non_generation_probe_only": true,
  "error": null,
  "snapshot": {
    "tuple-name": {
      "tuple": "tuple-name",
      "adapter": "runpod-vllm-serverless",
      "status": "live_revalidated",
      "checked": true,
      "dimensions": ["stock", "price"],
      "findings": []
    }
  }
}
```

`status` is one of:

- `ok`: snapshot was fetched and no tuple evidence is stale.
- `stale`: snapshot was fetched, but at least one tuple evidence row is stale.
- `missing`: configured snapshot source is absent.
- `unreachable`: HTTP snapshot source could not be reached.
- `invalid`: snapshot was reachable but failed the strict grammar.

Missing, unreachable, and invalid states do not fail closed by default for
backward compatibility. When the matching fail-closed environment variable is
enabled, the client emits synthetic `panopticon` blocker findings for the tuple
scope supplied by the caller.

## Runtime Boundaries

The client is a reader. It must not call provider generation APIs.

gpucall runtime, readiness, execution-catalog, and validator-plan consume the
same client report. Price evidence may be present in the snapshot as
`live_price_per_second`, but live price routing is a separate promotion step and
is not part of this communication contract.
