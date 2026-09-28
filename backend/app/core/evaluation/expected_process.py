"""TestCase 中与实际过程证据隔离的 golden expected_process.v1 DTO。"""

# ruff: noqa: D101, D415

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, StrictStr

EXPECTED_PROCESS_SCHEMA = "expected_process.v1"


class _ExpectedProcessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExpectedToolCallV1(_ExpectedProcessModel):
    tool_name: StrictStr


@dataclass(frozen=True, slots=True)
class ExpectedProcessV1:
    expected_tool_calls: tuple[ExpectedToolCallV1, ...] | None = None
    forbidden_tools: tuple[str, ...] | None = None
    required_steps: tuple[str, ...] | None = None
    relevant_document_ids: tuple[str, ...] | None = None
    expected_citations: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize a caller-expected namespace for evaluator input only."""
        values: dict[str, object] = {"schema_version": EXPECTED_PROCESS_SCHEMA, "provenance": "CALLER_EXPECTED"}
        if self.expected_tool_calls is not None:
            values["expected_tool_calls"] = tuple({"tool_name": item.tool_name} for item in self.expected_tool_calls)
        for name in ("forbidden_tools", "required_steps", "relevant_document_ids", "expected_citations"):
            value = getattr(self, name)
            if value is not None:
                values[name] = value
        return values


def parse_expected_process(case_metadata: object) -> ExpectedProcessV1 | None:
    """Parse optional frozen Case metadata without converting absence to an empty golden."""
    if not isinstance(case_metadata, Mapping):
        raise ValueError("invalid TestCase metadata")
    value = case_metadata.get("expected_process")
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - {
        "schema_version", "expected_tool_calls", "forbidden_tools", "required_steps",
        "relevant_document_ids", "expected_citations",
    }:
        raise ValueError("invalid expected_process.v1")
    if value.get("schema_version") != EXPECTED_PROCESS_SCHEMA:
        raise ValueError("unsupported expected_process schema")
    def strings(name: str) -> tuple[str, ...] | None:
        raw = value.get(name)
        if raw is None:
            return None
        if not isinstance(raw, (tuple, list)) or any(not isinstance(item, str) or not item for item in raw):
            raise ValueError(f"invalid expected_process field: {name}")
        return tuple(raw)
    tools_raw = value.get("expected_tool_calls")
    tools = None
    if tools_raw is not None:
        if not isinstance(tools_raw, (tuple, list)):
            raise ValueError("invalid expected_tool_calls")
        tools = tuple(ExpectedToolCallV1.model_validate(item) for item in tools_raw)
    return ExpectedProcessV1(
        expected_tool_calls=tools,
        forbidden_tools=strings("forbidden_tools"),
        required_steps=strings("required_steps"),
        relevant_document_ids=strings("relevant_document_ids"),
        expected_citations=strings("expected_citations"),
    )
