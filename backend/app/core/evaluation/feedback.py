"""Trace-to-Dataset feedback 的 caller-confirmed command 与错误类型。

这是一个纯 value/command 层：不读 Trace payload、不做 sanitization、
不推断 expected output / criticality、不触发 Evaluation。所有身份字段
（dataset/case/version）都由 caller 显式提供。
"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping
from uuid import UUID

from app.core.evaluation.catalog import AssertionSpec
from app.core.evaluation.immutable import JsonValue, require_text
from app.core.evaluation.references import CaseVersionRef


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TraceFeedbackError(ValueError):
    """Trace feedback command/service 的合同违反。"""


class TraceFeedbackCandidateError(TraceFeedbackError):
    """Trace 不是当前 project 可访问的 failing candidate。"""


@dataclass(frozen=True, slots=True)
class TraceFeedbackCommand:
    """把一条 failing Trace 回流为 Evaluation catalog 事实的显式命令。

    ``input_payload`` 是唯一内容来源且必须由 caller 提供（already-sanitized）；
    ``expected_output`` 为可选；版本号由 caller 显式分配（无 authoritative
    catalog，因此不做自动版本递增）。``evidence_refs`` 由 service 根据
    ``trace_id`` 内部构造，caller 不需要也无法传入。
    """

    project_id: UUID
    trace_id: UUID
    dataset_id: str
    dataset_version: str
    case_id: str
    case_version: str
    input_payload: JsonValue
    parent_dataset_version: str | None = None
    dataset_name: str | None = None
    case_name: str | None = None
    expected_output: JsonValue | None = None
    assertion_specs: tuple[AssertionSpec, ...] = ()
    base_case_refs: tuple[CaseVersionRef, ...] = ()
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now, compare=False)

    def __post_init__(self) -> None:
        require_text(self.dataset_id, "dataset_id")
        require_text(self.dataset_version, "dataset_version")
        require_text(self.case_id, "case_id")
        require_text(self.case_version, "case_version")
        if self.parent_dataset_version is not None:
            require_text(self.parent_dataset_version, "parent_dataset_version")
            if self.parent_dataset_version == self.dataset_version:
                raise ValueError("parent_dataset_version must differ from dataset_version")
        if self.dataset_name is not None:
            require_text(self.dataset_name, "dataset_name")
        if self.case_name is not None:
            require_text(self.case_name, "case_name")
        base_refs = tuple(self.base_case_refs)
        if len(base_refs) != len(set(base_refs)):
            raise ValueError("duplicate base_case_ref is not allowed")
        new_ref = CaseVersionRef(self.case_id, self.case_version)
        if new_ref in base_refs:
            raise ValueError("case ref is already present in base_case_refs")
        tags = tuple(self.tags)
        for tag in tags:
            require_text(tag, "tag")
        if len(tags) != len(set(tags)):
            raise ValueError("duplicate tag is not allowed")
        object.__setattr__(self, "assertion_specs", tuple(self.assertion_specs))
        object.__setattr__(self, "base_case_refs", base_refs)
        object.__setattr__(self, "tags", tags)
        object.__setattr__(self, "metadata", dict(self.metadata))
