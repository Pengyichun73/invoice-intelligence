"""Immutable version binding for every Harness execution."""

from dataclasses import dataclass

from .errors import require_text


@dataclass(frozen=True, slots=True)
class ExecutionVersionBinding:
    repository_version: str
    snapshot_version: str
    schema_version: str
    parser_version: str
    grammar_version: str
    redaction_version: str
    index_version: str
    model_version: str
    prompt_version: str
    patch_policy_version: str
    sandbox_policy_version: str
    watchdog_version: str

    def __post_init__(self) -> None:
        for name, value in self.__dataclass_fields__.items():
            require_text(name, getattr(self, name))

    def as_dict(self) -> dict[str, str]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }

