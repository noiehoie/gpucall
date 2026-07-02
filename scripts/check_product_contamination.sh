#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "product contamination guard requires a git checkout" >&2
  exit 2
fi

if ! command -v rg >/dev/null 2>&1; then
  echo "product contamination guard requires ripgrep (rg) on PATH" >&2
  exit 3
fi

scan_files() {
  git ls-files -z | while IFS= read -r -d '' path; do
    case "$path" in
      scripts/check_product_contamination.sh|third_party/openai/openapi.documented.yml)
        continue
        ;;
    esac
    [ -f "$path" ] && printf '%s\0' "$path"
  done
}

pattern='100\.([6-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}|api\.runpod\.ai/v2/[a-z0-9]{12,}|vllm-[a-z0-9]{12,}(?=["'\''\s/:]|$)|^\s*ssh_remote_cidr:\s+(?!(203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|""|null))([0-9]{1,3}\.){3}[0-9]{1,3}|\broot@|news-system|news_system|/Users/(tamotsu|admin)|\b(tamotsu|sugano|macmini|macstudio|netcup2?|菅野)\b|152\.53\.228\.117|159\.195\.29\.243|\bNEWS_[A-Z0-9_]*\b|PRIVATE KEY|\bsk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}|no eligible provider after policy, recipe, and circuit constraints|provider-smoke|sdk/python/dist/.*\.whl'

files="$(mktemp)"
hits="$(mktemp)"
trap 'rm -f "$files" "$hits"' EXIT
scan_files > "$files"

if [ ! -s "$files" ]; then
  echo "product contamination guard ok"
  exit 0
fi

# Judge by captured output, not exit codes: xargs may split the file list into
# several rg invocations, and the mix of "found" (0) and "not found" (1) exit
# codes previously collapsed into a false pass on some platforms.
set +e
xargs -0 rg --pcre2 -n "$pattern" < "$files" > "$hits" 2>&1
status=$?
set -e

if [ -s "$hits" ]; then
  cat "$hits"
  echo "product contamination patterns found" >&2
  exit 1
fi
if [ "$status" -gt 1 ] && [ "$status" -ne 123 ]; then
  echo "product contamination scan failed with status $status" >&2
  exit 2
fi

echo "product contamination guard ok"
