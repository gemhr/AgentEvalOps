"""Evaluation Run 与 Execution Attempt 的生命周期领域模型。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.evaluation.execution import ExecutionOutcome, ExecutionRequest, ExecutionTargetRef, OutcomeKind
from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue, JsonValue, freeze_json, require_text
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef, VersionRef, freeze_metadata


class RunStatus(StrEnum):
    """EvaluationRun 的完整持久化状态集合。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class AttemptStatus(StrEnum):
    """ExecutionAttempt 的 lifecycle state，与执行结果分离。"""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    TERMINAL = "TERMINAL"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.OUTCOME_UNKNOWN})
RETRYABLE_OUTCOMES = frozenset(
    {OutcomeKind.FAILURE, OutcomeKind.TIMEOUT, OutcomeKind.CANCELLED, OutcomeKind.OUTCOME_UNKNOWN}
)


class EvaluationPersistenceError(RuntimeError):
    """WP3 持久化边界的 typed base error。"""


class EvaluationEntityNotFound(EvaluationPersistenceError):
    """实体不存在或不属于指定租户。"""


class AttemptNotClaimed(EvaluationPersistenceError):
    """Atomic claim 未获得 ownership。"""


class AttemptClaimLost(EvaluationPersistenceError):
    """Claim token、状态或 lease 不再授权写入。"""


class ResultAlreadyFinalized(EvaluationPersistenceError):
    """同一 evaluator logical slot 已有 append-only 结果。"""


class RetryAlreadyCreated(EvaluationPersistenceError):
    """同一 source Attempt 已存在 direct retry child。"""


