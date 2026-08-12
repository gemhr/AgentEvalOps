"""Runtime-neutral Execution Target 合同与 terminal outcome algebra。"""

# ruff: noqa: D105, D415

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue, JsonValue, freeze_json, require_text
from app.core.evaluation.references import (
    ArtifactRef,
    CapabilityRequirement,
    CaseVersionRef,
    EvidenceRef,
    VersionRef,
    freeze_metadata,
)

FIXTURE_TARGET_KIND = "FIXTURE"


class OutcomeKind(StrEnum):
    """一次 execution attempt 的 terminal observation。"""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class UnsupportedTargetCapabilitiesError(ValueError):
    """Target 无法满足 Suite 的全部 required capabilities。"""


@dataclass(frozen=True, slots=True)
class ExecutionTargetRef:
    """不绑定具体 runtime 的 Execution Target identity 与能力声明。"""

    target_id: str
    target_kind: str
    target_version_ref: VersionRef | None = None
    capabilities: tuple[str, ...] = ()
    config_ref: VersionRef | None = None

    def __post_init__(self) -> None:
        require_text(self.target_id, "target_id")
        require_text(self.target_kind, "target_kind")
        if self.target_version_ref is not None and not isinstance(self.target_version_ref, VersionRef):
            raise TypeError("target_version_ref must be VersionRef")
        if self.config_ref is not None and not isinstance(self.config_ref, VersionRef):
            raise TypeError("config_ref must be VersionRef")
        capabilities = tuple(self.capabilities)
        for capability in capabilities:
            require_text(capability, "capability")
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("duplicate capability is not allowed")
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """交给 Execution Target 的 immutable logical execution request。"""

    request_id: str
    run_id: str
    attempt_id: str
    case_ref: CaseVersionRef
    input_payload: FrozenJsonValue
    timeout: timedelta
    idempotency_key: str
    execution_metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        require_text(self.request_id, "request_id")
        require_text(self.run_id, "run_id")
        require_text(self.attempt_id, "attempt_id")
        if not isinstance(self.case_ref, CaseVersionRef):
            raise TypeError("case_ref must be CaseVersionRef")
        if not isinstance(self.timeout, timedelta):
            raise TypeError("timeout must be timedelta")
        timeout_seconds = self.timeout.total_seconds()
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        require_text(self.idempotency_key, "idempotency_key")
        object.__setattr__(self, "input_payload", freeze_json(self.input_payload))
        object.__setattr__(self, "execution_metadata", freeze_metadata(self.execution_metadata))


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Target 对一次 execution attempt 给出的不可变 terminal observation。"""

    request_id: str
    kind: OutcomeKind
    started_at: datetime
    finished_at: datetime
    output_artifact_ref: ArtifactRef | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    error_category: str | None = None
    reason: str | None = None
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        require_text(self.request_id, "request_id")
        if not isinstance(self.kind, OutcomeKind):
            raise ValueError("unknown outcome kind")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        if self.finished_at.tzinfo is None or self.finished_at.utcoffset() is None:
            raise ValueError("finished_at must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")

        if self.kind is OutcomeKind.SUCCESS:
            if self.output_artifact_ref is None:
                raise ValueError("SUCCESS requires output_artifact_ref")
            if self.error_category is not None:
                raise ValueError("SUCCESS must not include error_category")
        else:
            if self.output_artifact_ref is not None:
                raise ValueError(f"{self.kind.value} must not include output_artifact_ref")
            if self.error_category is None:
                raise ValueError(f"{self.kind.value} requires error_category")
            if self.reason is None:
                raise ValueError(f"{self.kind.value} requires reason")

        if self.error_category is not None:
            require_text(self.error_category, "error_category")
        if self.reason is not None:
            require_text(self.reason, "reason")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))


class ExecutionTarget(Protocol):
    """未来 Fixture、HTTP、Replay 或 LocalAgent adapter 实现的异步端口。"""

    @property
    def target_ref(self) -> ExecutionTargetRef:
        """返回 Target identity、version 与 capability snapshot。"""
        ...

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        """执行 request 并返回无歧义 terminal outcome。"""
        ...


def validate_target_capabilities(
    requirements: tuple[CapabilityRequirement, ...] | list[CapabilityRequirement],
    target_capabilities: tuple[str, ...] | list[str],
) -> None:
    """在调用 Target 前 fail closed 地验证 Suite required capabilities。"""
    required = tuple(requirements)
    available = tuple(target_capabilities)
    for capability in available:
        require_text(capability, "target capability")
    missing = tuple(item.identifier for item in required if item.identifier not in available)
    if missing:
        raise UnsupportedTargetCapabilitiesError(f"target is missing required capabilities: {', '.join(missing)}")
