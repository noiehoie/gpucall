#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== git status =="
git status --short

echo "== secret scan =="
uv run gpucall security scan-secrets

echo "== tracked private artifact grep =="
scripts/check_product_contamination.sh

echo "== tracked sensitive path grep =="
if git ls-files | rg '(^|/)(0508fullaudit|0509githubfullaudit|admin/|known_hosts$|id_rsa$|id_ed25519$|.*\.pem$|.*\.key$|.*secret.*|AGENTS\.md$|RESTART_HANDOFF\.md$)|(^|/)\.env$'
then
  echo "tracked sensitive path found" >&2
  exit 1
fi

echo "== root migration tests =="
uv run pytest

echo "== sdk tests =="
(cd sdk/python && uv run --with-editable . pytest)

echo "== local onboarding docs =="
sed -n '1,6p' docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
sed -n '1,6p' docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md

echo "== local sdk helper =="
(cd sdk/python && uv run --with-editable . gpucall-recipe-draft --help | sed -n '1,12p')

if [ "${GPUCALL_PUBLIC_RELEASE_REMOTE:-0}" = "1" ]; then
  echo "== public onboarding raw docs =="
  curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md | sed -n '1,6p'
  curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md | sed -n '1,6p'

  echo "== public sdk helper wheel =="
  curl -fsSL https://github.com/noiehoie/gpucall/releases/download/v2.0.8/SHA256SUMS
  uv tool run --from https://github.com/noiehoie/gpucall/releases/download/v2.0.8/gpucall_sdk-2.0.8-py3-none-any.whl gpucall-recipe-draft --help | sed -n '1,12p'
fi

echo "public release audit ok"
