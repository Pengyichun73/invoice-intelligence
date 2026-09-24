"""Framework-independent recursive JSON value types."""

from typing import TypeAlias

JsonValue: TypeAlias = (
    bool | int | float | str | None | list["JsonValue"] | dict[str, "JsonValue"]
)