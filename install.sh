#!/usr/bin/env sh
set -eu

program="gpucall install"
check_only=0
dry_run=0
bootstrap_uv=1
install_providers=1
package_spec="${GPUCALL_PACKAGE_SPEC:-}"
local_source=""

repo_archive_base="${GPUCALL_REPO_ARCHIVE_BASE:-https://github.com/noiehoie/gpucall}"
gpucall_ref="${GPUCALL_REF:-main}"
uv_install_url="${GPUCALL_UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"

log() {
  printf '%s\n' "$*"
}

warn() {
  printf '%s\n' "warning: $*" >&2
}

die() {
  printf '%s\n' "error: $*" >&2
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

usage() {
  cat <<'EOF'
gpucall install

Usage:
  sh install.sh [options]

Options:
  --check-only          Run dependency and environment checks only.
  --dry-run             Print the install actions without changing the host.
  --no-bootstrap-uv     Fail if uv is not already installed.
  --without-providers   Install the gateway CLI without optional provider SDKs.
  --package-spec SPEC   Install this uv package spec instead of the default.
  --local PATH          Install from a local gpucall checkout.
  --ref REF             Install from this GitHub ref when using the default archive.
  -h, --help            Show this help.

Environment:
  GPUCALL_PACKAGE_SPEC      Full uv package spec to install.
  GPUCALL_REF               GitHub ref for the default archive install. Default: main.
  GPUCALL_REPO_ARCHIVE_BASE Repository archive base URL.
  GPUCALL_UV_INSTALL_URL    uv installer URL.
  GPUCALL_ALLOW_ROOT=1      Allow running as root. Not recommended.
  XDG_BIN_HOME              Preferred user binary directory for uv/gpucall.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check-only)
      check_only=1
      ;;
    --dry-run)
      dry_run=1
      ;;
    --no-bootstrap-uv)
      bootstrap_uv=0
      ;;
    --without-providers)
      install_providers=0
      ;;
    --package-spec)
      [ "$#" -ge 2 ] || die "--package-spec requires a value"
      package_spec="$2"
      shift
      ;;
    --local)
      [ "$#" -ge 2 ] || die "--local requires a path"
      local_source="$2"
      shift
      ;;
    --ref)
      [ "$#" -ge 2 ] || die "--ref requires a value"
      gpucall_ref="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
  shift
done

