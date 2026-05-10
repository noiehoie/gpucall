# gpucall Setup Plan

`gpucall setup` is the first-run operator journey for gpucall. It hides the
large internal config tree behind an operating profile, a checklist dashboard,
and setup-as-code.

Use the guided flow for a first manual install:

```bash
gpucall setup
gpucall setup status
gpucall setup next
```

Use a setup plan when an operator or SRE wants deterministic, repeatable setup:

```bash
gpucall setup apply --file gpucall.setup.yml --dry-run
gpucall setup apply --file gpucall.setup.yml --yes
```

## Plan Example

```yaml
setup_schema_version: 1
profile: internal-team

gateway:
  base_url: https://gpucall.example.internal
  caller_auth:
    mode: generated_gateway_key

providers:
  modal:
    enabled: true
    credentials:
      source: official_cli

  runpod:
    enabled: true
    credentials:
      source: gpucall_credentials
    endpoint_id: rp-xxxxxxxxxxxx

  hyperstack:
    enabled: false

object_store:
  provider: cloudflare_r2
  bucket: gpucall-data
  region: auto
  endpoint_url: https://xxxxx.r2.cloudflarestorage.com
  credentials:
    source: gpucall_credentials

tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.0/24
  allowed_hosts: []
  recipe_inbox: operator@gpucall.example.internal:/opt/gpucall/state/recipe_requests/inbox

recipe_automation:
  auto_materialize: true
  auto_promote_candidates: false
  auto_billable_validation: false
  auto_activate_validated: false
  promotion_work_dir: /opt/gpucall/state/recipe_requests/promotions

handoff_assets:
  onboarding_prompt_url: https://assets.example/gpucall/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
  onboarding_manual_url: https://assets.example/gpucall/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md
  caller_sdk_wheel_url: https://assets.example/gpucall/gpucall_sdk-2.0.8-py3-none-any.whl

external_systems:
  - name: example-system
    expected_workloads:
      - infer
      - vision

launch:
  run_static_check: true
  require_object_store: true
  require_gateway_auth: true
```

## Top-Level Fields

- `setup_schema_version`: must be `1`.
- `profile`: one of `local-trial`, `internal-team`,
  `production-multitenant`, `hardened-regulated`.
- `gateway`: gateway URL and caller authentication policy.
- `providers`: GPU execution surfaces to enable.
- `object_store`: DataRef object store configuration.
- `tenant_onboarding`: how external systems receive tenant-scoped API keys.
- `recipe_automation`: optional gateway-side automation for sanitized recipe
  request inbox processing.
- `handoff_assets`: optional operator-hosted onboarding documents and caller
  SDK wheel for private deployments or pre-release installations.
- `external_systems`: named systems for handoff prompt generation.
- `launch`: launch-check expectations.

## Credential Sources

Raw secrets do not belong in setup plan YAML. Do not write API keys or private
tokens into `gpucall.setup.yml`.

Supported v2 sources:

```yaml
credentials:
  source: official_cli
```

Use the provider's official login profile. Modal uses `modal setup`; RunPod
Flash uses `flash login`. gpucall does not store the provider secret in YAML.

```yaml
credentials:
  source: prompt
```

Ask for the secret during `gpucall setup apply`. The value is written to the
gpucall credentials store, not to repository config YAML.

This source is interactive. It is rejected with `gpucall setup apply --yes`
because unattended setup must not block on a hidden prompt.

```yaml
credentials:
  source: gpucall_credentials
```

Use credentials already present in the gpucall credentials store. This is the
preferred source for unattended production apply.

After a non-dry-run apply, gpucall reports post-apply checks for config
loading, secret-like YAML keys, and the static launch-check handoff. Treat any
failed post-apply check as a setup blocker before starting the gateway.

Not supported in v2 setup plans:

```yaml
api_key_env: RUNPOD_API_KEY
```

Environment-variable-name fields are intentionally not part of the setup plan
grammar. Operators should either prompt once into the gpucall credentials store
or pre-provision the credentials store through their own secret-management
process.

## Provider Rules

Modal:

```yaml
providers:
  modal:
    enabled: true
    credentials:
      source: official_cli
```

RunPod managed endpoint:

```yaml
providers:
  runpod:
    enabled: true
    credentials:
      source: gpucall_credentials
    endpoint_id: rp-xxxxxxxxxxxx
```

RunPod API keys are credentials. Endpoint IDs are routing metadata and may live
in config.

Hyperstack:

```yaml
providers:
  hyperstack:
    enabled: true
    credentials:
      source: gpucall_credentials
    ssh_key_path: /etc/gpucall/ssh/hyperstack_ed25519
```

Hyperstack API keys and SSH key paths belong in the credentials store. Region,
shape, model, and worker contracts remain catalog config.

## Object Store Rules

Object store configuration is separate from GPU provider configuration.

```yaml
object_store:
  provider: cloudflare_r2
  bucket: gpucall-data
  endpoint_url: https://xxxxx.r2.cloudflarestorage.com
  credentials:
    source: gpucall_credentials
```

The bucket, endpoint, region, and prefix are config. Access keys are
credentials.

## Tenant Handoff

```yaml
tenant_onboarding:
  mode: manual
```

Administrators issue keys explicitly.

```yaml
tenant_onboarding:
  mode: handoff_file
```

`gpucall admin tenant-onboard` may write `0600` handoff files.

```yaml
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 10.0.0.0/24
  recipe_inbox: operator@gpucall.example.internal:/opt/gpucall/state/recipe_requests/inbox
```

Trusted internal systems may request their own tenant key through
`POST /v2/bootstrap/tenant-key`. This mode requires at least one CIDR or host
allowlist entry.

## Recipe Automation

Recipe automation is gateway-side only. It starts after an external system has
submitted sanitized preflight or quality-feedback intake to the approved inbox.

```yaml
recipe_automation:
  auto_materialize: true
  auto_promote_candidates: true
  auto_billable_validation: false
  auto_activate_validated: false
  promotion_work_dir: /opt/gpucall/state/recipe_requests/promotions
```

The chain is ordered and fail-closed:

- `auto_materialize`: convert sanitized intake into canonical recipe YAML and
  move the original submission to `processed/` or `failed/`.
- `auto_promote_candidates`: prepare an isolated candidate tuple promotion
  workspace when the catalog has matching candidate contracts.
- `auto_billable_validation`: run billable tuple validation from that isolated
  workspace.
- `auto_activate_validated`: copy only successfully validated recipes/tuples
  into active production config.

Each step requires the previous one. Setup plan validation rejects impossible
chains such as billable validation without candidate promotion. The automation
does not invent provider credentials, endpoint IDs, or provider targets.

## Handoff Assets

Public releases use the built-in GitHub URLs. Private or pre-public
deployments can point handoff prompts at operator-hosted copies instead:

```yaml
handoff_assets:
  onboarding_prompt_url: https://assets.example/gpucall/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
  onboarding_manual_url: https://assets.example/gpucall/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md
  caller_sdk_wheel_url: https://assets.example/gpucall/gpucall_sdk-2.0.8-py3-none-any.whl
```

`gpucall setup export-handoff-prompt` uses these values when present. This
prevents external systems from depending on a public GitHub tag before the
operator has actually published it.

## Handoff Prompt

After setup, generate an external-system prompt:

```bash
gpucall setup export-handoff-prompt --system-name example-system
```

The prompt includes the gateway URL, bootstrap endpoint, recipe inbox, onboarding
docs, and SDK helper wheel URL. It does not include an API key.
