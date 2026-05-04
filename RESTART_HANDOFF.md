# gpucall3 Restart Handoff

Date: 2026-05-04

## Current State

The working context is very long. Restarting the assistant session is recommended before consuming the next external AI Council response.

The latest completed local/netcup hardening cycle was R3 closure. Final verification before this handoff:

- Local Python tests: `185 passed, 1 warning`
- TypeScript SDK: `npm run build` passed
- Isolated wheel install: `gpucall init` then `gpucall validate-config` passed
- Gateway package artifact: config templates included; root `gpucall_sdk` not included in gateway wheel
- netcup deployment: rebuilt/recreated Docker container
- netcup tests: `174 passed, 1 warning`
- netcup `readyz`: `{"status":"ready","object_store":true}`
- netcup live `/v2/tasks/sync` smoke: HTTP 200, `text-infer-light`, selected provider `modal-a10g`, non-empty inline result
- Final R3 closure audit subagent verdict: PASS

The remaining warning is a test-only Pydantic serializer warning involving a fake signed URL. It was not treated as a production blocker.

## Important Recent Fixes

- `gpucall/config.py`: Pydantic `ValidationError` is summarized with `include_input=False` so secret-bearing input values are not echoed.
- `gpucall/cli.py`: `gpucall init` now preserves `.example` provider templates instead of activating incomplete providers.
- `tests/test_config.py`: added regression tests for sanitized config validation errors and valid default init config.
- Earlier R3 fixes already in tree include:
  - config templates packaged via `gpucall/config_templates`
  - Python SDK rejects non-string message content
  - unmatched metrics route labels collapse to `UNMATCHED`
  - public provider errors hide raw output
  - Docker runs non-root and excludes SDK/docs/tests/config from build context

## User’s Current Direction

The user believes the previous “100-point checklist” was artificial and hallucination-prone. They want an external AI Council to audit the actual codebase, not a high-level summary.

The latest requested prompt must:

- enumerate gpucall component files by absolute path
- instruct the council to read every listed file line by line
- require findings to be tied to file path / line / function / config key / endpoint
- distinguish:
  - confirmed from code
  - needs official documentation confirmation
  - needs billable live test
  - speculation
- avoid an artificial fixed number of items

The latest assistant response already drafted such a prompt with file paths. If the user returns with an external AI Council answer, first parse it critically and do not assume it is correct.

## Known Process Cleanup

Before this handoff, stale gpucall-related processes were killed:

- old `gemini --yolo --resume ...`
- old `agent`/council onboarding script process
- old `ssh netcup ... stream smoke` process

No git repository is present in this workspace, so there is no `git status` source of truth.

## Next Session First Steps

1. Read this file first.
2. If the user provides the external AI Council answer, summarize findings into:
   - true blockers
   - likely false positives
   - needs official-doc verification
   - needs live/billable test
3. Do not start implementation until the findings are triaged unless the user explicitly orders “潰せ”.
4. If implementation is ordered, preserve the “honest deterministic router” principle:
   - no caller provider/recipe routing
   - no hidden prompt/message mutation
   - no fake provider production success
   - provider official contracts only
   - route decisions explainable by recipe/policy/provider contracts

