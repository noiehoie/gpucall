# Public Repository Release Checklist

This checklist records the deterministic checks required before making this
repository publicly visible.

For operator launch steps, use `gpucall launch-check`, `gpucall smoke`,
`gpucall audit verify`, `gpucall post-launch-report`, and tuple-specific
`gpucall tuple-smoke ...` commands only in private deployment runbooks. Keep
live endpoint IDs, hostnames, credentials, and local audit transcripts out of
the public repository.

## Required Checks

```bash
git status --short
uv run gpucall security scan-secrets
uv run pytest
(cd sdk/python && uv run --with-editable . pytest)
uv run gpucall validate-config --config-dir config
uv run gpucall launch-check --profile static --config-dir config --output-json "$XDG_STATE_HOME/gpucall/release/static-launch-check.json"
uv run gpucall release-check --config-dir config --output-dir "$XDG_STATE_HOME/gpucall/release"
git ls-files | rg '(^|/)(0508fullaudit|0509githubfullaudit|admin/|\.env$|known_hosts|id_rsa|id_ed25519|.*\.pem$|.*\.key$|.*secret.*|AGENTS\.md|RESTART_HANDOFF\.md)$'
git ls-files --error-unmatch docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md >/dev/null
rg -q '## Default Freshness TTL Policy' docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md
rg -q '## Service Mode Decision Table' docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md
rg -q '## Admin Automation Synthetic Dry-Run' docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md
scripts/check_product_contamination.sh
uv run python scripts/check_provider_parity.py
uv tool run --from https://github.com/noiehoie/gpucall/releases/download/v2.0.38/gpucall_sdk-2.0.38-py3-none-any.whl gpucall-recipe-draft --help
curl -fsSLO https://github.com/noiehoie/gpucall/releases/download/v2.0.38/SHA256SUMS
```

The product-contamination and private-artifact commands must
return no tracked sensitive operational files. Public examples may remain when
they contain placeholders only.

`release-check` is the public product artifact gate. It writes
`release-manifest.json`, `openapi.json`, `static-launch-check.json`, and
`production-acceptance.json`. It does not claim operator production traffic is
ready; live provider evidence, gateway auth, object-store credentials, and
tuple validation remain deployment launch gates.

## Publication Scope

The public v2.0 repository includes:

- gateway runtime code
- caller-side and administrator-side helper CLIs
- deterministic migration tooling
- sample configuration with credential references only
- Docker Compose, Helm, systemd, Postgres, Prometheus, and Grafana assets
- tests and documentation
- the tracked OOB user-experience product specification:
  `docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md`
- release assets for the public caller SDK helper wheel and checksum, so external
  systems can install `gpucall-recipe-draft` without cloning or installing the
  gateway package

The public v2.0 repository intentionally does not include:

- live credentials
- operator audit inboxes
- private AI council audit transcripts
- local state, cache, build artifacts, wheel files, or gateway distribution artifacts
- high-confidential provider live connection credentials or deployments
- private operator launch checklists
