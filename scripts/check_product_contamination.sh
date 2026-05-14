#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "product contamination guard requires a git checkout" >&2
  exit 2
fi

scan_files() {
  git ls-files -z | while IFS= read -r -d '' path; do
    case "$path" in
      scripts/check_product_contamination.sh|scripts/public_release_audit.sh|docs/PUBLIC_RELEASE_CHECKLIST.md|tests/test_public_release_audit.py|third_party/openai/openapi.documented.yml)
        continue
        ;;
      gpucall/*|config/*|docs/*|README.md|README.ja.md|sdk/python/gpucall_sdk/*|sdk/python/README.md|scripts/*)
        [ -f "$path" ] && printf '%s\0' "$path"
        ;;
    esac
  done
}

pattern='100\.([6-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}|api\.runpod\.ai/v2/[a-z0-9]{12,}|vllm-[a-z0-9]{12,}(?=["'\''\s/:]|$)|^\s*ssh_remote_cidr:\s+(?!(203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|""|null))([0-9]{1,3}\.){3}[0-9]{1,3}|\broot@|news-system|news_system|/Users/tamotsu|PRIVATE KEY|sk-[A-Za-z0-9]|AKIA[0-9A-Z]{16}|no eligible provider after policy, recipe, and circuit constraints|provider-smoke|sdk/python/dist/.*\.whl'

files="$(mktemp)"
trap 'rm -f "$files"' EXIT
scan_files > "$files"

if [ ! -s "$files" ]; then
  echo "product contamination guard ok"
  exit 0
fi

set +e
xargs -0 rg --pcre2 -n "$pattern" < "$files"
status=$?
set -e

if [ "$status" -eq 0 ]; then
  echo "product contamination patterns found" >&2
  exit 1
fi
if [ "$status" -ne 1 ]; then
  echo "product contamination scan failed with status $status" >&2
  exit 2
fi

echo "product contamination guard ok"
