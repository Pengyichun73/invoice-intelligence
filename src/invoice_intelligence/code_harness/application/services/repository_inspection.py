"""Safe repository manifest capture without following symlinks."""

from hashlib import sha256
from pathlib import Path
from time import sleep
from collections.abc import Mapping

from ...domain.errors import HarnessError, HarnessErrorCode
from ...domain.repository import FileKind, RepositorySnapshot, RepositorySource, SnapshotFile
from ...domain.versions import ExecutionVersionBinding


class RepositoryInspectionService:
    def __init__(
        self,
        *,
        max_file_bytes: int = 2_000_000,
        max_capture_retries: int = 2,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        if max_capture_retries < 0:
            raise ValueError("max_capture_retries must not be negative")
        self._max_file_bytes = max_file_bytes
        self._max_capture_retries = max_capture_retries

    def capture_snapshot(
        self,
        *,
        source: RepositorySource,
        snapshot_id: str,
        revision: int,
        versions: ExecutionVersionBinding,
    ) -> RepositorySnapshot:
        root = Path(source.root_path).resolve()
        if not root.is_dir():
            raise HarnessError(HarnessErrorCode.SNAPSHOT_NOT_FOUND)
        files, _ = self._capture_files(root)
        manifest = "\n".join(
            f"{item.path}\0{item.checksum_sha256}\0{item.size_bytes}"
            for item in files
        ).encode("utf-8")
        return RepositorySnapshot(
            snapshot_id=snapshot_id,
            tenant_id=source.tenant_id,
            repository_id=source.repository_id,
            revision=revision,
            source_revision=source.source_revision,
            manifest_checksum_sha256=sha256(manifest).hexdigest(),
            files=tuple(files),
            versions=versions,
        )

    def capture_snapshot_with_contents(
        self,
        *,
        source: RepositorySource,
        snapshot_id: str,
        revision: int,
        versions: ExecutionVersionBinding,
    ) -> tuple[RepositorySnapshot, Mapping[str, bytes]]:
        """Capture a manifest and the exact bytes used to calculate it."""
        root = Path(source.root_path).resolve()
        if not root.is_dir():
            raise HarnessError(HarnessErrorCode.SNAPSHOT_NOT_FOUND)
        files, contents = self._capture_files(root)
        manifest = "\n".join(
            f"{item.path}\0{item.checksum_sha256}\0{item.size_bytes}" for item in files
        ).encode("utf-8")
        return (
            RepositorySnapshot(
                snapshot_id=snapshot_id,
                tenant_id=source.tenant_id,
                repository_id=source.repository_id,
                revision=revision,
                source_revision=source.source_revision,
                manifest_checksum_sha256=sha256(manifest).hexdigest(),
                files=tuple(files),
                versions=versions,
            ),
            contents,
        )

    def _capture_files(
        self,
        root: Path,
    ) -> tuple[list[SnapshotFile], dict[str, bytes]]:
        for attempt in range(self._max_capture_retries + 1):
            files: list[SnapshotFile] = []
            contents: dict[str, bytes] = {}
            stable = True
            for path in sorted(root.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve()
                if root not in resolved.parents:
                    raise HarnessError(HarnessErrorCode.PATH_OUTSIDE_REPOSITORY)
                relative = path.relative_to(root).as_posix()
                if relative.startswith(".git/") or relative == ".git":
                    continue
                if _is_sensitive_path(relative):
                    continue
                before = path.stat()
                if before.st_size > self._max_file_bytes:
                    continue
                data = path.read_bytes()
                after = path.stat()
                if (
                    before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or len(data) != after.st_size
                ):
                    stable = False
                    break
                contents[relative] = data
                files.append(
                    SnapshotFile(
                        path=relative,
                        checksum_sha256=sha256(data).hexdigest(),
                        size_bytes=len(data),
                        kind=(
                            FileKind.TEXT
                            if _is_probably_text(relative, data)
                            else FileKind.BINARY
                        ),
                        mode=after.st_mode,
                    )
                )
            if stable and _candidate_paths(root, self._max_file_bytes) == set(contents):
                return files, contents
            if attempt < self._max_capture_retries:
                sleep(0)
        raise HarnessError(HarnessErrorCode.SNAPSHOT_CAPTURE_CONFLICT)


def _is_probably_text(path: str, data: bytes) -> bool:
    """Use a conservative local classifier; binary bytes never enter parsing."""
    if b"\x00" in data:
        return False
    suffix = Path(path).suffix.lower()
    if suffix in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
        ".7z",
        ".tar",
        ".woff",
        ".woff2",
        ".ico",
        ".db",
        ".sqlite",
    }:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _candidate_paths(root: Path, max_file_bytes: int) -> set[str]:
    candidates: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/") or relative == ".git":
            continue
        if _is_sensitive_path(relative):
            continue
        if path.stat().st_size > max_file_bytes:
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise HarnessError(HarnessErrorCode.PATH_OUTSIDE_REPOSITORY)
        candidates.add(relative)
    return candidates


def _is_sensitive_path(path: str) -> bool:
    normalized = path.lower().replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if name.startswith(".env") or name in {
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "secrets",
        "secrets.json",
        "id_rsa",
        "id_dsa",
        "known_hosts",
        "dockerconfig.json",
    }:
        return True
    return name.endswith((".pem", ".key", ".p12", ".pfx", ".jks"))
