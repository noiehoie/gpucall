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

Deferred to v2.1:

- penetration-style abuse tests
- chaos tests
- organization-wide policy engine
- full SIEM integration
