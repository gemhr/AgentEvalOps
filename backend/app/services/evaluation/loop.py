"""Attempt-oriented 最小 Evaluation Application Loop。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from app.core.evaluation.catalog import (
    AssertionSpec,
    EvaluationPolicy,
    EvaluatorKind,
    EvaluatorSpec,
    PolicyDisposition,
    ScoreDirection,
    TestCaseVersion,
)
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.execution import ExecutionOutcome, ExecutionTarget, ExecutionTargetRef, OutcomeKind
from app.core.evaluation.evidence_body_policy import EvidenceBodyMode, parse_evidence_body_policy
from app.core.evaluation.expected_process import parse_expected_process
from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue
from app.core.evaluation.ports import Evaluator, JudgeModelPort
from app.core.evaluation.process_trajectory import (
    PROCESS_TRAJECTORY_KIND,
    parse_process_requirements,
    parse_process_trajectory,
    seal_attempt_trajectory,
    thin_process_trajectory_ref,
    validate_process_requirements,
)
from app.core.evaluation.generation_evidence import FINAL_ANSWER_EVIDENCE_KIND
from app.core.evaluation.rag_artifact import RAG_ARTIFACT_EVIDENCE_KIND
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef, VersionRef
from app.core.evaluation.results import (
    EvaluationResult,
    EvaluationResultDraft,
    EvaluationVerdict,
    ProvenanceCompleteness,
)
from app.core.evaluation.run_attempts import (
    AttemptStatus,
    EvaluationRun,
    ExecutionAttempt,
    ResultAlreadyFinalized,
    RunNotFinishable,
    RunStatus,
    TERMINAL_RUN_STATUSES,
)
from app.services.evaluation.persistence import EvaluationPersistenceService

UuidFactory = Callable[[], UUID]
Clock = Callable[[], datetime]


class EvaluationLoopContractError(RuntimeError):
    """Loop composition、snapshot 或 provenance 合同不一致。"""


class TargetVersionRequired(EvaluationLoopContractError):
    """可评测 Target 必须提供 authoritative version。"""


class EvaluationLoopResult(StrEnum):
    """一次 Attempt loop 调用的 typed control result。"""

    PROGRESSED = "PROGRESSED"
    NOT_CLAIMED = "NOT_CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    ALREADY_COMPLETE = "ALREADY_COMPLETE"
    RUN_NOT_READY = "RUN_NOT_READY"


@dataclass(frozen=True, slots=True)
class ResolvedEvaluator:
    """Evaluator resolver 返回的显式 identity binding。"""

    evaluator_id: str
    evaluator_version: str
    evaluator: Evaluator
    judge_model: JudgeModelPort | None = None


class ExecutionTargetResolver(Protocol):
    """把 persisted runtime-neutral ref 解析为 executable Target。"""

    def resolve(self, target_ref: ExecutionTargetRef) -> ExecutionTarget:
        """解析 Target；未知配置异常原样传播。"""
        ...


class EvaluatorResolver(Protocol):
    """把 persisted EvaluatorSpec 解析为 executable binding。"""

    def resolve(self, spec: EvaluatorSpec) -> ResolvedEvaluator:
        """解析 Evaluator；未知配置异常原样传播。"""
        ...


@dataclass(frozen=True, slots=True)
class _Preflight:
    run: EvaluationRun
    attempt: ExecutionAttempt
    target: ExecutionTarget
    specs: tuple[EvaluatorSpec, ...]
    policy: EvaluationPolicy
    evaluators: tuple[ResolvedEvaluator, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _mapping(value: object, *, field_name: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != keys:
        raise EvaluationLoopContractError(f"invalid persisted {field_name} snapshot")
    return value


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise EvaluationLoopContractError(f"invalid persisted {field_name}")
    return value


def _version(value: object, *, field_name: str, required: bool = False) -> VersionRef | None:
    if value is None:
        if required:
            raise EvaluationLoopContractError(f"persisted {field_name} is required")
        return None
    item = _mapping(value, field_name=field_name, keys=frozenset({"kind", "opaque_value"}))
    try:
        return VersionRef(
            _text(item["kind"], field_name=f"{field_name} kind"),
            _text(item["opaque_value"], field_name=f"{field_name} value"),
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationLoopContractError(f"invalid persisted {field_name}") from exc


def _target_ref(snapshot: FrozenJsonValue) -> ExecutionTargetRef:
    item = _mapping(
        snapshot,
        field_name="execution target",
        keys=frozenset({"target_id", "target_kind", "target_version_ref", "config_ref", "capabilities"}),
    )
    capabilities = item["capabilities"]
    if not isinstance(capabilities, tuple):
        raise EvaluationLoopContractError("invalid persisted target capabilities")
    try:
        return ExecutionTargetRef(
            target_id=_text(item["target_id"], field_name="target id"),
            target_kind=_text(item["target_kind"], field_name="target kind"),
            target_version_ref=_version(item["target_version_ref"], field_name="target version"),
            config_ref=_version(item["config_ref"], field_name="target config"),
            capabilities=tuple(_text(value, field_name="target capability") for value in capabilities),
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationLoopContractError("invalid persisted execution target snapshot") from exc


def _validate_run_target_view(authoritative: ExecutionTargetRef, persisted: ExecutionTargetRef) -> None:
    """校验 Run repository 拥有的 Target 投影字段。"""
    if (
        persisted.target_id != authoritative.target_id
        or persisted.target_kind != authoritative.target_kind
        or persisted.target_version_ref != authoritative.target_version_ref
        or persisted.capabilities != authoritative.capabilities
        or (persisted.config_ref is not None and persisted.config_ref != authoritative.config_ref)
    ):
        raise EvaluationLoopContractError("persisted run execution target view mismatch")


def _validate_attempt_target_view(authoritative: ExecutionTargetRef, persisted: ExecutionTargetRef) -> None:
    """校验 Attempt repository 拥有的 Target 投影字段。"""
    if (
        persisted.target_id != authoritative.target_id
        or persisted.target_kind != authoritative.target_kind
        or persisted.target_version_ref != authoritative.target_version_ref
        or persisted.config_ref != authoritative.config_ref
        or persisted.capabilities != ()
    ):
        raise EvaluationLoopContractError("persisted attempt execution target view mismatch")


def _validate_resolved_target(authoritative: ExecutionTargetRef, resolved: ExecutionTargetRef) -> None:
    """Executable Target 必须保留 authoritative snapshot 的完整 identity。"""
    if resolved != authoritative:
        raise EvaluationLoopContractError("resolved execution target ref mismatch")


def _evaluator_spec(value: object) -> EvaluatorSpec:
    item = _mapping(
        value,
        field_name="evaluator",
        keys=frozenset(
            {
                "result_schema_ref", "comparison_semantics", "required_artifact_kinds", "required_evidence_kinds",
                "evaluator_id",
                "evaluator_version",
                "evaluator_kind",
                "config_ref",
                "config_snapshot",
                "threshold",
                "score_direction",
                "score_range",
                "comparison_tolerance",
                "prompt_ref",
                "required",
            }
        ),
    )
    score_range = item["score_range"]
    if score_range is not None and not isinstance(score_range, tuple):
        raise EvaluationLoopContractError("invalid persisted evaluator score_range")
    if not isinstance(item["required"], bool):
        raise EvaluationLoopContractError("invalid persisted evaluator required flag")
    try:
        return EvaluatorSpec(
            evaluator_id=_text(item["evaluator_id"], field_name="evaluator id"),
            evaluator_version=_text(item["evaluator_version"], field_name="evaluator version"),
            evaluator_kind=EvaluatorKind(_text(item["evaluator_kind"], field_name="evaluator kind")),
            config_ref=_version(item["config_ref"], field_name="evaluator config", required=True),
            config_snapshot=item["config_snapshot"],
            threshold=item["threshold"],
            score_direction=ScoreDirection(_text(item["score_direction"], field_name="score direction")),
            score_range=None if score_range is None else tuple(score_range),
            comparison_tolerance=item["comparison_tolerance"],
            prompt_ref=_version(item["prompt_ref"], field_name="evaluator prompt"),
            required=item["required"],
            result_schema_ref=_version(item["result_schema_ref"], field_name="result schema", required=True),
            comparison_semantics=_text(item["comparison_semantics"], field_name="comparison semantics"),
            required_artifact_kinds=tuple(item["required_artifact_kinds"]),
            required_evidence_kinds=tuple(item["required_evidence_kinds"]),
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationLoopContractError("invalid persisted evaluator snapshot") from exc


def _policy(value: object) -> EvaluationPolicy:
    item = _mapping(
        value,
        field_name="evaluation policy",
        keys=frozenset(
            {"required_result_missing", "evaluator_error", "evaluator_inconclusive", "metadata"}
        ),
    )
    try:
        return EvaluationPolicy(
            required_result_missing=PolicyDisposition(
                _text(item["required_result_missing"], field_name="required result missing policy")
            ),
            evaluator_error=PolicyDisposition(_text(item["evaluator_error"], field_name="evaluator error policy")),
            evaluator_inconclusive=PolicyDisposition(
                _text(item["evaluator_inconclusive"], field_name="evaluator inconclusive policy")
            ),
            metadata=item["metadata"],
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationLoopContractError("invalid persisted evaluation policy snapshot") from exc


def _suite(snapshot: FrozenJsonValue) -> tuple[tuple[EvaluatorSpec, ...], EvaluationPolicy]:
    item = _mapping(
        snapshot,
        field_name="suite",
        keys=frozenset(
            {
                "suite_id",
                "version",
                "created_at",
                "selected_cases",
                "evaluators",
                "evaluation_policy",
                "target_capability_requirements",
                "metadata",
            }
        ),
    )
    try:
        parse_evidence_body_policy(item)
    except (TypeError, ValueError) as exc:
        raise EvaluationLoopContractError("invalid frozen evidence body policy") from exc
    evaluators = item["evaluators"]
    if not isinstance(evaluators, tuple) or not evaluators:
        raise EvaluationLoopContractError("persisted suite evaluators are invalid")
    return tuple(_evaluator_spec(value) for value in evaluators), _policy(item["evaluation_policy"])


def _ordered_unique(*groups: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
    result: list[EvidenceRef] = []
    for group in groups:
        for item in group:
            if item not in result:
                result.append(item)
    return tuple(result)


def _evaluation_input(
    test_case: TestCaseVersion, attempt: ExecutionAttempt, run: EvaluationRun | None = None
) -> EvaluationInput:
    if attempt.execution_outcome_kind is not OutcomeKind.SUCCESS or attempt.output_artifact_ref is None:
        raise EvaluationLoopContractError("terminal SUCCESS attempt requires an output artifact")
    if any(reference.kind == PROCESS_TRAJECTORY_KIND for reference in test_case.evidence_refs):
        raise EvaluationLoopContractError("Case cannot provide reserved process_trajectory evidence")
    trajectory_refs = tuple(ref for ref in attempt.outcome_evidence_refs if ref.kind == PROCESS_TRAJECTORY_KIND)
    trajectory = None
    process_error = None
    if len(trajectory_refs) > 1:
        process_error = "REQUIRED_PROCESS_EVIDENCE_INVALID"
    elif trajectory_refs:
        try:
            trajectory = parse_process_trajectory(trajectory_refs[0])
            if run is not None and (
                trajectory.project_id != str(run.project_id)
                or trajectory.evaluation_run_id != str(run.run_id)
                or trajectory.evaluation_attempt_id != str(attempt.attempt_id)
                or trajectory.dataset_id != str(run.dataset_snapshot["dataset_id"])
                or trajectory.case_ref != attempt.case_ref
                or trajectory.execution_request_id != attempt.execution_request.request_id
                or trajectory.trajectory_id != f"trajectory://{attempt.attempt_id}"
            ):
                raise ValueError("trajectory canonical binding mismatch")
        except (KeyError, TypeError, ValueError):
            trajectory = None
            process_error = "REQUIRED_PROCESS_EVIDENCE_INVALID"
    expected_process = parse_expected_process(test_case.metadata)
    input_metadata: dict[str, object] = {
        "case": test_case.metadata,
        "execution_outcome": attempt.outcome_metadata,
    }
    if expected_process is not None:
        input_metadata["expected_process"] = expected_process.to_dict()
    return EvaluationInput(
        case_ref=attempt.case_ref,
        input_payload=test_case.input_payload,
        expected_output=test_case.expected_output,
        assertion_specs=test_case.assertion_specs,
        actual_artifact=attempt.output_artifact_ref,
        execution_outcome_ref=None,
        evidence_refs=_ordered_unique(test_case.evidence_refs, attempt.outcome_evidence_refs),
        process_trajectory=trajectory,
        process_evidence_error=process_error,
        metadata=input_metadata,
    )


def _case_from_attempt_snapshot(attempt: ExecutionAttempt) -> TestCaseVersion:
    """Rebuild evaluator input from the persisted Attempt work snapshot."""
    snapshot = attempt.request_snapshot.get("case_snapshot")
    if not isinstance(snapshot, Mapping):
        raise EvaluationLoopContractError("persisted attempt case snapshot is missing")
    try:
        return TestCaseVersion(
            case_id=str(snapshot["case_id"]),
            version=str(snapshot["version"]),
            name=str(snapshot["name"]),
            input_payload=snapshot["input_payload"],
            expected_output=snapshot.get("expected_output"),
            created_at=datetime.fromisoformat(str(snapshot["created_at"])),
            assertion_specs=tuple(
                AssertionSpec(
                    assertion_id=str(item["assertion_id"]),
                    kind=str(item["kind"]),
                    config=item.get("config", {}),
                    required=bool(item.get("required", True)),
                )
                for item in snapshot.get("assertion_specs", ())
            ),
            fixture_refs=tuple(
                ArtifactRef(
                    artifact_id=str(item["artifact_id"]),
                    digest=item.get("digest"),
                    media_type=item.get("media_type"),
                    metadata=item.get("metadata", {}),
                )
                for item in snapshot.get("fixture_refs", ())
            ),
            evidence_refs=tuple(
                EvidenceRef(
                    kind=str(item["kind"]),
                    identifier=str(item["identifier"]),
                    media_type=item.get("media_type"),
                    schema_version=item.get("schema_version"),
                    metadata=item.get("metadata", {}),
                )
                for item in snapshot.get("evidence_refs", ())
            ),
            tags=tuple(snapshot.get("tags", ())),
            metadata=snapshot.get("metadata", {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationLoopContractError("persisted attempt case snapshot is invalid") from exc


def _validate_draft(draft: EvaluationResultDraft, spec: EvaluatorSpec) -> None:
    if not isinstance(draft, EvaluationResultDraft):
        raise EvaluationLoopContractError("evaluator returned an invalid draft type")
    if (draft.evaluator_id, draft.evaluator_version) != (spec.evaluator_id, spec.evaluator_version):
        raise EvaluationLoopContractError("evaluator draft identity mismatch")
    if draft.config_ref != spec.config_ref:
        raise EvaluationLoopContractError("evaluator draft config mismatch")
    if draft.prompt_ref != spec.prompt_ref:
        raise EvaluationLoopContractError("evaluator draft prompt mismatch")


def _policy_verdict(disposition: PolicyDisposition) -> EvaluationVerdict:
    return EvaluationVerdict.FAIL if disposition is PolicyDisposition.FAIL else EvaluationVerdict.INCONCLUSIVE


def _normalize_draft(
    draft: EvaluationResultDraft,
    policy: EvaluationPolicy,
) -> tuple[EvaluationResultDraft, str]:
    if draft.verdict is EvaluationVerdict.ERROR:
        final = _policy_verdict(policy.evaluator_error)
        source = "EVALUATOR_ERROR"
    elif draft.verdict is EvaluationVerdict.INCONCLUSIVE:
        final = _policy_verdict(policy.evaluator_inconclusive)
        source = "EVALUATOR_INCONCLUSIVE"
    else:
        final = draft.verdict
        source = "UNCHANGED"
    return (
        EvaluationResultDraft(
            evaluator_id=draft.evaluator_id,
            evaluator_version=draft.evaluator_version,
            config_ref=draft.config_ref,
            verdict=final,
            reason=draft.reason,
            score=draft.score,
            evidence_refs=draft.evidence_refs,
            prompt_ref=draft.prompt_ref,
            metadata=draft.metadata,
        ),
        source,
    )


def _process_error_draft(spec: EvaluatorSpec, reason_code: str) -> EvaluationResultDraft:
    return EvaluationResultDraft(
        evaluator_id=spec.evaluator_id,
        evaluator_version=spec.evaluator_version,
        config_ref=spec.config_ref,
        prompt_ref=spec.prompt_ref,
        verdict=EvaluationVerdict.ERROR,
        score=None,
        reason=reason_code,
        metadata={"source_status": "EVALUATOR_ERROR", "process_evidence_error": reason_code},
    )


def _result(
    *,
    run: EvaluationRun,
    attempt: ExecutionAttempt,
    test_case: TestCaseVersion,
    spec: EvaluatorSpec,
    draft: EvaluationResultDraft,
    input_value: EvaluationInput,
    normalization_source: str,
    result_id: UUID,
    created_at: datetime,
) -> EvaluationResult:
    if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
        raise EvaluationLoopContractError("result clock must return a UTC datetime")
    target_version = attempt.execution_target_ref.target_version_ref
    if target_version is None or attempt.output_artifact_ref is None:
        raise EvaluationLoopContractError("complete result provenance is unavailable")
    return EvaluationResult(
        result_id=str(result_id),
        run_id=str(run.run_id),
        attempt_id=str(attempt.attempt_id),
        dataset_id=str(run.dataset_snapshot["dataset_id"]),
        dataset_version=run.dataset_ref.opaque_value,
        case_id=attempt.case_ref.case_id,
        case_version=attempt.case_ref.version,
        suite_id=str(run.suite_snapshot["suite_id"]),
        suite_version=run.suite_ref.opaque_value,
        evaluator_id=spec.evaluator_id,
        evaluator_version=spec.evaluator_version,
        config_ref=spec.config_ref,
        prompt_ref=spec.prompt_ref,
        execution_target_id=attempt.execution_target_ref.target_id,
        target_version_ref=target_version,
        execution_request_id=attempt.execution_request.request_id,
        verdict=draft.verdict,
        reason=draft.reason,
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        output_artifact_ref=attempt.output_artifact_ref,
        score=draft.score,
        evidence_refs=_result_support_refs(input_value.evidence_refs, draft.evidence_refs, input_value.process_trajectory),
        metadata={
            "case": test_case.metadata,
            "execution_outcome": attempt.outcome_metadata,
            "evaluator": draft.metadata,
            "policy_normalization": {
                "source": normalization_source,
                "final_verdict": draft.verdict.value,
            },
        },
        created_at=created_at,
    )


def _result_support_refs(
    input_refs: tuple[EvidenceRef, ...],
    draft_refs: tuple[EvidenceRef, ...],
    trajectory: object | None,
) -> tuple[EvidenceRef, ...]:
    refs = tuple(
        ref for ref in _ordered_unique(input_refs, draft_refs)
        if ref.kind not in {PROCESS_TRAJECTORY_KIND, RAG_ARTIFACT_EVIDENCE_KIND, FINAL_ANSWER_EVIDENCE_KIND}
    )
    if trajectory is None:
        return refs
    from app.core.evaluation.process_trajectory import ProcessTrajectoryV1

    if not isinstance(trajectory, ProcessTrajectoryV1):
        raise EvaluationLoopContractError("validated process trajectory has invalid type")
    return (*refs, thin_process_trajectory_ref(trajectory))


class EvaluationLoopService:
    """只推进一个 caller 指定 Attempt 的最小 Application orchestrator。"""

    def __init__(
        self,
        persistence: EvaluationPersistenceService,
        target_resolver: ExecutionTargetResolver,
        evaluator_resolver: EvaluatorResolver,
        *,
        uuid_factory: UuidFactory = uuid4,
        clock: Clock = _utc_now,
    ) -> None:
        self._persistence = persistence
        self._target_resolver = target_resolver
        self._evaluator_resolver = evaluator_resolver
        self._uuid_factory = uuid_factory
        self._clock = clock

    async def execute_attempt(
        self,
        project_id: UUID,
        attempt_id: UUID,
        test_case: TestCaseVersion | None = None,
        *,
        lease: timedelta,
        worker_ref: str | None = None,
        task_ref: str | None = None,
        finalize_run: bool = True,
    ) -> EvaluationLoopResult:
        """推进一个 Attempt；不遍历 Run、不自动 retry/reconcile。"""
        attempt = await self._persistence.get_attempt(project_id, attempt_id)
        test_case = test_case or _case_from_attempt_snapshot(attempt)
        run = await self._persistence.get_run(project_id, attempt.run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return EvaluationLoopResult.ALREADY_COMPLETE
        if attempt.status in {AttemptStatus.CLAIMED, AttemptStatus.RUNNING}:
            return EvaluationLoopResult.IN_PROGRESS

        preflight = self._preflight(project_id, run, attempt, test_case)
        if attempt.status is AttemptStatus.PENDING:
            claim = await self._persistence.claim_attempt(
                project_id,
                attempt_id,
                lease=lease,
                worker_ref=worker_ref,
                task_ref=task_ref,
            )
            if not claim.claimed:
                return EvaluationLoopResult.NOT_CLAIMED
            if claim.claim_token is None:
                raise EvaluationLoopContractError("claimed attempt has no claim token")
            attempt = await self._persistence.start_attempt(project_id, attempt_id, claim.claim_token)
            outcome = await preflight.target.execute(attempt.execution_request)
            if not isinstance(outcome, ExecutionOutcome):
                raise EvaluationLoopContractError("target returned an invalid outcome type")
            if any(reference.kind == PROCESS_TRAJECTORY_KIND for reference in outcome.evidence_refs):
                raise EvaluationLoopContractError("Target cannot provide reserved process_trajectory evidence")
            trajectory_ref = seal_attempt_trajectory(
                run, attempt, _target_ref(run.execution_target_snapshot), outcome, sealed_at=self._clock()
            )
            body_policy = parse_evidence_body_policy(run.suite_snapshot)
            durable_refs = tuple(
                reference for reference in outcome.evidence_refs
                if reference.kind not in {RAG_ARTIFACT_EVIDENCE_KIND, FINAL_ANSWER_EVIDENCE_KIND}
                or (
                    reference.kind == FINAL_ANSWER_EVIDENCE_KIND
                    and body_policy.mode is EvidenceBodyMode.APPROVED_SAFE_SNAPSHOT
                    and "final_answer.body" in body_policy.approved_fields
                )
            )
            outcome = replace(outcome, evidence_refs=(*durable_refs, trajectory_ref))
            attempt.validate_outcome(outcome)
            attempt = await self._persistence.record_outcome(project_id, attempt_id, claim.claim_token, outcome)
        elif attempt.status is not AttemptStatus.TERMINAL:
            raise EvaluationLoopContractError("unknown attempt lifecycle state")

        if attempt.claim_token is None:
            raise EvaluationLoopContractError("terminal attempt has no claim token")
        if attempt.execution_outcome_kind is not OutcomeKind.SUCCESS:
            return await self._finish(project_id, run.run_id, preflight.specs) if finalize_run else EvaluationLoopResult.PROGRESSED

        input_value = _evaluation_input(test_case, attempt, run)
        existing = await self._persistence.list_results(project_id, run.run_id, attempt.attempt_id)
        finalized = {(item.evaluator_id, item.evaluator_version) for item in existing}
        for spec, binding in zip(preflight.specs, preflight.evaluators, strict=True):
            slot = (spec.evaluator_id, spec.evaluator_version)
            if slot in finalized:
                continue
            draft = await self._evaluate(binding.evaluator, spec, input_value, binding.judge_model)
            normalized, source = _normalize_draft(draft, preflight.policy)
            result = _result(
                run=run,
                attempt=attempt,
                test_case=test_case,
                spec=spec,
                draft=normalized,
                input_value=input_value,
                normalization_source=source,
                result_id=self._uuid_factory(),
                created_at=self._clock(),
            )
            try:
                await self._persistence.finalize_result(
                    project_id,
                    attempt.attempt_id,
                    attempt.claim_token,
                    result,
                )
            except ResultAlreadyFinalized:
                current = await self._persistence.list_results(project_id, run.run_id, attempt.attempt_id)
                if not any(self._same_authoritative_slot(item, result) for item in current):
                    raise
            finalized.add(slot)

        return await self._finish(project_id, run.run_id, preflight.specs) if finalize_run else EvaluationLoopResult.PROGRESSED

    def _preflight(
        self,
        project_id: UUID,
        run: EvaluationRun,
        attempt: ExecutionAttempt,
        test_case: TestCaseVersion,
    ) -> _Preflight:
        if run.project_id != project_id or attempt.project_id != project_id or attempt.run_id != run.run_id:
            raise EvaluationLoopContractError("run/attempt ownership mismatch")
        if attempt.case_ref != _test_case_ref(test_case):
            raise EvaluationLoopContractError("caller TestCase identity mismatch")
        persisted_case = _case_from_attempt_snapshot(attempt)
        if persisted_case.semantic_digest != test_case.semantic_digest:
            raise EvaluationLoopContractError("caller TestCase semantic content mismatch")
        if attempt.execution_request.input_payload != persisted_case.input_payload:
            raise EvaluationLoopContractError("persisted TestCase input does not match execution request")
        authoritative_target = _target_ref(run.execution_target_snapshot)
        _validate_run_target_view(authoritative_target, run.execution_target_ref)
        _validate_attempt_target_view(authoritative_target, attempt.execution_target_ref)
        if authoritative_target.target_version_ref is None:
            raise TargetVersionRequired("authoritative target version is required")
        target = self._target_resolver.resolve(authoritative_target)
        _validate_resolved_target(authoritative_target, target.target_ref)
        specs, policy = _suite(run.suite_snapshot)
        evaluators: list[ResolvedEvaluator] = []
        for spec in specs:
            if spec.result_schema_ref != VersionRef("evaluation_result", "v1") or (
                spec.comparison_semantics != "verdict_transition.v1"
            ):
                raise EvaluationLoopContractError("unsupported evaluator result schema or comparison semantics")
            binding = self._evaluator_resolver.resolve(spec)
            if (binding.evaluator_id, binding.evaluator_version) != (spec.evaluator_id, spec.evaluator_version):
                raise EvaluationLoopContractError("resolved evaluator identity mismatch")
            evaluators.append(binding)
        return _Preflight(run, attempt, target, specs, policy, tuple(evaluators))

    async def _evaluate(
        self,
        evaluator: Evaluator,
        spec: EvaluatorSpec,
        input_value: EvaluationInput,
        judge_model: JudgeModelPort | None,
    ) -> EvaluationResultDraft:
        try:
            if PROCESS_TRAJECTORY_KIND in spec.required_evidence_kinds:
                if input_value.process_evidence_error is not None:
                    return _process_error_draft(spec, input_value.process_evidence_error)
                if input_value.process_trajectory is None:
                    return _process_error_draft(spec, "REQUIRED_PROCESS_EVIDENCE_MISSING")
                try:
                    requirements = parse_process_requirements(spec.config_snapshot.get("process_evidence_requirements"))
                except (KeyError, TypeError, ValueError):
                    return _process_error_draft(spec, "REQUIRED_PROCESS_EVIDENCE_INVALID")
                process_error = validate_process_requirements(input_value.process_trajectory, requirements)
                if process_error is not None:
                    return _process_error_draft(spec, process_error)
            if not set(spec.required_evidence_kinds).issubset({ref.kind for ref in input_value.evidence_refs}) or (
                spec.required_artifact_kinds and (
                    input_value.actual_artifact is None
                    or input_value.actual_artifact.media_type not in spec.required_artifact_kinds
                )
            ):
                return EvaluationResultDraft(
                    evaluator_id=spec.evaluator_id, evaluator_version=spec.evaluator_version,
                    config_ref=spec.config_ref, prompt_ref=spec.prompt_ref, verdict=EvaluationVerdict.ERROR,
                    reason="required_evaluator_input_missing", metadata={"source_status": "REQUIRED_EVIDENCE_MISSING"},
                )
            draft = await evaluator.evaluate(input_value, EvaluatorContext(spec, judge_model=judge_model))
        except Exception as exc:
            draft = EvaluationResultDraft(
                evaluator_id=spec.evaluator_id,
                evaluator_version=spec.evaluator_version,
                config_ref=spec.config_ref,
                prompt_ref=spec.prompt_ref,
                verdict=EvaluationVerdict.ERROR,
                reason=f"evaluator_exception:{type(exc).__name__}",
                metadata={
                    "source_status": "EVALUATOR_EXCEPTION",
                    "exception_type": type(exc).__name__,
                },
            )
        _validate_draft(draft, spec)
        if any(reference.kind == PROCESS_TRAJECTORY_KIND for reference in draft.evidence_refs):
            return _process_error_draft(spec, "REQUIRED_PROCESS_EVIDENCE_INVALID")
        if PROCESS_TRAJECTORY_KIND in spec.required_evidence_kinds and draft.evidence_refs:
            return _process_error_draft(spec, "REQUIRED_PROCESS_EVIDENCE_INVALID")
        return draft

    async def _finish(
        self,
        project_id: UUID,
        run_id: UUID,
        specs: tuple[EvaluatorSpec, ...],
    ) -> EvaluationLoopResult:
        try:
            await self._persistence.finish_run(project_id, run_id)
            return EvaluationLoopResult.PROGRESSED
        except RunNotFinishable:
            run = await self._persistence.get_run(project_id, run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return EvaluationLoopResult.ALREADY_COMPLETE
            attempts = await self._persistence.list_attempts(project_id, run_id)
            latest = _latest_attempts(attempts)
            if not latest or any(item.status is not AttemptStatus.TERMINAL for item in latest):
                return EvaluationLoopResult.RUN_NOT_READY
            required = {(spec.evaluator_id, spec.evaluator_version) for spec in specs if spec.required}
            for attempt in latest:
                if attempt.execution_outcome_kind is not OutcomeKind.SUCCESS:
                    continue
                results = await self._persistence.list_results(project_id, run_id, attempt.attempt_id)
                done = {(item.evaluator_id, item.evaluator_version) for item in results}
                if required - done:
                    return EvaluationLoopResult.RUN_NOT_READY
            raise

    @staticmethod
    def _same_authoritative_slot(existing: EvaluationResult, candidate: EvaluationResult) -> bool:
        return (
            existing.run_id,
            existing.attempt_id,
            existing.dataset_id,
            existing.dataset_version,
            existing.case_id,
            existing.case_version,
            existing.suite_id,
            existing.suite_version,
            existing.evaluator_id,
            existing.evaluator_version,
            existing.config_ref,
            existing.prompt_ref,
            existing.execution_target_id,
            existing.target_version_ref,
            existing.execution_request_id,
            existing.output_artifact_ref,
        ) == (
            candidate.run_id,
            candidate.attempt_id,
            candidate.dataset_id,
            candidate.dataset_version,
            candidate.case_id,
            candidate.case_version,
            candidate.suite_id,
            candidate.suite_version,
            candidate.evaluator_id,
            candidate.evaluator_version,
            candidate.config_ref,
            candidate.prompt_ref,
            candidate.execution_target_id,
            candidate.target_version_ref,
            candidate.execution_request_id,
            candidate.output_artifact_ref,
        )


def _test_case_ref(test_case: TestCaseVersion) -> CaseVersionRef:
    """构造 caller Case 的 typed identity，保持 preflight 比较集中。"""
    return CaseVersionRef(test_case.case_id, test_case.version)


def _latest_attempts(attempts: tuple[ExecutionAttempt, ...]) -> tuple[ExecutionAttempt, ...]:
    latest: dict[object, ExecutionAttempt] = {}
    for attempt in attempts:
        current = latest.get(attempt.case_ref)
        if current is None or current.attempt_no < attempt.attempt_no:
            latest[attempt.case_ref] = attempt
    return tuple(latest.values())
