# gpucall v2 OOB User Experience Product Spec

Updated: 2026-05-31 12:35 JST

This document is the tracked product specification for the gpucall v2
Out-of-Box experience. It supersedes the local design notes under `tasks/`.

The target experience is: a first-time user reads the GitHub README, installs
gpucall, runs setup, configures provider credentials, hands an external-system
onboarding prompt to a caller-side AI CLI, and reaches either a working
route-scoped production path or a bounded, machine-classified blocker with the
next action.

## Product Principles

- gpucall is provider-neutral. Modal is the recommended first happy path, not
  the only supported provider.
- The first run must be useful even with zero cloud GPU providers configured.
  It must complete local trial, initialize control-plane state, and show a
  bounded next action.
- Panopticon is a provider control-plane service. It observes provider state and
  writes freshness-bounded evidence. It does not execute user workloads, mutate
  provider resources, or run billable validation by itself.
- Provider mutation is allowed only through explicit plan/apply gates. Modal
  worker deployment is a setup-scoped bootstrap mutation and still requires a
  concrete consent artifact.
- Route readiness is scoped to `recipe + tuple + mode + provider`. There is no
  global green state called `production-routing-ready`.
- Existing recipes must not suppress caller intake. Every caller onboarding run
  must leave assessment, trace, profile, contract, intake, and submission
  evidence.
- Secrets, raw payloads, presigned URLs, DataRef URIs, and provider raw outputs
  must not appear in logs, handoff packages, reports, or release artifacts.

## State Vocabulary

### Setup State

- `installed`: gpucall CLI is installed.
- `local-trial-ready`: local trial completed without provider credentials.
- `provider-selection-required`: no cloud provider has been configured.
- `provider-configured`: provider registry and credential store contain the
  required non-secret state and secrets for a provider.
- `provider-skipped`: the user explicitly skipped the provider for this run.
- `provider-pending`: credentials or partial metadata exist, but supply
  prerequisites are incomplete.
- `onboarding-ready`: gateway self-check, caller auth, handoff package, inboxes,
  admin automation processing, positive route-validation budget policy,
  validated-route activation automation, and required object-store scope are
  ready.
- `onboarding-ready-provisional`: caller onboarding may begin, but caller-side
  reachability or another caller-only check remains unproven.
- `onboarding-blocked`: handoff should not be given because the closed
  demand-to-supply loop cannot progress.
- `some-route-production-ready`: at least one exact route is ready. Output must
  also list blocked routes and requested-route readiness.

### Control-Plane Service State

- `service-uninitialized`: config, state, and evidence paths do not exist.
- `service-initialized`: paths and initial config exist, but no service manager
  is running it.
- `service-enabled`: optional diagnostic state used by external supervisors
  after registration but before running proof. `gpucall setup` may collapse this
  into `service-running` or `service-error`; it is never sufficient for
  `onboarding-ready`.
- `service-running`: health check, pid, or status endpoint proves the refresh
  loop is running.
- `service-error`: startup, permission, credential scope, or config validation
  failed.

### Provider State

- `credential-missing`: credentials have not been entered.
- `credential-configured`: credentials are stored in the credential store.
- `credential-authenticated`: non-generation provider auth probe succeeded.
- `credential-auth-failed`: non-generation provider auth probe failed.
- `supply-missing`: worker, endpoint, function, or VM does not exist.
- `supply-pending`: provisioning or user input is required.
- `supply-exists`: provider-side supply object exists.
- `supply-ready`: fresh non-generation readiness evidence exists.

### Route State

- `route-validation-missing`: exact route has no accepted validation evidence.
- `pending-budget-approval`: exact route is ready for billable validation, but
  the estimated validation cost exceeds the configured automatic validation
  budget. This is an operator approval wait, not an OOB product failure.
- `route-validation-accepted`: exact route has accepted validation evidence.
- `route-production-ready`: exact route has both fresh provider evidence and
  accepted validation evidence.
- `route-production-blocked`: exact route is blocked by policy, budget,
  provider evidence, validation evidence, object store, or caller contract.

## Default Freshness TTL Policy

Every freshness gate must have a packaged default TTL. Policy overrides may
shorten or lengthen these values, but an undefined TTL must fail closed.

These are the v2 packaged defaults:

