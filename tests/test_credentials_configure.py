from __future__ import annotations

import os
import stat

from gpucall.configure import configure_command
from gpucall.credentials import configured_credentials, load_credentials, save_credentials


def test_credentials_save_uses_0600_and_env_override(tmp_path, monkeypatch) -> None:
    path = tmp_path / "credentials.yml"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(path))

    save_credentials("runpod", {"api_key": "from-file"})
    monkeypatch.setenv("GPUCALL_RUNPOD_API_KEY", "from-env")

    mode = stat.S_IMODE(os.stat(path).st_mode)
    creds = load_credentials()

    assert mode == 0o600
    assert creds["runpod"]["api_key"] == "from-env"


def test_configure_runpod_interactive_flow(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "credentials.yml"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(path))
    answers = iter(["runpod-serverless", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret-key")

    configure_command(tmp_path / "config")

    creds = load_credentials()
    out = capsys.readouterr().out
    assert creds["runpod"] == {"api_key": "secret-key"}
    assert "api_key:runpod" in configured_credentials()
    assert "tuples/runpod.yml" in out
    assert "runpod-serverless" in out
    assert "Setup session finished" in out
    assert "gpucall doctor" in out


def test_configure_status_uses_checkmark(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "credentials.yml"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(path))
    save_credentials("runpod", {"api_key": "secret-key"})
    answers = iter(["done"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    configure_command(tmp_path / "config")

    out = capsys.readouterr().out
    assert "✓ runpod-serverless" in out
    assert "(configured)" not in out
    assert "1." in out


def test_configure_prompt_accepts_numbered_selection(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "credentials.yml"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(path))
    answers = iter(["2", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret-key")

    configure_command(tmp_path / "config")

    creds = load_credentials()
    out = capsys.readouterr().out
    assert creds["runpod"] == {"api_key": "secret-key"}
    assert "2.   runpod-serverless" in out


def test_configure_runpod_flash_install_hint_uses_uv(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "credentials.yml"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(path))
    answers = iter(["3", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("shutil.which", lambda binary: None if binary == "flash" else binary)

    configure_command(tmp_path / "config")

    captured = capsys.readouterr()
    assert "uv pip install --python /opt/gpucall/.venv/bin/python runpod-flash" in captured.err
    assert "pip install runpod-flash" not in captured.err


def test_configure_cloudflare_r2_flow(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "credentials.yml"
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(path))
    answers = iter(["5", "auto", "https://example.r2.cloudflarestorage.com", "gpucall", "n"])
    secrets = iter(["access-key", "secret-key"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(secrets))

    configure_command(tmp_path / "config")

    creds = load_credentials()
    out = capsys.readouterr().out
    object_store = (tmp_path / "config" / "object_store.yml").read_text(encoding="utf-8")
    assert creds["aws"]["access_key_id"] == "access-key"
    assert "cloudflare r2 object store" in out
    assert "region: auto" in object_store
    assert "bucket: gpucall" in object_store


def test_runpod_flash_detects_runpod_config_toml(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".runpod").mkdir()
    (tmp_path / ".runpod" / "config.toml").write_text("[credentials]\n", encoding="utf-8")

    assert "sdk_profile:runpod-flash" in configured_credentials()
