# Public Repository Release Checklist

This checklist records the deterministic checks required before making this
repository publicly visible.

## Required Checks

```bash
git status --short
uv run gpucall security scan-secrets
uv run pytest
(cd sdk/python && uv run --with-editable . pytest)
uv run gpucall validate-config --config-dir config
uv run gpucall launch-check --profile static --config-dir config
git ls-files | rg '(^|/)(0508fullaudit|admin/|\.env$|known_hosts|id_rsa|id_ed25519|.*\.pem$|.*\.key$|.*secret.*|AGENTS\.md|RESTART_HANDOFF\.md)$'
rg -n '100\.91\.94\.11|root@|news-system|/Users/tamotsu|PRIVATE KEY|sk-[A-Za-z0-9]|AKIA[0-9A-Z]{16}' . --glob '!.git/**' --glob '!sdk/python/.venv/**' --glob '!sdk/typescript/node_modules/**' --glob '!scripts/public_release_audit.sh' --glob '!docs/PUBLIC_RELEASE_CHECKLIST.md'
uv tool run --from https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/gpucall_sdk-2.0.0a2-py3-none-any.whl gpucall-recipe-draft --help
```

The final `git ls-files | rg ...` and private-artifact `rg ...` commands must
return no tracked sensitive operational files. Public examples may remain when
they contain placeholders only.

## Publication Scope

The public v2.0 repository includes:

- gateway runtime code
- caller-side and administrator-side helper CLIs
- deterministic migration tooling
- sample configuration with credential references only
- Docker Compose, Helm, systemd, Postgres, Prometheus, and Grafana assets
- tests and documentation
- the public caller SDK helper wheel under `sdk/python/dist/`, so external
  systems can install `gpucall-recipe-draft` without cloning or installing the
  gateway package

The public v2.0 repository intentionally does not include:

- live credentials
- operator audit inboxes
- private AI council audit transcripts
- local state, cache, build artifacts, or gateway distribution artifacts
- high-confidential provider live connection credentials or deployments
