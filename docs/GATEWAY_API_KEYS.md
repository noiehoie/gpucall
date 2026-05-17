# Gateway API Key Operations

This document defines how gpucall administrators issue, register, hand off,
verify, and rotate caller-facing gateway API keys.

Provider credentials are not gateway API keys. Never give provider credentials
from `credentials.yml` to an external system. External systems receive only:

- `GPUCALL_BASE_URL`
- `GPUCALL_API_KEY`
- `GPUCALL_RECIPE_INBOX` when recipe request submission is allowed

## Key Model

gpucall supports two gateway key modes:

- Tenant-scoped keys: `GPUCALL_TENANT_API_KEYS` or `auth.tenant_keys`
- Legacy default-tenant keys: `GPUCALL_API_KEYS` or `auth.api_keys`

Use tenant-scoped keys for every real external system. Legacy default keys are
only for simple local setups and compatibility.

Tenant-scoped keys have the form:

```text
tenant-name:gateway-api-key
```

At request time, clients send only the key:

```http
Authorization: Bearer <gateway-api-key>
```

The gateway maps the key back to the tenant and applies that tenant's rate,
budget, and object-prefix controls.

## 1. Create Tenant Quota Metadata

Create a tenant before issuing a key:

```bash
gpucall admin tenant-create \
  --name example-system \
  --daily-budget-usd 25 \
  --monthly-budget-usd 500 \
  --max-request-estimated-cost-usd 10 \
  --object-prefix example-system
```

This writes tenant quota metadata under the configured gpucall config directory.
It does not create a secret.

Verify:

```bash
gpucall admin tenant-list
```

## 2. Issue A Gateway API Key

Generate and register a tenant-scoped key:

```bash
gpucall admin tenant-key-create --name example-system
```

The command:

- generates a random `gpk_...` token;
- writes it to the gpucall credentials file as `auth.tenant_keys`;
- prints the token once for handoff;
- prints a stable SHA-256 fingerprint for future verification.

The output includes `api_key`. Treat that value as a secret. Do not paste it in
tickets, READMEs, completion reports, or logs.

Verify without exposing the key:

```bash
gpucall admin tenant-key-list
```

This prints only tenant names and key fingerprints.

## 3. One-Step Internal Onboarding Automation

For internal systems, administrators often want one deterministic command that:

- creates tenant quota metadata if it does not already exist;
- generates and registers a tenant-scoped gateway API key;
- writes a handoff file for the target system;
- avoids printing the raw key to stdout when a handoff file is used.

This route is disabled unless the operator explicitly enables it in
`admin.yml`:

```yaml
api_key_handoff_mode: handoff_file
```

Prefer the CLI over hand-editing `admin.yml`:

```bash
gpucall admin automation-status --config-dir /etc/gpucall
gpucall admin automation-configure \
  --config-dir /etc/gpucall \
  --handoff-mode handoff_file
```

The default is:

```yaml
api_key_handoff_mode: manual
```

In `manual` mode, `tenant-onboard` refuses to run. Administrators must use
`tenant-create` and `tenant-key-create`, then pass the key through their
approved manual secret channel.

For fully unattended internal environments, use:

```bash
gpucall admin automation-configure \
  --config-dir /etc/gpucall \
  --handoff-mode trusted_bootstrap \
  --bootstrap-allowed-cidr 10.0.0.42/32 \
  --bootstrap-gateway-url https://gpucall.example.internal \
  --bootstrap-recipe-inbox admin@gpucall.example.internal:/srv/gpucall/state/recipe_requests/inbox
```

In this mode, trusted clients can request their own tenant-scoped key:

```bash
curl -fsS -X POST "$GPUCALL_BASE_URL/v2/bootstrap/tenant-key" \
  -H 'content-type: application/json' \
  -d '{"system_name":"example-system"}'
```

The response contains a one-time `api_key` and a `handoff` object. Store it in
the caller system's secret manager or local integration environment. If the
tenant key already exists, gpucall returns `409` and does not reprint the key.
If the request is outside the configured CIDR/host allowlist, gpucall returns
`403`.

Use:

```bash
gpucall admin tenant-onboard \
  --name example-system \
  --gateway-url https://gpucall-gateway.example.internal \
  --recipe-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox \
  --daily-budget-usd 25 \
  --monthly-budget-usd 500 \
  --max-request-estimated-cost-usd 10 \
  --format env \
  --output /secure/handoff/example-system.gpucall.env
```

The output file is created with mode `0600`. It contains:

```bash
GPUCALL_TENANT='example-system'
GPUCALL_BASE_URL='https://gpucall-gateway.example.internal'
GPUCALL_API_KEY='gpk_...'
GPUCALL_RECIPE_INBOX='admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox'
GPUCALL_ONBOARDING_PROMPT_URL='https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md'
GPUCALL_ONBOARDING_MANUAL_URL='https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md'
GPUCALL_SDK_WHEEL_URL='https://github.com/noiehoie/gpucall/releases/download/v2.0.18/gpucall_sdk-2.0.18-py3-none-any.whl'
```

For machine-to-machine provisioning, use JSON:

