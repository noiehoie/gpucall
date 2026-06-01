from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


def test_install_script_is_present_executable_and_syntax_valid() -> None:
    assert INSTALL_SH.exists()
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR
    subprocess.run(["sh", "-n", str(INSTALL_SH)], check=True)


def test_install_script_uses_uv_tool_not_global_pip_or_sudo() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "tool install --force" in text
    assert "pip install" not in text
    assert "sudo " not in text
    assert "apt-get" not in text
    assert "brew install" not in text
    assert "GPUCALL_ALLOW_ROOT=1" in text
    assert "do not run this installer as root" in text


def test_install_script_has_preflight_and_non_mutating_modes() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    for expected in (
        "--check-only",
        "--dry-run",
        "--no-bootstrap-uv",
        "dependency preflight",
        "docker compose",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_BIN_HOME",
        "GPUCALL_PACKAGE_SPEC",
    ):
        assert expected in text


def test_install_script_dry_run_from_checkout_does_not_install() -> None:
    env = os.environ.copy()
    env["GPUCALL_ALLOW_ROOT"] = "1"
    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--dry-run", "--no-bootstrap-uv"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    output = result.stdout + result.stderr
    assert "gpucall install: dependency preflight" in output
    assert "gpucall install: dry-run" in output
    assert "gpucall[providers] @ file://" in output
    assert "gpucall setup starter-plan --profile local-trial" in output
    assert "Modal credentials and cloud happy path" in output


def test_install_script_bootstraps_uv_from_xdg_data_bin(tmp_path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env sh
set -eu
cat <<'UV_INSTALLER'
#!/usr/bin/env sh
set -eu
[ "${INSTALLER_NO_MODIFY_PATH:-}" = "1" ] || exit 91
bin_dir="${UV_INSTALL_DIR:-$(dirname -- "${XDG_DATA_HOME:-$HOME/.local/share}")/bin}"
mkdir -p "$bin_dir"
cat > "$bin_dir/uv" <<'UV'
#!/usr/bin/env sh
set -eu
if [ "${1:-}" = "tool" ] && [ "${2:-}" = "install" ]; then
  bin_dir="${UV_INSTALL_DIR:-$(dirname -- "${XDG_DATA_HOME:-$HOME/.local/share}")/bin}"
  mkdir -p "$bin_dir"
  cat > "$bin_dir/gpucall" <<'GPUCALL'
#!/usr/bin/env sh
printf 'fake gpucall\\n'
GPUCALL
  chmod 755 "$bin_dir/gpucall"
  exit 0
fi
exit 2
UV
chmod 755 "$bin_dir/uv"
UV_INSTALLER
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    home = tmp_path / "home"
    xdg_root = tmp_path / "xdg"
    for path in (home, xdg_root / "config", xdg_root / "share", xdg_root / "state", xdg_root / "cache"):
        path.mkdir(parents=True)
    env = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "GPUCALL_ALLOW_ROOT": "1",
        "XDG_CONFIG_HOME": str(xdg_root / "config"),
        "XDG_DATA_HOME": str(xdg_root / "share"),
        "XDG_STATE_HOME": str(xdg_root / "state"),
        "XDG_CACHE_HOME": str(xdg_root / "cache"),
    }

    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--package-spec", "gpucall-test"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    output = result.stdout + result.stderr

    assert "user_bin_dir: " + str(xdg_root / "bin") in output
    assert "uv install did not put uv on PATH" not in output
    assert f"gpucall install: installed {xdg_root / 'bin' / 'gpucall'}" in output
    assert not (home / ".profile").exists()
    assert not (home / ".zshrc").exists()


def test_readmes_start_with_installer_not_setup_binary() -> None:
    for relative in ("README.md", "README.ja.md", "docs/SETUP_PLAN.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall/main/install.sh | sh" in text
        assert "GPUCALL_REF=<ref> sh" in text
        assert text.index("install.sh") < text.index("gpucall setup")


def test_compose_does_not_mount_modal_dotfile_into_gpucall_config() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GPUCALL_MODAL_CONFIG_FILE" not in compose
    assert ".modal.toml" not in compose
    assert "MODAL_TOKEN_ID" in compose
    assert "MODAL_TOKEN_SECRET" in compose
