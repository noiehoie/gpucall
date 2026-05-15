from __future__ import annotations

import hashlib
import http.client
import importlib
import ipaddress
import os
import socket
import ssl
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, Request, build_opener


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, pinned_ip=None, **kwargs):
        self.pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout, self.source_address)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, pinned_ip=None, **kwargs):
        self.pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host)
        else:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class PinnedHTTPHandler(HTTPHandler):
    def __init__(self, pinned_ip):
        self.pinned_ip = pinned_ip
        super().__init__()

    def http_open(self, req):
        return self.do_open(lambda host, **kwargs: PinnedHTTPConnection(host, pinned_ip=self.pinned_ip, **kwargs), req)


class PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, pinned_ip, context=None):
        self.pinned_ip = pinned_ip
        super().__init__(context=context)

    def https_open(self, req):
        return self.do_open(
            lambda host, **kwargs: PinnedHTTPSConnection(host, pinned_ip=self.pinned_ip, **kwargs),
            req,
            context=self._context,
        )


def _get_pinned_opener(pinned_ip, scheme):
    handlers = [_NoRedirectHandler()]
    if scheme == "http":
        handlers.append(PinnedHTTPHandler(pinned_ip))
    elif scheme == "https":
        handlers.append(PinnedHTTPSHandler(pinned_ip))
    return build_opener(*handlers)


def prompt_from_payload(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    if messages:
        return "\n".join(str(message.get("content", "")) for message in messages if str(message.get("content", "")))
    inline = payload.get("inline_inputs") or {}
    parts: list[str] = []
    if "prompt" in inline:
        parts.append(str(inline["prompt"]["value"]))
    else:
        parts.extend(str(value.get("value", "")) for value in inline.values())

    for ref in payload.get("input_refs") or []:
        parts.append(fetch_data_ref_text(ref))
    return "\n".join(part for part in parts if part)


def fetch_data_ref_text(ref: dict[str, Any]) -> str:
    return _decode_ref_body(fetch_data_ref_bytes(ref), ref)


def fetch_data_ref_bytes(ref: dict[str, Any]) -> bytes:
    uri = str(ref["uri"])
    max_bytes = _max_ref_bytes(ref)
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        if not _ambient_s3_allowed(ref):
            raise ValueError("s3 data refs require gateway-presigned worker capability")
        body = _fetch_s3_ref_bytes(parsed.netloc, parsed.path.lstrip("/"), max_bytes, ref)
    else:
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"unsupported data ref scheme: {parsed.scheme}")
        if ref.get("gateway_presigned") is not True:
            raise ValueError("http(s) data refs must be gateway-presigned")
        pinned_ip = _validate_http_ref_uri(parsed)
        request = Request(uri, headers={"user-agent": "gpucall-worker/2.0"})
        opener = _get_pinned_opener(pinned_ip, parsed.scheme)
        with opener.open(request, timeout=_ref_timeout_seconds()) as response:
            if int(getattr(response, "status", 200)) != 200:
                raise ValueError(f"data ref fetch failed with HTTP {response.status}")
            body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"data ref exceeds worker limit: {max_bytes} bytes")
    _verify_ref_sha256(body, ref)
    return body


def _fetch_s3_ref_bytes(bucket: str, key: str, max_bytes: int, ref: dict[str, Any]) -> bytes:
    if not bucket or not key:
        raise ValueError("s3 data ref must include bucket and key")
    try:
        boto3 = importlib.import_module("boto3")
    except ImportError as exc:
        raise RuntimeError("boto3 is required for s3:// data refs") from exc

    client_kwargs: dict[str, Any] = {}
    endpoint_url = ref.get("endpoint_url") or os.getenv("AWS_ENDPOINT_URL_S3") or os.getenv("R2_ENDPOINT_URL")
    region_name = ref.get("region") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if endpoint_url:
        client_kwargs["endpoint_url"] = str(endpoint_url)
    if region_name:
        client_kwargs["region_name"] = str(region_name)

    client = boto3.client("s3", **client_kwargs)
    response = client.get_object(Bucket=bucket, Key=key)
    stream = response["Body"]
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"data ref exceeds worker limit: {max_bytes} bytes")
    return b"".join(chunks)


def _decode_ref_body(body: bytes, ref: dict[str, Any]) -> str:
    _verify_ref_sha256(body, ref)
    content_type = str(ref.get("content_type") or "").lower()
    if content_type and not (content_type.startswith("text/") or "json" in content_type):
        return body.hex()
    return body.decode("utf-8")


def _verify_ref_sha256(body: bytes, ref: dict[str, Any]) -> None:
    expected = ref.get("sha256")
    if expected:
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise ValueError("data ref sha256 mismatch")


def _max_ref_bytes(ref: dict[str, Any]) -> int:
    configured = int(os.getenv("GPUCALL_WORKER_MAX_REF_BYTES", "16777216"))
    declared = ref.get("bytes")
    if declared is None:
        return configured
    declared_int = int(declared)
    if declared_int < 0:
        raise ValueError("data ref bytes must be non-negative")
    return min(configured, declared_int)


def _validate_http_ref_uri(parsed) -> str:
    if parsed.username or parsed.password:
        raise ValueError("http(s) data refs must not include URI userinfo")
    host = parsed.hostname
    if not host:
        raise ValueError("http(s) data refs must include a host")
    allowed_hosts = {
        item.strip().lower()
        for item in os.getenv("GPUCALL_WORKER_DATAREF_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    if not allowed_hosts:
        raise ValueError("http(s) data refs require GPUCALL_WORKER_DATAREF_ALLOWED_HOSTS")
    if host.lower() not in allowed_hosts:
        raise ValueError("http(s) data ref host is not in GPUCALL_WORKER_DATAREF_ALLOWED_HOSTS")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [item[4][0] for item in addr_info]
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve data ref host: {host}") from exc
    if not addresses:
        raise ValueError(f"could not resolve data ref host: {host}")

    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("http(s) data ref host resolves to non-public or disallowed addresses only")
    return addresses[0]


def _ref_timeout_seconds() -> float:
    try:
        return max(float(os.getenv("GPUCALL_WORKER_REF_TIMEOUT_SECONDS", "30")), 1.0)
    except ValueError:
        return 30.0


def _ambient_s3_allowed(ref: dict[str, Any]) -> bool:
    if ref.get("allow_worker_s3_credentials") is True:
        return True
    return os.getenv("GPUCALL_WORKER_ALLOW_AMBIENT_S3", "").strip().lower() in {"1", "true", "yes", "on"}