```bash
gpucall admin tenant-onboard \
  --name example-system \
  --gateway-url https://gpucall-gateway.example.internal \
  --recipe-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox \
  --format json \
  --output /secure/handoff/example-system.gpucall.json
```

`--output` is required. gpucall does not print automated handoff payloads
containing raw API keys to stdout.

`tenant-onboard` refuses to continue when a tenant key already exists. This is
intentional: re-running onboarding must not silently reprint an existing secret.
For rotation, follow the rotation procedure below.

## 4. Batch Onboarding For Parallel Migrations

When several systems are migrated at the same time, do not run an ad hoc shell
loop. Use a manifest so gpucall can validate the batch before issuing any key.

Example manifest:

```yaml
systems:
  - name: example-news
    daily_budget_usd: 25
    monthly_budget_usd: 500
  - name: example-analysis
    requests_per_minute: 30
    daily_budget_usd: 10
```

Run:

```bash
gpucall admin tenant-onboard-batch \
  --manifest systems.yml \
  --gateway-url https://gpucall-gateway.example.internal \
  --recipe-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox \
  --format env \
  --output /secure/handoff
```

The command enforces:

- each system has a valid tenant name;
- no duplicate tenant names exist in the manifest;
- no tenant key already exists for any listed system;
- no handoff file already exists;
- the handoff directory is `0700`;
- each handoff file is `0600`;
- stdout contains fingerprints and paths only, never raw API keys.

This is the physical guardrail for parallel migration:

```text
1 manifest row = 1 tenant = 1 API key = 1 handoff file
```

## 5. Runtime Registration Alternatives

The CLI writes to the XDG credentials file by default:

```text
$XDG_CONFIG_HOME/gpucall/credentials.yml
```

or:

```text
~/.config/gpucall/credentials.yml
```

For container or secret-manager deployments, provide the same mapping through
the gateway process environment instead:

```bash
export GPUCALL_TENANT_API_KEYS='example-system:gpk_...'
```

Multiple systems are comma-separated:

```bash
export GPUCALL_TENANT_API_KEYS='example-news:gpk_...,example-analysis:gpk_...'
```

Do not store gateway API keys in tenant YAML, provider YAML, recipe YAML, tuple
YAML, README files, or tests.

## 6. Handoff Package For An External System

The gpucall administrator prepares a per-system handoff package. It contains
facts, not provider credentials:

```text
GPUCALL_BASE_URL=https://gpucall-gateway.example.internal
GPUCALL_API_KEY=<gateway key generated for this system>
GPUCALL_RECIPE_INBOX=admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox
ONBOARDING_PROMPT=https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
ONBOARDING_MANUAL=https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md
CALLER_HELPER_INSTALL=https://github.com/noiehoie/gpucall/releases/download/v2.0.18/gpucall_sdk-2.0.18-py3-none-any.whl
```

The GitHub URLs in this package are references or default distribution
locations. They are not a substitute for the environment-specific handoff. The
administrator must set the real gateway base URL, recipe inbox, key delivery
route, and any private SDK/helper mirror used by the installed router. External
systems should treat this handoff and the live gateway OpenAPI schema as
authoritative.

Only `GPUCALL_API_KEY` is secret. The other values may still be operationally
sensitive, but they are not provider credentials.

The external system must load these values from its own secret manager,
deployment environment, or local integration environment. It must not hard-code
the API key.

## 7. External System Acceptance Rules

An external system is not `Go` until all of these are true:

- `GPUCALL_BASE_URL` is set to the real gateway URL.
- `GPUCALL_API_KEY` is set to a real tenant-scoped gateway key.
- `GPUCALL_API_KEY=dummy` is never used.
- `Authorization: Bearer $GPUCALL_API_KEY` is sent on gateway requests.
- A live canary reaches the gateway and gets the expected response.
- Unknown workload preflight is submitted to the approved inbox, not merely
  generated as a command.
- Logs and completion reports show only `GPUCALL_API_KEY=<set>` and never the
  raw token.

If any of these fail, the external system reports `No-Go`.

## 8. Rotation And Revocation

To rotate a tenant key:

1. Generate a new key with a temporary tenant entry or by editing
   `auth.tenant_keys` in the gateway credentials/secret manager.
2. Deploy the new key to the external system.
3. Run the external system live canary.
4. Remove the old key from `auth.tenant_keys` or `GPUCALL_TENANT_API_KEYS`.
5. Restart or reload the gateway process if its deployment reads credentials
   only at startup.
6. Confirm `gpucall admin tenant-key-list` shows the expected fingerprint.

To revoke a system immediately, remove its tenant key from the gateway
credentials or environment and restart/reload the gateway if required. The next
request with that key must return `401 unauthorized`.

## 9. Operator Checks

Before giving a key to a system:

```bash
gpucall validate-config
gpucall admin tenant-list
gpucall admin tenant-key-list
gpucall launch-check --profile static
```

After a system claims migration is complete:

```bash
gpucall audit verify
gpucall admin tenant-usage
```

The completion report from the external system must include:

- `GPUCALL_BASE_URL=<set>`
- `GPUCALL_API_KEY=<set>`
- live canary result
- preflight submission status
- confirmation that no API key, Authorization header, prompt body, DataRef URI,
  or presigned URL was printed
