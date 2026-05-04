# Security Notes

## Data Plane

Large or sensitive payload bytes must be uploaded to S3-compatible object storage. The gateway receives metadata and `DataRef` references, not the object body.

## Secrets

Secrets are loaded from credentials files or environment variables. They must not be committed to YAML policy, recipe, or provider files.

## Audit

Audit entries are hash chained and redacted. Inline inputs and signed URLs are fingerprinted rather than logged in plaintext.

## Threat Model Summary

Primary risks:

- Accidental secret commit in config YAML
- Oversized request body DoS
- Signed URL leakage
- Provider credential leakage
- Orphan remote resources
- Provider cross-tenant cleanup mistakes

MVP controls:

- `extra="forbid"` Pydantic config schemas
- `gpucall security scan-secrets`
- request body limit middleware
- audit redaction
- provider-specific cleanup
- Hyperstack orphan reconciliation
- local-only Docker binding by default
- production auth fail-closed mode with `GPUCALL_ENV=production` or `GPUCALL_PRODUCTION=1`
- constant-time bearer token comparison
- actual body-size checks for requests that omit or falsify `Content-Length`

## Production Authentication

In development, a gateway with no configured API key can be used locally. In production mode, protected routes fail closed when no key is configured. Set one of:

```bash
export GPUCALL_ENV=production
export GPUCALL_API_KEYS="<comma-separated tokens>"
```

or:

```bash
export GPUCALL_PRODUCTION=1
```

`/healthz` and `/readyz` remain public. Task, object, job, and metrics routes require the configured bearer token unless explicitly documented otherwise.

## Hyperstack SSH Host Keys

Hyperstack production SSH requires known-host pinning:

```bash
export GPUCALL_HYPERSTACK_KNOWN_HOSTS=/path/to/known_hosts
```

Without this setting, Hyperstack SSH is rejected in production mode. Development mode may still use first-use host-key acceptance for local validation only.

Deferred to v2.1:

- penetration-style abuse tests
- chaos tests
- organization-wide policy engine
- full SIEM integration
