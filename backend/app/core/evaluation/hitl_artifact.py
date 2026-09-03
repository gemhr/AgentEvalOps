"""HITL Tool Approval evaluation artifact —— JSON-ready、append-only 的 scenario 级快照。

复用既有 artifact 语义（``stateful_artifact`` / ``episodic_artifact`` 风格）：
不新建 Dashboard / DB table，只提供可序列化、可追溯、不携带 raw runtime payload
的 evaluation 记录，让人能看到 scenario id、evidence correlation、assertion
结果、expected invariant、observed evidence summary 与 failure reason。
"""

# ruff: noqa: D105, D415

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, Field

from app.core.evaluation.hitl_evidence import (
    HitlEvaluationInput,
    HitlEvidenceProvenance,
)
from app.core.evaluation.hitl_evaluator import (
    HITL_TOOL_APPROVAL_EVALUATOR_ID,
    HITL_TOOL_APPROVAL_EVALUATOR_VERSION,
    HitlEvaluation,
)
from app.core.evaluation.stateful_assertion import AssertionStatus

HITL_TOOL_APPROVAL_EVALUATION_SCHEMA_VERSION = "hitl-tool-approval-evaluation.v1"


class HitlLifecycleSummaryV1(BaseModel):
    """一个 approval lifecycle 的 evidence summary（只含 safe correlation 字段）。"""

    model_config = ConfigDict(extra="forbid")

    approval_id: StrictStr
    tool_name: StrictStr
    invocation_identity_digest: StrictStr
    invocation_binding_digest: StrictStr
    requested_sequence: StrictInt | None = None
    decision_status: StrictStr | None = None
    decision_sequence: StrictInt | None = None
    tool_started_sequences: list[StrictInt] = Field(default_factory=list)
    tool_completed_sequences: list[StrictInt] = Field(default_factory=list)


class HitlToolApprovalEvaluationRecordV1(BaseModel):
    """一个 HITL scenario evaluation 的完整 artifact（JSON-ready）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictStr = HITL_TOOL_APPROVAL_EVALUATION_SCHEMA_VERSION
    scenario_id: StrictStr
    run_id: StrictStr
    evaluator_id: StrictStr = HITL_TOOL_APPROVAL_EVALUATOR_ID
    evaluator_version: StrictStr = HITL_TOOL_APPROVAL_EVALUATOR_VERSION
    evidence_provenance: HitlEvidenceProvenance
    trace_complete: StrictBool
    terminal_status: StrictStr | None = None
    aggregate_status: AssertionStatus
    assertions: list[dict[str, object]] = Field(default_factory=list)
    failure_reasons: list[StrictStr] = Field(default_factory=list)
    lifecycles: list[HitlLifecycleSummaryV1] = Field(default_factory=list)
    unbound_tool_started_sequences: list[StrictInt] = Field(default_factory=list)
    ambiguous_identity_digests: list[StrictStr] = Field(default_factory=list)


def build_hitl_evaluation_record(
    evaluation: HitlEvaluation,
    input_value: HitlEvaluationInput,
) -> HitlToolApprovalEvaluationRecordV1:
    """把 evaluation 结果 + evidence 投影为 artifact record（DERIVABLE_WITHOUT_RERUN）。"""
    lifecycles = [
        HitlLifecycleSummaryV1(
            approval_id=item.approval_id,
            tool_name=item.tool_name,
            invocation_identity_digest=item.invocation_identity_digest,
            invocation_binding_digest=item.invocation_binding_digest,
            requested_sequence=item.requested.sequence if item.requested is not None else None,
            decision_status=(
                item.effective_decision.decision_status.value
                if item.effective_decision is not None and item.effective_decision.decision_status is not None
                else None
            ),
            decision_sequence=item.effective_decision.sequence if item.effective_decision is not None else None,
            tool_started_sequences=[event.sequence for event in item.tool_started],
            tool_completed_sequences=[event.sequence for event in item.tool_completed],
        )
        for item in input_value.lifecycles
    ]
    return HitlToolApprovalEvaluationRecordV1(
        scenario_id=evaluation.scenario_id,
        run_id=evaluation.run_id,
        evidence_provenance=input_value.provenance,
        trace_complete=input_value.trace_complete,
        terminal_status=input_value.terminal_status,
        aggregate_status=evaluation.status,
        assertions=[item.to_metadata() for item in evaluation.assertions],
        failure_reasons=list(evaluation.failure_reasons),
        lifecycles=lifecycles,
        unbound_tool_started_sequences=[event.sequence for event in input_value.unbound_tool_started],
        ambiguous_identity_digests=list(input_value.ambiguous_identity_digests),
    )


__all__ = [
    "HITL_TOOL_APPROVAL_EVALUATION_SCHEMA_VERSION",
    "HitlLifecycleSummaryV1",
    "HitlToolApprovalEvaluationRecordV1",
    "build_hitl_evaluation_record",
]