| Evidence kind | Default TTL | Gate behavior |
| --- | ---: | --- |
| provider credential auth | 300 seconds | Required before provider supply checks. |
| endpoint/function existence | 300 seconds | Required before candidate validation. |
| worker/health/capacity/queue/model readiness | 300 seconds | Required before candidate validation and routing. |
| provider stock/capacity | 300 seconds | Required before provisioning or validation planning. |
| provider cost/storage/live cost | 300 seconds | Required before budget-sensitive planning. |
| provider live price | 3600 seconds | Required before strict production budget checks. |
| worker contract compatibility | 86400 seconds | Required before route validation. |
| live tuple catalog overlay | 300 seconds | Required before automatic tuple candidate use. |
| route validation evidence | policy value, default 604800 seconds | Required before production routing for the exact route and config hash. |
| caller handoff package self-check | 3600 seconds | Required before displaying `onboarding-ready`. |
| admin automation synthetic intake dry-run | 3600 seconds | Required before displaying `onboarding-ready`. |

Route validation evidence is valid only for the exact recipe hash, tuple hash,
mode, provider, worker contract hash, and relevant route config hash recorded in
the evidence. A config hash mismatch makes the evidence rejected even if its TTL
has not expired.

## Service Mode Decision Table

`gpucall setup` must select a service mode deterministically and show the result
in `gpucall setup status`.

| Install profile | Platform signal | Panopticon mode | Admin automation mode | Ready requirement |
| --- | --- | --- | --- | --- |
| local trial | no provider selected | `service-initialized` or foreground status probe | `service-initialized` | May be `local-trial-ready`, not `onboarding-ready`. |
| Linux user install | systemd user available | systemd user service | systemd user service/timer | Must be `service-running` for `onboarding-ready`. |
| macOS user install | launchd available | launchd user agent | launchd user agent | Must be `service-running` for `onboarding-ready`. |
| compose/server install | compose profile selected | docker compose service | docker compose service | Must be `service-running` for `onboarding-ready`. |
| CI/non-interactive validation | `CI=true` or explicit dry-run | foreground dry-run or disabled | synthetic dry-run only | Cannot claim `onboarding-ready` unless explicitly running services are verified. |
| production operator | `--profile production` | supervised service | supervised worker/timer | Gateway, Panopticon, admin automation, inboxes, and auth must all pass. |

If no safe supervisor is available, setup must stop at `service-initialized`,
print the exact foreground/status command, and avoid `Now you are good to go`.

Compose/server mode is selected explicitly with
`GPUCALL_SETUP_SERVICE_MODE=docker-compose-service`. It must also receive
`GPUCALL_SETUP_COMPOSE_FILE`; otherwise setup stops at `service-initialized`
with a bounded explanation. The default compose service names are
`gpucall-panopticon` and `gpucall-recipe-admin`; operators may override them
with `GPUCALL_SETUP_PANOPTICON_COMPOSE_SERVICE` and
`GPUCALL_SETUP_ADMIN_COMPOSE_SERVICE`.

## Provider Registry And Credential Boundary

The provider registry stores state and non-secret metadata only:

- provider name and provider state
- endpoint ID, function name, Modal environment, region, worker contract id
- Hyperstack SSH CIDR and SSH key path as operational metadata
- deployment id, ownership tag, cleanup manifest reference
- provider account docs URL and token/API-key creation URL

The credential store stores secrets only:

- API keys
- token secret and token ID when the provider treats both as credentials
- password values
- secret access keys
- private key contents

Redaction rules:

- Logs and handoff packages must never contain raw provider credentials.
- Handoff packages must not include provider metadata that the caller does not
  need, including endpoint IDs, SSH CIDR, SSH key path, region, deployment id,
  or cleanup manifest paths.
- `gpucall setup status --admin-detail` may show non-secret metadata to the
  operator, but must mask local usernames and home-directory prefixes in paths.
- SSH key path display must use a redacted form such as
  `<xdg-config>/gpucall/keys/<fingerprint>.pub` unless an explicit local admin
  detail flag is used.
- CIDR values are operationally sensitive. They may be shown in local admin
  status, but not in caller prompts, handoff packages, public docs, or logs
  intended for support bundles.

## Setup-Scoped Modal Mutation Consent

Modal worker deploy is the preferred OOB happy path, but it is still a provider
mutation. Consent must be concrete, not a generic boolean.

A valid consent artifact must contain:

- provider: `modal`
- action: `deploy_worker`
- target app/function/environment
- worker package version and worker package hash
- provider registry snapshot hash
- route/setup config hash
- dry-run result id
- plan hash
- estimated cost class
- created_at and expires_at
- ownership tag and cleanup manifest path

