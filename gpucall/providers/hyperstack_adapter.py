from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from gpucall.domain import ArtifactManifest, CompiledPlan, ProviderError, ProviderResult
from gpucall.config import default_state_dir
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.payloads import plan_payload
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter

HYPERSTACK_API_BASE = "https://infrahub-api.nexgencloud.com/v1"
DEFAULT_HYPERSTACK_IMAGE = "Ubuntu Server 22.04 LTS R570 CUDA 12.8 with Docker"


class HyperstackAdapter(ProviderAdapter):
    def __init__(
        self,
        name: str = "hyperstack",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        ssh_key_path: str | None = None,
        environment_name: str = "default",
        flavor_name: str = "n3-A100x1",
        image_name: str = DEFAULT_HYPERSTACK_IMAGE,
        key_name: str = "gpucall-key",
        lease_manifest_path: str | None = None,
        ssh_remote_cidr: str | None = None,
        model: str | None = None,
        max_model_len: int | None = None,
    ) -> None:
        self.name = name
        self.api_key = api_key or os.getenv("GPUCALL_HYPERSTACK_API_KEY", "")
        self.base_url = (base_url or HYPERSTACK_API_BASE).rstrip("/")
        self.ssh_key_path = ssh_key_path or os.getenv("GPUCALL_HYPERSTACK_SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa"))
        self.environment_name = environment_name
        self.flavor_name = flavor_name
        self.image_name = image_name
        self.key_name = key_name
        self.model = model
        self.max_model_len = max_model_len
        self.ssh_remote_cidr = ssh_remote_cidr or ""
        self.lease_manifest_path = Path(
            lease_manifest_path
            or os.getenv("GPUCALL_HYPERSTACK_LEASE_MANIFEST", str(default_state_dir() / "hyperstack_leases.jsonl"))
        ).expanduser()

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        if not self.api_key:
            raise ProviderError("Hyperstack API key is not configured", retryable=False, status_code=401)
        meta = await asyncio.to_thread(self._provision_and_start, plan)
        return RemoteHandle(
            provider=self.name,
            remote_id=meta["vm_id"],
            expires_at=plan.expires_at(),
            meta=meta,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        return await asyncio.to_thread(self._wait_sync, handle, plan)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        await asyncio.to_thread(self._destroy_sync, handle.meta)

    def _headers(self) -> dict[str, str]:
        return {"api_key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"}

    def _session(self):
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
        except ImportError as exc:
            raise ProviderError("requests/urllib3 are required for Hyperstack", retryable=False, status_code=501) from exc
        session = requests.Session()
        retry = Retry(total=0)
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _provision_and_start(self, plan: CompiledPlan) -> dict[str, Any]:
        if not self.ssh_remote_cidr:
            raise ProviderError("Hyperstack ssh_remote_cidr must be explicitly configured", retryable=False, status_code=400)
        try:
            network = ipaddress.ip_network(self.ssh_remote_cidr, strict=False)
        except ValueError as exc:
            raise ProviderError("Hyperstack ssh_remote_cidr is invalid", retryable=False, status_code=400) from exc
        if network.prefixlen == 0:
            raise ProviderError("Hyperstack ssh_remote_cidr must not allow all addresses", retryable=False, status_code=400)
        session = self._session()
        vm_name = f"gpucall-managed-{plan.plan_id[:12]}-{uuid4().hex[:8]}"
        self._record_lease({"event": "provision.requested", "vm_name": vm_name, "plan_id": plan.plan_id, "expires_at": plan.expires_at().isoformat()})
        response = session.post(
            f"{self.base_url}/core/virtual-machines",
            headers=self._headers(),
            json={
                "name": vm_name,
                "environment_name": self.environment_name,
                "image_name": self.image_name,
                "flavor_name": self.flavor_name,
                "key_name": self.key_name,
                "count": 1,
                "assign_floating_ip": True,
                "create_bootable_volume": False,
                "enable_port_randomization": False,
                "labels": ["gpucall-managed", f"gpucall-plan-{plan.plan_id[:12]}"],
            },
            timeout=10,
        )
        if response.status_code not in {200, 201, 202}:
            retryable = response.status_code in {404, 409, 423, 429, 500, 502, 503, 504}
            code = "PROVIDER_PROVISION_UNAVAILABLE" if retryable else "PROVIDER_PROVISION_FAILED"
            raise ProviderError(
                f"Hyperstack provision failed: {response.status_code}",
                retryable=retryable,
                status_code=503 if retryable else 502,
                code=code,
            )
        data = response.json()
        instances = data.get("instances") or []
        vm_id = (instances[0].get("id") if instances else None) or data.get("id") or data.get("instance", {}).get("id")
        if not vm_id:
            raise ProviderError("Hyperstack response did not include vm id", retryable=True, status_code=502)
        self._record_lease(
            {
                "event": "provision.created",
                "vm_name": vm_name,
                "vm_id": vm_id,
                "plan_id": plan.plan_id,
                "expires_at": plan.expires_at().isoformat(),
            }
        )
        sg_rule_id: str | None = None
        try:
            ip_address = self._wait_active(session, vm_id)
            sg_rule_id = self._ensure_ssh_rule(session, vm_id)
            if sg_rule_id:
                self._record_lease({"event": "sg_rule.created", "vm_id": vm_id, "sg_rule_id": sg_rule_id})
            ssh = self._connect_ssh(ip_address)
            model = self.model or "Qwen/Qwen2.5-1.5B-Instruct"
            max_model_len = self.max_model_len or plan.token_budget or 32768
            self._upload_worker_files(ssh, plan)
            cmd = (
                "set -euo pipefail\n"
                "test -s /tmp/gpucall/input.json\n"
                "test -s /tmp/gpucall/worker.py\n"
                "python3 - <<'PY'\n"
                "import os, subprocess, sys\n"
                "os.environ.setdefault('GPUCALL_WORKER_MODEL', " + repr(model) + ")\n"
                "os.environ.setdefault('GPUCALL_WORKER_MAX_MODEL_LEN', " + repr(str(max_model_len)) + ")\n"
                "os.environ.setdefault('GPUCALL_WORKER_TENSOR_PARALLEL_SIZE', '1')\n"
                "if subprocess.call([sys.executable, '-m', 'pip', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:\n"
                "    subprocess.check_call(['sudo', 'apt-get', 'update'])\n"
                "    subprocess.check_call(['sudo', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', 'python3-pip'])\n"
                "deps='/tmp/gpucall/deps'\n"
                "os.makedirs(deps, exist_ok=True)\n"
                "subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', '--target', deps, "
                "'boto3>=1.34', 'cryptography>=42', 'vllm==0.6.3', 'transformers==4.45.2', 'huggingface-hub[hf_transfer]', 'hf_transfer'])\n"
                "env=os.environ.copy()\n"
                "env['PYTHONPATH']=deps + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')\n"
                "subprocess.check_call([sys.executable, '/tmp/gpucall/worker.py'], env=env)\n"
                "PY\n"
            )
            _, stdout, _ = ssh.exec_command(cmd)
            return {
                "vm_id": vm_id,
                "vm_name": vm_name,
                "ip_address": ip_address,
                "sg_rule_id": sg_rule_id,
                "ssh_client": ssh,
                "ssh_channel": stdout.channel,
            }
        except Exception:
            self._destroy_sync({"vm_id": vm_id, "sg_rule_id": sg_rule_id})
            raise

    def _upload_worker_files(self, ssh: Any, plan: CompiledPlan) -> None:
        payload = json.dumps(plan_payload(plan), separators=(",", ":"))
        sftp = ssh.open_sftp()
        try:
            try:
                sftp.mkdir("/tmp/gpucall")
            except OSError:
                pass
            _sftp_write_text(sftp, "/tmp/gpucall/input.json", payload)
            _sftp_write_text(sftp, "/tmp/gpucall/worker.py", _hyperstack_worker_script())
        finally:
            sftp.close()

    def _wait_active(self, session: Any, vm_id: str) -> str:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = session.get(f"{self.base_url}/core/virtual-machines/{vm_id}", headers=self._headers(), timeout=5)
            if response.status_code == 200:
                instance = response.json().get("instance", {})
                status = instance.get("status")
                if status == "ACTIVE":
                    ip = instance.get("floating_ip") or instance.get("public_ip") or instance.get("access_ip")
                    if ip:
                        return str(ip)
                if status == "ERROR":
                    raise ProviderError(f"Hyperstack VM {vm_id} entered ERROR", retryable=False, status_code=502)
            time.sleep(10)
        raise ProviderError(f"Hyperstack VM {vm_id} did not become ACTIVE", retryable=True, status_code=504)

    def _ensure_ssh_rule(self, session: Any, vm_id: str) -> str | None:
        response = session.post(
            f"{self.base_url}/core/virtual-machines/{vm_id}/sg-rules",
            headers=self._headers(),
            json={
                "direction": "ingress",
                "ethertype": "IPv4",
                "protocol": "tcp",
                "remote_ip_prefix": self.ssh_remote_cidr,
                "port_range_min": 22,
                "port_range_max": 22,
            },
            timeout=10,
        )
        if response.status_code in {200, 201, 202, 204, 409}:
            if response.status_code == 204:
                return None
            try:
                data = response.json()
            except Exception:
                return None
            return _extract_sg_rule_id(data)
        retryable = response.status_code in {429, 500, 502, 503, 504}
        raise ProviderError(
            f"Hyperstack SSH security rule failed: {response.status_code}",
            retryable=retryable,
            status_code=502 if response.status_code >= 500 else response.status_code,
        )

    def _connect_ssh(self, ip_address: str):
        try:
            import paramiko  # type: ignore
        except ImportError as exc:
            raise ProviderError("paramiko is required for Hyperstack", retryable=False, status_code=501) from exc
        deadline = time.monotonic() + 300
        ready = False
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((ip_address, 22), timeout=5):
                    ready = True
                    break
            except OSError:
                time.sleep(5)
        if not ready:
            raise ProviderError(f"SSH port not open on {ip_address}", retryable=True, status_code=504)
        ssh = paramiko.SSHClient()
        known_hosts = os.getenv("GPUCALL_HYPERSTACK_KNOWN_HOSTS")
        if known_hosts:
            ssh.load_host_keys(known_hosts)
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        elif os.getenv("GPUCALL_ENV", "").strip().lower() in {"prod", "production"} or os.getenv(
            "GPUCALL_PRODUCTION", ""
        ).strip().lower() in {"1", "true", "yes", "on"}:
            raise ProviderError("Hyperstack production SSH requires GPUCALL_HYPERSTACK_KNOWN_HOSTS", retryable=False, status_code=500)
        else:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip_address, username="ubuntu", key_filename=self.ssh_key_path, timeout=10)
        return ssh

    def _wait_sync(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        channel = handle.meta.get("ssh_channel")
        if channel is None:
            raise ProviderError("Hyperstack missing SSH channel", retryable=False, status_code=502)
        deadline = time.monotonic() + plan.timeout_seconds
        while time.monotonic() < deadline:
            if channel.exit_status_ready():
                status = channel.recv_exit_status()
                if status != 0:
                    raise ProviderError(f"Hyperstack script failed ({status})", retryable=True, status_code=502)
                ssh = handle.meta["ssh_client"]
                _, stdout, _ = ssh.exec_command("cat /tmp/gpucall/output.txt")
                value = stdout.read().decode().strip()
                if plan.artifact_export is not None:
                    return ProviderResult(kind="artifact_manifest", artifact_manifest=ArtifactManifest.model_validate_json(value))
                return ProviderResult(kind="inline", value=value)
            time.sleep(2)
        raise ProviderError("Hyperstack SSH execution timed out", retryable=True, status_code=504)

    def _destroy_sync(self, meta: dict[str, Any]) -> None:
        ssh = meta.get("ssh_client")
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass
        vm_id = meta.get("vm_id")
        if not vm_id:
            return
        session = self._session()
        sg_rule_id = meta.get("sg_rule_id")
        if sg_rule_id:
            self._delete_sg_rule_sync(session, str(vm_id), str(sg_rule_id))
        for attempt in range(5):
            try:
                response = session.delete(f"{self.base_url}/core/virtual-machines/{vm_id}", headers=self._headers(), timeout=10)
                if response.status_code in {200, 202, 204, 404}:
                    self._record_lease({"event": "destroyed", "vm_id": vm_id, "destroyed_at": datetime.now(timezone.utc).isoformat()})
                    return
            except Exception:
                pass
            time.sleep(2**attempt)
        raise ProviderError(f"CRITICAL LEAK: failed to destroy Hyperstack VM {vm_id}", retryable=True, status_code=500)

    def _delete_sg_rule_sync(self, session: Any, vm_id: str, sg_rule_id: str) -> None:
        try:
            response = session.delete(
                f"{self.base_url}/core/virtual-machines/{vm_id}/sg-rules/{sg_rule_id}",
                headers=self._headers(),
                timeout=10,
            )
            if response.status_code in {200, 202, 204, 404}:
                self._record_lease({"event": "sg_rule.destroyed", "vm_id": vm_id, "sg_rule_id": sg_rule_id})
        except Exception:
            return

    async def reconcile_orphans(self) -> None:
        if not self.api_key:
            return
        await asyncio.to_thread(self._reconcile_orphans_sync)

    def _reconcile_orphans_sync(self) -> None:
        session = self._session()
        active_leases = self._active_manifest_leases()
        now = datetime.now(timezone.utc)
        for lease in active_leases:
            vm_id = lease.get("vm_id")
            expires_at = _parse_dt(lease.get("expires_at"))
            if vm_id and expires_at and expires_at <= now:
                self._destroy_sync({"vm_id": vm_id})

        for vm in self._list_managed_vms(session):
            vm_id = vm.get("id")
            vm_name = str(vm.get("name") or "")
            if not vm_id or not vm_name.startswith("gpucall-managed-"):
                continue
            if any(lease.get("vm_id") == vm_id for lease in active_leases):
                continue
            created_at = _parse_dt(vm.get("created_at") or vm.get("created") or vm.get("createdAt"))
            if created_at is None or (now - created_at).total_seconds() >= _orphan_grace_seconds():
                self._destroy_sync({"vm_id": vm_id})

    def _list_managed_vms(self, session: Any) -> list[dict[str, Any]]:
        try:
            response = session.get(f"{self.base_url}/core/virtual-machines", headers=self._headers(), timeout=10)
        except Exception:
            return []
        if response.status_code != 200:
            return []
        data = response.json()
        rows = data.get("instances") or data.get("virtual_machines") or data.get("data") or []
        return [row for row in rows if isinstance(row, dict) and str(row.get("name") or "").startswith("gpucall-managed-")]

    def _record_lease(self, event: dict[str, Any]) -> None:
        self.lease_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        event = {"provider": self.name, "recorded_at": datetime.now(timezone.utc).isoformat(), **event}
        with self.lease_manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")

    def _active_manifest_leases(self) -> list[dict[str, Any]]:
        if not self.lease_manifest_path.exists():
            return []
        active: dict[str, dict[str, Any]] = {}
        with self.lease_manifest_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                vm_id = row.get("vm_id")
                if not vm_id:
                    continue
                if row.get("event") == "destroyed":
                    active.pop(str(vm_id), None)
                elif row.get("event") == "provision.created":
                    active[str(vm_id)] = row
        return list(active.values())


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def region_from_hyperstack_environment(environment_name: str) -> str:
    parts = str(environment_name or "").split("-")
    if len(parts) >= 2 and parts[0] == "default":
        return "-".join(parts[1:])
    return str(environment_name or "")


def hyperstack_config_findings(provider: Any) -> list[str]:
    if not provider.ssh_remote_cidr:
        return [f"provider {provider.name!r} must declare ssh_remote_cidr"]
    try:
        network = ipaddress.ip_network(provider.ssh_remote_cidr, strict=False)
    except ValueError:
        return [f"provider {provider.name!r} ssh_remote_cidr is invalid"]
    if network.prefixlen == 0:
        return [f"provider {provider.name!r} ssh_remote_cidr must not allow all addresses"]
    return []


def hyperstack_catalog_findings(providers: list[Any], credentials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    api_key = credentials.get("hyperstack", {}).get("api_key")
    if not api_key:
        return [
            {
                "provider": provider.name,
                "adapter": provider.adapter,
                "severity": "error",
                "reason": "missing Hyperstack API key; cannot verify official provider catalog",
            }
            for provider in providers
        ]
    return _hyperstack_catalog_findings(providers, api_key)


def _hyperstack_catalog_findings(providers: list[Any], api_key: str) -> list[dict[str, Any]]:
    try:
        import requests
    except ImportError as exc:
        return [
            {
                "provider": provider.name,
                "adapter": provider.adapter,
                "severity": "error",
                "reason": f"requests is unavailable; cannot verify official provider catalog: {exc}",
            }
            for provider in providers
        ]

    headers = {"api_key": api_key, "Accept": "application/json", "Content-Type": "application/json"}
    try:
        base_urls = {str(provider.endpoint or HYPERSTACK_API_BASE).rstrip("/") for provider in providers}
        if len(base_urls) != 1:
            raise ValueError("all Hyperstack providers must use the same official endpoint during catalog validation")
        base_url = next(iter(base_urls))
        images = _hyperstack_region_images(requests.get(f"{base_url}/core/images", headers=headers, timeout=20).json())
        flavors = _hyperstack_region_flavors(requests.get(f"{base_url}/core/flavors", headers=headers, timeout=20).json())
        environments = _hyperstack_environment_names(
            requests.get(f"{base_url}/core/environments", headers=headers, timeout=20).json()
        )
    except Exception as exc:
        return [
            {
                "provider": provider.name,
                "adapter": provider.adapter,
                "severity": "error",
                "reason": f"official Hyperstack catalog lookup failed: {exc}",
            }
            for provider in providers
        ]

    findings: list[dict[str, Any]] = []
    for provider in providers:
        region = region_from_hyperstack_environment(provider.target or "")
        if provider.target not in environments:
            findings.append(
                {
                    "provider": provider.name,
                    "adapter": provider.adapter,
                    "severity": "error",
                    "field": "target",
                    "configured": provider.target,
                    "reason": "environment_name is not present in official Hyperstack /core/environments catalog",
                }
            )
        if provider.image not in images.get(region, set()):
            findings.append(
                {
                    "provider": provider.name,
                    "adapter": provider.adapter,
                    "severity": "error",
                    "field": "image",
                    "configured": provider.image,
                    "region": region,
                    "reason": "image_name is not present in official Hyperstack /core/images catalog for provider region",
                }
            )
        if provider.instance not in flavors.get(region, set()):
            findings.append(
                {
                    "provider": provider.name,
                    "adapter": provider.adapter,
                    "severity": "error",
                    "field": "instance",
                    "configured": provider.instance,
                    "region": region,
                    "reason": "flavor_name is not present in official Hyperstack /core/flavors catalog for provider region",
                }
            )
    return findings


def _hyperstack_region_images(payload: dict[str, Any]) -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {}
    for group in payload.get("images") or []:
        if not isinstance(group, dict):
            continue
        region = str(group.get("region_name") or "")
        if not region:
            continue
        rows.setdefault(region, set())
        for image in group.get("images") or []:
            if isinstance(image, dict) and image.get("name"):
                rows[region].add(str(image["name"]))
    return rows


def _hyperstack_region_flavors(payload: dict[str, Any]) -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {}
    for group in payload.get("data") or []:
        if not isinstance(group, dict):
            continue
        region = str(group.get("region_name") or "")
        if not region:
            continue
        rows.setdefault(region, set())
        for flavor in group.get("flavors") or []:
            if isinstance(flavor, dict) and flavor.get("name"):
                rows[region].add(str(flavor["name"]))
    return rows


def _hyperstack_environment_names(payload: dict[str, Any]) -> set[str]:
    return {str(row["name"]) for row in payload.get("environments") or [] if isinstance(row, dict) and row.get("name")}


@register_adapter(
    "hyperstack",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="hyperstack-vm",
        output_contract="plain-text",
        config_validator=hyperstack_config_findings,
        catalog_validator=hyperstack_catalog_findings,
    ),
)
def build_hyperstack_adapter(spec, credentials):
    missing = [
        field
        for field, value in {
            "target": spec.target,
            "instance": spec.instance,
            "image": spec.image,
            "key_name": spec.key_name,
            "model": spec.model,
            "max_model_len": spec.max_model_len,
        }.items()
        if value in {None, ""}
    ]
    if missing:
        raise ValueError(f"hyperstack provider requires explicit fields: {', '.join(missing)}")
    hyperstack = credentials.get("hyperstack", {})
    return HyperstackAdapter(
        name=spec.name,
        api_key=hyperstack.get("api_key"),
        base_url=str(spec.endpoint) if spec.endpoint else None,
        ssh_key_path=hyperstack.get("ssh_key_path"),
        environment_name=str(spec.target),
        flavor_name=str(spec.instance),
        image_name=str(spec.image),
        key_name=str(spec.key_name),
        ssh_remote_cidr=spec.ssh_remote_cidr,
        lease_manifest_path=spec.lease_manifest_path,
        model=spec.model,
        max_model_len=spec.max_model_len,
    )


def _orphan_grace_seconds() -> float:
    try:
        return max(float(os.getenv("GPUCALL_HYPERSTACK_ORPHAN_GRACE_SECONDS", "600")), 0.0)
    except ValueError:
        return 600.0


def _extract_sg_rule_id(data: dict[str, Any]) -> str | None:
    candidates = [
        data.get("id"),
        data.get("sg_rule_id"),
        data.get("security_rule_id"),
    ]
    for key in ("security_rule", "firewall_rule", "sg_rule", "rule"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidates.extend([nested.get("id"), nested.get("sg_rule_id"), nested.get("security_rule_id")])
    for candidate in candidates:
        if candidate is not None and str(candidate):
            return str(candidate)
    return None


def _sftp_write_text(sftp: Any, path: str, value: str) -> None:
    with sftp.file(path, "w") as handle:
        handle.write(value)


def _hyperstack_worker_script() -> str:
    return r'''
import hashlib
import json
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def main():
    payload = json.load(open("/tmp/gpucall/input.json", "r", encoding="utf-8"))
    artifact_result = execute_artifact_workload(payload)
    if artifact_result is not None:
        open("/tmp/gpucall/output.txt", "w", encoding="utf-8").write(json.dumps(artifact_result.get("artifact_manifest") or artifact_result, sort_keys=True, separators=(",", ":")))
        return
    model = os.environ["GPUCALL_WORKER_MODEL"]
    max_model_len = int(os.environ["GPUCALL_WORKER_MAX_MODEL_LEN"])
    text = generate_text(payload, model=model, max_model_len=max_model_len)
    open("/tmp/gpucall/output.txt", "w", encoding="utf-8").write(text)


def generate_text(payload, *, model, max_model_len):
    from vllm import LLM, SamplingParams

    llm = LLM(
        model=model,
        max_model_len=bounded_model_len(model, max_model_len),
        gpu_memory_utilization=float(os.environ.get("GPUCALL_WORKER_GPU_MEMORY_UTILIZATION", "0.90")),
        trust_remote_code=True,
        tensor_parallel_size=int(os.environ.get("GPUCALL_WORKER_TENSOR_PARALLEL_SIZE", "1")),
        disable_log_stats=True,
    )
    prompt = format_prompt_for_model(llm, model, payload)
    response_format = payload.get("response_format") or {}
    sampling_kwargs = {
        "temperature": float(payload["temperature"]) if payload.get("temperature") is not None else 0.0,
        "max_tokens": int(payload.get("max_tokens") or os.environ.get("GPUCALL_WORKER_MAX_TOKENS", "256")),
    }
    if payload.get("repetition_penalty") is not None:
        sampling_kwargs["repetition_penalty"] = float(payload["repetition_penalty"])
    if payload.get("stop_tokens"):
        sampling_kwargs["stop"] = list(payload["stop_tokens"])
    guided = guided_decoding_params(response_format) if payload.get("guided_decoding") else None
    if guided is not None:
        sampling_kwargs["guided_decoding"] = guided
    params = SamplingParams(**sampling_kwargs)
    outputs = llm.generate([prompt], params, use_tqdm=False)
    return outputs[0].outputs[0].text.strip()


def execute_artifact_workload(payload):
    task = str(payload.get("task") or "")
    if task == "split-infer" and payload.get("split_learning") is not None:
        spec = payload["split_learning"]
        activation = fetch_ref_bytes(spec["activation_ref"])
        return {
            "kind": "inline",
            "value": json.dumps(
                {
                    "kind": "split_learning_activation_accepted",
                    "activation_sha256": hashlib.sha256(activation).hexdigest(),
                    "dp_epsilon": spec.get("dp_epsilon"),
                    "irreversibility_claim": spec.get("irreversibility_claim"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    if task not in {"train", "fine-tune"} or payload.get("artifact_export") is None:
        return None
    export = payload["artifact_export"]
    key = artifact_dek()
    plaintext = artifact_plaintext(payload, task=task)
    nonce = hashlib.sha256(plaintext + key).digest()[:12]
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise RuntimeError("cryptography is required for worker artifact encryption") from exc
    associated = json.dumps(
        {
            "plan_hash": (payload.get("attestations") or {}).get("governance_hash"),
            "artifact_chain_id": export.get("artifact_chain_id"),
            "version": export.get("version"),
            "key_id": export.get("key_id"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated)
    ciphertext_sha256 = hashlib.sha256(ciphertext).hexdigest()
    uri = write_artifact_ciphertext(export, ciphertext)
    manifest = {
        "artifact_id": hashlib.sha256(f"{export['artifact_chain_id']}:{export['version']}:{ciphertext_sha256}".encode("utf-8")).hexdigest(),
        "artifact_chain_id": export["artifact_chain_id"],
        "version": export["version"],
        "classification": str(payload.get("data_classification") or "restricted"),
        "ciphertext_uri": uri,
        "ciphertext_sha256": ciphertext_sha256,
        "key_id": export["key_id"],
        "producer_plan_hash": str((payload.get("attestations") or {}).get("governance_hash") or ""),
        "attestation_evidence_ref": None,
        "parent_artifact_ids": list(export.get("parent_artifact_ids") or []),
        "legal_hold": bool(export.get("legal_hold") or False),
        "retention_until": export.get("retention_until"),
    }
    return {"kind": "artifact_manifest", "artifact_manifest": manifest}


def artifact_plaintext(payload, *, task):
    inputs = []
    for ref in payload.get("input_refs") or []:
        body = fetch_ref_bytes(ref)
        inputs.append({"uri": str(ref.get("uri")), "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "content_type": ref.get("content_type")})
    return json.dumps({"kind": "gpucall-chained-artifact", "task": task, "recipe": payload.get("recipe"), "plan_id": payload.get("plan_id"), "inputs": inputs}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def artifact_dek():
    raw = os.environ.get("GPUCALL_WORKER_ARTIFACT_DEK_HEX", "")
    if not raw:
        raise RuntimeError("GPUCALL_WORKER_ARTIFACT_DEK_HEX is required for artifact export")
    key = bytes.fromhex(raw)
    if len(key) != 32:
        raise RuntimeError("GPUCALL_WORKER_ARTIFACT_DEK_HEX must encode a 32-byte AES-256 key")
    return key


def write_artifact_ciphertext(export, ciphertext):
    uri = os.environ.get("GPUCALL_WORKER_ARTIFACT_URI")
    if uri:
        if uri.startswith("file://"):
            path = uri.removeprefix("file://")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(ciphertext)
            return uri
        if not uri.startswith("s3://"):
            raise RuntimeError("GPUCALL_WORKER_ARTIFACT_URI must be s3:// or file://")
        put_s3(uri, ciphertext)
        return uri
    bucket = os.environ.get("GPUCALL_WORKER_ARTIFACT_BUCKET")
    prefix = os.environ.get("GPUCALL_WORKER_ARTIFACT_PREFIX", "gpucall/artifacts").strip("/")
    if not bucket:
        raise RuntimeError("GPUCALL_WORKER_ARTIFACT_BUCKET or GPUCALL_WORKER_ARTIFACT_URI is required for artifact export")
    uri = f"s3://{bucket}/{prefix}/{export['artifact_chain_id']}/{export['version']}/artifact.bin"
    put_s3(uri, ciphertext)
    return uri


def put_s3(uri, body):
    import boto3
    bucket_key = uri.removeprefix("s3://")
    bucket, _, key = bucket_key.partition("/")
    kwargs = {}
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("R2_ENDPOINT_URL")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if region:
        kwargs["region_name"] = region
    boto3.client("s3", **kwargs).put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/octet-stream")


def guided_decoding_params(response_format):
    if response_format.get("type") not in {"json_object", "json_schema"}:
        return None
    install_pyairports_stub()
    try:
        from vllm.sampling_params import GuidedDecodingParams
    except Exception:
        return None
    if response_format.get("type") == "json_object":
        try:
            import inspect
            params = inspect.signature(GuidedDecodingParams).parameters
            if "json_object" in params:
                return GuidedDecodingParams(json_object=True)
        except Exception:
            pass
        return GuidedDecodingParams(json={})
    return GuidedDecodingParams(json=response_format.get("json_schema") or {})


def install_pyairports_stub():
    import sys
    import types
    if "pyairports.airports" in sys.modules:
        return
    package = sys.modules.get("pyairports") or types.ModuleType("pyairports")
    airports = types.ModuleType("pyairports.airports")
    airports.AIRPORT_LIST = []
    package.airports = airports
    sys.modules["pyairports"] = package
    sys.modules["pyairports.airports"] = airports


def format_prompt_for_model(llm, model_id, payload):
    raw_prompt = prompt_from_payload(payload).strip()
    if "Instruct" not in model_id and not model_id.startswith("Qwen/"):
        return raw_prompt
    messages = messages_from_payload(payload, raw_prompt)
    tokenizer = llm.get_tokenizer()
    template = getattr(tokenizer, "apply_chat_template", None)
    if callable(template):
        return template(messages, tokenize=False, add_generation_prompt=True)
    if model_id.startswith("Qwen/"):
        rendered = []
        for message in messages:
            role = message.get("role", "user")
            if role not in {"system", "user", "assistant", "tool"}:
                raise ValueError("unsupported chat role for Qwen template: " + str(role))
            rendered.append("<|im_start|>" + role + "\n" + message.get("content", "") + "<|im_end|>")
        rendered.append("<|im_start|>assistant\n")
        return "\n".join(rendered)
    return raw_prompt


def system_prompt_for_payload(payload):
    return str(payload.get("system_prompt") or "")


def messages_from_payload(payload, raw_prompt):
    raw_messages = payload.get("messages") or []
    if raw_messages:
        return [
            {"role": str(message.get("role", "user")), "content": str(message.get("content", ""))}
            for message in raw_messages
            if str(message.get("content", ""))
        ]
    messages = []
    system_prompt = system_prompt_for_payload(payload)
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + [m for m in messages if m["role"] != "system"]
        if len(messages) == 1 and raw_prompt:
            messages.append({"role": "user", "content": raw_prompt})
    return messages or ([{"role": "user", "content": raw_prompt}] if raw_prompt else [])


def prompt_from_payload(payload):
    messages = payload.get("messages") or []
    if messages:
        return "\n".join(str(message.get("content", "")) for message in messages if str(message.get("content", "")))
    inline = payload.get("inline_inputs") or {}
    parts = []
    if "prompt" in inline:
        parts.append(str(inline["prompt"]["value"]))
    else:
        parts.extend(str(value.get("value", "")) for value in inline.values())
    for ref in payload.get("input_refs") or []:
        parts.append(fetch_data_ref_text(ref))
    return "\n".join(part for part in parts if part)


def fetch_data_ref_text(ref):
    uri = str(ref["uri"])
    max_bytes = min(int(os.environ.get("GPUCALL_WORKER_MAX_REF_BYTES", "16777216")), int(ref.get("bytes") or 16777216))
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        body = fetch_s3(parsed.netloc, parsed.path.lstrip("/"), max_bytes, ref)
    elif parsed.scheme in {"http", "https"}:
        if ref.get("gateway_presigned") is not True:
            raise ValueError("http(s) input_refs must be gateway-presigned")
        request = Request(uri, headers={"user-agent": "gpucall-worker/2.0"})
        with urlopen(request, timeout=float(os.environ.get("GPUCALL_WORKER_REF_TIMEOUT_SECONDS", "30"))) as response:
            body = response.read(max_bytes + 1)
    else:
        raise ValueError(f"unsupported data ref scheme: {parsed.scheme}")
    if len(body) > max_bytes:
        raise ValueError(f"data ref exceeds worker limit: {max_bytes} bytes")
    expected = ref.get("sha256")
    if expected and hashlib.sha256(body).hexdigest() != expected:
        raise ValueError("data ref sha256 mismatch")
    content_type = str(ref.get("content_type") or "").lower()
    if content_type and not (content_type.startswith("text/") or "json" in content_type):
        return body.hex()
    return body.decode("utf-8")


def fetch_s3(bucket, key, max_bytes, ref):
    import boto3

    kwargs = {}
    endpoint = ref.get("endpoint_url") or os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("R2_ENDPOINT_URL")
    region = ref.get("region") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if region:
        kwargs["region_name"] = region
    body = boto3.client("s3", **kwargs).get_object(Bucket=bucket, Key=key)["Body"]
    chunks = []
    total = 0
    while True:
        chunk = body.read(min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"data ref exceeds worker limit: {max_bytes} bytes")
    return b"".join(chunks)


def bounded_model_len(model_id, max_model_len):
    if model_id == "facebook/opt-125m":
        return min(max_model_len, 2048)
    if model_id == "Qwen/Qwen2.5-1.5B-Instruct":
        return min(max_model_len, 32768)
    if model_id in {"Qwen/Qwen2.5-7B-Instruct-1M", "Qwen/Qwen2.5-14B-Instruct-1M"}:
        return min(max_model_len, 1010000)
    return max_model_len


if __name__ == "__main__":
    main()
'''.strip()
