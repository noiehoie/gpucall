#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== git status =="
git status --short

echo "== secret scan =="
uv run gpucall security scan-secrets

echo "== private artifact grep =="
if rg -n '100\.91\.94\.11|root@|news-system|/Users/tamotsu|PRIVATE KEY|sk-[A-Za-z0-9]|AKIA[0-9A-Z]{16}' . \
  --glob '!.git/**' \
  --glob '!sdk/python/.venv/**' \
  --glob '!sdk/typescript/node_modules/**' \
  --glob '!sdk/python/build/**' \
  --glob '!scripts/public_release_audit.sh' \
  --glob '!docs/PUBLIC_RELEASE_CHECKLIST.md'
then
  echo "private artifact patterns found" >&2
  exit 1
fi

echo "== tracked sensitive path grep =="
if git ls-files | rg '(^|/)(0508fullaudit|admin/|\.env$|known_hosts|id_rsa|id_ed25519|.*\.pem$|.*\.key$|.*secret.*|AGENTS\.md|RESTART_HANDOFF\.md)$'
then
  echo "tracked sensitive path found" >&2
  exit 1
fi

echo "== root migration tests =="
uv run pytest

echo "== sdk tests =="
(cd sdk/python && uv run --with-editable . pytest)

echo "== public onboarding raw docs =="
curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md | sed -n '1,6p'
curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md | sed -n '1,6p'

echo "== public sdk helper wheel =="
curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/SHA256SUMS
uv tool run --from https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/gpucall_sdk-2.0.0a2-py3-none-any.whl gpucall-recipe-draft --help | sed -n '1,12p'

echo "public release audit ok"
