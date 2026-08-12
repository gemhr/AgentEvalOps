"""Attempt-oriented EvaluationLoopService application tests。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.evaluation import (
    ArtifactRef,
    AssertionSpec,
    CaseVersionRef,
    EvaluationPolicy,
    EvaluationResultDraft,
    EvaluationVerdict,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionTargetRef,
    EvidenceRef,
    OutcomeKind,
    PolicyDisposition,
    ScoreDirection,
    TestCaseVersion as CaseVersion,
    VersionRef,
)
from app.core.evaluation.run_attempts import (
    AttemptStatus,
    EvaluationRun,
    ExecutionAttempt,
    ResultAlreadyFinalized,
    RunNotFinishable,
    RunStatus,
)
from app.services.evaluation import (
    ClaimResult,
    EvaluationLoopContractError,
    EvaluationLoopResult,
    EvaluationLoopService,
    ResolvedEvaluator,
    TargetVersionRequired,
)

NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
PROJECT_ID = UUID("10000000-0000-4000-a000-000000000001")
CASE_REF = CaseVersionRef("case-a", "v1")
CASE_EVIDENCE = EvidenceRef("CASE", "shared")
OUTCOME_EVIDENCE = EvidenceRef("OUTCOME", "outcome")
DRAFT_EVIDENCE = EvidenceRef("DRAFT", "draft")


def spec(
    evaluator_id: str,
    *,
    required: bool = True,
) -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id,
        "v1",
        EvaluatorKind.DETERMINISTIC,
        VersionRef("config", f"{evaluator_id}-config"),
        ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot={"threshold": 0.5},
        score_range=(0.0, 1.0),
        prompt_ref=VersionRef("prompt", f"{evaluator_id}-prompt"),
        required=required,
    )


def serialize_spec(value: EvaluatorSpec) -> dict[str, object]:
    return {
        "evaluator_id": value.evaluator_id,
        "evaluator_version": value.evaluator_version,
        "evaluator_kind": value.evaluator_kind.value,
        "config_ref": {"kind": value.config_ref.kind, "opaque_value": value.config_ref.opaque_value},
        "config_snapshot": value.config_snapshot,
        "threshold": value.threshold,
        "score_direction": value.score_direction.value,
        "score_range": value.score_range,
        "comparison_tolerance": value.comparison_tolerance,
        "prompt_ref": {
            "kind": value.prompt_ref.kind,
            "opaque_value": value.prompt_ref.opaque_value,
        },
        "required": value.required,
    }


def make_context(
    *,
    specs: tuple[EvaluatorSpec, ...] | None = None,
    status: AttemptStatus = AttemptStatus.PENDING,
    outcome_kind: OutcomeKind | None = None,
    target_version: VersionRef | None = VersionRef("git", "abc123"),
    target_capabilities: tuple[str, ...] = ("TEXT",),
    target_config: VersionRef | None = VersionRef("target-config", "v1"),
    policy: EvaluationPolicy | None = None,
) -> tuple[EvaluationRun, ExecutionAttempt, CaseVersion]:
    specs = specs or (spec("eval-a"),)
    policy = policy or EvaluationPolicy()
    run_id = uuid4()
    attempt_id = uuid4()
    authoritative_target = ExecutionTargetRef(
        "fixture-target",
        "FIXTURE",
        target_version,
        target_capabilities,
        target_config,
    )
    run_target_view = replace(authoritative_target, config_ref=None)
    attempt_target_view = replace(authoritative_target, capabilities=())
    request = ExecutionRequest(
        str(uuid4()),
        str(run_id),
        str(attempt_id),
        CASE_REF,
        {"question": ["value"]},
        timedelta(seconds=30),
        "stable-key",
    )
    terminal = status is AttemptStatus.TERMINAL
    token = uuid4() if status is not AttemptStatus.PENDING else None
    run = EvaluationRun(
        run_id=run_id,
        project_id=PROJECT_ID,
        dataset_ref=VersionRef("DATASET", "d1"),
        suite_ref=VersionRef("SUITE", "s1"),
        execution_target_ref=run_target_view,
        dataset_snapshot={"dataset_id": "dataset", "version": "d1", "cases": []},
        suite_snapshot={
            "suite_id": "suite",
            "version": "s1",
            "created_at": NOW.isoformat(),
            "selected_cases": ({"case_id": "case-a", "version": "v1"},),
            "evaluators": tuple(serialize_spec(value) for value in specs),
            "evaluation_policy": {
                "required_result_missing": policy.required_result_missing.value,
                "evaluator_error": policy.evaluator_error.value,
                "evaluator_inconclusive": policy.evaluator_inconclusive.value,
                "metadata": policy.metadata,
            },
            "target_capability_requirements": (),
            "metadata": {},
        },
        execution_target_snapshot={
            "target_id": authoritative_target.target_id,
            "target_kind": authoritative_target.target_kind,
            "target_version_ref": None
            if target_version is None
            else {"kind": target_version.kind, "opaque_value": target_version.opaque_value},
            "config_ref": None
            if target_config is None
            else {"kind": target_config.kind, "opaque_value": target_config.opaque_value},
            "capabilities": authoritative_target.capabilities,
        },
        created_at=NOW,
        status=RunStatus.RUNNING if status is not AttemptStatus.PENDING else RunStatus.PENDING,
        started_at=NOW if status is not AttemptStatus.PENDING else None,
    )
    artifact = ArtifactRef("actual", "sha256:actual", "application/json") if outcome_kind is OutcomeKind.SUCCESS else None
    attempt = ExecutionAttempt(
        attempt_id=attempt_id,
        project_id=PROJECT_ID,
        run_id=run_id,
        case_ref=CASE_REF,
        attempt_no=1,
        execution_target_ref=attempt_target_view,
        execution_request=request,
        request_snapshot={"input_payload": request.input_payload, "timeout_seconds": 30, "execution_metadata": {}},
        created_at=NOW,
        status=status,
        claim_token=token,
        claimed_at=NOW if token else None,
        started_at=NOW if status in {AttemptStatus.RUNNING, AttemptStatus.TERMINAL} else None,
        finished_at=NOW if terminal else None,
        lease_expires_at=NOW + timedelta(minutes=5) if status in {AttemptStatus.CLAIMED, AttemptStatus.RUNNING} else None,
        execution_outcome_kind=outcome_kind,
        output_artifact_ref=artifact,
        outcome_evidence_refs=(CASE_EVIDENCE, OUTCOME_EVIDENCE) if terminal else (),
        outcome_metadata={"shared": "outcome", "outcome_only": True} if terminal else {},
        error_category=None if outcome_kind in {None, OutcomeKind.SUCCESS} else "TARGET",
        reason=None if outcome_kind in {None, OutcomeKind.SUCCESS} else outcome_kind.value,
    )
    test_case = CaseVersion(
        "case-a",
        "v1",
        "case",
        {"question": ["value"]},
        NOW,
        expected_output={"answer": 42},
        assertion_specs=(AssertionSpec("answer", "EXACT"),),
        evidence_refs=(CASE_EVIDENCE, CASE_EVIDENCE),
        metadata={"shared": "case", "case_only": True},
    )
    return run, attempt, test_case


def authoritative_target_ref(run: EvaluationRun) -> ExecutionTargetRef:
    snapshot = run.execution_target_snapshot
    version = snapshot["target_version_ref"]
    config = snapshot["config_ref"]
    return ExecutionTargetRef(
        str(snapshot["target_id"]),
        str(snapshot["target_kind"]),
        None if version is None else VersionRef(str(version["kind"]), str(version["opaque_value"])),
        tuple(str(value) for value in snapshot["capabilities"]),
        None if config is None else VersionRef(str(config["kind"]), str(config["opaque_value"])),
    )


class RecordingTarget:
    def __init__(self, target_ref: ExecutionTargetRef, outcome_kind: OutcomeKind, *, error: Exception | None = None):
        self.target_ref = target_ref
        self.outcome_kind = outcome_kind
        self.error = error
        self.requests: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.outcome_kind is OutcomeKind.SUCCESS:
            return ExecutionOutcome(
                request.request_id,
                self.outcome_kind,
                NOW,
                NOW,
                ArtifactRef("actual", "sha256:actual", "application/json"),
                (CASE_EVIDENCE, OUTCOME_EVIDENCE),
                metadata={"shared": "outcome", "outcome_only": True},
            )
        return ExecutionOutcome(
            request.request_id,
            self.outcome_kind,
            NOW,
            NOW,
            evidence_refs=(OUTCOME_EVIDENCE,),
            error_category="TARGET",
            reason=self.outcome_kind.value,
        )


class TargetResolver:
    def __init__(self, target: RecordingTarget):
        self.target = target
        self.calls: list[ExecutionTargetRef] = []

    def resolve(self, target_ref: ExecutionTargetRef) -> RecordingTarget:
        self.calls.append(target_ref)
        return self.target


class DraftEvaluator:
    def __init__(
        self,
        value: EvaluatorSpec,
        *,
        verdict: EvaluationVerdict = EvaluationVerdict.PASS,
        error: Exception | None = None,
        changes: dict[str, object] | None = None,
    ):
        self.spec = value
        self.verdict = verdict
        self.error = error
        self.changes = changes or {}
        self.calls: list[object] = []

    async def evaluate(self, evaluation_input, context):
        self.calls.append(evaluation_input)
        if self.error is not None:
            raise self.error
        values: dict[str, object] = {
            "evaluator_id": self.spec.evaluator_id,
            "evaluator_version": self.spec.evaluator_version,
            "config_ref": self.spec.config_ref,
            "prompt_ref": self.spec.prompt_ref,
            "verdict": self.verdict,
            "reason": "evaluated",
            "score": 0.0 if self.verdict is EvaluationVerdict.FAIL else 1.0,
            "evidence_refs": (OUTCOME_EVIDENCE, DRAFT_EVIDENCE),
            "metadata": {"shared": "evaluator", "evaluator_only": True},
        }
        values.update(self.changes)
        return EvaluationResultDraft(**values)


class EvaluatorResolver:
    def __init__(self, evaluators: dict[str, DraftEvaluator], *, mismatch: bool = False):
        self.evaluators = evaluators
        self.mismatch = mismatch
        self.calls: list[str] = []

    def resolve(self, value: EvaluatorSpec) -> ResolvedEvaluator:
        self.calls.append(value.evaluator_id)
        evaluator = self.evaluators[value.evaluator_id]
        return ResolvedEvaluator(
            "mismatch" if self.mismatch else value.evaluator_id,
            value.evaluator_version,
            evaluator,
        )


class FakePersistence:
    def __init__(self, run: EvaluationRun, attempt: ExecutionAttempt):
        self.run = run
        self.attempts = [attempt]
        self.results = []
        self.calls: list[str] = []
        self.claim_wins = True
        self.finish_not_ready = False
        self.duplicate_mode: str | None = None

    @property
    def attempt(self) -> ExecutionAttempt:
        return self.attempts[0]

    async def get_run(self, project_id, run_id):
        self.calls.append("get_run")
        return self.run

    async def get_attempt(self, project_id, attempt_id):
        self.calls.append("get_attempt")
        return self.attempt

    async def list_attempts(self, project_id, run_id):
        self.calls.append("list_attempts")
        return tuple(self.attempts)

    async def list_results(self, project_id, run_id, attempt_id=None):
        self.calls.append("list_results")
        return tuple(item for item in self.results if attempt_id is None or item.attempt_id == str(attempt_id))

    async def claim_attempt(self, project_id, attempt_id, **kwargs):
        self.calls.append("claim")
        if not self.claim_wins:
            return ClaimResult(False)
        token = uuid4()
        self.attempts[0] = replace(
            self.attempt,
            status=AttemptStatus.CLAIMED,
            claim_token=token,
            claimed_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=5),
        )
        self.run = replace(self.run, status=RunStatus.RUNNING, started_at=NOW)
        return ClaimResult(True, self.attempt, token)

    async def start_attempt(self, project_id, attempt_id, token):
        self.calls.append("start")
        self.attempts[0] = replace(self.attempt, status=AttemptStatus.RUNNING, started_at=NOW)
        return self.attempt

    async def record_outcome(self, project_id, attempt_id, token, outcome):
        self.calls.append("record")
        self.attempts[0] = replace(
            self.attempt,
            status=AttemptStatus.TERMINAL,
            finished_at=outcome.finished_at,
            lease_expires_at=None,
            execution_outcome_kind=outcome.kind,
            output_artifact_ref=outcome.output_artifact_ref,
            outcome_evidence_refs=outcome.evidence_refs,
            outcome_metadata=outcome.metadata,
            error_category=outcome.error_category,
            reason=outcome.reason,
        )
        return self.attempt

    async def finalize_result(self, project_id, attempt_id, token, result):
        self.calls.append(f"finalize:{result.evaluator_id}")
        if self.duplicate_mode is not None:
            if self.duplicate_mode == "exact":
                self.results.append(replace(result, result_id=str(uuid4())))
            else:
                self.results.append(
                    replace(result, result_id=str(uuid4()), config_ref=VersionRef("config", "foreign"))
                )
            self.duplicate_mode = None
            raise ResultAlreadyFinalized("race")
        self.results.append(result)

    async def finish_run(self, project_id, run_id):
        self.calls.append("finish")
        if self.finish_not_ready:
            raise RunNotFinishable("not ready")
        kind = self.attempt.execution_outcome_kind
        if kind is OutcomeKind.OUTCOME_UNKNOWN:
            status = RunStatus.OUTCOME_UNKNOWN
        elif kind in {OutcomeKind.FAILURE, OutcomeKind.TIMEOUT, OutcomeKind.CANCELLED}:
            status = RunStatus.FAILED
        else:
            status = RunStatus.COMPLETED
        self.run = replace(self.run, status=status, finished_at=NOW, status_reason=None if status is RunStatus.COMPLETED else "done")
        return status


def make_loop(
    run: EvaluationRun,
    attempt: ExecutionAttempt,
    specs: tuple[EvaluatorSpec, ...],
    *,
    target_kind: OutcomeKind = OutcomeKind.SUCCESS,
    target_error: Exception | None = None,
    evaluators: dict[str, DraftEvaluator] | None = None,
    target_override: RecordingTarget | None = None,
    resolver_mismatch: bool = False,
):
    persistence = FakePersistence(run, attempt)
    target = target_override or RecordingTarget(authoritative_target_ref(run), target_kind, error=target_error)
    evaluators = evaluators or {value.evaluator_id: DraftEvaluator(value) for value in specs}
    loop = EvaluationLoopService(
        persistence,
        TargetResolver(target),
        EvaluatorResolver(evaluators, mismatch=resolver_mismatch),
        uuid_factory=uuid4,
        clock=lambda: NOW,
    )
    return loop, persistence, target, evaluators


@pytest.mark.asyncio
async def test_pending_success_runs_multi_evaluator_in_order_and_assembles_namespaced_facts():
    specs = (spec("required"), spec("optional", required=False))
    run, attempt, case = make_context(specs=specs)
    evaluators = {
        "required": DraftEvaluator(specs[0], verdict=EvaluationVerdict.FAIL),
        "optional": DraftEvaluator(specs[1]),
    }
    loop, persistence, target, _ = make_loop(run, attempt, specs, evaluators=evaluators)

    result = await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))

    assert result is EvaluationLoopResult.PROGRESSED
    assert persistence.calls.index("claim") < persistence.calls.index("start") < persistence.calls.index("record")
    assert persistence.calls[-1] == "finish"
    assert [item.evaluator_id for item in persistence.results] == ["required", "optional"]
    assert persistence.results[0].verdict is EvaluationVerdict.FAIL
    assert persistence.run.status is RunStatus.COMPLETED
    assert target.requests == [attempt.execution_request]
    evaluation_input = evaluators["required"].calls[0]
    assert evaluation_input.evidence_refs == (CASE_EVIDENCE, OUTCOME_EVIDENCE)
    assert evaluation_input.metadata["case"]["shared"] == "case"
    assert evaluation_input.metadata["execution_outcome"]["shared"] == "outcome"
    assert persistence.results[0].evidence_refs == (CASE_EVIDENCE, OUTCOME_EVIDENCE, DRAFT_EVIDENCE)
    assert persistence.results[0].metadata["case"]["shared"] == "case"
    assert persistence.results[0].metadata["evaluator"]["shared"] == "evaluator"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capabilities", "config_ref"),
    [
        (("TEXT",), None),
        ((), VersionRef("target-config", "v1")),
        (("TEXT",), VersionRef("target-config", "v1")),
    ],
)
async def test_valid_persisted_target_projections_pass_preflight(capabilities, config_ref):
    specs = (spec("eval"),)
    run, attempt, case = make_context(
        specs=specs,
        target_capabilities=capabilities,
        target_config=config_ref,
    )
    loop, persistence, target, _ = make_loop(run, attempt, specs)

    assert await loop.execute_attempt(
        PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1)
    ) is EvaluationLoopResult.PROGRESSED
    assert target.target_ref == authoritative_target_ref(run)
    assert persistence.run.status is RunStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "run_capabilities",
        "run_version",
        "attempt_config",
        "attempt_version",
        "attempt_id",
        "attempt_kind",
        "resolved_capabilities",
        "resolved_config",
    ],
)
async def test_target_projection_and_resolved_target_mismatches_fail_before_claim(failure):
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs)
    target_override = None
    if failure == "run_capabilities":
        run = replace(run, execution_target_ref=replace(run.execution_target_ref, capabilities=("OTHER",)))
    elif failure == "run_version":
        run = replace(
            run,
            execution_target_ref=replace(run.execution_target_ref, target_version_ref=VersionRef("git", "other")),
        )
    elif failure == "attempt_config":
        attempt = replace(
            attempt,
            execution_target_ref=replace(attempt.execution_target_ref, config_ref=VersionRef("target-config", "other")),
        )
    elif failure == "attempt_version":
        attempt = replace(
            attempt,
            execution_target_ref=replace(attempt.execution_target_ref, target_version_ref=VersionRef("git", "other")),
        )
    elif failure == "attempt_id":
        attempt = replace(
            attempt,
            execution_target_ref=replace(attempt.execution_target_ref, target_id="other"),
        )
    elif failure == "attempt_kind":
        attempt = replace(
            attempt,
            execution_target_ref=replace(attempt.execution_target_ref, target_kind="OTHER"),
        )
    elif failure == "resolved_capabilities":
        target_override = RecordingTarget(
            replace(authoritative_target_ref(run), capabilities=("OTHER",)), OutcomeKind.SUCCESS
        )
    else:
        target_override = RecordingTarget(
            replace(authoritative_target_ref(run), config_ref=VersionRef("target-config", "other")),
            OutcomeKind.SUCCESS,
        )
    loop, persistence, target, _ = make_loop(run, attempt, specs, target_override=target_override)

    with pytest.raises(EvaluationLoopContractError):
        await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))
    assert "claim" not in persistence.calls and target.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "run_status"),
    [
        (OutcomeKind.FAILURE, RunStatus.FAILED),
        (OutcomeKind.TIMEOUT, RunStatus.FAILED),
        (OutcomeKind.CANCELLED, RunStatus.FAILED),
        (OutcomeKind.OUTCOME_UNKNOWN, RunStatus.OUTCOME_UNKNOWN),
    ],
)
async def test_target_non_success_records_no_result_and_finishes(kind, run_status):
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs)
    loop, persistence, _, evaluators = make_loop(run, attempt, specs, target_kind=kind)

    assert await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1)) is EvaluationLoopResult.PROGRESSED
    assert persistence.attempt.execution_outcome_kind is kind
    assert persistence.run.status is run_status
    assert persistence.results == []
    assert evaluators["eval"].calls == []


@pytest.mark.asyncio
async def test_escaped_target_exception_propagates_without_outcome_or_finish():
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs)
    error = RuntimeError("adapter escaped")
    loop, persistence, _, _ = make_loop(run, attempt, specs, target_error=error)

    with pytest.raises(RuntimeError, match="adapter escaped"):
        await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))
    assert persistence.attempt.status is AttemptStatus.RUNNING
    assert "record" not in persistence.calls and "finish" not in persistence.calls


@pytest.mark.asyncio
async def test_target_and_evaluator_cancellation_are_not_converted_to_persisted_facts():
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs)
    loop, persistence, _, _ = make_loop(run, attempt, specs, target_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))
    assert persistence.attempt.status is AttemptStatus.RUNNING and persistence.results == []

    run, attempt, case = make_context(
        specs=specs,
        status=AttemptStatus.TERMINAL,
        outcome_kind=OutcomeKind.SUCCESS,
    )
    evaluators = {"eval": DraftEvaluator(specs[0], error=asyncio.CancelledError())}
    loop, persistence, _, _ = make_loop(run, attempt, specs, evaluators=evaluators)
    with pytest.raises(asyncio.CancelledError):
        await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))
    assert persistence.results == [] and "finish" not in persistence.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        (PolicyDisposition.FAIL, EvaluationVerdict.FAIL),
        (PolicyDisposition.INCONCLUSIVE, EvaluationVerdict.INCONCLUSIVE),
    ],
)
async def test_evaluator_exception_is_sanitized_normalized_and_continues(disposition, expected):
    specs = (spec("broken"), spec("next"))
    policy = EvaluationPolicy(evaluator_error=disposition)
    run, attempt, case = make_context(specs=specs, policy=policy)
    evaluators = {
        "broken": DraftEvaluator(specs[0], error=ValueError("secret-value")),
        "next": DraftEvaluator(specs[1]),
    }
    loop, persistence, _, _ = make_loop(run, attempt, specs, evaluators=evaluators)

    await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))

    assert [item.verdict for item in persistence.results] == [expected, EvaluationVerdict.PASS]
    assert "ValueError" in persistence.results[0].reason
    assert "secret-value" not in persistence.results[0].reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_verdict", "policy", "expected"),
    [
        (EvaluationVerdict.ERROR, EvaluationPolicy(evaluator_error=PolicyDisposition.FAIL), EvaluationVerdict.FAIL),
        (
            EvaluationVerdict.INCONCLUSIVE,
            EvaluationPolicy(evaluator_inconclusive=PolicyDisposition.FAIL),
            EvaluationVerdict.FAIL,
        ),
        (
            EvaluationVerdict.INCONCLUSIVE,
            EvaluationPolicy(evaluator_inconclusive=PolicyDisposition.INCONCLUSIVE),
            EvaluationVerdict.INCONCLUSIVE,
        ),
    ],
)
async def test_explicit_error_and_inconclusive_drafts_follow_persisted_policy(source_verdict, policy, expected):
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs, policy=policy)
    evaluators = {"eval": DraftEvaluator(specs[0], verdict=source_verdict)}
    loop, persistence, _, _ = make_loop(run, attempt, specs, evaluators=evaluators)

    await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))
    assert persistence.results[0].verdict is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"evaluator_id": "other"},
        {"evaluator_version": "other"},
        {"config_ref": VersionRef("config", "other")},
        {"prompt_ref": VersionRef("prompt", "other")},
    ],
)
async def test_invalid_draft_provenance_fails_closed_without_finalize(changes):
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs)
    evaluators = {"eval": DraftEvaluator(specs[0], changes=changes)}
    loop, persistence, _, _ = make_loop(run, attempt, specs, evaluators=evaluators)

    with pytest.raises(EvaluationLoopContractError):
        await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))
    assert persistence.results == [] and "finish" not in persistence.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["target_version", "case_ref", "case_input", "target_ref", "evaluator_binding"])
async def test_deterministic_preflight_failures_do_not_claim_or_execute(failure):
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs, target_version=None if failure == "target_version" else VersionRef("git", "abc123"))
    resolver_mismatch = failure == "evaluator_binding"
    target_override = None
    if failure == "case_ref":
        case = replace(case, case_id="other")
    elif failure == "case_input":
        case = replace(case, input_payload={"different": True})
    elif failure == "target_ref":
        target_override = RecordingTarget(replace(authoritative_target_ref(run), target_id="other"), OutcomeKind.SUCCESS)
    loop, persistence, target, _ = make_loop(
        run,
        attempt,
        specs,
        target_override=target_override,
        resolver_mismatch=resolver_mismatch,
    )

    expected = TargetVersionRequired if failure == "target_version" else EvaluationLoopContractError
    with pytest.raises(expected):
        await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))
    assert "claim" not in persistence.calls and target.requests == [] and persistence.results == []


@pytest.mark.asyncio
async def test_claim_loser_does_not_execute_target():
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs)
    loop, persistence, target, _ = make_loop(run, attempt, specs)
    persistence.claim_wins = False

    assert await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1)) is EvaluationLoopResult.NOT_CLAIMED
    assert target.requests == [] and persistence.results == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AttemptStatus.CLAIMED, AttemptStatus.RUNNING])
async def test_owned_attempt_reentry_is_in_progress(status):
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs, status=status)
    loop, persistence, target, _ = make_loop(run, attempt, specs)

    assert await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1)) is EvaluationLoopResult.IN_PROGRESS
    assert target.requests == [] and "claim" not in persistence.calls


@pytest.mark.asyncio
async def test_terminal_run_is_already_complete_without_resolving_or_executing():
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs, status=AttemptStatus.TERMINAL, outcome_kind=OutcomeKind.SUCCESS)
    run = replace(run, status=RunStatus.COMPLETED, finished_at=NOW)
    loop, persistence, target, evaluators = make_loop(run, attempt, specs)

    assert await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1)) is EvaluationLoopResult.ALREADY_COMPLETE
    assert target.requests == [] and evaluators["eval"].calls == [] and persistence.results == []


@pytest.mark.asyncio
async def test_terminal_success_reentry_skips_existing_slot_and_only_fills_missing():
    specs = (spec("existing"), spec("missing", required=False))
    run, attempt, case = make_context(specs=specs, status=AttemptStatus.TERMINAL, outcome_kind=OutcomeKind.SUCCESS)
    evaluators = {value.evaluator_id: DraftEvaluator(value) for value in specs}
    loop, persistence, target, _ = make_loop(run, attempt, specs, evaluators=evaluators)
    existing_draft = EvaluationResultDraft(
        "existing", "v1", specs[0].config_ref, EvaluationVerdict.PASS, "existing", prompt_ref=specs[0].prompt_ref
    )
    from app.services.evaluation.loop import _evaluation_input, _result

    input_value = _evaluation_input(case, attempt)
    persistence.results.append(
        _result(
            run=run,
            attempt=attempt,
            test_case=case,
            spec=specs[0],
            draft=existing_draft,
            input_value=input_value,
            normalization_source="UNCHANGED",
            result_id=uuid4(),
            created_at=NOW,
        )
    )

    await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))

    assert target.requests == []
    assert evaluators["existing"].calls == [] and len(evaluators["missing"].calls) == 1
    assert [item.evaluator_id for item in persistence.results] == ["existing", "missing"]


@pytest.mark.asyncio
async def test_authoritatively_incomplete_finish_maps_to_run_not_ready():
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs, status=AttemptStatus.TERMINAL, outcome_kind=OutcomeKind.SUCCESS)
    loop, persistence, _, _ = make_loop(run, attempt, specs)
    persistence.finish_not_ready = True
    other_id = uuid4()
    other_ref = CaseVersionRef("other", "v1")
    other_request = ExecutionRequest(
        str(uuid4()),
        str(run.run_id),
        str(other_id),
        other_ref,
        {"question": "other"},
        timedelta(seconds=30),
        "other-key",
    )
    persistence.attempts.append(
        ExecutionAttempt(
            attempt_id=other_id,
            project_id=PROJECT_ID,
            run_id=run.run_id,
            case_ref=other_ref,
            attempt_no=1,
            execution_target_ref=run.execution_target_ref,
            execution_request=other_request,
            request_snapshot={"input_payload": other_request.input_payload},
            created_at=NOW,
            status=AttemptStatus.RUNNING,
            claim_token=uuid4(),
            claimed_at=NOW,
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=5),
        )
    )

    assert await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1)) is EvaluationLoopResult.RUN_NOT_READY


@pytest.mark.asyncio
async def test_duplicate_result_exact_reread_converges_but_mismatch_propagates():
    specs = (spec("eval"),)
    run, attempt, case = make_context(specs=specs, status=AttemptStatus.TERMINAL, outcome_kind=OutcomeKind.SUCCESS)
    loop, persistence, _, _ = make_loop(run, attempt, specs)
    persistence.duplicate_mode = "exact"
    assert await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1)) is EvaluationLoopResult.PROGRESSED

    run, attempt, case = make_context(specs=specs, status=AttemptStatus.TERMINAL, outcome_kind=OutcomeKind.SUCCESS)
    loop, persistence, _, _ = make_loop(run, attempt, specs)
    persistence.duplicate_mode = "mismatch"
    with pytest.raises(ResultAlreadyFinalized):
        await loop.execute_attempt(PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1))


def test_loop_source_has_no_forbidden_runtime_or_lifecycle_dependencies():
    source = (Path(__file__).parents[2] / "app" / "services" / "evaluation" / "loop.py").read_text(encoding="utf-8")
    forbidden = (
        "app.core.evals",
        "app.core.traces",
        "app.infrastructure",
        "celery",
        "LLMEngine",
        "asyncio.timeout",
        "asyncio.wait_for",
        ".retry_attempt(",
        ".reconcile_stale(",
    )
    assert all(value not in source for value in forbidden)
