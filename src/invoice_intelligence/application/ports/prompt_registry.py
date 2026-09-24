"""Optional prompt registry boundary for structured vision extraction."""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class VisionPromptTemplate:
    """Versioned prompt text without images, invoice values, or remote responses."""

    prompt_version: str
    system: str
    user_instruction: str
    mandatory_rules: str
    source: Literal["local", "langsmith"] = "local"

    def __post_init__(self) -> None:
        for name, value in (
            ("prompt_version", self.prompt_version),
            ("system", self.system),
            ("user_instruction", self.user_instruction),
            ("mandatory_rules", self.mandatory_rules),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"Vision prompt {name} must be non-empty and normalized")


class VisionPromptRegistry(Protocol):
    """Resolve approved prompt text while retaining a local fail-safe template."""

    async def resolve(
        self,
        prompt_version: str,
        fallback: VisionPromptTemplate,
    ) -> VisionPromptTemplate:
        """Return a version-matching prompt without uploading request content."""

        ...
