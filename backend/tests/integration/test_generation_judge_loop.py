# ruff: noqa: D101, D102, D415

"""Generation Judge 经真实 EvaluationLoop / PostgreSQL 的持久化闭环。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.adapters.evaluation.fixture import FixtureExecution, FixtureExecutionTarget
from app.core.evaluation.catalog import (
    DatasetVersion,
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluatorKind,
    EvaluatorSpec,
    ScoreDirection,
    TestCaseVersion as CaseVersion,
)
from app.core.evaluation.execution import ExecutionTargetRef, OutcomeKind
from app.core.evaluation.generation_evidence import FinalAnswerEvidenceV1, build_final_answer_evidence
from app.core.evaluation.generation_judge import (
    CORRECTNESS_PROMPT_REF,
    FAITHFULNESS_PROMPT_REF,
    GENERATION_CORRECTNESS,
    GENERATION_FAITHFULNESS,
    GenerationCorrectnessEvaluator,
    GenerationFaithfulnessEvaluator,
)
from app.core.evaluation.ports import JudgeModelResponse
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1, build_rag_artifact_evidence
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, VersionRef
from app.core.evaluation.results import EvaluationVerdict
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import PostgresEvaluationPersistenceUnitOfWork
from app.services.evaluation import EvaluationLoopResult, EvaluationLoopService, EvaluationPersistenceService, ResolvedEvaluator

from .conftest import TEST_PROJECT_ID

NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
RUN_ID = "11111111-1111-4111-8111-111111111111"


def _answer_evidence():
    content = "Paris"
    return build_final_answer_evidence(
        FinalAnswerEvidenceV1.model_validate(
            {
                "schema_version": "final-answer-evidence.v1",
                "evidence_id": f"final-answer://{RUN_ID}",
                "run_id": RUN_ID,
                "attempt_id": RUN_ID,
                "media_type": "text/plain; charset=utf-8",
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
            }
        )
    )


def _rag_evidence():
    item = {
        "document_id": "doc",
        "chunk_id": "chunk",
        "rank": 1,
        "retrieval_rank": 1,
        "rerank_rank": None,
        "retrieval_score": 0.9,
        "retrieval_score_kind": "VECTOR",
        "retrieval_channels": ["VECTOR"],
        "rerank_score": None,
        "rerank_score_kind": None,
        "source": {"source_type": "md", "collection": "kb", "display_name": "x", "document_version": "v1"},
        "page": None,
        "section": None,
        "sheet": None,
        "content_hash": "hash",
        "selected": True,
    }
    artifact = RagEvaluationArtifactV1.model_validate(
        {
            "schema_version": "rag-evaluation-artifact.v1",
            "artifact_id": f"rag-eval://{RUN_ID}/r1",
            "run_id": RUN_ID,
            "attempt_id": RUN_ID,
            "retrieval_id": "r1",
            "invocation_index": 1,
            "retrieval_status": "SUCCEEDED",
            "query": "capital?",
            "rewritten_query": "capital?",
            "retrieved_items": [item],
            "ranked_items": [item],
            "selected_items": [
                {
                    "document_id": "doc",
                    "chunk_id": "chunk",
                    "selection_rank": 1,
                    "context_block_id": "block",
                    "citation_id": "citation",
                    "context_content_hash": "context-hash",
                    "text": "Paris is the capital of France.",
                }
            ],
            "citations": [],
            "retrieval_latency_ms": 1,
            "rerank_latency_ms": None,
            "total_latency_ms": 1,
            "degraded": False,
            "degradation_reasons": [],
            "error": None,
            "budget_usage": {},
        }
    )
    return build_rag_artifact_evidence(artifact, "COMPLETE")


def _spec(metric: str) -> EvaluatorSpec:
    return EvaluatorSpec(
        metric,
        "v1",
        EvaluatorKind.LLM_JUDGE,
        VersionRef("judge_config", "v1"),
        ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot={
            "judge_model_ref": {"kind": "llm_model", "opaque_value": "test/judge"},
            "temperature": 0.0,
            "evaluation_timeout_seconds": 1.0,
            "max_input_chars": 10000,
        },
        threshold=0.8,
        score_range=(0.0, 1.0),
        prompt_ref=CORRECTNESS_PROMPT_REF if metric == GENERATION_CORRECTNESS else FAITHFULNESS_PROMPT_REF,
    )


class StaticJudge:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0

    async def structured_generate(self, **kwargs):
        self.calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class TargetResolver:
    def __init__(self, target) -> None:
        self.target = target

    def resolve(self, target_ref):
        return self.target


class JudgeResolver:
    def __init__(self, correctness_judge, faithfulness_judge) -> None:
        self.correctness_judge = correctness_judge
        self.faithfulness_judge = faithfulness_judge

    def resolve(self, spec):
        if spec.evaluator_id == GENERATION_CORRECTNESS:
            return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, GenerationCorrectnessEvaluator(), self.correctness_judge)
        return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, GenerationFaithfulnessEvaluator(), self.faithfulness_judge)


@pytest.mark.asyncio
async def test_judge_results_persist_and_judge_failure_does_not_change_successful_attempt(db_session) -> None:
    case_ref = CaseVersionRef("case-a", "v1")
    case = CaseVersion("case-a", "v1", "case", {"query": "What is France's capital?"}, NOW, expected_output="Paris")
    specs = (_spec(GENERATION_CORRECTNESS), _spec(GENERATION_FAITHFULNESS))
    service = EvaluationPersistenceService(lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory))
    target_ref = ExecutionTargetRef("fixture-target", "FIXTURE", VersionRef("fixture", "v1"))
    run, (attempt,) = await service.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=DatasetVersion("dataset", "d1", "dataset", NOW, case_version_refs=(case_ref,)),
        suite=EvaluationSuiteVersion("suite", "s1", (case_ref,), specs, EvaluationPolicy(), NOW),
        cases={case_ref: case},
        target=target_ref,
        timeout=timedelta(seconds=30),
    )
    target = FixtureExecutionTarget(
        target_ref,
        {
            case_ref: FixtureExecution(
                kind=OutcomeKind.SUCCESS,
                started_at=NOW,
                finished_at=NOW,
                output_artifact_ref=ArtifactRef(f"localagent-run://{RUN_ID}"),
                evidence_refs=(_answer_evidence(), _rag_evidence()),
            )
        },
    )
    correctness = StaticJudge(JudgeModelResponse({"score": 1.0, "reason": "correct"}, VersionRef("llm_model", "test/judge")))
    faithfulness = StaticJudge(RuntimeError("provider body must not persist"))
    loop = EvaluationLoopService(
        service,
        TargetResolver(target),
        JudgeResolver(correctness, faithfulness),
        uuid_factory=uuid4,
        clock=lambda: NOW,
    )

    assert await loop.execute_attempt(TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=1)) is EvaluationLoopResult.PROGRESSED

    fresh_service = EvaluationPersistenceService(lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory))
    reloaded_attempt = await fresh_service.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)
    results = await fresh_service.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id)
    assert reloaded_attempt.execution_outcome_kind.value == "SUCCESS"
    assert [(item.evaluator_id, item.verdict, item.score) for item in results] == [
        (GENERATION_CORRECTNESS, EvaluationVerdict.PASS, 1.0),
        (GENERATION_FAITHFULNESS, EvaluationVerdict.INCONCLUSIVE, None),
    ]
    assert results[0].prompt_ref == CORRECTNESS_PROMPT_REF
    assert results[0].metadata["evaluator"]["judge_model_ref"]["opaque_value"] == "test/judge"
    assert results[1].reason == "judge_provider_failure"
    assert "provider body" not in str(results[1].metadata)
    assert all(len(item.evidence_refs) == 2 for item in results)
    assert (correctness.calls, faithfulness.calls) == (1, 1)