Interactive confirmation must show the dry-run summary and plan hash. In
non-interactive mode, `--yes` is accepted only when paired with the exact
generated plan hash, for example `--accept-plan-hash <hash>`. A generic
`allow_setup_scoped_provider_mutation: true` flag is invalid unless it is bound
to the same provider, action, target, dry-run result id, and plan hash.

## Admin Automation Synthetic Dry-Run

`onboarding-ready` requires evidence that the demand-to-supply loop can advance.
It is not enough that the admin automation process is running.

Setup must run a non-billable synthetic intake dry-run before claiming
`onboarding-ready`.

The dry-run must prove:

- recipe request inbox is writable by setup and readable by admin automation
- admin automation can parse a sanitized synthetic intake
- materialization can create or select a recipe candidate in dry-run mode
- the submission is moved or classified as `processed`, `pending`, or `failed`
- failed/pending records contain bounded reason codes and next commands
- no provider mutation, generation, or billable validation is performed
- no raw prompt, payload, key, presigned URL, or DataRef URI is written

The evidence artifact must include synthetic intake id, admin run id, status,
timestamps, redacted paths, and failure class if any. It must expire according
to the default TTL table.

## Caller Auth Lifecycle

Handoff generation must not create an unmanaged permanent secret.

Each caller auth record must carry:

- system name
- scope
- fingerprint
- created_at
- expires_at or explicit non-expiring policy reason
- rotation command
- revocation command
- last verification status

The standard handoff package must not contain a raw caller API key. If the
selected mode requires a one-time secret handoff, that secret must be delivered
as a separate chmod 600 local artifact or trusted-bootstrap exchange with
created_at, expires_at, fingerprint, and revocation metadata. Reports, logs,
manifests, and support bundles must show only `<set>`, `<redacted>`, or the
fingerprint.

Rotation and revocation are product requirements:

- `gpucall setup status` must show key age, expiry, and fingerprint.
- `gpucall admin tenant-key-rotate <system>` must produce a new key and a
  caller re-test command.
- `gpucall admin tenant-key-revoke <system>` must make the next request with
  that key return `401 unauthorized`.
- Handoff regeneration must prefer a fresh one-time handoff secret or an
  explicit trusted-bootstrap mode, not reuse a stale raw key silently.

## OOB Flow

1. User finds gpucall on GitHub and reads the README.
2. README shows one primary install command.
3. User runs the install command.
4. Installer checks OS, shell, Python, uv, XDG paths, and writable locations.
5. If uv is missing, installer explains and installs it when safe.
6. If installer cannot continue, it prints a bounded reason and one next
   command.
7. Installer succeeds and prints `gpucall setup`.
8. User runs `gpucall setup`.
9. Setup recommends local trial for first-time users.
10. Local trial creates only XDG config/state/cache/data.
11. Gateway minimal config is checked.
12. Panopticon config/state/evidence paths are initialized.
13. Service mode is selected from the decision table.
14. If provider credentials are absent, status is bounded as
    `no providers configured`; it must not hang.
15. Setup displays supported cloud GPU providers and marks Modal as the
    recommended first happy path.
16. Setup also displays RunPod and Hyperstack as first-class providers with
    credential requirements, account URLs, token/API-key URLs, and docs URLs.
17. User may configure zero or more providers.
18. Zero provider selection keeps `local-trial-ready` and
    `provider-selection-required`, then prints the provider setup resume command.
19. Provider credentials are saved only in the credential store.
20. Provider non-secret metadata is saved only in the provider registry.
21. Panopticon reloads provider registry and runs non-generation auth probes.
22. Modal authenticated state produces a setup-scoped deploy plan.
23. Modal deploy runs only with concrete consent.
24. Modal deploy writes ownership tag, deployment id, and cleanup manifest.
25. Panopticon bootstrap refresh observes function existence, worker contract,
    readiness, cost, stock, and health where applicable.
26. RunPod credentials with no endpoint ID are accepted as
    `supply-pending: endpoint provisioning required`.
27. Hyperstack credentials with missing SSH/CIDR/key prerequisites are accepted
    as `provider-config-blocker`.
28. Gateway URL and auth are checked.
29. Recipe request inbox and quality feedback inbox are created.
30. Admin automation service mode and status are checked.
31. Synthetic admin automation dry-run is executed.
32. Object store/DataRef scope is checked. Text-only onboarding may proceed
    without object store; file/vision validation and routing require it.
