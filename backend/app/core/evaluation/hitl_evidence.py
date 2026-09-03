"""HITL Tool Approval Evidence v1 —— typed runtime evidence 的 strict consumer DTO 与 correlation 投影。

本模块是 Stage5-Phase7-WP3 的窄证据契约层：只消费 LocalAgent Journal 已发布的
safe public event 投影（approval_id / tool_name / invocation_identity_digest /
invocation_binding_digest / decision_status / sequence），严格校验、按
``run_id + approval_id + invocation_binding_digest`` 关联 approval lifecycle，
并把 TOOL_STARTED / TOOL_COMPLETED 通过 ``invocation_identity_digest``（run-scoped）
绑定到 lifecycle。它不做任何判定：不输出 PASS / FAIL / BLOCKED，不重建 Runtime
state machine，不访问 DB / HTTP。HITL Evaluator（``hitl_evaluator``）只消费本模块
输出的 ``HitlEvaluationInput``。

核心原则：

- LocalAgent = Runtime Fact Producer；AgentEvalOps = Fact Consumer + Evaluator。
  本模块绝不推断、补写或恢复 Runtime 状态，也不暴露 raw invocation_id / raw
  arguments / path / prompt / actor identity。
- correlation 只使用 WP2 冻结的 safe public 字段；``invocation_binding_digest``
  关联 approval request/decision，``invocation_identity_digest``（两側事件都携带）
  在同一 ``run_id`` scope 内关联 Tool execution evidence。
- Evidence Completeness：``trace_complete=True`` 才允许把"未观察到 TOOL_STARTED"
  解释为真实 zero execution；incomplete trace 下所有 absence-of-evidence 语义
  交由 Evaluator 判 BLOCKED，绝不自动 PASS。
- 每个 evidence envelope 必须声明 ``provenance``；synthetic bad-case fixture 不得
  伪装成真实 Runtime 事实。
"""

# ruff: noqa: D105, D415

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from app.core.evaluation.references import EvidenceRef

HITL_TOOL_APPROVAL_EVIDENCE_SCHEMA_VERSION = "hitl-tool-approval-evidence.v1"
HITL_TOOL_APPROVAL_EVIDENCE_KIND = "hitl_tool_approval"
HITL_TOOL_APPROVAL_MEDIA_TYPE = "application/vnd.localagent.hitl-tool-approval+json"
HITL_TOOL_APPROVAL_EVIDENCE_REF_SCHEMA_VERSION = "v1"

