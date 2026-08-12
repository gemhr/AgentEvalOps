"""Evaluation persistence Application orchestration tests。"""

# ruff: noqa: D101, D105, D415

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.evaluation import (
    ArtifactRef, CapabilityRequirement, CaseVersionRef, DatasetVersion, EvaluationPolicy, EvaluationResult,
    EvaluationSuiteVersion, EvaluationVerdict, EvaluatorKind, EvaluatorSpec, ExecutionTargetRef, OutcomeKind,
    PolicyDisposition, ProvenanceCompleteness, ScoreDirection, TestCaseVersion as CaseVersion, VersionRef,
)
from app.core.evaluation.run_attempts import AttemptStatus, RunNotFinishable, RunStatus
from app.services.evaluation import EvaluationPersistenceService

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


class FakeUow:
    def __init__(self):
        self.runs = SimpleNamespace(
            add_run_with_attempts=AsyncMock(), get_run=AsyncMock(), lock_run=AsyncMock(),
            set_running_if_pending=AsyncMock(return_value=True), finish_run=AsyncMock(return_value=True),
        )
        self.attempts = SimpleNamespace(
            get_attempt=AsyncMock(), list_attempts=AsyncMock(), list_latest_attempts=AsyncMock(),
            claim_attempt=AsyncMock(), mark_running=AsyncMock(), record_outcome=AsyncMock(),
            create_retry=AsyncMock(), list_stale_candidates=AsyncMock(), reconcile_stale=AsyncMock(),
        )
        self.results = SimpleNamespace(
            get_result=AsyncMock(), list_results=AsyncMock(), list_finalized_slots=AsyncMock(return_value=frozenset()),
            insert_final_result=AsyncMock(),
        )
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.rollback()


def catalog():
    ref = CaseVersionRef("case", "v1")
    case = CaseVersion("case", "v1", "case", {"q": [1]}, NOW)
    dataset = DatasetVersion("dataset", "d1", "dataset", NOW, case_version_refs=(ref,))
    spec = EvaluatorSpec(
        "eval", "e1", EvaluatorKind.DETERMINISTIC, VersionRef("cfg", "1"),
        ScoreDirection.HIGHER_IS_BETTER, config_snapshot={"model": {"temperature": 0}},
        threshold=0.75, score_range=(0, 1), comparison_tolerance=0.01,
        prompt_ref=VersionRef("prompt", "p1"), required=True,
    )
    policy = EvaluationPolicy(
        required_result_missing=PolicyDisposition.FAIL,
        evaluator_error=PolicyDisposition.INCONCLUSIVE,
        evaluator_inconclusive=PolicyDisposition.FAIL,
        metadata={"owner": "quality"},
    )
    suite = EvaluationSuiteVersion(
        "suite", "s1", (ref,), (spec,), policy, NOW,
        target_capability_requirements=(CapabilityRequirement("chat"),), metadata={"tier": "regression"},
    )
    return ref, case, dataset, suite


async def persisted_success_context():
    ref, case, dataset, suite = catalog()
    seed = FakeUow()
    run, (attempt,) = await EvaluationPersistenceService(lambda: seed).create_run(
        project_id=uuid4(), dataset=dataset, suite=suite, cases={ref: case},
        target=ExecutionTargetRef("target", "FIXTURE", VersionRef("git", "abc"), ("chat",)),
        timeout=timedelta(seconds=10),
    )
    token = uuid4()
    artifact = ArtifactRef("artifact", "sha256:abc", "application/json", {"source": "target"})
    terminal = replace(
        attempt, status=AttemptStatus.TERMINAL, claim_token=token, claimed_at=NOW, started_at=NOW,
        finished_at=NOW, lease_expires_at=NOW, execution_outcome_kind=OutcomeKind.SUCCESS,
        output_artifact_ref=artifact,
    )
    result = EvaluationResult(
        result_id=str(uuid4()), run_id=str(run.run_id), attempt_id=str(terminal.attempt_id),
        dataset_id="dataset", dataset_version="d1", case_id="case", case_version="v1",
        suite_id="suite", suite_version="s1", evaluator_id="eval", evaluator_version="e1",
        config_ref=VersionRef("cfg", "1"), prompt_ref=VersionRef("prompt", "p1"),
        execution_target_id="target", target_version_ref=VersionRef("git", "abc"),
        execution_request_id=terminal.execution_request.request_id, output_artifact_ref=artifact,
        verdict=EvaluationVerdict.PASS, reason="ok",
        provenance_completeness=ProvenanceCompleteness.COMPLETE, created_at=NOW,
    )
    return run, terminal, token, result


