from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlparse
from uuid import uuid4

import boto3

from gpucall.credentials import load_credentials
from gpucall.domain import DataRef, ObjectStoreConfig, PresignGetRequest, PresignGetResponse, PresignPutRequest, PresignPutResponse


class ObjectStore:
    def __init__(self, config: ObjectStoreConfig) -> None:
        self.config = config
        kwargs = {}
        if config.endpoint is not None:
            kwargs["endpoint_url"] = str(config.endpoint)
        if config.region:
            kwargs["region_name"] = config.region
        aws = load_credentials().get("aws", {})
        if aws.get("access_key_id") and aws.get("secret_access_key"):
            kwargs["aws_access_key_id"] = aws["access_key_id"]
            kwargs["aws_secret_access_key"] = aws["secret_access_key"]
        if aws.get("endpoint_url") and "endpoint_url" not in kwargs:
            kwargs["endpoint_url"] = aws["endpoint_url"]
        if aws.get("region") and "region_name" not in kwargs:
            kwargs["region_name"] = aws["region"]
        self.client = boto3.client("s3", **kwargs)

    def presign_put(self, request: PresignPutRequest, *, tenant_prefix: str | None = None) -> PresignPutResponse:
        key = self._key_for(request.name, tenant_prefix=tenant_prefix)
        upload_url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.config.bucket,
                "Key": key,
                "ContentType": request.content_type,
            },
            ExpiresIn=self.config.presign_ttl_seconds,
            HttpMethod="PUT",
        )
        ref = DataRef(
            uri=f"s3://{self.config.bucket}/{key}",
            sha256=request.sha256,
            bytes=request.bytes,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.config.presign_ttl_seconds),
            content_type=request.content_type,
            endpoint_url=str(self.config.endpoint) if self.config.endpoint is not None else None,
            region=self.config.region,
        )
        return PresignPutResponse(upload_url=upload_url, data_ref=ref)

    def presign_get(self, request: PresignGetRequest, *, tenant_prefix: str | None = None) -> PresignGetResponse:
        self._validate_ref(request.data_ref, tenant_prefix=tenant_prefix)
        bucket, key = self._bucket_key(request.data_ref)
        download_url = self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=self.config.presign_ttl_seconds,
            HttpMethod="GET",
        )
        worker_ref_data = request.data_ref.model_dump()
        worker_ref_data.update(
            {
                "uri": download_url,
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=self.config.presign_ttl_seconds),
                "gateway_presigned": True,
            }
        )
        worker_ref = DataRef.model_validate(worker_ref_data)
        return PresignGetResponse(download_url=download_url, data_ref=worker_ref)

    def _key_for(self, name: str, *, tenant_prefix: str | None = None) -> str:
        clean = PurePosixPath(name).name or "object"
        prefix = self.config.prefix.rstrip("/")
        if tenant_prefix:
            safe_tenant = PurePosixPath(tenant_prefix).name
            if safe_tenant:
                prefix = f"{prefix}/tenants/{safe_tenant}"
        return f"{prefix}/{uuid4().hex}/{clean}"

    def _validate_ref(self, ref: DataRef, *, tenant_prefix: str | None = None) -> None:
        if ref.expires_at is not None and ref.expires_at <= datetime.now(timezone.utc):
            raise ValueError("data_ref is expired")
        bucket, key = self._bucket_key(ref)
        if bucket != self.config.bucket:
            raise ValueError("data_ref bucket is not allowed")
        prefix = self.config.prefix.rstrip("/") + "/"
        if not key.startswith(prefix):
            raise ValueError("data_ref key is outside configured prefix")
        if tenant_prefix:
            safe_tenant = PurePosixPath(tenant_prefix).name
            if safe_tenant:
                tenant_prefix_key = f"{self.config.prefix.rstrip('/')}/tenants/{safe_tenant}/"
                if not key.startswith(tenant_prefix_key):
                    raise ValueError("data_ref key is outside tenant object prefix")

    @staticmethod
    def _bucket_key(ref: DataRef) -> tuple[str, str]:
        parsed = urlparse(str(ref.uri))
        if parsed.scheme != "s3":
            raise ValueError("data_ref must use s3://")
        return parsed.netloc, parsed.path.lstrip("/")
