"""Evaluation Domain 使用的递归不可变 JSON 快照。"""

# ruff: noqa: D105, D415

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


class FrozenDict(Mapping[str, Any]):
    """保持插入顺序且不可变、可哈希的字符串键映射。"""

    __slots__ = ("_items", "_values")

    def __init__(self, items: Mapping[str, Any] | None = None) -> None:
        source = items or {}
        frozen_items = tuple(source.items())
        object.__setattr__(self, "_items", frozen_items)
        object.__setattr__(self, "_values", MappingProxyType(dict(frozen_items)))

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("FrozenDict is immutable")

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __hash__(self) -> int:
        return hash(frozenset(self._items))

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._items)!r})"


FrozenJsonValue: TypeAlias = JsonScalar | tuple["FrozenJsonValue", ...] | FrozenDict


def freeze_json(value: JsonValue) -> FrozenJsonValue:
    """校验 JSON-compatible 值并递归转换为不可变快照。"""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return FrozenDict(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def require_text(value: str, field_name: str) -> None:
    """要求标识符类字符串包含非空白字符。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def json_compatible(value: JsonValue) -> JsonValue:
    """递归投影不可变 JSON 快照为纯 JSON 类型（dict/list/标量）。

    只用于 JSON serialization projection，不修改 Domain DTO 的不可变性：
    ``FrozenDict`` / 任意 ``Mapping`` → dict、``tuple`` → list、``Enum`` →
    value、``UUID`` → str、``datetime`` → ISO 字符串；其余标量原样保留。
    非 JSON 类型抛 ``TypeError``（fail closed）。
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return value
    if isinstance(value, Enum):
        return json_compatible(value.value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")