@pytest.mark.asyncio
async def test_create_run_persists_initial_attempts_in_one_uow():
    ref, case, dataset, suite = catalog()
    uow = FakeUow()
    run, attempts = await EvaluationPersistenceService(lambda: uow).create_run(
        project_id=uuid4(), dataset=dataset, suite=suite, cases={ref: case},
        target=ExecutionTargetRef("target", "FIXTURE", capabilities=("chat",)), timeout=timedelta(seconds=10),
    )
    assert run.status is RunStatus.PENDING
    assert len(attempts) == 1 and attempts[0].attempt_no == 1
    uow.runs.add_run_with_attempts.assert_awaited_once_with(run, attempts)
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_run_freezes_complete_suite_snapshot():
    ref, case, dataset, suite = catalog()
    uow = FakeUow()
    run, _ = await EvaluationPersistenceService(lambda: uow).create_run(
        project_id=uuid4(), dataset=dataset, suite=suite, cases={ref: case},
        target=ExecutionTargetRef("target", "FIXTURE", capabilities=("chat",)), timeout=timedelta(seconds=10),
    )
    evaluator = run.suite_snapshot["evaluators"][0]
    assert evaluator["evaluator_id"] == "eval"
    assert evaluator["evaluator_version"] == "e1"
    assert evaluator["evaluator_kind"] == "DETERMINISTIC"
    assert evaluator["config_ref"] == {"kind": "cfg", "opaque_value": "1"}
    assert evaluator["config_snapshot"] == {"model": {"temperature": 0}}
    assert evaluator["threshold"] == 0.75
    assert evaluator["score_direction"] == "HIGHER_IS_BETTER"
    assert evaluator["score_range"] == (0, 1)
    assert evaluator["comparison_tolerance"] == 0.01
    assert evaluator["prompt_ref"] == {"kind": "prompt", "opaque_value": "p1"}
    assert evaluator["required"] is True
    assert run.suite_snapshot["evaluation_policy"] == {
        "required_result_missing": "FAIL",
        "evaluator_error": "INCONCLUSIVE",
        "evaluator_inconclusive": "FAIL",
        "metadata": {"owner": "quality"},
    }
    assert run.suite_snapshot["target_capability_requirements"] == ({"identifier": "chat"},)
    assert run.suite_snapshot["metadata"] == {"tier": "regression"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"config_ref": VersionRef("cfg", "2")}, "config"),
        ({"prompt_ref": VersionRef("prompt", "p2")}, "prompt"),
        ({"target_version_ref": VersionRef("git", "def")}, "target version"),
        ({"output_artifact_ref": ArtifactRef("other")}, "artifact"),
    ],
)
async def test_finalize_result_rejects_mismatched_complete_provenance(change, message):
    run, terminal, token, result = await persisted_success_context()
    uow = FakeUow()
    uow.attempts.get_attempt.return_value = terminal
    uow.runs.get_run.return_value = run
    with pytest.raises(ValueError, match=message):
        await EvaluationPersistenceService(lambda: uow).finalize_result(
            terminal.project_id, terminal.attempt_id, token, replace(result, **change)
        )
    uow.results.insert_final_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_result_accepts_complete_matching_provenance():
    run, terminal, token, result = await persisted_success_context()
    uow = FakeUow()
    uow.attempts.get_attempt.return_value = terminal
    uow.runs.get_run.return_value = run
    await EvaluationPersistenceService(lambda: uow).finalize_result(
        terminal.project_id, terminal.attempt_id, token, result
    )
    uow.results.insert_final_result.assert_awaited_once_with(terminal.project_id, result, token)
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_run_failure_rolls_back_uow():
    ref, case, dataset, suite = catalog()
    uow = FakeUow()
    uow.runs.add_run_with_attempts.side_effect = RuntimeError("insert failed")
    with pytest.raises(RuntimeError):
        await EvaluationPersistenceService(lambda: uow).create_run(
            project_id=uuid4(), dataset=dataset, suite=suite, cases={ref: case},
            target=ExecutionTargetRef("target", "FIXTURE", capabilities=("chat",)), timeout=timedelta(seconds=10),
        )
    uow.rollback.assert_awaited_once()
    uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_delivery_returns_not_claimed():
    uow = FakeUow()
    uow.attempts.claim_attempt.return_value = None
    result = await EvaluationPersistenceService(lambda: uow).claim_attempt(
        uuid4(), uuid4(), lease=timedelta(seconds=30)
    )
    assert not result.claimed and result.claim_token is None
    uow.runs.set_running_if_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_retry_requires_explicit_authorization_and_reason():
    ref, case, dataset, suite = catalog()
    seed = FakeUow()
    _, (attempt,) = await EvaluationPersistenceService(lambda: seed).create_run(
        project_id=uuid4(), dataset=dataset, suite=suite, cases={ref: case},
        target=ExecutionTargetRef("target", "FIXTURE", capabilities=("chat",)), timeout=timedelta(seconds=10),
    )
    terminal = attempt.__class__(
        **{name: getattr(attempt, name) for name in attempt.__dataclass_fields__ if name not in {
            "status", "claim_token", "claimed_at", "started_at", "finished_at", "lease_expires_at",
            "execution_outcome_kind", "error_category", "reason"
        }},
        status=AttemptStatus.TERMINAL, claim_token=uuid4(), claimed_at=NOW, started_at=NOW,
        finished_at=NOW, lease_expires_at=NOW, execution_outcome_kind=OutcomeKind.OUTCOME_UNKNOWN,
        error_category="UNKNOWN", reason="lost",
    )
    uow = FakeUow()
    uow.attempts.get_attempt.return_value = terminal
    uow.runs.get_run.return_value = SimpleNamespace(status=RunStatus.RUNNING)
    with pytest.raises(ValueError, match="authorization"):
        await EvaluationPersistenceService(lambda: uow).retry_attempt(terminal.project_id, terminal.attempt_id)


