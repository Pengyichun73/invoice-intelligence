"""Explicit grammar registry; missing registrations fail closed."""

from dataclasses import dataclass

from ...domain.errors import HarnessError, HarnessErrorCode, require_text


@dataclass(frozen=True, slots=True)
class GrammarRegistration:
    language: str
    parser_package: str
    parser_version: str
    grammar_version: str
    schema_version: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            require_text(name, getattr(self, name))


class GrammarRegistry:
    def __init__(self, registrations: tuple[GrammarRegistration, ...] = ()) -> None:
        self._registrations = {item.language: item for item in registrations}

    def require(self, language: str) -> GrammarRegistration:
        try:
            return self._registrations[language]
        except KeyError as exc:
            raise HarnessError(
                HarnessErrorCode.PARSER_NOT_CONFIGURED,
                f"no approved grammar for {language}",
            ) from exc

