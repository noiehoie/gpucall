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
    assert "next: gpucall setup" in output


def test_readmes_start_with_installer_not_setup_binary() -> None:
    for relative in ("README.md", "README.ja.md", "docs/SETUP_PLAN.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall/main/install.sh | sh" in text
        assert text.index("install.sh") < text.index("gpucall setup")


def test_compose_does_not_mount_modal_dotfile_into_gpucall_config() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GPUCALL_MODAL_CONFIG_FILE" not in compose
    assert ".modal.toml" not in compose
    assert "MODAL_TOKEN_ID" in compose
    assert "MODAL_TOKEN_SECRET" in compose