@pytest.mark.asyncio
async def test_finish_run_refuses_active_attempt():
    uow = FakeUow()
    uow.runs.lock_run.return_value = SimpleNamespace(status=RunStatus.RUNNING)
    uow.attempts.list_latest_attempts.return_value = (SimpleNamespace(status=AttemptStatus.RUNNING),)
    with pytest.raises(RunNotFinishable, match="active"):
        await EvaluationPersistenceService(lambda: uow).finish_run(uuid4(), uuid4())


@pytest.mark.asyncio
async def test_finish_run_unknown_has_priority_over_confirmed_failure():
    uow = FakeUow()
    uow.runs.lock_run.return_value = SimpleNamespace(status=RunStatus.RUNNING)
    uow.attempts.list_latest_attempts.return_value = (
        SimpleNamespace(status=AttemptStatus.TERMINAL, execution_outcome_kind=OutcomeKind.FAILURE),
        SimpleNamespace(status=AttemptStatus.TERMINAL, execution_outcome_kind=OutcomeKind.OUTCOME_UNKNOWN),
    )
    status = await EvaluationPersistenceService(lambda: uow).finish_run(uuid4(), uuid4())
    assert status is RunStatus.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_finish_run_confirmed_failure_is_failed():
    uow = FakeUow()
    uow.runs.lock_run.return_value = SimpleNamespace(status=RunStatus.RUNNING)
    uow.attempts.list_latest_attempts.return_value = (
        SimpleNamespace(status=AttemptStatus.TERMINAL, execution_outcome_kind=OutcomeKind.TIMEOUT),
    )
    assert await EvaluationPersistenceService(lambda: uow).finish_run(uuid4(), uuid4()) is RunStatus.FAILED


@pytest.mark.asyncio
async def test_evaluator_fail_slot_still_allows_completed_run():
    uow = FakeUow()
    attempt_id = uuid4()
    uow.runs.lock_run.return_value = SimpleNamespace(
        status=RunStatus.RUNNING,
        suite_snapshot={"evaluators": ({"evaluator_id": "eval", "evaluator_version": "e1", "required": True},)},
    )
    uow.attempts.list_latest_attempts.return_value = (
        SimpleNamespace(
            attempt_id=attempt_id, status=AttemptStatus.TERMINAL, execution_outcome_kind=OutcomeKind.SUCCESS,
        ),
    )
    # Slot presence controls completion；verdict PASS/FAIL 不参与 infrastructure lifecycle。
    uow.results.list_finalized_slots.return_value = frozenset({("case", "v1", "eval", "e1")})
    assert await EvaluationPersistenceService(lambda: uow).finish_run(uuid4(), uuid4()) is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_missing_required_slot_survives_as_partial_summary_not_run_state():
    uow = FakeUow()
    uow.runs.lock_run.return_value = SimpleNamespace(
        status=RunStatus.RUNNING,
        suite_snapshot={"evaluators": ({"evaluator_id": "eval", "evaluator_version": "e1", "required": True},)},
    )
    uow.attempts.list_latest_attempts.return_value = (
        SimpleNamespace(attempt_id=uuid4(), status=AttemptStatus.TERMINAL, execution_outcome_kind=OutcomeKind.SUCCESS),
    )
    with pytest.raises(RunNotFinishable, match="incomplete"):
        await EvaluationPersistenceService(lambda: uow).finish_run(uuid4(), uuid4())
    uow.runs.finish_run.assert_not_awaited()
