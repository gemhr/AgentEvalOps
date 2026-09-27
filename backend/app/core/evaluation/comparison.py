"""Baseline vs Candidate EvaluationRun 的最小 Regression Comparison Domain。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.evaluation.immutable import FrozenJsonValue, freeze_json, require_text
from app.core.evaluation.references import VersionRef


class RegressionClassification(StrEnum):
    """跨 Run 对齐后单个 (case, evaluator) 槽位的最小比较分类。"""

    REGRESSION = "REGRESSION"
    IMPROVEMENT = "IMPROVEMENT"
    UNCHANGED = "UNCHANGED"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class ComparisonCompatibility(StrEnum):
    """强度有序的跨 Run 语义兼容判断。"""

    COMPARABLE = "COMPARABLE"
    CONDITIONALLY_COMPARABLE = "CONDITIONALLY_COMPARABLE"
    INCOMPARABLE = "INCOMPARABLE"


class CaseMembership(StrEnum):
    """两个 Suite selection 之间的 Case membership。"""

    BOTH = "BOTH"
    NEW_CASE = "NEW_CASE"
    REMOVED_CASE = "REMOVED_CASE"


class AttemptAvailability(StrEnum):
    """权威 Attempt 的执行可用性。"""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    MISSING = "MISSING"


class ProvenanceSentinel(StrEnum):
    """实际不可得与语义不适用的不同身份值。"""

    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class ExecutionSubjectSnapshot:
    """Run 捕获的实际 Execution Subject 事实，不从当前配置回填。"""

    subject_kind: str
    agent_id: str | ProvenanceSentinel
    agent_version: str | ProvenanceSentinel
    workflow_id: str | ProvenanceSentinel
    workflow_version: str | ProvenanceSentinel
    toolset_identity: FrozenJsonValue | ProvenanceSentinel
    provider_binding_identity: FrozenJsonValue | ProvenanceSentinel
    runtime_version: str | ProvenanceSentinel
    deployment_environment: str | ProvenanceSentinel
    run_mode: str | ProvenanceSentinel
    profile: str | ProvenanceSentinel
    fixture_target_identity: str | ProvenanceSentinel = ProvenanceSentinel.NOT_APPLICABLE
    fixture_content_identity: FrozenJsonValue | ProvenanceSentinel = ProvenanceSentinel.NOT_APPLICABLE

    def __post_init__(self) -> None:
        require_text(self.subject_kind, "subject_kind")
        for name in ("toolset_identity", "provider_binding_identity", "fixture_content_identity"):
            value = getattr(self, name)
            if not isinstance(value, ProvenanceSentinel):
                object.__setattr__(self, name, freeze_json(value))


class ComparisonReason(StrEnum):
    """稳定、可测试的短 reason 标签，不承载动态文本。"""

    VERDICT_REGRESSED = "verdict_regressed"
    VERDICT_IMPROVED = "verdict_improved"
    VERDICT_UNCHANGED = "verdict_unchanged"
    BASELINE_MISSING = "baseline_missing"
    CANDIDATE_MISSING = "candidate_missing"
    INCONCLUSIVE_RESULT = "inconclusive_result"
    EVALUATOR_CONFIG_MISMATCH = "evaluator_config_mismatch"
    CASE_IDENTITY_COLLISION = "CASE_IDENTITY_COLLISION"
    CASE_CONTENT_CHANGED = "CASE_CONTENT_CHANGED"
    CASE_VERSION_LABEL_DRIFT = "CASE_VERSION_LABEL_DRIFT"
    EVALUATOR_VERSION_CHANGED = "EVALUATOR_VERSION_CHANGED"
    EVALUATOR_IDENTITY_COLLISION = "EVALUATOR_IDENTITY_COLLISION"
    INSUFFICIENT_EVALUATOR_PROVENANCE = "INSUFFICIENT_EVALUATOR_PROVENANCE"
    REQUIRED_EVIDENCE_MISSING = "REQUIRED_EVIDENCE_MISSING"
    EVALUATOR_CONFIG_CHANGED = "EVALUATOR_CONFIG_CHANGED"
    EVALUATOR_PROMPT_CHANGED = "EVALUATOR_PROMPT_CHANGED"
    EVALUATOR_THRESHOLD_CHANGED = "EVALUATOR_THRESHOLD_CHANGED"
    EVALUATOR_DIRECTION_CHANGED = "EVALUATOR_DIRECTION_CHANGED"
    EVALUATOR_TOLERANCE_CHANGED = "EVALUATOR_TOLERANCE_CHANGED"
    JUDGE_BINDING_CHANGED = "JUDGE_BINDING_CHANGED"
    MODEL_REVISION_UNVERIFIABLE = "MODEL_REVISION_UNVERIFIABLE"
    TARGET_KIND_MISMATCH = "TARGET_KIND_MISMATCH"
    TARGET_BINDING_DRIFT = "TARGET_BINDING_DRIFT"
    SUBJECT_LINEAGE_MISMATCH = "SUBJECT_LINEAGE_MISMATCH"
    SUBJECT_BINDING_DRIFT = "SUBJECT_BINDING_DRIFT"
    INSUFFICIENT_SUBJECT_PROVENANCE = "INSUFFICIENT_SUBJECT_PROVENANCE"
    MISSING_BASELINE_RESULT = "MISSING_BASELINE_RESULT"
    MISSING_CANDIDATE_RESULT = "MISSING_CANDIDATE_RESULT"
    BOTH_RESULTS_MISSING = "BOTH_RESULTS_MISSING"
    EVALUATOR_ERROR = "EVALUATOR_ERROR"
    EVALUATOR_INCONCLUSIVE = "EVALUATOR_INCONCLUSIVE"
    LEGACY_INSUFFICIENT_PROVENANCE = "LEGACY_INSUFFICIENT_PROVENANCE"
    EVALUATOR_ADDED = "EVALUATOR_ADDED"
    EVALUATOR_REMOVED = "EVALUATOR_REMOVED"
    NEW_CASE = "NEW_CASE"
    REMOVED_CASE = "REMOVED_CASE"
    ATTEMPT_FAILURE = "ATTEMPT_FAILURE"
    ATTEMPT_TIMEOUT = "ATTEMPT_TIMEOUT"
    ATTEMPT_CANCELLED = "ATTEMPT_CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class RegressionComparisonError(RuntimeError):
    """Comparison 边界的 typed base error。"""


class RunsNotComparable(RegressionComparisonError):
    """Run 不满足最小 eligibility / comparability 条件。"""


class ResultAlignmentAmbiguous(RegressionComparisonError):
    """同一 Run 内一个跨 Run 对齐键对应多个 Result，fail closed。"""


@dataclass(frozen=True, slots=True)
class AlignedResultComparison:
    """一个 (case, evaluator) 槽位对齐后的比较结果。

    只引用两侧 Result identity 与最小 score evidence，不复制完整 Result payload。
    """

    case_id: str
    case_version: str
    evaluator_id: str
    evaluator_version: str
    baseline_result_id: str | None = None
    candidate_result_id: str | None = None
    classification: RegressionClassification = RegressionClassification.NOT_COMPARABLE
    reason: ComparisonReason = ComparisonReason.BASELINE_MISSING
    baseline_score: float | None = None
    candidate_score: float | None = None
    score_delta: float | None = None
    score_regressed: bool | None = None
    compatibility: ComparisonCompatibility = ComparisonCompatibility.COMPARABLE
    reason_codes: tuple[str, ...] = ()
    case_membership: CaseMembership = CaseMembership.BOTH
    baseline_case_version: str | None = None
    candidate_case_version: str | None = None
    baseline_evaluator_version: str | None = None
    candidate_evaluator_version: str | None = None
    baseline_expected: bool = True
    candidate_expected: bool = True
    baseline_required: bool = True
    candidate_required: bool = True
    baseline_attempt_id: str | None = None
    candidate_attempt_id: str | None = None
    baseline_attempt_outcome: AttemptAvailability = AttemptAvailability.MISSING
    candidate_attempt_outcome: AttemptAvailability = AttemptAvailability.MISSING
    score_transition: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("case_id", "case_version", "evaluator_id", "evaluator_version"):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.classification, RegressionClassification):
            raise ValueError("unknown classification")
        if not isinstance(self.reason, ComparisonReason):
            raise ValueError("unknown reason")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))


@dataclass(frozen=True, slots=True)
class RunComparisonProvenance:
    """一侧 Run 用于 comparison provenance 的最小身份快照。

    dataset/suite/target version 差异是 Regression 比较的合法输入，必须保留为 provenance。
    """

    dataset_id: str
    dataset_version: str
    suite_id: str
    suite_version: str
    execution_target_id: str
    execution_target_kind: str
    target_version_ref: VersionRef | None = None

    def __post_init__(self) -> None:
        for field_name in ("dataset_id", "dataset_version", "suite_id", "suite_version", "execution_target_id", "execution_target_kind"):
            require_text(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class EvaluationRunReference:
    """Resolved immutable terminal Run input captured by the comparison projection."""

    project_id: UUID
    run_id: UUID
    status: str
    finished_at: datetime
    dataset_snapshot: object
    suite_snapshot: object
    target_snapshot: object
    subject_snapshot: ExecutionSubjectSnapshot
    case_manifest: tuple[object, ...]
    evaluator_manifest: tuple[object, ...]
    attempt_refs: tuple[object, ...]
    result_refs: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class EvaluationRunComparison:
    """两个 COMPLETED EvaluationRun 的 immutable comparison 结果。"""

    project_id: UUID
    baseline_run_id: UUID
    candidate_run_id: UUID
    baseline_provenance: RunComparisonProvenance
    candidate_provenance: RunComparisonProvenance
    comparisons: tuple[AlignedResultComparison, ...] = ()
    comparison_contract_version: str = "stage11.wp3.v1"
    baseline_reference: EvaluationRunReference | None = None
    candidate_reference: EvaluationRunReference | None = None
    accepted_conditional_reasons: tuple[str, ...] = ()
    computed_at: datetime | None = None
    semantic_digest: str = ""
