# Contributing

Honest context first: this is a **single-maintainer** project, developed with
heavy AI assistance and gated by deterministic tests. Issues, bug reports with
reproduction steps, and focused PRs are welcome; large unsolicited refactors
will likely be declined.

## Ground rules the codebase enforces

These are product invariants, not style preferences. PRs that weaken them will
not merge:

1. **No inference in control decisions.** Recipe selection, tuple routing,
   fallback order, budget admission, validation acceptance, cleanup, and
   production promotion are deterministic rule evaluation. No LLM calls, no
   heuristics, in the gateway control path.
2. **Fail closed.** Unknown workloads, stale evidence, missing prices,
   unvalidated routes, exceeded budgets → reject with a machine-readable
   failure artifact. Never route to a weaker path silently.
3. **Secrets never cross boundaries.** No API keys, presigned URLs, DataRef
   URIs, or raw payloads in logs, reports, handoff packages, or test output.
4. **Callers declare intent, not infrastructure.** Nothing may turn `model`
   or any caller field into a raw provider/GPU selector.

## Development setup

```bash
uv sync
uv run pytest                                    # full suite (~25 min), hermetic
uv run pytest tests/test_compiler.py -q          # focused runs are fine
uv run gpucall validate-config --config-dir config
uv run gpucall security scan-secrets
```

Tests must not require cloud credentials, network access to providers, or a
database server. If your change needs provider behavior, model it the way the
existing suites do (fixtures under `tests/fixtures/config`, echo adapter,
`GPUCALL_ALLOW_FAKE_AUTO_TUPLES=1`).

## Practical notes

- `config/surfaces` and `config/workers` are **generated** — see
  [config/README.md](config/README.md). Edit the matrices in
  `config/candidate_sources/` and regenerate; never hand-edit the output.
- Every behavior change needs a test that fails without it.
- Commit messages: explain the observed failure and the root cause, not just
  the change. See recent history for the expected shape.
- CI must be green (`.github/workflows/ci.yml`) before review.

## Licensing of contributions

The gateway is AGPL-3.0-only and the SDK is Apache-2.0. By submitting a
contribution you certify the [Developer Certificate of Origin](https://developercertificate.org/)
(add `Signed-off-by:` with `git commit -s`) and agree that your contribution
is licensed under the license of the component it touches.
