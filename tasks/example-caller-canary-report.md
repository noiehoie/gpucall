# example-caller Production Canary Report

Date: 2026-07-02 JST
Caller: example-caller on <caller-host> (`<caller-repo>`, `LLM_BACKEND=gpucall`)
Gateway: <gateway-host> `<gateway-ip>:18088` (v2.0.67 → v2.0.68 → v2.0.69 during this session)
Sanitization: no API keys, raw prompts, presigned URLs, DataRef URIs, or provider raw outputs.

## Executive summary

The caller was found broken in production at session start: the daily pipeline
had aborted at the analysis stage twice (12:12 and 12:18 JST). All failures
were classified, separated by owner, fixed, and re-verified without human
relay. Final state: caller tests 32/32 PASS, 5-intent canary 5/5 PASS
(GO), production rank workload re-run evidence below.

## Failure classification (test-boundary discipline)

| Observation | Classification | Owner | Resolution |
| --- | --- | --- | --- |
| 12:12 JST `400 inline input exceeds policy limit` on rank analysis (2 attempts) | caller defect: inline/DataRef branch bug in `src/util/gpucall_client.py` | caller | fixed caller-side at 12:13:21 JST (file mtime evidence); 12:18 retry no longer produced 400 |
| 12:18 JST + 14:09 JST `502 PROVIDER_ERROR` on rank analysis | product defect: catalog/worker context contract mismatch (surface declared 131072, deployed Modal worker capped Qwen2.5 at 32768; actual prompt 88,049 tokens per Modal vLLM logs) | gpucall product | fixed in v2.0.69: worker honors 131072 via Qwen2.5 model-card YaRN config; Modal worker redeployed through the consent-gated setup flow (plan hash `12d4aa43d17ed1f3`) |
| Canary intents extract_json / translate_text / summarize_text / vision failing `NO_ELIGIBLE_TUPLE` | environment defect: route validation evidence for light/vision routes invalidated by a 2026-06-29 config-hash change; silently unroutable for 9 days | gpucall operator (product gap) | 5 routes re-validated via `tuple-smoke` (canary 1 → fanout 4); product fix in v2.0.69 adds automatic re-validation of drift-invalidated routes to the admin watch service |
| Translate stage `503 PROVIDER_CAPACITY_UNAVAILABLE` ×20 and overseas-vision 21/21 failures during pipeline runs | gateway admission working as designed: default per-tuple concurrency limit is 1 and per-provider-family limit is 2 (`GPUCALL_TUPLE_CONCURRENCY_LIMIT` / `GPUCALL_PROVIDER_FAMILY_CONCURRENCY_LIMIT`), so a 20-21-way parallel caller stage is deterministically shed | operator decision | bounded, classified, retryable. Raising the limits multiplies concurrent GPU billing, so this stays an explicit operator budget decision, not an autonomous change. Recommended operator action: set `GPUCALL_TUPLE_CONCURRENCY_LIMIT=4` and family limit accordingly in the gateway environment if parallel vision/translate stages should run hot, or throttle caller-side parallelism |
| `[DB永続化] connection to <db-host>:5432 refused` in the successful morning run | caller-side infrastructure, outside gpucall scope | example-caller operator | reported only; not a gpucall blocker (fleet reference lists the caller database on a different fleet host — target IP mismatch worth an operator look) |

## Evidence

### Caller tests

```text
ssh <caller-host> 'cd <caller-repo> && uv run pytest tests/test_gpucall_v2.py -q'
32 passed in 0.03s
```

### 5-intent canary (after route revalidation, gateway v2.0.68)

```text
extract_json=PASS chars=53
translate_text=PASS chars=37
summarize_text=PASS chars=49
rank_text_items=PASS chars=7
vision=PASS chars=179
canary_result=GO
```

Script: `.gpucall-migration/canary_20260702.py` (caller repo, sanitized output only).

### Root-cause evidence for the 502 (Modal worker logs, sanitized)

```text
ValueError: The decoder prompt (length 88049) is longer than the maximum model length of 32768.
ValueError: The decoder prompt (length 78414) is longer than the maximum model length of 32768.
```

Surface catalog for the selected tuple declared `max_model_len: 131072`; the
deterministic router correctly trusted the catalog; the worker contract was
wrong. This is exactly the class of drift the North Star's worker-contract
evidence is meant to catch — recorded as a product requirement, not a
example-caller special case.

### Morning production run (same day, pre-incident)

```text
06:39:31 Analysis call completed (attempt 1.1): response_len=44865
```

The production rank workload does complete through gpucall when the prompt
fits the worker's real context bound; the failure mode was input-size
dependent, which is why it escaped the 2026-06-20 acceptance run.

### Production rank workload re-run (gateway v2.0.69, YaRN worker)

See `tasks/example-caller-canary-report.json` for the machine-readable result of
the post-fix production pipeline re-run.

## Go / No-Go

Recorded in `tasks/example-caller-canary-report.json` (`decision` field) after
the post-fix production re-run.
