# Public Repository Release Checklist

This checklist records the deterministic checks required before making this
repository publicly visible.

## Required Checks

```bash
git status --short
uv run gpucall security scan-secrets
PYTHONPATH=sdk/python uv run pytest -q
uv run gpucall validate-config --config-dir config
uv run gpucall launch-check --profile static --config-dir config
git ls-files | rg '(^|/)(0508fullaudit|admin/|\.env$|known_hosts|id_rsa|id_ed25519|.*\.pem$|.*\.key$|.*secret.*|AGENTS\.md)$'
```

The final `git ls-files | rg ...` command must return no tracked sensitive
operational files. Public examples may remain when they contain placeholders
only.

## Publication Scope

The public v2.0 repository includes:

- gateway runtime code
- caller-side and administrator-side helper CLIs
- deterministic migration tooling
- sample configuration with credential references only
- Docker Compose, Helm, systemd, Postgres, Prometheus, and Grafana assets
- tests and documentation

The public v2.0 repository intentionally does not include:

- live credentials
- operator audit inboxes
- private AI council audit transcripts
- local state, cache, build, or distribution artifacts
- high-confidential provider live connection credentials or deployments
