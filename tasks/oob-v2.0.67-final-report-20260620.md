# gpucall v2.0.67 OOB final acceptance report

Date: 2026-06-20 JST

Decision: Go

Scope:

- Public release artifact: v2.0.67
- Clean-room install on netcup2 with a fresh HOME/XDG tree
- Production-configured netcup2 gateway/handoff
- macmini news-system external caller canary

Sanitization:

- No API keys, authorization headers, raw prompts, presigned URLs, DataRef URIs, raw payload contents, or provider raw outputs are included.

## Result summary

- Public v2.0.67 release assets are present.
- Clean-room install from the public install script succeeds even when `uv` is absent.
- Clean-room local trial reaches `local-trial-ready` and prints the next Modal happy-path commands.
- Production netcup2 setup reaches `onboarding-ready`.
- Generated handoff points to the v2.0.67 SDK wheel and includes the corrected `max_tokens` / `timeout_seconds` contract.
- news-system caller-side tests pass.
- news-system external canary passes all 5 intents:
  - extract_json
  - translate_text
  - summarize_text
  - rank_text_items
  - vision

## Release artifact evidence

Command:

```bash
gh release view v2.0.67 --repo noiehoie/gpucall --json tagName,url,assets --jq '{tagName,url,assets:[.assets[].name]}'
```

Output:

```json
{"assets":["gpucall-2.0.67-py3-none-any.whl","gpucall-2.0.67.tar.gz","gpucall_sdk-2.0.67-py3-none-any.whl","gpucall_sdk-2.0.67.tar.gz","SHA256SUMS"],"tagName":"v2.0.67","url":"https://github.com/noiehoie/gpucall/releases/tag/v2.0.67"}
```

Command:

```bash
curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall/v2.0.67/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md \
  | grep -nE 'GPUCALL_SDK_WHEEL_URL|max_tokens|timeout_seconds|For vision requests' \
  | sed -n '1,40p'
```

Output:

```text
382:  "max_tokens": 64
422:- Do not add default `max_tokens` or `timeout_seconds` fields to every
425:  `timeout_seconds` is sent, it must be at or below the accepted recipe lease.
426:- For vision requests, put image/file DataRefs in `input_refs` and put the text
```

## Clean-room install evidence

Clean HOME:

```text
/tmp/gpucall-oob-v2067-20260620T201656
```

Install command:

```bash
env -i HOME="$home" USER=admin SHELL=/bin/bash PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  XDG_CONFIG_HOME="$home/.config" \
  XDG_DATA_HOME="$home/.local/share" \
  XDG_STATE_HOME="$home/.local/state" \
  XDG_CACHE_HOME="$home/.cache" \
  GPUCALL_REF=v2.0.67 \
  sh -c 'curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall/v2.0.67/install.sh | sh'
```

Key output:

```text
gpucall install: dependency preflight
  uv: missing; installer will bootstrap uv from https://astral.sh/uv/install.sh
  package: gpucall[providers] @ https://github.com/noiehoie/gpucall/archive/v2.0.67.zip
gpucall install: installing uv
everything's installed!
gpucall install: installing gpucall
 + gpucall==2.0.67 (from https://github.com/noiehoie/gpucall/archive/v2.0.67.zip)
Installed 3 executables: gpucall, gpucall-migrate, gpucall-recipe-admin
gpucall install: installed /tmp/gpucall-oob-v2067-20260620T201656/.local/bin/gpucall
gpucall install: next commands
  gpucall setup
  gpucall setup starter-plan --profile local-trial
  gpucall setup apply --file gpucall.setup.yml --dry-run
  gpucall setup apply --file gpucall.setup.yml --yes
After local trial, gpucall setup next will guide you to the Modal credentials and cloud happy path.
```

Setup commands:

```bash
gpucall --version
gpucall setup status
gpucall setup starter-plan --profile local-trial --output gpucall.setup.yml
gpucall setup apply --file gpucall.setup.yml --dry-run
gpucall setup apply --file gpucall.setup.yml --yes
gpucall setup status
gpucall setup next
```

Key output:

