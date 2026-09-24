"""S3-compatible immutable object storage adapter."""

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from urllib.parse import quote, urlsplit

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from invoice_intelligence.application.errors import (
    StorageChecksumMismatchError,
    StorageError,
    StorageInvalidReferenceError,
    StorageObjectNotFoundError,
    StoragePermissionDeniedError,
)
from invoice_intelligence.domain.storage import (
    ObjectKind,
    StorageObjectMetadata,
    StorageWriteRequest,
)


class S3FileStorage:
    """Store immutable objects in three fixed private buckets."""

    def __init__(
        self,
        client: BaseClient,
        buckets: Mapping[ObjectKind, str],
        tenant_key_secret: bytes,
    ) -> None:
        if set(buckets) != set(ObjectKind) or len(set(buckets.values())) != 3:
            raise ValueError("Exactly three distinct object-kind buckets are required")
        if not tenant_key_secret:
            raise ValueError("tenant_key_secret must not be empty")
        self._client = client
        self._buckets = dict(buckets)
        self._tenant_key_secret = tenant_key_secret

    def validate_startup(self) -> None:
        """Fail closed when a configured bucket is absent or publicly readable."""

        for bucket in self._buckets.values():
            try:
                self._client.head_bucket(Bucket=bucket)
                acl = self._client.get_bucket_acl(Bucket=bucket)
                for grant in acl.get("Grants", []):
                    grantee = grant.get("Grantee", {})
                    if grantee.get("URI") or grantee.get("Type") == "Group":
                        raise StoragePermissionDeniedError(
                            "Object storage bucket must not grant public or group access"
                        )
                try:
                    policy = self._client.get_bucket_policy(Bucket=bucket).get("Policy")
                except ClientError as exc:
                    code = str(exc.response.get("Error", {}).get("Code", ""))
                    if code not in {"NoSuchBucketPolicy", "NoSuchPolicy", "404"}:
                        raise
                else:
                    if policy and self._policy_is_public(policy):
                        raise StoragePermissionDeniedError(
                            "Object storage bucket policy permits a public principal"
                        )
            except StoragePermissionDeniedError:
                raise
            except ClientError as exc:
                raise self._map_error(exc) from exc

    @staticmethod
    def _policy_is_public(raw_policy: str) -> bool:
        try:
            policy = json.loads(raw_policy)
        except (TypeError, ValueError) as exc:
            raise StoragePermissionDeniedError("Object storage bucket policy is invalid") from exc
        statements = policy.get("Statement", []) if isinstance(policy, dict) else []
        if isinstance(statements, dict):
            statements = [statements]
        for statement in statements:
            if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
                continue
            principal = statement.get("Principal")
            if principal == "*" or (
                isinstance(principal, dict) and principal.get("AWS") == "*"
            ):
                return True
        return False

    async def save(self, document_id: str, content: bytes, mime_type: str) -> str:
        checksum = hashlib.sha256(content).hexdigest()
        metadata = await self.save_object(
            StorageWriteRequest(
                object_id=document_id,
                tenant_id="legacy-unscoped",
                kind=ObjectKind.ORIGINAL,
                content=content,
                media_type=mime_type,
                checksum=checksum,
            )
        )
        return metadata.storage_uri

    async def read(self, storage_uri: str) -> bytes:
        return await asyncio.to_thread(self._read_sync, storage_uri)

    async def save_object(self, request: StorageWriteRequest) -> StorageObjectMetadata:
        return await asyncio.to_thread(self._save_sync, request)

    async def head(self, storage_uri: str) -> StorageObjectMetadata:
        return await asyncio.to_thread(self._head_sync, storage_uri)

    async def delete(self, storage_uri: str) -> None:
        await asyncio.to_thread(self._delete_sync, storage_uri)

    def _save_sync(self, request: StorageWriteRequest) -> StorageObjectMetadata:
        actual = hashlib.sha256(request.content).hexdigest()
        if not hmac.compare_digest(actual, request.checksum):
            raise StorageChecksumMismatchError("Content checksum does not match request")
        bucket = self._buckets[request.kind]
        key = self._key(request)
        checksum_b64 = base64.b64encode(bytes.fromhex(request.checksum)).decode("ascii")
        try:
            self._client.put_object(
                Bucket=bucket,
                Key=key,
                Body=request.content,
                ContentType=request.media_type,
                ChecksumSHA256=checksum_b64,
                Metadata={"sha256": request.checksum, "object-kind": request.kind.value},
                IfNoneMatch="*",
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"PreconditionFailed", "412"}:
                existing = self._head_sync(f"s3://{bucket}/{quote(key, safe='/')}")
                if hmac.compare_digest(existing.checksum, request.checksum):
                    return existing
                raise StorageChecksumMismatchError(
                    "Immutable object key already contains different content"
                ) from exc
            raise self._map_error(exc) from exc
        except BotoCoreError as exc:
            raise StorageError("Object storage is temporarily unavailable") from exc
        return self._head_sync(f"s3://{bucket}/{quote(key, safe='/')}")

    def _read_sync(self, storage_uri: str) -> bytes:
        bucket, key, kind = self._parse_uri(storage_uri)
        try:
            response = self._client.get_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
            content = response["Body"].read()
        except ClientError as exc:
            raise self._map_error(exc) from exc
        except BotoCoreError as exc:
            raise StorageError("Object storage is temporarily unavailable") from exc
        checksum = hashlib.sha256(content).hexdigest()
        native = response.get("ChecksumSHA256")
        if native and native != base64.b64encode(bytes.fromhex(checksum)).decode("ascii"):
            raise StorageChecksumMismatchError("Stored object checksum is inconsistent")
        return content

    def _head_sync(self, storage_uri: str) -> StorageObjectMetadata:
        bucket, key, kind = self._parse_uri(storage_uri)
        try:
            response = self._client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            raise self._map_error(exc) from exc
        except BotoCoreError as exc:
            raise StorageError("Object storage is temporarily unavailable") from exc
        native = response.get("ChecksumSHA256")
        metadata_checksum = str(response.get("Metadata", {}).get("sha256", ""))
        checksum = metadata_checksum
        native_verified = False
        if native:
            try:
                checksum = base64.b64decode(native, validate=True).hex()
                native_verified = True
            except ValueError as exc:
                raise StorageChecksumMismatchError("Invalid native object checksum") from exc
            if metadata_checksum and not hmac.compare_digest(checksum, metadata_checksum):
                raise StorageChecksumMismatchError("Native and metadata checksums differ")
        if len(checksum) != 64:
            content = self._read_sync(storage_uri)
            checksum = hashlib.sha256(content).hexdigest()
        return StorageObjectMetadata(
            storage_uri=storage_uri,
            kind=kind,
            media_type=str(response.get("ContentType") or "application/octet-stream"),
            size_bytes=int(response["ContentLength"]),
            checksum=checksum,
            native_checksum_verified=native_verified,
        )

    def _delete_sync(self, storage_uri: str) -> None:
        bucket, key, _ = self._parse_uri(storage_uri)
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            raise self._map_error(exc) from exc
        except BotoCoreError as exc:
            raise StorageError("Object storage is temporarily unavailable") from exc

    def _key(self, request: StorageWriteRequest) -> str:
        tenant = hmac.new(
            self._tenant_key_secret,
            request.tenant_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        suffix = {
            "application/pdf": "pdf",
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "application/json": "json",
            "text/plain": "txt",
        }.get(request.media_type, "bin")
        parts = [tenant, request.object_id]
        if request.page_number is not None:
            parts.append(f"page-{request.page_number}")
        if request.producer_version:
            version = hashlib.sha256(request.producer_version.encode("utf-8")).hexdigest()[:16]
            parts.append(version)
        return "/".join(parts) + f".{suffix}"

    def _parse_uri(self, storage_uri: str) -> tuple[str, str, ObjectKind]:
        parsed = urlsplit(storage_uri)
        key = parsed.path.removeprefix("/")
        if parsed.scheme != "s3" or not parsed.netloc or not key or parsed.query or parsed.fragment:
            raise StorageInvalidReferenceError("Invalid S3 object reference")
        matches = [kind for kind, bucket in self._buckets.items() if bucket == parsed.netloc]
        if len(matches) != 1:
            raise StorageInvalidReferenceError("Object bucket is outside the configured boundary")
        return parsed.netloc, key, matches[0]

    @staticmethod
    def _map_error(exc: ClientError) -> StorageError:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        if code in {"NoSuchKey", "NotFound", "404"} or status == 404:
            return StorageObjectNotFoundError("Stored object does not exist")
        if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"} or status in {
            401,
            403,
        }:
            return StoragePermissionDeniedError("Object storage permission was denied")
        return StorageError("Object storage is temporarily unavailable")


class S3DownloadAccessIssuer:
    def __init__(self, client: BaseClient, allowed_buckets: frozenset[str]) -> None:
        self._client = client
        self._allowed_buckets = allowed_buckets

    async def issue(self, storage_uri: str, expires_seconds: int) -> str:
        parsed = urlsplit(storage_uri)
        key = parsed.path.removeprefix("/")
        if parsed.scheme != "s3" or parsed.netloc not in self._allowed_buckets or not key:
            raise StorageInvalidReferenceError("Object reference is outside configured buckets")
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": parsed.netloc, "Key": key},
            ExpiresIn=expires_seconds,
        )
