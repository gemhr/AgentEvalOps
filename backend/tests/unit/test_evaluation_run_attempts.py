"""Run/Attempt domain lifecycle tests。"""

# ruff: noqa: D415

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core.evaluation import ArtifactRef, CaseVersionRef, ExecutionRequest, ExecutionTargetRef, OutcomeKind, VersionRef
from app.core.evaluation.run_attempts import AttemptStatus, EvaluationRun, ExecutionAttempt, RunStatus

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def make_run(**overrides):
    values = {
        "run_id": uuid4(), "project_id": uuid4(), "dataset_ref": VersionRef("DATASET", "d1"),
        "suite_ref": VersionRef("SUITE", "s1"),
        "execution_target_ref": ExecutionTargetRef("target", "FIXTURE", VersionRef("git", "abc")),
        "dataset_snapshot": {"nested": {"cases": ["c1"]}}, "suite_snapshot": {"evaluators": []},
        "execution_target_snapshot": {"capabilities": ["chat"]}, "created_at": NOW,
    }
    values.update(overrides)
    return EvaluationRun(**values)


def make_attempt(**overrides):
    attempt_id = overrides.pop("attempt_id", uuid4())
    run_id = overrides.pop("run_id", uuid4())
    case_ref = CaseVersionRef("case", "v1")
    request = ExecutionRequest(str(uuid4()), str(run_id), str(attempt_id), case_ref, {"x": [1]}, timedelta(seconds=5), "stable-key")
    values = {
        "attempt_id": attempt_id, "project_id": uuid4(), "run_id": run_id, "case_ref": case_ref,
        "attempt_no": 1, "execution_target_ref": ExecutionTargetRef("target", "FIXTURE"),
        "execution_request": request, "request_snapshot": {"input_payload": {"x": [1]}, "timeout_seconds": 5},
        "created_at": NOW,
    }
    values.update(overrides)
    return ExecutionAttempt(**values)


def test_run_snapshots_are_deeply_immutable():
    source = {"nested": {"cases": ["c1"]}}
    run = make_run(dataset_snapshot=source)
    source["nested"]["cases"].append("c2")
    assert run.dataset_snapshot["nested"]["cases"] == ("c1",)
    with pytest.raises(TypeError):
        run.dataset_snapshot["nested"]["x"] = 1


def test_run_state_set_has_no_partial_or_reconciling():
    assert {item.value for item in RunStatus} == {"PENDING", "RUNNING", "COMPLETED", "FAILED", "OUTCOME_UNKNOWN"}


def test_run_terminal_is_immutable():
    terminal = make_run(status=RunStatus.COMPLETED, started_at=NOW, finished_at=NOW)
    with pytest.raises(ValueError, match="terminal run"):
        terminal.finish(RunStatus.FAILED, NOW, "no")


def test_attempt_state_is_separate_from_outcome():
    assert {item.value for item in AttemptStatus} == {"PENDING", "CLAIMED", "RUNNING", "TERMINAL"}
    assert "OUTCOME_UNKNOWN" not in {item.value for item in AttemptStatus}


def test_attempt_request_attempt_and_idempotency_identities_are_distinct():
    attempt = make_attempt()
    assert str(attempt.attempt_id) != attempt.execution_request.request_id
    assert str(attempt.attempt_id) != attempt.execution_request.idempotency_key
    assert attempt.execution_request.request_id != attempt.execution_request.idempotency_key


def test_retry_creates_new_identity_and_preserves_stable_key():
    token = uuid4()
    source = make_attempt(
        status=AttemptStatus.TERMINAL, claim_token=token, claimed_at=NOW, started_at=NOW,
        finished_at=NOW, lease_expires_at=NOW, execution_outcome_kind=OutcomeKind.FAILURE,
        error_category="TARGET", reason="failed",
    )
    child = source.build_retry(attempt_id=uuid4(), request_id=str(uuid4()), created_at=NOW)
    assert child.attempt_id != source.attempt_id
    assert child.execution_request.request_id != source.execution_request.request_id
    assert child.execution_request.idempotency_key == source.execution_request.idempotency_key
    assert child.retry_of_attempt_id == source.attempt_id
    assert child.attempt_no == 2
    assert child.status is AttemptStatus.PENDING


@pytest.mark.parametrize("kind", [OutcomeKind.SUCCESS])
def test_success_attempt_cannot_be_execution_retried(kind):
    source = make_attempt(
        status=AttemptStatus.TERMINAL, claim_token=uuid4(), claimed_at=NOW, started_at=NOW,
        finished_at=NOW, lease_expires_at=NOW, execution_outcome_kind=kind,
        output_artifact_ref=ArtifactRef("artifact"),
    )
    with pytest.raises(ValueError, match="non-success"):
        source.build_retry(attempt_id=uuid4(), request_id=str(uuid4()), created_at=NOW)


def test_domain_entities_are_frozen():
    run = make_run()
    with pytest.raises(FrozenInstanceError):
        run.status = RunStatus.RUNNING