```text
gpucall 2.0.67

Profile: unselected
OOB readiness: onboarding-blocked
Next command:
  gpucall setup next

Wrote starter setup plan: gpucall.setup.yml

No changes written because --dry-run is set.

Applied setup plan.
Panopticon bootstrap refresh:
  [warn] no cloud providers configured
Post-apply checks:
  [ok] validate-config: 32 recipes, 3233 tuples
  [ok] security scan-secrets: 0 findings
Profile: local-trial
OOB readiness: local-trial-ready
Local trial is complete.

To use gpucall with external systems, configure a cloud provider next.
Recommended happy path: Modal.

If you already have a Modal account and token, run:
  gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml
  gpucall setup apply --file gpucall.modal.setup.yml --dry-run
  gpucall setup apply --file gpucall.modal.setup.yml --accept-plan-hash <plan_hash>
  # Non-interactive apply: add --yes to the final command.

If you do not yet have any cloud GPU provider account, create a Modal account and token first.
Without provider credentials, gpucall will not start cloud routing; it remains fail-closed.
```

Clean-room conclusion:

- Install command is understandable and bounded.
- Missing `uv` is handled by the installer.
- With no cloud credentials, gpucall does not hang and does not pretend cloud routing is ready.
- The next action is machine-readable and human-readable.

## Production setup evidence

Command:

```bash
ssh netcup2 '/home/admin/.local/bin/gpucall setup apply --file /home/admin/.local/state/gpucall/setup/oob-reapply-20260620.yml --config-dir /home/admin/.config/gpucall --yes'
ssh netcup2 '/home/admin/.local/bin/gpucall setup status --config-dir /home/admin/.config/gpucall'
```

Key output:

```text
Applied setup plan.

Panopticon bootstrap refresh:
  [warn] inline live probes skipped; setup preflight evidence written and Provider Panopticon service will refresh provider evidence in the background

Admin automation synthetic dry-run:
  [ok] synthetic intake parsed and classified without provider mutation or billable work

Caller handoff packages:
  [ok] news-system: Caller handoff package generated.
    package: /home/admin/.local/share/gpucall/handoffs/news-system
    give to caller-side AI CLI: /home/admin/.local/share/gpucall/handoffs/news-system/caller-ai-onboarding-prompt.md

Post-apply checks:
  [ok] validate-config: 32 recipes, 3233 tuples
  [ok] security scan-secrets: 0 findings

Profile: internal-team
OOB readiness: onboarding-ready
Admin synthetic dry-run: ok
```

Gateway restart evidence:

```text
stopping_gateway_pid=1357409
started_gateway_pid=1357498
{"status":"ok"}
LISTEN 0      2048                   100.93.87.4:18088      0.0.0.0:*    users:(("gpucall",pid=1357498,fd=19))
```

Handoff verification:

```text
15:- GPUCALL_SDK_WHEEL_URL: `https://github.com/noiehoie/gpucall/releases/download/v2.0.67/gpucall_sdk-2.0.67-py3-none-any.whl`
29:- Do not send `max_tokens` or `timeout_seconds` as default routing selectors. Omit them unless the caller has an explicit lower workload contract; if `timeout_seconds` is sent, it must be at or below the accepted recipe lease.
230:   Do not add default `max_tokens` or `timeout_seconds` fields to every request. They can change recipe selection or violate recipe lease policy; omit them unless a caller-owned workload contract requires a lower bound.
231:   For vision requests, put image/file DataRefs in `input_refs` and put the text prompt in `inline_inputs.prompt`.
```

## External caller canary evidence

Command:

```bash
ssh macmini 'cd /Users/admin/Developer/news-system && uv run pytest tests/test_gpucall_v2.py -q'
```

Output:

```text
................................                                         [100%]
32 passed in 0.05s
```

Command:

```bash
ssh macmini 'cd /Users/admin/Developer/news-system && set -a && . ./.env >/dev/null 2>&1 && set +a && export LLM_BACKEND=gpucall && uv run python <sanitized_canary_script>'
```

Output:

```text
extract_json=PASS chars=28
translate_text=PASS chars=15
summarize_text=PASS chars=55
rank_text_items=PASS chars=43
vision=PASS chars=179
canary_result=GO
```

## Final decision

Go.

Remaining caveat:

- This result depends on the configured Modal provider path staying funded and routable. That is an operational dependency, not an OOB product-flow blocker in this run.

