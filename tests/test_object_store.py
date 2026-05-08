from __future__ import annotations

import types

import pytest

from gpucall.domain import DataRef, ObjectStoreConfig, PresignGetRequest, PresignPutRequest
from gpucall.object_store import ObjectStore


def test_object_store_presign_put_builds_s3_data_ref(monkeypatch) -> None:
    class FakeS3:
        def generate_presigned_url(self, *args, **kwargs):
            assert args[0] == "put_object"
            assert kwargs["Params"]["Bucket"] == "bucket"
            return "https://example.com/upload"

    monkeypatch.setattr("gpucall.object_store.boto3", types.SimpleNamespace(client=lambda *args, **kwargs: FakeS3()))
    store = ObjectStore(ObjectStoreConfig(bucket="bucket", region="ap-northeast-1", prefix="prefix"))

    response = store.presign_put(
        PresignPutRequest(name="prompt.txt", bytes=4, sha256="a" * 64, content_type="text/plain")
    )

    assert str(response.upload_url) == "https://example.com/upload"
    assert str(response.data_ref.uri).startswith("s3://bucket/prefix/")
    assert response.data_ref.content_type == "text/plain"


def test_object_store_presign_get_restricts_bucket_and_prefix(monkeypatch) -> None:
    class FakeS3:
        def generate_presigned_url(self, *args, **kwargs):
            assert args[0] == "get_object"
            assert kwargs["Params"] == {"Bucket": "bucket", "Key": "prefix/object.txt"}
            return "https://example.com/download"

    monkeypatch.setattr("gpucall.object_store.boto3", types.SimpleNamespace(client=lambda *args, **kwargs: FakeS3()))
    store = ObjectStore(ObjectStoreConfig(bucket="bucket", region="ap-northeast-1", prefix="prefix"))

    response = store.presign_get(PresignGetRequest(data_ref=DataRef(uri="s3://bucket/prefix/object.txt", sha256="a" * 64)))

    assert str(response.download_url) == "https://example.com/download"
    assert str(response.data_ref.uri) == "https://example.com/download"
    assert response.data_ref.gateway_presigned is True
    assert response.data_ref.sha256 == "a" * 64


def test_object_store_presign_get_restricts_tenant_prefix(monkeypatch) -> None:
    class FakeS3:
        def generate_presigned_url(self, *args, **kwargs):
            return "https://example.com/download"

    monkeypatch.setattr("gpucall.object_store.boto3", types.SimpleNamespace(client=lambda *args, **kwargs: FakeS3()))
    store = ObjectStore(ObjectStoreConfig(bucket="bucket", region="ap-northeast-1", prefix="prefix"))

    response = store.presign_get(
        PresignGetRequest(data_ref=DataRef(uri="s3://bucket/prefix/tenants/tenant-a/object.txt", sha256="a" * 64)),
        tenant_prefix="tenant-a",
    )
    assert response.data_ref.gateway_presigned is True

    with pytest.raises(ValueError, match="tenant object prefix"):
        store.presign_get(
            PresignGetRequest(data_ref=DataRef(uri="s3://bucket/prefix/tenants/tenant-a/object.txt", sha256="a" * 64)),
            tenant_prefix="tenant-b",
        )
