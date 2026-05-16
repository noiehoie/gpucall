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

    def fake_client(*args, **kwargs):
        config = kwargs.get("config")
        assert config is not None
        assert config.signature_version == "s3v4"
        assert config.region_name == "ap-northeast-1"
        assert config.s3 == {"addressing_style": "virtual"}
        return FakeS3()

    monkeypatch.setattr("gpucall.object_store.boto3", types.SimpleNamespace(client=fake_client))
    store = ObjectStore(ObjectStoreConfig(bucket="bucket", region="ap-northeast-1", prefix="prefix"))

    response = store.presign_put(
        PresignPutRequest(name="prompt.txt", bytes=4, sha256="a" * 64, content_type="text/plain")
    )

    assert str(response.upload_url) == "https://example.com/upload"
    assert str(response.data_ref.uri).startswith("s3://bucket/prefix/")
    assert response.data_ref.content_type == "text/plain"


def test_object_store_allows_empty_prefix_for_own_data_refs(monkeypatch) -> None:
    generated_keys: list[str] = []

    class FakeS3:
        def generate_presigned_url(self, *args, **kwargs):
            generated_keys.append(kwargs["Params"]["Key"])
            return "https://example.com/presigned"

    monkeypatch.setattr("gpucall.object_store.boto3", types.SimpleNamespace(client=lambda *args, **kwargs: FakeS3()))
    store = ObjectStore(ObjectStoreConfig(bucket="bucket", region="ap-northeast-1", prefix=""))

    put = store.presign_put(
        PresignPutRequest(name="prompt.txt", bytes=4, sha256="a" * 64, content_type="text/plain")
    )
    get = store.presign_get(PresignGetRequest(data_ref=put.data_ref))

    assert generated_keys[0].endswith("/prompt.txt")
    assert not generated_keys[0].startswith("/")
    assert str(put.data_ref.uri).startswith("s3://bucket/")
    assert get.data_ref.gateway_presigned is True


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


def test_object_store_presign_get_decodes_s3_key_percent_encoding(monkeypatch) -> None:
    class FakeS3:
        def generate_presigned_url(self, *args, **kwargs):
            assert kwargs["Params"] == {"Bucket": "bucket", "Key": "prefix/file name.txt"}
            return "https://example.com/download"

    monkeypatch.setattr("gpucall.object_store.boto3", types.SimpleNamespace(client=lambda *args, **kwargs: FakeS3()))
    store = ObjectStore(ObjectStoreConfig(bucket="bucket", region="ap-northeast-1", prefix="prefix"))

    response = store.presign_get(PresignGetRequest(data_ref=DataRef(uri="s3://bucket/prefix/file%20name.txt", sha256="a" * 64)))

    assert response.data_ref.gateway_presigned is True


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