33. Handoff package is generated with populated non-secret values.
34. Handoff package excludes raw caller API keys, provider credentials, and
    caller-irrelevant provider metadata.
35. `onboarding-ready` is displayed only when gateway, auth, inboxes,
    Panopticon service, admin automation, synthetic dry-run, and required object
    store scope pass.
36. Otherwise setup displays `onboarding-ready-provisional` or
    `onboarding-blocked` with exact next commands.
37. Caller-side AI CLI receives only the handoff prompt and caller repository.
38. Caller-side AI CLI must not clone, vendor, or modify the gpucall repository.
39. Caller-side AI CLI installs only the SDK/helper wheel.
40. Caller-side AI CLI checks gateway reachability from the caller host.
41. Caller-side AI CLI runs assessment, trace, profile, and draft-contract.
42. Caller-side AI CLI submits sanitized intake even when a matching recipe may
    already exist.
43. Admin automation classifies the intake as processed, pending, or failed.
44. Existing recipe match links the intake evidence to that recipe.
45. Missing recipe materializes a new recipe candidate and tuple candidates.
46. Validation planning uses only fresh provider evidence and explicit budget
    policy.
47. Billable validation does not run without explicit budget policy or operator
    confirmation.
48. If validation cost exceeds the automatic budget, gpucall records
    `PENDING_BUDGET_APPROVAL` with estimated cost, current budget, recommended
    budget, and explicit approval commands.
49. Accepted validation evidence is stored for the exact route and config hash.
50. Gateway routes only when exact route provider evidence is fresh and route
    validation evidence is accepted.
50. Caller receives the normal application result, or a bounded No-Go reason
    with owner and next action.

## OOB Test Matrix

- clean host with uv missing: installer installs uv or stops with a bounded next
  command.
- clean host with no credentials: local trial succeeds and Panopticon reports
  `no providers configured`.
- provider selection zero: `provider-selection-required` is preserved.
- Panopticon initialized but not running: status shows `service-initialized` and
  start command.
- Panopticon running but stale evidence: service state and freshness state are
  separate.
- service credential scope unreadable: status shows
  `service-error: credential-scope-unreadable`.
- Modal deploy dry-run: no mutation without concrete consent and plan hash.
- non-interactive Modal deploy: `--yes` alone does not mutate provider state.
- RunPod credentials with endpoint zero: accepted as
  `supply-provisioning-required`, not as ready endpoint.
- Hyperstack missing SSH/CIDR/key: classified as `provider-config-blocker`.
- provider registry audit: metadata is saved; secrets are not.
- provider metadata redaction: SSH path/CIDR do not appear in handoff or support
  logs.
- admin automation process running but synthetic dry-run failing:
  `onboarding-blocked`.
- synthetic dry-run passing: evidence artifact exists and is within TTL.
- handoff package: populated values, no secrets in reports, no provider internal
  metadata.
- caller onboarding with existing recipe: intake evidence is still submitted.
- caller onboarding with no recipe: recipe candidate and tuple candidates are
  materialized.
- object store absent: text-only can proceed, file/vision validation/routing is
  blocked.
- validation evidence missing: gateway returns route validation blocker, not
  provider blocker.
- validation config hash mismatch: evidence is rejected.
- one route ready and requested route blocked: gateway does not route requested
  route.
- caller key rotation: old and new key can be distinguished by fingerprint.
- caller key revocation: revoked key returns `401 unauthorized`.
- cleanup dry-run: local resources and provider resources are separated.
- provider cleanup apply: only ownership-tag/deployment-id/manifest-matching
  provider resources are eligible.

## Release Audit Coverage

The public release audit must keep this specification executable by checking:

1. The tracked spec exists and is referenced from README and release checklist.
2. Setup status renders the default TTL policy and service lifecycle states.
3. The service mode table is represented in setup logic for local trial,
   systemd user service, launchd user agent, docker compose service, and
   CI/non-interactive dry-runs.
4. Modal provider mutation consent is bound to a deterministic plan hash and
   writes a cleanup manifest.
5. Provider registry metadata is separated from credential-store secrets.
6. Admin automation synthetic dry-run is fresh before `onboarding-ready`.
7. Admin automation service and Panopticon service must both be
   `service-running` before `onboarding-ready`.
8. Caller auth lifecycle records include fingerprint, rotation, revocation, and
   non-expiring policy reason.
9. OOB tests cover clean local trial, cloud credentials with endpoint pending,
   Compose service mode bounded failure, and stale/missing control-plane
   evidence.
