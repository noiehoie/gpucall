from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_process_environment(tmp_path, monkeypatch):
    xdg_config = tmp_path / "xdg-config"
    xdg_state = tmp_path / "xdg-state"
    credentials = tmp_path / "credentials.yml"
    credentials.write_text("version: 1\nproviders: {}\n", encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))

    for name in (
        "GPUCALL_API_KEY",
        "GPUCALL_API_KEYS",
        "GPUCALL_CONFIG_DIR",
        "GPUCALL_STATE_DIR",
        "GPUCALL_RUNPOD_API_KEY",
        "GPUCALL_RUNPOD_ENDPOINT_ID",
        "GPUCALL_RUNPOD_FLASH_ENDPOINT_ID",
        "GPUCALL_HYPERSTACK_API_KEY",
        "GPUCALL_HYPERSTACK_SSH_KEY_PATH",
        "GPUCALL_HYPERSTACK_LEASE_MANIFEST",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_ENDPOINT_URL_S3",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "R2_ENDPOINT_URL",
        "GPUCALL_MODAL_APP",
        "GPUCALL_MODAL_FN",
        "GPUCALL_MODAL_STREAM_FN",
        "GPUCALL_ALLOW_CALLER_ROUTING",
        "GPUCALL_ALLOW_FAKE_AUTO_TUPLES",
        "GPUCALL_RUNPOD_FLASH_EXPERIMENTAL_WORKER",
    ):
        monkeypatch.delenv(name, raising=False)