_EVIDENCE_ID = re.compile(r"^hitl-tool-approval://(.+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# 与 LocalAgent WP2 Journal/stream 安全 allowlist 一致的 correlation 字段；
# 任何额外 payload 字段（含 legacy raw invocation_id / attempt_id）都被拒绝。
_REQUESTED_FIELDS = frozenset(
    {"approval_id", "tool_name", "invocation_identity_digest", "invocation_binding_digest", "risk_level"}
)
_DECIDED_FIELDS = frozenset(
    {"approval_id", "invocation_identity_digest", "invocation_binding_digest", "decision_status"}
)
_TOOL_FIELDS = frozenset({"tool_name", "invocation_identity_digest"})


class HitlEvidenceContractError(ValueError):
    """HITL evidence integrity / correlation 违规 —— projection fail closed。"""


class HitlEvidenceProvenance(StrEnum):
    """Evidence 真实性来源；synthetic bad-case 不得冒充 Runtime 事实。"""

    REAL_LOCALAGENT_EVIDENCE = "REAL_LOCALAGENT_EVIDENCE"
    DETERMINISTIC_TEST_EVIDENCE = "DETERMINISTIC_TEST_EVIDENCE"
    HYPOTHETICAL_BAD_CASE_FIXTURE = "HYPOTHETICAL_BAD_CASE_FIXTURE"


class HitlRuntimeEventTypeV1(StrEnum):
    """HITL evaluator 消费的 Runtime event 类型（WP1/WP2 冻结事件名的窄子集）。"""

    TOOL_APPROVAL_REQUESTED = "TOOL_APPROVAL_REQUESTED"
    TOOL_APPROVAL_DECIDED = "TOOL_APPROVAL_DECIDED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"


class HitlDecisionStatusV1(StrEnum):
    """``TOOL_APPROVAL_DECIDED`` 的 decision_status 值域（与 LocalAgent payload 一致）。

    APPROVED / REJECTED 是人类决定；INVALIDATED_CANCELLED / INVALIDATED_TIMEOUT
    是 Runtime lifecycle fact。本枚举只做只读观察，不复制 Runtime state ownership。
    """

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INVALIDATED_CANCELLED = "INVALIDATED_CANCELLED"
    INVALIDATED_TIMEOUT = "INVALIDATED_TIMEOUT"


class HitlRuntimeEventV1(BaseModel):
    """一个 Runtime event 的 safe public 投影（Journal ``sequence`` 单调有序）。"""

    model_config = ConfigDict(extra="forbid")

    sequence: StrictInt
    event_type: HitlRuntimeEventTypeV1
    run_id: StrictStr
    step_id: StrictStr | None = None
    approval_id: StrictStr | None = None
    tool_name: StrictStr | None = None
    invocation_identity_digest: StrictStr | None = None
    invocation_binding_digest: StrictStr | None = None
    decision_status: HitlDecisionStatusV1 | None = None
    risk_level: StrictStr | None = None
    actor_id_digest: StrictStr | None = None

    @field_validator("invocation_identity_digest", "invocation_binding_digest", "actor_id_digest")
    @classmethod
    def _digest_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SHA256.fullmatch(value):
            raise ValueError("digest must be a lowercase SHA-256 hex string")
        return value

    @field_validator("run_id", "step_id", "approval_id", "tool_name")
    @classmethod
    def _identifier_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or len(value) > 128:
            raise ValueError("identifier must be 1..128 characters")
        return value

    @model_validator(mode="after")
    def _validate_event_correlation(self) -> "HitlRuntimeEventV1":
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.event_type is HitlRuntimeEventTypeV1.TOOL_APPROVAL_REQUESTED:
            missing = _REQUESTED_FIELDS - _present(self)
            if missing:
                raise ValueError(f"TOOL_APPROVAL_REQUESTED missing safe correlation fields: {sorted(missing)}")
        elif self.event_type is HitlRuntimeEventTypeV1.TOOL_APPROVAL_DECIDED:
            missing = _DECIDED_FIELDS - _present(self)
            if missing:
                raise ValueError(f"TOOL_APPROVAL_DECIDED missing safe correlation fields: {sorted(missing)}")
        elif self.event_type in (HitlRuntimeEventTypeV1.TOOL_STARTED, HitlRuntimeEventTypeV1.TOOL_COMPLETED):
            missing = _TOOL_FIELDS - _present(self)
            if missing:
                raise ValueError(f"{self.event_type.value} missing safe correlation fields: {sorted(missing)}")
        return self


def _present(event: HitlRuntimeEventV1) -> set[str]:
    fields = {"approval_id", "tool_name", "invocation_identity_digest", "invocation_binding_digest"}
    present = {name for name in fields if getattr(event, name) is not None}
    if event.event_type is HitlRuntimeEventTypeV1.TOOL_APPROVAL_DECIDED and event.decision_status is not None:
        present.add("decision_status")
    if event.event_type is HitlRuntimeEventTypeV1.TOOL_APPROVAL_REQUESTED and event.risk_level is not None:
        present.add("risk_level")
    return present


class HitlToolApprovalEvidenceV1(BaseModel):
    """一个 Run 的完整 HITL evidence envelope（单 run、sequence 严格递增）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["hitl-tool-approval-evidence.v1"]
    evidence_id: StrictStr
    run_id: StrictStr
    provenance: HitlEvidenceProvenance
    # True 仅当 capture 覆盖 run 从 start 到 terminal 的完整 Journal；
    # False 时 evaluator 不得把"未观察到执行"解释为 zero execution。
    trace_complete: StrictBool
    terminal_status: StrictStr | None = None
    events: tuple[HitlRuntimeEventV1, ...]

    @field_validator("events")
    @classmethod
    def _events_sorted(cls, value: tuple[HitlRuntimeEventV1, ...]) -> tuple[HitlRuntimeEventV1, ...]:
        sequences = [event.sequence for event in value]
        if any(later <= earlier for earlier, later in zip(sequences, sequences[1:], strict=False)):
            raise ValueError("event sequences must be strictly increasing")
        return value

    @model_validator(mode="after")
    def _validate_identity(self) -> "HitlToolApprovalEvidenceV1":
        match = _EVIDENCE_ID.fullmatch(self.evidence_id)
        if match is None or match.group(1) != self.run_id:
            raise ValueError("evidence_id does not match run_id")
        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("event run_id does not match evidence envelope run_id")
        return self


class HitlApprovalLifecycle:
    """按 ``approval_id`` 关联的一个 approval lifecycle（requested/decided/tool events）。

    这不是 Runtime approval state：只是 ordered evidence 的只读分组投影。
    """

    __slots__ = (
        "approval_id",
        "run_id",
        "tool_name",
        "invocation_identity_digest",
        "invocation_binding_digest",
        "requested",
        "decided",
        "tool_started",
        "tool_completed",
    )

    def __init__(
        self,
        *,
        approval_id: str,
        run_id: str,
        tool_name: str,
        invocation_identity_digest: str,
        invocation_binding_digest: str,
    ) -> None:
        self.approval_id = approval_id
        self.run_id = run_id
        self.tool_name = tool_name
        self.invocation_identity_digest = invocation_identity_digest
        self.invocation_binding_digest = invocation_binding_digest
        self.requested: HitlRuntimeEventV1 | None = None
        self.decided: list[HitlRuntimeEventV1] = []
        self.tool_started: list[HitlRuntimeEventV1] = []
        self.tool_completed: list[HitlRuntimeEventV1] = []

    @property
    def first_sequence(self) -> int:
        """Lifecycle 首个事件 sequence（分组排序用）。"""
        candidates = [event.sequence for event in (self.requested, *self.decided) if event is not None]
        return min(candidates)

    @property
    def effective_decision(self) -> HitlRuntimeEventV1 | None:
        """首个 decision 事件（first-decision-wins 观察投影）。"""
        return self.decided[0] if self.decided else None


@dataclass(frozen=True, slots=True)
class HitlEvaluationInput:
    """HITL Evaluator 的唯一输入：correlation 分组完成后的 ordered evidence 投影。

    不含任何判定结果。``ambiguous_identity_digests`` 记录同一 run 内被多个
    approval lifecycle 共享的 ``invocation_identity_digest`` —— 这是 correlation
    冲突的直接观察，绝不能被静默忽略。
    """

    run_id: str
    provenance: HitlEvidenceProvenance
    trace_complete: bool
    terminal_status: str | None
    lifecycles: tuple[HitlApprovalLifecycle, ...]
    unbound_tool_started: tuple[HitlRuntimeEventV1, ...]
    ambiguous_identity_digests: tuple[str, ...]


def build_hitl_evidence_ref(evidence: HitlToolApprovalEvidenceV1) -> EvidenceRef:
    """映射为既有 inline EvidenceRef，不新建 Artifact Store 或 DB table。"""
    return EvidenceRef(
        kind=HITL_TOOL_APPROVAL_EVIDENCE_KIND,
        identifier=evidence.evidence_id,
        media_type=HITL_TOOL_APPROVAL_MEDIA_TYPE,
        schema_version=HITL_TOOL_APPROVAL_EVIDENCE_REF_SCHEMA_VERSION,
        metadata={"payload": evidence.model_dump(mode="json")},
    )


def hitl_evidence_from_ref(
    ref: EvidenceRef,
    *,
    expected_run_id: str | None = None,
) -> HitlToolApprovalEvidenceV1:
    """从 EvidenceRef 严格恢复 typed evidence；identity mismatch fail closed。"""
    if ref.kind != HITL_TOOL_APPROVAL_EVIDENCE_KIND:
        raise HitlEvidenceContractError("evidence ref kind is not hitl_tool_approval")
    try:
        payload = ref.metadata["payload"]
        evidence = HitlToolApprovalEvidenceV1.model_validate(payload)
    except (KeyError, TypeError, ValueError) as error:
        raise HitlEvidenceContractError("malformed hitl tool approval evidence") from error
    if ref.identifier != evidence.evidence_id:
        raise HitlEvidenceContractError("hitl evidence ref identity mismatch")
    if expected_run_id is not None and evidence.run_id != expected_run_id:
        raise HitlEvidenceContractError("hitl evidence run identity mismatch")
    return evidence


def build_hitl_evaluation_input(evidence: HitlToolApprovalEvidenceV1) -> HitlEvaluationInput:
    """把 typed evidence 关联分组为 evaluator 输入；correlation 冲突显式呈现。

    - approval lifecycle 按 ``approval_id`` 分组；``TOOL_STARTED`` /
      ``TOOL_COMPLETED`` 通过 ``invocation_identity_digest``（run-scoped）绑定；
    - 未匹配任何 lifecycle 的 TOOL_STARTED 归入 ``unbound_tool_started``
      （ALLOW 风险工具合法地没有 approval lifecycle）；
    - 决策事件缺少对应 REQUESTED、或同一 identity digest 被多个 lifecycle 共享，
      是 correlation 完整性事实，原样暴露给 evaluator，不做静默修复。
    """
    lifecycles: dict[str, HitlApprovalLifecycle] = {}
    orphan_decided: list[HitlRuntimeEventV1] = []
    for event in evidence.events:
        if event.event_type is HitlRuntimeEventTypeV1.TOOL_APPROVAL_REQUESTED:
            assert event.approval_id is not None and event.tool_name is not None
            assert event.invocation_identity_digest is not None
            assert event.invocation_binding_digest is not None
            existing = lifecycles.get(event.approval_id)
            if existing is not None:
                raise HitlEvidenceContractError(
                    f"duplicate TOOL_APPROVAL_REQUESTED for approval_id {event.approval_id}"
                )
            lifecycle = HitlApprovalLifecycle(
                approval_id=event.approval_id,
                run_id=evidence.run_id,
                tool_name=event.tool_name,
                invocation_identity_digest=event.invocation_identity_digest,
                invocation_binding_digest=event.invocation_binding_digest,
            )
            lifecycle.requested = event
            lifecycles[event.approval_id] = lifecycle
        elif event.event_type is HitlRuntimeEventTypeV1.TOOL_APPROVAL_DECIDED:
            assert event.approval_id is not None
            lifecycle = lifecycles.get(event.approval_id)
            if lifecycle is None:
                orphan_decided.append(event)
            else:
                lifecycle.decided.append(event)
    for event in evidence.events:
        if event.event_type not in (HitlRuntimeEventTypeV1.TOOL_STARTED, HitlRuntimeEventTypeV1.TOOL_COMPLETED):
            continue
        assert event.invocation_identity_digest is not None
        matches = [
            lifecycle
            for lifecycle in lifecycles.values()
            if lifecycle.invocation_identity_digest == event.invocation_identity_digest
        ]
        if not matches:
            # 未匹配任何 approval lifecycle 的 tool event（ALLOW 风险工具）不绑定；
            # unbound TOOL_STARTED 在下方统一收集。
            continue
        for lifecycle in matches:
            if event.event_type is HitlRuntimeEventTypeV1.TOOL_STARTED:
                lifecycle.tool_started.append(event)
            else:
                lifecycle.tool_completed.append(event)
    # orphan decided 也需要 lifecycle 容器，让 correlation 断言可见。
    for event in orphan_decided:
        assert event.approval_id is not None
        lifecycle = HitlApprovalLifecycle(
            approval_id=event.approval_id,
            run_id=evidence.run_id,
            tool_name=event.tool_name or "",
            invocation_identity_digest=event.invocation_identity_digest or "",
            invocation_binding_digest=event.invocation_binding_digest or "",
        )
        lifecycle.decided.append(event)
        lifecycles[event.approval_id] = lifecycle

    unbound_starts = tuple(
        event
        for event in evidence.events
        if event.event_type is HitlRuntimeEventTypeV1.TOOL_STARTED
        and not any(
            lifecycle.invocation_identity_digest == event.invocation_identity_digest
            for lifecycle in lifecycles.values()
        )
    )
    by_identity: dict[str, set[str]] = {}
    for lifecycle in lifecycles.values():
        by_identity.setdefault(lifecycle.invocation_identity_digest, set()).add(lifecycle.approval_id)
    ambiguous = tuple(sorted(digest for digest, approvals in by_identity.items() if len(approvals) > 1))
    ordered = tuple(sorted(lifecycles.values(), key=lambda item: item.first_sequence))
    return HitlEvaluationInput(
        run_id=evidence.run_id,
        provenance=evidence.provenance,
        trace_complete=evidence.trace_complete,
        terminal_status=evidence.terminal_status,
        lifecycles=ordered,
        unbound_tool_started=unbound_starts,
        ambiguous_identity_digests=ambiguous,
    )


__all__ = [
    "HITL_TOOL_APPROVAL_EVIDENCE_KIND",
    "HITL_TOOL_APPROVAL_EVIDENCE_REF_SCHEMA_VERSION",
    "HITL_TOOL_APPROVAL_EVIDENCE_SCHEMA_VERSION",
    "HITL_TOOL_APPROVAL_MEDIA_TYPE",
    "HitlApprovalLifecycle",
    "HitlDecisionStatusV1",
    "HitlEvaluationInput",
    "HitlEvidenceContractError",
    "HitlEvidenceProvenance",
    "HitlRuntimeEventV1",
    "HitlRuntimeEventTypeV1",
    "HitlToolApprovalEvidenceV1",
    "build_hitl_evaluation_input",
    "build_hitl_evidence_ref",
    "hitl_evidence_from_ref",
]
