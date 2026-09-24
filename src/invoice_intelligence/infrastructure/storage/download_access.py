"""Local development download authorization."""

import asyncio
import base64
import hashlib
import hmac
import time
from urllib.parse import quote, urlsplit

from invoice_intelligence.application.errors import StoragePermissionDeniedError


class LocalDownloadAccessIssuer:
    def __init__(self, secret: bytes, api_prefix: str) -> None:
        if not secret:
            raise ValueError("Local download signing secret must not be empty")
        self._secret = secret
        self._api_prefix = api_prefix.rstrip("/")

    async def issue(self, storage_uri: str, expires_seconds: int) -> str:
        expires = int(time.time()) + expires_seconds
        payload = f"{expires}\0{storage_uri}".encode()
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        parsed = urlsplit(storage_uri)
        document_id = parsed.path.removeprefix("/").rsplit("/", 1)[-1].split(".", 1)[0]
        if parsed.path in ("", "/"):
            document_id = parsed.netloc.split(".", 1)[0]
        return (
            f"{self._api_prefix}/documents/{document_id}/content"
            f"?token={quote(encoded + '.' + signature)}"
        )

    async def verify(self, token: str, expected_storage_uri: str) -> None:
        await asyncio.to_thread(self._verify_sync, token, expected_storage_uri)

    def _verify_sync(self, token: str, expected_storage_uri: str) -> None:
        try:
            encoded, supplied = token.split(".", 1)
            expected = hmac.new(
                self._secret, encoded.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, supplied):
                raise ValueError
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            expires_raw, storage_uri = payload.split("\0", 1)
            if int(expires_raw) < int(time.time()) or not hmac.compare_digest(
                storage_uri, expected_storage_uri
            ):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise StoragePermissionDeniedError("Download token is invalid or expired") from exc
