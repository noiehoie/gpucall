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
git ls-files | rg '(^|/)(0508fullaudit|0509githubfullaudit|admin/|\.env$|known_hosts|id_rsa|id_ed25519|.*\.pem$|.*\.key$|.*secret.*|AGENTS\.md|RESTART_HANDOFF\.md)$'
git ls-files | while IFS= read -r path; do case "$path" in scripts/public_release_audit.sh|docs/PUBLIC_RELEASE_CHECKLIST.md|tests/test_public_release_audit.py) continue ;; esac; [ -f "$path" ] && printf '%s\0' "$path"; done | xargs -0 rg --pcre2 -n '100\.([6-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}|api\.runpod\.ai/v2/[a-z0-9]{12,}|vllm-[a-z0-9]{12,}|^\s*ssh_remote_cidr:\s+(?!(203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|""|null))([0-9]{1,3}\.){3}[0-9]{1,3}|\broot@|news-system|/Users/tamotsu|PRIVATE KEY|sk-[A-Za-z0-9]|AKIA[0-9A-Z]{16}|no eligible provider after policy, recipe, and circuit constraints|provider-smoke|https://raw\.githubusercontent\.com/noiehoie/gpucall/main/|sdk/python/dist/.*\.whl'
uv tool run --from https://github.com/noiehoie/gpucall/releases/download/v2.0.17/gpucall_sdk-2.0.17-py3-none-any.whl gpucall-recipe-draft --help
curl -fsSLO https://github.com/noiehoie/gpucall/releases/download/v2.0.17/SHA256SUMS
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