script_dir=""
case "$0" in
  */*)
    script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
    ;;
esac

home_dir="${HOME:-}"
[ -n "$home_dir" ] || die "HOME is required"

uid_value=$(id -u 2>/dev/null || printf 'unknown')
if [ "$uid_value" = "0" ] && [ "${GPUCALL_ALLOW_ROOT:-0}" != "1" ]; then
  die "do not run this installer as root; use a normal operator account"
fi

os_name=$(uname -s 2>/dev/null || printf 'unknown')
arch_name=$(uname -m 2>/dev/null || printf 'unknown')
case "$os_name" in
  Linux|Darwin) ;;
  *) die "unsupported OS: $os_name" ;;
esac
case "$arch_name" in
  x86_64|amd64|arm64|aarch64) ;;
  *) die "unsupported architecture: $arch_name" ;;
esac

xdg_config_home="${XDG_CONFIG_HOME:-$home_dir/.config}"
xdg_data_home="${XDG_DATA_HOME:-$home_dir/.local/share}"
xdg_state_home="${XDG_STATE_HOME:-$home_dir/.local/state}"
xdg_cache_home="${XDG_CACHE_HOME:-$home_dir/.cache}"
if [ -n "${XDG_BIN_HOME:-}" ]; then
  user_bin_dir="$XDG_BIN_HOME"
elif [ -n "${XDG_DATA_HOME:-}" ]; then
  user_bin_dir="$(dirname -- "$xdg_data_home")/bin"
else
  user_bin_dir="$home_dir/.local/bin"
fi

prepend_path() {
  path_dir="$1"
  [ -n "$path_dir" ] || return
  case ":$PATH:" in
    *":$path_dir:"*) ;;
    *) PATH="$path_dir:$PATH" ;;
  esac
}

check_parent_writable() {
  target_dir="$1"
  if [ -d "$target_dir" ]; then
    [ -w "$target_dir" ] || die "$target_dir is not writable"
    return
  fi
  parent_dir=$(dirname -- "$target_dir")
  [ -d "$parent_dir" ] || parent_dir=$(dirname -- "$parent_dir")
  [ -d "$parent_dir" ] || die "parent directory for $target_dir does not exist"
  [ -w "$parent_dir" ] || die "parent directory for $target_dir is not writable"
}

check_parent_writable "$xdg_config_home"
check_parent_writable "$xdg_data_home"
check_parent_writable "$xdg_state_home"
check_parent_writable "$xdg_cache_home"

log "$program: dependency preflight"
log "  os: $os_name"
log "  arch: $arch_name"
log "  user: $(id 2>/dev/null || printf 'unknown')"
log "  xdg_config_home: $xdg_config_home"
log "  xdg_data_home: $xdg_data_home"
log "  xdg_state_home: $xdg_state_home"
log "  xdg_cache_home: $xdg_cache_home"
log "  user_bin_dir: $user_bin_dir"

if have uv; then
  uv_cmd=$(command -v uv)
  log "  uv: $uv_cmd"
else
  uv_cmd="$user_bin_dir/uv"
  if [ "$bootstrap_uv" = "1" ]; then
    have curl || die "curl is required to bootstrap uv"
    log "  uv: missing; installer will bootstrap uv from $uv_install_url"
  else
    die "uv is missing; rerun without --no-bootstrap-uv or install uv first"
  fi
fi

if have python3; then
  log "  python3: $(command -v python3) ($(python3 --version 2>&1))"
else
  warn "python3 not found on PATH; uv may still provide Python for tool installs"
fi

if have docker; then
  log "  docker: $(docker --version 2>/dev/null || command -v docker)"
  if docker compose version >/dev/null 2>&1; then
    log "  docker compose: $(docker compose version)"
  else
    warn "docker compose not found; production-like gateway setup will need Docker Compose"
  fi
else
  warn "docker not found; CLI install can continue, but production-like gateway setup will need Docker"
  warn "docker compose not found; production-like gateway setup will need Docker Compose"
fi

case ":$PATH:" in
  *":$user_bin_dir:"*) ;;
  *) warn "$user_bin_dir is not on PATH; add it before running gpucall from a new shell" ;;
esac

if [ -z "$package_spec" ]; then
  extras=""
  [ "$install_providers" = "1" ] && extras="[providers]"
  if [ -n "$local_source" ]; then
    local_abs=$(CDPATH= cd -- "$local_source" && pwd -P)
    [ -f "$local_abs/pyproject.toml" ] || die "--local path does not contain pyproject.toml"
    [ -d "$local_abs/gpucall" ] || die "--local path does not contain the gpucall package"
    package_spec="gpucall$extras @ file://$local_abs"
  elif [ -n "$script_dir" ] && [ -f "$script_dir/install.sh" ] && [ -f "$script_dir/pyproject.toml" ] && [ -d "$script_dir/gpucall" ]; then
    package_spec="gpucall$extras @ file://$script_dir"
  else
    case "$gpucall_ref" in
      main|master|develop|trunk)
        archive_path="heads/$gpucall_ref"
        ;;
      *)
        archive_path="tags/$gpucall_ref"
        ;;
    esac
    package_spec="gpucall$extras @ $repo_archive_base/archive/refs/$archive_path.zip"
  fi
fi

log "  package: $package_spec"

if [ "$check_only" = "1" ]; then
  log "$program: preflight complete"
  exit 0
fi

if [ "$dry_run" = "1" ]; then
  log "$program: dry-run"
  if have uv; then
    log "  would run: $(command -v uv) tool install --force \"$package_spec\""
  else
    log "  would run: curl -fsSL \"$uv_install_url\" | sh"
    log "  would run: $uv_cmd tool install --force \"$package_spec\""
  fi
  log "  next: gpucall setup"
  exit 0
fi

mkdir -p "$xdg_config_home" "$xdg_data_home" "$xdg_state_home" "$xdg_cache_home"

if ! have uv; then
  log "$program: installing uv"
  curl -fsSL "$uv_install_url" | sh
  prepend_path "$user_bin_dir"
  prepend_path "$home_dir/.local/bin"
  prepend_path "$home_dir/.cargo/bin"
  export PATH
fi

if ! have uv && [ -x "$uv_cmd" ]; then
  prepend_path "$(dirname -- "$uv_cmd")"
  export PATH
fi

have uv || die "uv install did not put uv on PATH; expected uv at $uv_cmd or another PATH entry"
uv_cmd=$(command -v uv)

log "$program: installing gpucall"
"$uv_cmd" tool install --force "$package_spec"

prepend_path "$user_bin_dir"
prepend_path "$home_dir/.local/bin"
export PATH
if have gpucall; then
  log "$program: installed $(command -v gpucall)"
else
  warn "gpucall was installed by uv but is not on PATH"
  warn "try: export PATH=\"$user_bin_dir:\$PATH\""
fi

log "$program: next commands"
log "  gpucall setup"
log "  gpucall setup status"
log "  gpucall setup next"
