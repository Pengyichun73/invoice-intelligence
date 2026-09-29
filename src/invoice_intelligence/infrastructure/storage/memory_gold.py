"""Dedicated private MinIO/S3 bucket for immutable annotation payloads."""

import asyncio
import base64
import hashlib
import hmac

from botocore.client import BaseClient  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from invoice_intelligence.application.errors import StorageError
from invoice_intelligence.infrastructure.storage.s3 import S3FileStorage

_MAX_GOLD_BYTES = 262_144


class S3GoldObjectStore:
    def __init__(self, client: BaseClient, bucket: str, key_secret: bytes) -> None:
        if not bucket or "/" in bucket or ".." in bucket:
            raise ValueError("A dedicated gold bucket is required")
        if not key_secret:
            raise ValueError("Gold object key secret is required")
        self._client = client
        self._bucket = bucket
        self._key_secret = key_secret

    def validate_startup(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            acl = self._client.get_bucket_acl(Bucket=self._bucket)
            for grant in acl.get("Grants", []):
                grantee = grant.get("Grantee", {})
                if grantee.get("URI") or grantee.get("Type") == "Group":
                    raise StorageError("Gold bucket grants group access")
            try:
                policy = self._client.get_bucket_policy(Bucket=self._bucket).get("Policy")
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code not in {"NoSuchBucketPolicy", "NoSuchPolicy", "404"}:
                    raise
            else:
                if policy and S3FileStorage._policy_is_public(policy):
                    raise StorageError("Gold bucket policy is public")
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("Gold bucket is unavailable") from exc

    async def put(self, tenant_id: str, content: bytes, checksum: str) -> str:
        return await asyncio.to_thread(self._put_sync, tenant_id, content, checksum)

    async def get(self, tenant_id: str, object_ref: str, checksum: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, tenant_id, object_ref, checksum)

    def _put_sync(self, tenant_id: str, content: bytes, checksum: str) -> str:
        if len(content) > _MAX_GOLD_BYTES or not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(), checksum
        ):
            raise StorageError("Gold payload failed size or checksum validation")
        key = self._key(tenant_id, checksum)
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=content,
                ContentType="application/json", IfNoneMatch="*",
                ChecksumSHA256=base64.b64encode(bytes.fromhex(checksum)).decode("ascii"),
                Metadata={"sha256": checksum},
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"PreconditionFailed", "412"}:
                self._get_sync(tenant_id, f"s3://{self._bucket}/{key}", checksum)
                return f"s3://{self._bucket}/{key}"
            raise StorageError("Gold object write failed") from exc
        except BotoCoreError as exc:
            raise StorageError("Gold object write failed") from exc
        return f"s3://{self._bucket}/{key}"

    def _get_sync(self, tenant_id: str, object_ref: str, checksum: str) -> bytes:
        expected_ref = f"s3://{self._bucket}/{self._key(tenant_id, checksum)}"
        if not hmac.compare_digest(object_ref, expected_ref):
            raise StorageError("Gold object tenant scope is invalid")
        key = object_ref.removeprefix(f"s3://{self._bucket}/")
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            if response.get("ContentLength", _MAX_GOLD_BYTES + 1) > _MAX_GOLD_BYTES:
                raise StorageError("Gold object exceeds the size limit")
            content = response["Body"].read(_MAX_GOLD_BYTES + 1)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("Gold object read failed") from exc
        if len(content) > _MAX_GOLD_BYTES or not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(), checksum
        ):
            raise StorageError("Gold object checksum is invalid")
        return bytes(content)

    def _key(self, tenant_id: str, checksum: str) -> str:
        tenant_hash = hmac.new(
            self._key_secret, tenant_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        object_hash = hmac.new(
            self._key_secret, f"{tenant_id}:{checksum}".encode(), hashlib.sha256
        ).hexdigest()
        return f"gold/{tenant_hash}/{object_hash}"
