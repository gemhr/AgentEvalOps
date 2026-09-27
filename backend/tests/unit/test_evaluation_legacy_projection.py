from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.evals.entities import EvalRun, SessionScore, TraceScore
from app.core.evaluation.results import EvaluationVerdict
from app.infrastructure.db.models import (
    EvalRunModel,
    EvaluationResultModel,
    SessionScoreModel,
    TraceScoreModel,
)
from app.infrastructure.db.repositories.evaluation_projection_repo import PostgresEvaluationProjectionRepository
from app.registry.constants import EvaluationStatus, ScoreDataType, ScoreSource, ScoreStatus
from app.services.evaluation.legacy_projection import LegacyEvaluationProjectionService


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def trace_score(*, source: ScoreSource = ScoreSource.AUTOMATED) -> TraceScore:
    return TraceScore(
        id=uuid4(),
        trace_id=uuid4(),
        project_id=uuid4(),
        name="quality",
        value="0.9",
        source=source,
        status=ScoreStatus.SUCCESS,
        reason="good",
        metadata={"evaluator": "quality-v1"},
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_projection_service_preserves_verdict_and_rejects_manual_annotation():
    repository = MagicMock()
    repository.insert_trace_score = AsyncMock()
    service = LegacyEvaluationProjectionService(repository)
    score = trace_score()
    result_id = uuid4()

    await service.project_trace_result(score, result_id, EvaluationVerdict.INCONCLUSIVE)
    repository.insert_trace_score.assert_awaited_once_with(score, result_id, EvaluationVerdict.INCONCLUSIVE)

    with pytest.raises(ValueError, match="AUTOMATED"):
        await service.project_trace_result(
            trace_score(source=ScoreSource.ANNOTATION), result_id, EvaluationVerdict.PASS
        )
    assert repository.insert_trace_score.await_count == 1


@pytest.mark.asyncio
async def test_projection_repository_inserts_trace_result_link_and_original_verdict():
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    repository = PostgresEvaluationProjectionRepository(session)
    score = trace_score()
    result_id = uuid4()

    await repository.insert_trace_score(score, result_id, EvaluationVerdict.ERROR)

    row = session.add.call_args.args[0]
    assert isinstance(row, TraceScoreModel)
    assert row.canonical_result_id == result_id
    assert row.canonical_verdict == "ERROR"
    assert row.source == ScoreSource.AUTOMATED
    assert row.metadata_ == score.metadata
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_projection_repository_supports_session_scores_and_unknown_run_status():
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    repository = PostgresEvaluationProjectionRepository(session)
    canonical_run_id = uuid4()
    run = EvalRun(
        id=uuid4(),
        project_id=uuid4(),
        target_type="SESSION",
        metric_names=["coherence"],
        status=EvaluationStatus.OUTCOME_UNKNOWN,
        created_at=NOW,
    )
    await repository.insert_run(run, canonical_run_id)
    run_row = session.add.call_args.args[0]
    assert isinstance(run_row, EvalRunModel)
    assert run_row.canonical_run_id == canonical_run_id
    assert run_row.status is EvaluationStatus.OUTCOME_UNKNOWN

    score = SessionScore(
        id=uuid4(),
        session_id="session-1",
        project_id=run.project_id,
        name="coherence",
        data_type=ScoreDataType.NUMERIC,
        value="0.8",
        source=ScoreSource.AUTOMATED,
        status=ScoreStatus.SUCCESS,
        created_at=NOW,
        updated_at=NOW,
    )
    result_id = uuid4()
    await repository.insert_session_score(score, result_id, EvaluationVerdict.FAIL)
    score_row = session.add.call_args.args[0]
    assert isinstance(score_row, SessionScoreModel)
    assert score_row.canonical_result_id == result_id
    assert score_row.canonical_verdict == "FAIL"
    assert score_row.source == ScoreSource.AUTOMATED


def test_canonical_projection_schema_has_same_project_links_and_nullable_unique_ids():
    assert {constraint.name for constraint in EvalRunModel.__table__.constraints} >= {
        "fk_eval_runs_canonical_run",
        "uq_eval_runs_canonical_run_id",
    }
    assert {constraint.name for constraint in TraceScoreModel.__table__.constraints} >= {
        "fk_trace_scores_canonical_result",
        "uq_trace_scores_canonical_result_id",
        "ck_trace_scores_canonical_result_verdict_pair",
    }
    assert {constraint.name for constraint in SessionScoreModel.__table__.constraints} >= {
        "fk_session_scores_canonical_result",
        "uq_session_scores_canonical_result_id",
        "ck_session_scores_canonical_result_verdict_pair",
    }
    assert "uq_evaluation_results_project_id_id" in {
        constraint.name for constraint in EvaluationResultModel.__table__.constraints
    }
    assert TraceScoreModel.__table__.c.canonical_result_id.nullable
    assert SessionScoreModel.__table__.c.canonical_result_id.nullable
