# Operations Notes

## 2026-05-11 PREMORTEM Follow-Up

After the Postgres migration and quality-floor routing fix, the major PREMORTEM
risks reduced are:

- SQLite under concurrent load: Docker Compose production-like runtime now uses
  Postgres for gateway jobs and idempotency. SQLite remains dev/test fallback.
- Smallest-capable quality trap: intentless recipe selection now avoids
  draft/smoke quality recipes where possible, and `text-infer-light` is a
  standard-quality production route.

Remaining product risks to address next:

- Config rigidity: recipes/tuples still require file deployment plus gateway
  restart. Add config reload or a deterministic promotion/reload path.
- Provider drift: add scheduled provider SDK/version/changelog checks for Modal,
  RunPod, Hyperstack, and other provider adapters.
- Onboarding friction: keep reducing the happy path for external systems while
  preserving fail-closed governance.
- MVP gaps: track streaming, function-calling, and larger file/DataRef requests
  as explicit bypass risks.
- Complexity ratio: avoid broadening task scope until active consumer count and
  operational evidence justify it.
