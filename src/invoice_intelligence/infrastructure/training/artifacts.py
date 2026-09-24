"""Bounded training artifact checksum verification."""

from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import httpx

from invoice_intelligence.application.errors import (
    TrainingProviderPermanentError,
    TrainingProviderUnavailableError,
)


class SafeTrainingArtifactReader:
    def __init__(
        self,
        *,
        allowed_https_hosts: tuple[str, ...],
        max_bytes: int,
        timeout_seconds: float,
        allowed_file_root: Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._hosts = frozenset(host.lower() for host in allowed_https_hosts)
        self._max_bytes = max_bytes
        self._allowed_file_root = (
            allowed_file_root.expanduser().resolve() if allowed_file_root else None
        )
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
        )
        self._owns_client = client is None

    async def read_and_verify(
        self, uri: str, *, expected_sha256: str
    ) -> tuple[int, str]:
        parsed = urlparse(uri)
        if parsed.scheme == "https":
            if parsed.hostname is None or parsed.hostname.lower() not in self._hosts:
                raise TrainingProviderPermanentError("artifact_host_not_allowed")
            size, digest = await self._read_https(uri)
        elif parsed.scheme == "file" and self._allowed_file_root is not None:
            path = Path(parsed.path).resolve()
            if self._allowed_file_root not in path.parents:
                raise TrainingProviderPermanentError("artifact_path_not_allowed")
            size, digest = await __import__("asyncio").to_thread(self._read_file, path)
        else:
            raise TrainingProviderPermanentError("artifact_scheme_not_allowed")
        if digest != expected_sha256:
            raise TrainingProviderPermanentError("artifact_checksum_mismatch")
        return size, digest

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _read_https(self, uri: str) -> tuple[int, str]:
        try:
            async with self._client.stream("GET", uri) as response:
                if response.status_code in {408, 429} or response.status_code >= 500:
                    raise TrainingProviderUnavailableError("artifact_temporarily_unavailable")
                if response.status_code >= 300:
                    raise TrainingProviderPermanentError("artifact_download_rejected")
                digest = sha256()
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise TrainingProviderPermanentError("artifact_too_large")
                    digest.update(chunk)
                if size == 0:
                    raise TrainingProviderPermanentError("artifact_empty")
                return size, digest.hexdigest()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TrainingProviderUnavailableError("artifact_temporarily_unavailable") from exc

    def _read_file(self, path: Path) -> tuple[int, str]:
        if not path.is_file():
            raise TrainingProviderPermanentError("artifact_file_missing")
        digest = sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > self._max_bytes:
                    raise TrainingProviderPermanentError("artifact_too_large")
                digest.update(chunk)
        if size == 0:
            raise TrainingProviderPermanentError("artifact_empty")
        return size, digest.hexdigest()