class RunNotFinishable(EvaluationPersistenceError):
    """Run 尚不满足显式 finish 条件。"""


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """一次 Evaluation 的不可变输入快照与受控 lifecycle。"""

    run_id: UUID
    project_id: UUID
    dataset_ref: VersionRef
    suite_ref: VersionRef
    execution_target_ref: ExecutionTargetRef
    dataset_snapshot: FrozenJsonValue
    suite_snapshot: FrozenJsonValue
    execution_target_snapshot: FrozenJsonValue
    created_at: datetime
    subject_ref: FrozenJsonValue | None = None
    status: RunStatus = RunStatus.PENDING
    status_reason: str | None = None
    metadata: FrozenDict = field(default_factory=FrozenDict)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise ValueError("unknown run status")
        for name in ("created_at", "started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if self.status in TERMINAL_RUN_STATUSES:
            if self.finished_at is None:
                raise ValueError("terminal run requires finished_at")
        elif self.finished_at is not None:
            raise ValueError("non-terminal run must not have finished_at")
        if self.status is RunStatus.PENDING and self.started_at is not None:
            raise ValueError("PENDING run must not have started_at")
        if self.status_reason is not None:
            require_text(self.status_reason, "status_reason")
        object.__setattr__(self, "dataset_snapshot", freeze_json(self.dataset_snapshot))
        object.__setattr__(self, "suite_snapshot", freeze_json(self.suite_snapshot))
        object.__setattr__(self, "execution_target_snapshot", freeze_json(self.execution_target_snapshot))
        if self.subject_ref is not None:
            object.__setattr__(self, "subject_ref", freeze_json(self.subject_ref))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))

    def mark_running(self, started_at: datetime) -> EvaluationRun:
        """只允许第一次 claim 将 PENDING Run 启动。"""
        if self.status is not RunStatus.PENDING:
            raise ValueError("only PENDING run can start")
        return replace(self, status=RunStatus.RUNNING, started_at=started_at)

    def finish(self, status: RunStatus, finished_at: datetime, reason: str | None = None) -> EvaluationRun:
        """不可逆地结束 Run。"""
        if self.status in TERMINAL_RUN_STATUSES:
            raise ValueError("terminal run is immutable")
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("finish requires terminal run status")
        if status is not RunStatus.COMPLETED and not reason:
            raise ValueError("non-completed run requires reason")
        return replace(self, status=status, status_reason=reason, finished_at=finished_at)


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """一次 TestCase execution try 的 immutable snapshot。"""

    attempt_id: UUID
    project_id: UUID
    run_id: UUID
    case_ref: CaseVersionRef
    attempt_no: int
    execution_target_ref: ExecutionTargetRef
    execution_request: ExecutionRequest
    request_snapshot: FrozenJsonValue
    created_at: datetime
    retry_of_attempt_id: UUID | None = None
    status: AttemptStatus = AttemptStatus.PENDING
    claim_token: UUID | None = None
    worker_ref: str | None = None
    task_ref: str | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    lease_expires_at: datetime | None = None
    execution_outcome_kind: OutcomeKind | None = None
    output_artifact_ref: ArtifactRef | None = None
    outcome_evidence_refs: tuple[EvidenceRef, ...] = ()
    outcome_metadata: FrozenDict = field(default_factory=FrozenDict)
    error_category: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.attempt_no <= 0:
            raise ValueError("attempt_no must be positive")
        if (self.attempt_no == 1) != (self.retry_of_attempt_id is None):
            raise ValueError("initial/retry lineage is inconsistent")
        if str(self.attempt_id) != self.execution_request.attempt_id:
            raise ValueError("request attempt identity mismatch")
        if str(self.run_id) != self.execution_request.run_id:
            raise ValueError("request run identity mismatch")
        if self.case_ref != self.execution_request.case_ref:
            raise ValueError("request case identity mismatch")
        if not isinstance(self.status, AttemptStatus):
            raise ValueError("unknown attempt status")
        terminal = self.status is AttemptStatus.TERMINAL
        if terminal != (self.execution_outcome_kind is not None and self.finished_at is not None):
            raise ValueError("attempt terminal state/outcome is inconsistent")
        if self.status in {AttemptStatus.CLAIMED, AttemptStatus.RUNNING, AttemptStatus.TERMINAL} and self.claim_token is None:
            raise ValueError("owned attempt requires claim_token")
        if self.status in {AttemptStatus.CLAIMED, AttemptStatus.RUNNING} and self.lease_expires_at is None:
            raise ValueError("active owned attempt requires lease")
        object.__setattr__(self, "request_snapshot", freeze_json(self.request_snapshot))
        object.__setattr__(self, "outcome_evidence_refs", tuple(self.outcome_evidence_refs))
        object.__setattr__(self, "outcome_metadata", freeze_metadata(self.outcome_metadata))

    def build_retry(self, *, attempt_id: UUID, request_id: str, created_at: datetime) -> ExecutionAttempt:
        """从 terminal non-success Attempt 创建新 identity 的 PENDING child。"""
        if self.status is not AttemptStatus.TERMINAL or self.execution_outcome_kind not in RETRYABLE_OUTCOMES:
            raise ValueError("only terminal non-success attempt can be retried")
        request = ExecutionRequest(
            request_id=request_id,
            run_id=str(self.run_id),
            attempt_id=str(attempt_id),
            case_ref=self.case_ref,
            input_payload=self.execution_request.input_payload,
            timeout=self.execution_request.timeout,
            idempotency_key=self.execution_request.idempotency_key,
            execution_metadata=self.execution_request.execution_metadata,
        )
        return ExecutionAttempt(
            attempt_id=attempt_id,
            project_id=self.project_id,
            run_id=self.run_id,
            case_ref=self.case_ref,
            attempt_no=self.attempt_no + 1,
            retry_of_attempt_id=self.attempt_id,
            execution_target_ref=self.execution_target_ref,
            execution_request=request,
            request_snapshot={
                "input_payload": request.input_payload,
                "timeout_seconds": request.timeout.total_seconds(),
                "execution_metadata": request.execution_metadata,
            },
            created_at=created_at,
        )

    def validate_outcome(self, outcome: ExecutionOutcome) -> None:
        """校验 outcome 属于本 Attempt 的 request。"""
        if outcome.request_id != self.execution_request.request_id:
            raise ValueError("outcome request identity mismatch")
