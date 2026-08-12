"""Run/Attempt persistence use cases 与短事务编排。"""

# ruff: noqa: D415

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.core.evaluation.catalog import (
    DatasetVersion,
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluatorSpec,
    TestCaseVersion,
)
from app.core.evaluation.execution import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionTargetRef,
    OutcomeKind,
    validate_target_capabilities,
)
from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue
from app.core.evaluation.references import ArtifactRef, CapabilityRequirement, CaseVersionRef, VersionRef
from app.core.evaluation.repositories import EvaluationPersistenceUnitOfWork
from app.core.evaluation.results import EvaluationResult, ProvenanceCompleteness
from app.core.evaluation.run_attempts import (
    AttemptClaimLost,
    AttemptStatus,
    EvaluationEntityNotFound,
    EvaluationRun,
    ExecutionAttempt,
    RETRYABLE_OUTCOMES,
    RunNotFinishable,
    RunStatus,
)

UowFactory = Callable[[], EvaluationPersistenceUnitOfWork]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(value: object) -> object:
    if isinstance(value, FrozenDict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _version(ref: VersionRef | None) -> dict[str, str] | None:
    return None if ref is None else {"kind": ref.kind, "opaque_value": ref.opaque_value}


def _artifact(ref: ArtifactRef | None) -> dict[str, object] | None:
    if ref is None:
        return None
    return {
        "artifact_id": ref.artifact_id,
        "digest": ref.digest,
        "media_type": ref.media_type,
        "metadata": _plain(ref.metadata),
    }


def _serialize_evaluator_spec(spec: EvaluatorSpec) -> dict[str, object]:
    snapshot = {
        "evaluator_id": spec.evaluator_id,
        "evaluator_version": spec.evaluator_version,
        "evaluator_kind": spec.evaluator_kind.value,
        "config_ref": _version(spec.config_ref),
        "config_snapshot": _plain(spec.config_snapshot),
        "threshold": spec.threshold,
        "score_direction": spec.score_direction.value,
        "score_range": None if spec.score_range is None else list(spec.score_range),
        "comparison_tolerance": spec.comparison_tolerance,
        "prompt_ref": _version(spec.prompt_ref),
        "required": spec.required,
    }
    if set(snapshot) != {item.name for item in fields(EvaluatorSpec)}:
        raise RuntimeError("EvaluatorSpec snapshot serializer is out of sync with the domain")
    return snapshot


def _serialize_policy(policy: EvaluationPolicy) -> dict[str, object]:
    snapshot = {
        "required_result_missing": policy.required_result_missing.value,
        "evaluator_error": policy.evaluator_error.value,
        "evaluator_inconclusive": policy.evaluator_inconclusive.value,
        "metadata": _plain(policy.metadata),
    }
    if set(snapshot) != {item.name for item in fields(EvaluationPolicy)}:
        raise RuntimeError("EvaluationPolicy snapshot serializer is out of sync with the domain")
    return snapshot


def _serialize_capability(requirement: CapabilityRequirement) -> dict[str, str]:
    snapshot = {"identifier": requirement.identifier}
    if set(snapshot) != {item.name for item in fields(CapabilityRequirement)}:
        raise RuntimeError("CapabilityRequirement snapshot serializer is out of sync with the domain")
    return snapshot


def _serialize_suite_snapshot(suite: EvaluationSuiteVersion) -> dict[str, object]:
    snapshot = {
        "suite_id": suite.suite_id,
        "version": suite.version,
        "created_at": suite.created_at.isoformat(),
        "selected_cases": [
            {"case_id": ref.case_id, "version": ref.version} for ref in suite.case_selection
        ],
        "evaluators": [_serialize_evaluator_spec(spec) for spec in suite.evaluator_specs],
        "evaluation_policy": _serialize_policy(suite.evaluation_policy),
        "target_capability_requirements": [
            _serialize_capability(requirement) for requirement in suite.target_capability_requirements
        ],
        "metadata": _plain(suite.metadata),
    }
    suite_fields = {item.name for item in fields(EvaluationSuiteVersion)}
    serialized_fields = {
        "suite_id",
        "version",
        "case_selection",
        "evaluator_specs",
        "evaluation_policy",
        "created_at",
        "target_capability_requirements",
        "metadata",
    }
    if suite_fields != serialized_fields:
        raise RuntimeError("EvaluationSuiteVersion snapshot serializer is out of sync with the domain")
    return snapshot


def _target_snapshot(ref: ExecutionTargetRef) -> dict[str, object]:
    return {
        "target_id": ref.target_id,
        "target_kind": ref.target_kind,
        "target_version_ref": _version(ref.target_version_ref),
        "config_ref": _version(ref.config_ref),
        "capabilities": list(ref.capabilities),
    }


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Atomic claim 的 typed owner/non-owner 结果。"""

    claimed: bool
    attempt: ExecutionAttempt | None = None
    claim_token: UUID | None = None


class EvaluationPersistenceService:
    """不跨外部执行持有 transaction 的 WP3 application facade。"""

    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def get_run(self, project_id: UUID, run_id: UUID) -> EvaluationRun:
        """按 tenant scope 读取 Run，不暴露 Repository 或 Session。"""
        async with self._uow_factory() as uow:
            run = await uow.runs.get_run(project_id, run_id)
            if run is None:
                raise EvaluationEntityNotFound("run not found")
            return run

    async def get_attempt(self, project_id: UUID, attempt_id: UUID) -> ExecutionAttempt:
        """按 tenant scope 读取 Attempt，不暴露 Repository 或 Session。"""
        async with self._uow_factory() as uow:
            attempt = await uow.attempts.get_attempt(project_id, attempt_id)
            if attempt is None:
                raise EvaluationEntityNotFound("attempt not found")
            return attempt

    async def list_attempts(self, project_id: UUID, run_id: UUID) -> tuple[ExecutionAttempt, ...]:
        """列出指定 tenant Run 的全部 Attempts。"""
        async with self._uow_factory() as uow:
            return await uow.attempts.list_attempts(project_id, run_id)

    async def list_results(
        self,
        project_id: UUID,
        run_id: UUID,
        attempt_id: UUID | None = None,
    ) -> tuple[EvaluationResult, ...]:
        """列出指定 tenant Run/Attempt 的 append-only Results。"""
        async with self._uow_factory() as uow:
            return await uow.results.list_results(project_id, run_id, attempt_id)

    async def create_run(
        self,
        *,
        project_id: UUID,
        dataset: DatasetVersion,
        suite: EvaluationSuiteVersion,
        cases: Mapping[CaseVersionRef, TestCaseVersion],
        target: ExecutionTargetRef,
        timeout: timedelta,
        subject_ref: object | None = None,
        metadata: object | None = None,
    ) -> tuple[EvaluationRun, tuple[ExecutionAttempt, ...]]:
        """校验 selection 后原子创建 Run 与全部 initial Attempts。"""
        if not suite.case_selection:
            raise ValueError("case selection must not be empty")
        if not suite.evaluator_specs:
            raise ValueError("evaluator selection must not be empty")
        dataset_refs = set(dataset.case_version_refs)
        if any(ref not in dataset_refs for ref in suite.case_selection):
            raise ValueError("suite case selection is outside dataset")
        if any(ref not in cases or (cases[ref].case_id, cases[ref].version) != (ref.case_id, ref.version) for ref in suite.case_selection):
            raise ValueError("selected case snapshot is missing or mismatched")
        validate_target_capabilities(suite.target_capability_requirements, target.capabilities)

        run_id = uuid4()
        created_at = _now()
        run = EvaluationRun(
            run_id=run_id,
            project_id=project_id,
            dataset_ref=VersionRef("DATASET", dataset.version),
            suite_ref=VersionRef("SUITE", suite.version),
            execution_target_ref=target,
            dataset_snapshot={
                "dataset_id": dataset.dataset_id,
                "version": dataset.version,
                "cases": [{"case_id": ref.case_id, "version": ref.version} for ref in dataset.case_version_refs],
            },
            suite_snapshot=_serialize_suite_snapshot(suite),
            execution_target_snapshot=_target_snapshot(target),
            subject_ref=subject_ref,
            metadata={} if metadata is None else metadata,
            created_at=created_at,
        )
        attempts: list[ExecutionAttempt] = []
        for ref in suite.case_selection:
            attempt_id = uuid4()
            request_id = str(uuid4())
            request = ExecutionRequest(
                request_id=request_id,
                run_id=str(run_id),
                attempt_id=str(attempt_id),
                case_ref=ref,
                input_payload=cases[ref].input_payload,
                timeout=timeout,
                idempotency_key=str(uuid4()),
                execution_metadata={},
            )
            attempts.append(
                ExecutionAttempt(
                    attempt_id=attempt_id,
                    project_id=project_id,
                    run_id=run_id,
                    case_ref=ref,
                    attempt_no=1,
                    execution_target_ref=target,
                    execution_request=request,
                    request_snapshot={
                        "input_payload": _plain(request.input_payload),
                        "timeout_seconds": timeout.total_seconds(),
                        "execution_metadata": _plain(request.execution_metadata),
                    },
                    created_at=created_at,
                )
            )
        async with self._uow_factory() as uow:
            await uow.runs.add_run_with_attempts(run, tuple(attempts))
            await uow.commit()
        return run, tuple(attempts)

    async def claim_attempt(
        self,
        project_id: UUID,
        attempt_id: UUID,
        *,
        lease: timedelta,
        worker_ref: str | None = None,
        task_ref: str | None = None,
    ) -> ClaimResult:
        """以 candidate UUID 执行 PostgreSQL CAS，并首次启动 Run。"""
        if lease.total_seconds() <= 0:
            raise ValueError("lease must be positive")
        token = uuid4()
        async with self._uow_factory() as uow:
            attempt = await uow.attempts.claim_attempt(project_id, attempt_id, token, lease, worker_ref, task_ref)
            if attempt is None:
                await uow.commit()
                return ClaimResult(False)
            await uow.runs.set_running_if_pending(project_id, attempt.run_id)
            await uow.commit()
            return ClaimResult(True, attempt, token)

    async def start_attempt(self, project_id: UUID, attempt_id: UUID, claim_token: UUID) -> ExecutionAttempt:
        """Fenced CLAIMED -> RUNNING；commit 后才可调用 Target。"""
        async with self._uow_factory() as uow:
            attempt = await uow.attempts.mark_running(project_id, attempt_id, claim_token)
            if attempt is None:
                raise AttemptClaimLost("attempt is not owned, claimed, or lease-valid")
            await uow.commit()
            return attempt

    async def record_outcome(
        self, project_id: UUID, attempt_id: UUID, claim_token: UUID, outcome: ExecutionOutcome
    ) -> ExecutionAttempt:
        """Fenced RUNNING -> TERMINAL 并原子保存完整 outcome。"""
        async with self._uow_factory() as uow:
            current = await uow.attempts.get_attempt(project_id, attempt_id)
            if current is None:
                raise EvaluationEntityNotFound("attempt not found")
            current.validate_outcome(outcome)
            attempt = await uow.attempts.record_outcome(project_id, attempt_id, claim_token, outcome)
            if attempt is None:
                raise AttemptClaimLost("attempt outcome write lost fencing race")
            await uow.commit()
            return attempt

    async def retry_attempt(
        self,
        project_id: UUID,
        attempt_id: UUID,
        *,
        allow_unknown_retry: bool = False,
        reason: str | None = None,
    ) -> ExecutionAttempt:
        """显式创建新 Attempt；绝不 reset source。"""
        async with self._uow_factory() as uow:
            source = await uow.attempts.get_attempt(project_id, attempt_id)
            if source is None:
                raise EvaluationEntityNotFound("attempt not found")
            run = await uow.runs.get_run(project_id, source.run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                raise ValueError("retry requires RUNNING run")
            if source.execution_outcome_kind not in RETRYABLE_OUTCOMES:
                raise ValueError("attempt outcome is not retryable")
            if source.execution_outcome_kind is OutcomeKind.OUTCOME_UNKNOWN:
                if not allow_unknown_retry or not reason or not reason.strip():
                    raise ValueError("OUTCOME_UNKNOWN retry requires explicit authorization and reason")
            child = source.build_retry(attempt_id=uuid4(), request_id=str(uuid4()), created_at=_now())
            await uow.attempts.create_retry(child)
            await uow.commit()
            return child

    async def finalize_result(
        self, project_id: UUID, attempt_id: UUID, claim_token: UUID, result: EvaluationResult
    ) -> None:
        """验证完整 provenance 后 insert-only finalized Result。"""
        async with self._uow_factory() as uow:
            attempt = await uow.attempts.get_attempt(project_id, attempt_id)
            if attempt is None:
                raise EvaluationEntityNotFound("attempt not found")
            run = await uow.runs.get_run(project_id, attempt.run_id)
            if run is None:
                raise EvaluationEntityNotFound("run not found")
            if attempt.claim_token != claim_token or attempt.status is not AttemptStatus.TERMINAL:
                raise AttemptClaimLost("result finalization is not fenced")
            if attempt.execution_outcome_kind is not OutcomeKind.SUCCESS:
                raise ValueError("result requires terminal SUCCESS attempt")
            expected = (
                str(run.run_id), str(attempt.attempt_id), run.dataset_ref.opaque_value,
                attempt.case_ref.case_id, attempt.case_ref.version, run.suite_ref.opaque_value,
                attempt.execution_target_ref.target_id, attempt.execution_request.request_id,
            )
            actual = (
                result.run_id, result.attempt_id, result.dataset_version,
                result.case_id, result.case_version, result.suite_version,
                result.execution_target_id, result.execution_request_id,
            )
            if expected != actual:
                raise ValueError("result provenance does not match persisted run/attempt")
            if result.dataset_id != str(run.dataset_snapshot["dataset_id"]) or result.suite_id != str(run.suite_snapshot["suite_id"]):
                raise ValueError("result catalog provenance mismatch")
            if result.provenance_completeness is not ProvenanceCompleteness.COMPLETE:
                raise ValueError("finalized result requires COMPLETE provenance")
            specs = {
                (item["evaluator_id"], item["evaluator_version"]): item for item in run.suite_snapshot["evaluators"]
            }
            spec = specs.get((result.evaluator_id, result.evaluator_version))
            if spec is None:
                raise ValueError("evaluator provenance is outside suite snapshot")
            if _version(result.config_ref) != spec["config_ref"]:
                raise ValueError("result evaluator config provenance mismatch")
            if _version(result.prompt_ref) != spec["prompt_ref"]:
                raise ValueError("result evaluator prompt provenance mismatch")
            if result.target_version_ref != attempt.execution_target_ref.target_version_ref:
                raise ValueError("result target version provenance mismatch")
            if _artifact(result.output_artifact_ref) != _artifact(attempt.output_artifact_ref):
                raise ValueError("result output artifact provenance mismatch")
            score_range = spec["score_range"]
            if result.score is not None and score_range is not None and not (score_range[0] <= result.score <= score_range[1]):
                raise ValueError("result score is outside evaluator range")
            await uow.results.insert_final_result(project_id, result, claim_token)
            await uow.commit()

    async def list_missing_slots(self, project_id: UUID, run_id: UUID, attempt_id: UUID) -> frozenset[tuple[str, str]]:
        """返回 successful Attempt 尚未 finalized 的 required evaluator slots。"""
        async with self._uow_factory() as uow:
            run = await uow.runs.get_run(project_id, run_id)
            attempt = await uow.attempts.get_attempt(project_id, attempt_id)
            if run is None or attempt is None or attempt.run_id != run_id:
                raise EvaluationEntityNotFound("run/attempt not found")
            existing = await uow.results.list_finalized_slots(project_id, run_id, attempt_id)
            required = frozenset(
                (item["evaluator_id"], item["evaluator_version"])
                for item in run.suite_snapshot["evaluators"] if item["required"]
            )
            done = frozenset((slot[2], slot[3]) for slot in existing)
            return required - done

    async def reconcile_stale(self, project_id: UUID, attempt_id: UUID, *, reason: str) -> ExecutionAttempt:
        """以 DB clock stale CAS terminalize 为 OUTCOME_UNKNOWN。"""
        if not reason.strip():
            raise ValueError("reconciliation reason is required")
        async with self._uow_factory() as uow:
            attempt = await uow.attempts.reconcile_stale(project_id, attempt_id, reason)
            if attempt is None:
                raise AttemptClaimLost("attempt is not stale or reconciliation lost race")
            await uow.commit()
            return attempt

    async def finish_run(self, project_id: UUID, run_id: UUID, *, failure_reason: str = "execution failed") -> RunStatus:
        """按 active > unknown > confirmed failure > slots complete 的优先级结束 Run。"""
        async with self._uow_factory() as uow:
            run = await uow.runs.lock_run(project_id, run_id)
            if run is None:
                raise EvaluationEntityNotFound("run not found")
            if run.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                raise RunNotFinishable("terminal run is immutable")
            latest = await uow.attempts.list_latest_attempts(project_id, run_id)
            if not latest or any(item.status is not AttemptStatus.TERMINAL for item in latest):
                raise RunNotFinishable("run still has active attempts")
            kinds = {item.execution_outcome_kind for item in latest}
            if OutcomeKind.OUTCOME_UNKNOWN in kinds:
                status, reason = RunStatus.OUTCOME_UNKNOWN, "latest attempt outcome is unknown"
            elif kinds & {OutcomeKind.FAILURE, OutcomeKind.TIMEOUT, OutcomeKind.CANCELLED}:
                status, reason = RunStatus.FAILED, failure_reason
            else:
                required = frozenset(
                    (item["evaluator_id"], item["evaluator_version"])
                    for item in run.suite_snapshot["evaluators"] if item["required"]
                )
                for attempt in latest:
                    slots = await uow.results.list_finalized_slots(project_id, run_id, attempt.attempt_id)
                    if required - frozenset((slot[2], slot[3]) for slot in slots):
                        raise RunNotFinishable("required evaluator slots are incomplete")
                status, reason = RunStatus.COMPLETED, None
            if not await uow.runs.finish_run(project_id, run_id, status, reason):
                raise RunNotFinishable("run finish CAS failed")
            await uow.commit()
            return status
