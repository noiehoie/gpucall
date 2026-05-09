#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== git status =="
git status --short

echo "== secret scan =="
uv run gpucall security scan-secrets

echo "== tracked private artifact grep =="
if git ls-files | while IFS= read -r path; do
  case "$path" in
    scripts/public_release_audit.sh|docs/PUBLIC_RELEASE_CHECKLIST.md|tests/test_public_release_audit.py) continue ;;
  esac
  [ -f "$path" ] && printf '%s\0' "$path"
done | xargs -0 rg --pcre2 -n '100\.([6-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}|api\.runpod\.ai/v2/[a-z0-9]{12,}|vllm-[a-z0-9]{12,}|^\s*ssh_remote_cidr:\s+(?!(203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|""|null))([0-9]{1,3}\.){3}[0-9]{1,3}|\broot@|news-system|/Users/tamotsu|PRIVATE KEY|sk-[A-Za-z0-9]|AKIA[0-9A-Z]{16}|no eligible provider after policy, recipe, and circuit constraints|provider-smoke|https://raw\.githubusercontent\.com/noiehoie/gpucall3/main/|sdk/python/dist/.*\.whl'
then
  echo "private artifact patterns found" >&2
  exit 1
fi

echo "== tracked sensitive path grep =="
if git ls-files | rg '(^|/)(0508fullaudit|0509githubfullaudit|admin/|\.env$|known_hosts|id_rsa|id_ed25519|.*\.pem$|.*\.key$|.*secret.*|AGENTS\.md|RESTART_HANDOFF\.md)$'
then
  echo "tracked sensitive path found" >&2
  exit 1
fi

echo "== root migration tests =="
uv run pytest

echo "== sdk tests =="
(cd sdk/python && uv run --with-editable . pytest)

echo "== public onboarding raw docs =="
curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall3/v2.0.8/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md | sed -n '1,6p'
curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall3/v2.0.8/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md | sed -n '1,6p'

echo "== public sdk helper wheel =="
curl -fsSL https://github.com/noiehoie/gpucall3/releases/download/v2.0.8/SHA256SUMS
uv tool run --from https://github.com/noiehoie/gpucall3/releases/download/v2.0.8/gpucall_sdk-2.0.8-py3-none-any.whl gpucall-recipe-draft --help | sed -n '1,12p'

echo "public release audit ok"
