from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from gpucall.configure_registry import register_configure_target
from gpucall.credentials import save_credentials


@register_configure_target("modal", success_message=None, credential_contracts=("sdk_profile:modal",))
def configure_modal(_config_dir: Path) -> bool:
    if not shutil.which("modal"):
        print("Error: 'modal' CLI not found.", file=sys.stderr)
        print("Install it with: uv pip install --python /opt/gpucall/.venv/bin/python modal", file=sys.stderr)
        return False
    print("\n[INFO] Launching Modal's official setup. Follow the prompts below.\n")
    try:
        result = subprocess.run(["modal", "setup"])
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"\n'modal setup' failed (exit code {result.returncode}).", file=sys.stderr)
        return False
    print("\nModal authentication configured successfully.")
    return True


@register_configure_target("runpod-serverless", success_message=None, credential_contracts=("api_key:runpod",))
def configure_runpod_serverless(_config_dir: Path) -> bool:
    try:
        api_key = getpass.getpass("Enter your RunPod API Key (will be hidden): ").strip()
        if not api_key:
            return False
        save_credentials("runpod", {"api_key": api_key})
        print("RunPod Serverless endpoint ID belongs in providers/runpod.yml as target.")
        return True
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False


@register_configure_target("runpod-flash", label="runpod flash sdk login", success_message=None, credential_contracts=("sdk_profile:runpod-flash",))
def configure_runpod_flash(config_dir: Path) -> bool:
    if not shutil.which("flash"):
        print("Error: 'flash' CLI not found.", file=sys.stderr)
        print("Install it with: uv pip install --python /opt/gpucall/.venv/bin/python runpod-flash", file=sys.stderr)
        return False
    print("\n[INFO] Launching RunPod Flash's official setup. Follow the prompts below.\n")
    try:
        result = subprocess.run(["flash", "login"])
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"\n'flash login' failed (exit code {result.returncode}).", file=sys.stderr)
        return False
    _mirror_runpod_flash_config(config_dir)
    print("\nRunPod Flash authentication configured successfully.")
    return True


@register_configure_target("hyperstack", credential_contracts=("api_key:hyperstack", "ssh_key:hyperstack"))
def configure_hyperstack(_config_dir: Path) -> bool:
    try:
        api_key = getpass.getpass("Enter your Hyperstack API Key (will be hidden): ").strip()
        if not api_key:
            return False
        ssh_key_path = input("Enter SSH key path (optional, default ~/.ssh/id_rsa): ").strip()
        save_credentials("hyperstack", {"api_key": api_key, "ssh_key_path": ssh_key_path})
        return True
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False


@register_configure_target("cloudflare-r2", label="cloudflare r2 object store", credential_contracts=("object_store:s3",))
def configure_cloudflare_r2(config_dir: Path) -> bool:
    try:
        access_key = getpass.getpass("Enter Cloudflare R2 Access Key ID (will be hidden): ").strip()
        secret_key = getpass.getpass("Enter Cloudflare R2 Secret Access Key (will be hidden): ").strip()
        if not access_key or not secret_key:
            return False
        region = input("Enter Cloudflare R2 region (default auto): ").strip() or "auto"
        endpoint = input("Enter Cloudflare R2 S3 endpoint URL: ").strip()
        bucket = input("Enter Cloudflare R2 bucket name: ").strip()
        save_credentials("aws", {"access_key_id": access_key, "secret_access_key": secret_key, "region": region, "endpoint_url": endpoint})
        if bucket:
            _write_object_store(config_dir, bucket=bucket, region=region, endpoint=endpoint)
        return True
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False


@register_configure_target("auth", label="gateway auth", credential_contracts=("gateway_auth:api_keys",))
def configure_auth(_config_dir: Path) -> bool:
    try:
        api_key = getpass.getpass("Enter gpucall Gateway API key for clients (will be hidden): ").strip()
        if not api_key:
            return False
        save_credentials("auth", {"api_keys": api_key})
        return True
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False


def _mirror_runpod_flash_config(config_dir: Path) -> None:
    source = Path(os.path.expanduser("~/.runpod/config.toml"))
    if not source.exists():
        return
    target = config_dir / ".runpod" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o600)


def _write_object_store(config_dir: Path, *, bucket: str, region: str, endpoint: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "object_store.yml"
    data = {
        "provider": "s3",
        "bucket": bucket,
        "region": region,
        "endpoint": endpoint or None,
        "prefix": "gpucall",
        "presign_ttl_seconds": 900,
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    print(f"Updated {path}")
