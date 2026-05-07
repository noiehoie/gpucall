from __future__ import annotations

import hashlib
import sys
import types

import pytest

from gpucall.worker_contracts.io import prompt_from_payload
from gpucall.domain import DataRef


def test_worker_fetches_gateway_presigned_https_data_ref_text(monkeypatch) -> None:
    body = b"secret payload"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int) -> bytes:
            return body

    def fake_urlopen(request, timeout):
        assert request.full_url == "https://objects.example/prompt.txt"
        assert timeout >= 1
        return FakeResponse()

    monkeypatch.setattr("gpucall.worker_contracts.io.urlopen", fake_urlopen)
    payload = {
        "inline_inputs": {},
        "input_refs": [
            {
                "uri": "https://objects.example/prompt.txt",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "content_type": "text/plain",
                "gateway_presigned": True,
            }
        ],
    }

    assert prompt_from_payload(payload) == "secret payload"


def test_worker_rejects_untrusted_https_data_ref(monkeypatch) -> None:
    payload = {
        "inline_inputs": {},
        "input_refs": [
            {
                "uri": "https://169.254.169.254/latest/meta-data",
                "sha256": "a" * 64,
                "bytes": 10,
                "content_type": "text/plain",
            }
        ],
    }

    with pytest.raises(ValueError, match="gateway-presigned"):
        prompt_from_payload(payload)


def test_worker_rejects_raw_s3_data_ref_by_default() -> None:
    payload = {
        "inline_inputs": {},
        "input_refs": [
            {
                "uri": "s3://bucket/path/to/prompt.txt",
                "sha256": "a" * 64,
                "bytes": 10,
                "content_type": "text/plain",
            }
        ],
    }

    with pytest.raises(ValueError, match="gateway-presigned worker capability"):
        prompt_from_payload(payload)


def test_worker_fetches_s3_data_ref_when_ambient_credentials_are_explicitly_enabled(monkeypatch) -> None:
    body = b"s3 secret payload"

    class FakeBody:
        def __init__(self) -> None:
            self._body = body
            self._offset = 0

        def read(self, size: int) -> bytes:
            chunk = self._body[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    class FakeClient:
        def get_object(self, Bucket: str, Key: str):
            assert Bucket == "bucket"
            assert Key == "path/to/prompt.txt"
            return {"Body": FakeBody()}

    fake_boto3 = types.SimpleNamespace(client=lambda service, **kwargs: FakeClient())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("GPUCALL_WORKER_ALLOW_AMBIENT_S3", "1")
    payload = {
        "inline_inputs": {},
        "input_refs": [
            {
                "uri": "s3://bucket/path/to/prompt.txt",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "content_type": "text/plain",
            }
        ],
    }

    assert prompt_from_payload(payload) == "s3 secret payload"


def test_data_ref_accepts_s3_scheme() -> None:
    ref = DataRef(uri="s3://bucket/path/to/object.txt")

    assert str(ref.uri).startswith("s3://bucket/")
