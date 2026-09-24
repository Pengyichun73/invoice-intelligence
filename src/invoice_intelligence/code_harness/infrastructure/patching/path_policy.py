"""Repository-relative patch path policy."""

from pathlib import PurePosixPath

from ...domain.errors import HarnessError, HarnessErrorCode


def validate_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        not normalized
        or candidate.is_absolute()
        or ".." in candidate.parts
        or ":" in normalized
        or normalized.startswith(".git/")
    ):
        raise HarnessError(
            HarnessErrorCode.PATH_OUTSIDE_REPOSITORY,
            "patch path is outside the repository policy",
        )
    return normalized

