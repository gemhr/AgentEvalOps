"""Stage5 Phase3 RAG Dataset、Suite 与 metric adapter tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.adapters.evaluation.rag_metrics import RagMetricEvaluatorResolver
from app.core.evaluation.dataset import load_dataset
from app.core.evaluation.dataset_bridge import bridge_dataset_to_catalog
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.execution import ExecutionOutcome, OutcomeKind
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef
from app.core.evaluation.process_trajectory import (
    Availability,
    Completeness,
    CoverageV1,
    ProcessKind,
    Provenance,
    Sensitivity,
    build_process_evidence,
    build_process_trajectory,
    parse_process_requirements,
    validate_process_requirements,
)
from app.services.evaluation.rag_baseline import build_rag_baseline_suite
from app.services.evaluation.rag_baseline import execute_rag_quality_baseline
from app.services.evaluation.loop import EvaluationLoopService
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1, build_rag_artifact_evidence
from tests.unit.test_rag_artifact import artifact_payload
from tests.unit.test_wp3_coordinator import memory_persistence

DATASET = (
    Path(__file__).resolve().parents[2]
    / "evaluation_assets"
    / "rag_quality_v1"
    / "rag_evaluation_dataset.v1.json"
)


def test_dataset_v1_identity_case_mix_and_no_answer_semantics() -> None:
    dataset = load_dataset(DATASET)

    assert dataset.dataset_id == "rag-evaluation-dataset"
    assert dataset.version == "v1"
    assert len(dataset) == 24
    assert len({case.case_id for case in dataset.cases}) == 24
    retrieval = [case for case in dataset.cases if case.ground_truth.retrieval is not None]
    no_answer = [case for case in dataset.cases if case.metadata.get("case_type") == "NO_ANSWER"]
    assert len(retrieval) == 20
    assert len(no_answer) == 4
    assert all(case.ground_truth.retrieval is None for case in no_answer)
    assert all(case.ground_truth.ranking is None for case in no_answer)
    assert all(case.ground_truth.generation is not None for case in no_answer)


def test_bridge_preserves_versioned_rag_ground_truth_and_suite() -> None:
    dataset = load_dataset(DATASET)
    catalog, cases = bridge_dataset_to_catalog(
        dataset, created_at=datetime(2026, 8, 23, tzinfo=timezone.utc)
    )
    suite = build_rag_baseline_suite(dataset)
    assert all(spec.required_evidence_kinds == ("process_trajectory",) for spec in suite.evaluator_specs)

    assert catalog.case_version_refs == suite.case_selection
    assert all(ref.version == "v1" for ref in suite.case_selection)
    assert [spec.evaluator_id for spec in suite.evaluator_specs] == [
        "recall_at_1",
        "recall_at_3",
        "recall_at_5",
        "mrr",
        "ndcg_at_3",
        "ndcg_at_5",
    ]
    case = cases[CaseVersionRef("exact-http-evaluation-v2", "v1")]
    assert case.metadata["rag_ground_truth"]["retrieval"]["relevant_chunks"]


@pytest.mark.asyncio
async def test_default_rag_baseline_runs_from_safe_process_evidence(monkeypatch) -> None:
    dataset = load_dataset(DATASET)
    case = next(item for item in dataset.cases if item.case_id == "exact-http-evaluation-v2")
    dataset = dataset.model_copy(update={"cases": [case]})
    relevant = case.ground_truth.retrieval.relevant_chunks[0]

    class FakeTarget:
        def __init__(self, target_ref, base_url, *, bearer_token):
            self.target_ref = target_ref

        async def execute(self, request):
            item = artifact_payload()["retrieved_items"][0]
            item = {**item, "document_id": relevant.document_id, "chunk_id": relevant.chunk_id}
            payload = artifact_payload(
                artifact_id=f"rag-eval://{request.attempt_id}/r1", run_id=request.attempt_id,
                attempt_id=request.attempt_id, query="private-query", rewritten_query="private-query",
                retrieved_items=[item], ranked_items=[item], selected_items=[], citations=[],
            )
            artifact = RagEvaluationArtifactV1.model_validate(payload)
            return ExecutionOutcome(
                request_id=request.request_id, kind=OutcomeKind.SUCCESS,
                started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
                output_artifact_ref=ArtifactRef("output", "sha256:output"),
                evidence_refs=(build_rag_artifact_evidence(artifact, "COMPLETE"),),
            )

        async def aclose(self):
            pass

    monkeypatch.setattr("app.services.evaluation.rag_baseline.LocalAgentHttpExecutionTarget", FakeTarget)
    persistence = memory_persistence()
    report = await execute_rag_quality_baseline(
        persistence=persistence, project_id=UUID("20000000-0000-4000-a000-000000000002"),
        dataset=dataset, base_url="http://localhost", bearer_token="test",
    )
    assert report["evaluated_retrieval_cases"] == 1
    assert report["metrics"]["recall_at_1"] == 1.0
    assert report["case_results"][0]["retrieval_status"] == "SUCCEEDED"
    assert "private-query" not in str(report)
    attempts = await persistence.list_attempts(UUID("20000000-0000-4000-a000-000000000002"), UUID(report["run_id"]))
    assert [ref.kind for ref in attempts[0].outcome_evidence_refs] == ["process_trajectory"]


@pytest.mark.asyncio
async def test_real_artifact_shape_flows_through_recall_mrr_and_ndcg_adapters() -> None:
    dataset = load_dataset(DATASET)
    _catalog, cases = bridge_dataset_to_catalog(
        dataset, created_at=datetime(2026, 8, 23, tzinfo=timezone.utc)
    )
    ref = CaseVersionRef("exact-http-evaluation-v2", "v1")
    case = cases[ref]
    relevant = case.metadata["rag_ground_truth"]["retrieval"]["relevant_chunks"][0]
    payload = artifact_payload()
    payload["retrieved_items"][0]["document_id"] = relevant["document_id"]
    payload["retrieved_items"][0]["chunk_id"] = relevant["chunk_id"]
    payload["ranked_items"][0]["document_id"] = relevant["document_id"]
    payload["ranked_items"][0]["chunk_id"] = relevant["chunk_id"]
    payload["selected_items"][0]["document_id"] = relevant["document_id"]
    payload["selected_items"][0]["chunk_id"] = relevant["chunk_id"]
    payload["citations"][0]["document_id"] = relevant["document_id"]
    payload["citations"][0]["chunk_id"] = relevant["chunk_id"]
    evidence = EvidenceRef(
        kind="rag_evaluation_artifact",
        identifier=payload["artifact_id"],
        metadata={"payload": payload},
    )
    value = EvaluationInput(
        case_ref=ref,
        input_payload=case.input_payload,
        expected_output=None,
        assertion_specs=(),
        actual_artifact=ArtifactRef("output", "localagent-run://test"),
        evidence_refs=(evidence,),
        metadata={"case": case.metadata},
    )
    suite = build_rag_baseline_suite(dataset, process_evidence=False)
    resolver = RagMetricEvaluatorResolver()
    scores = {}
    for spec in suite.evaluator_specs:
        draft = await resolver.resolve(spec).evaluator.evaluate(value, EvaluatorContext(spec))
        scores[spec.evaluator_id] = draft.score

    assert scores == {
        "recall_at_1": 1.0,
        "recall_at_3": 1.0,
        "recall_at_5": 1.0,
        "mrr": 1.0,
        "ndcg_at_3": 1.0,
        "ndcg_at_5": 1.0,
    }


@pytest.mark.asyncio
async def test_rag_metric_fails_closed_without_rag_artifact_even_when_process_support_exists() -> None:
    dataset = load_dataset(DATASET)
    _catalog, cases = bridge_dataset_to_catalog(
        dataset, created_at=datetime(2026, 8, 23, tzinfo=timezone.utc)
    )
    ref = CaseVersionRef("exact-http-evaluation-v2", "v1")
    case = cases[ref]
    value = EvaluationInput(
        case_ref=ref,
        input_payload=case.input_payload,
        expected_output=None,
        assertion_specs=(),
        evidence_refs=(EvidenceRef(
            kind="process_trajectory", identifier="trajectory://attempt-1", metadata={}
        ),),
        metadata={"case": case.metadata},
    )
    spec = build_rag_baseline_suite(dataset, process_evidence=False).evaluator_specs[0]
    draft = await RagMetricEvaluatorResolver().resolve(spec).evaluator.evaluate(
        value, EvaluatorContext(spec)
    )

    assert draft.verdict.value == "INCONCLUSIVE"
    assert draft.reason == "rag_metric_input_unavailable"


def _process_rag_input(
    case, retrieved, *, ranked=None, strategy="BASELINE", coverage=Completeness.COMPLETE, duplicate=False
):
    records = []
    ranked = retrieved if ranked is None else ranked
    for index in range(2 if duplicate else 1):
        retrieved_items = tuple({
            "document_id": doc_id,
            "chunk_id": chunk_id,
            "rank": rank,
            "retrieval_rank": rank,
        } for rank, (doc_id, chunk_id) in enumerate(retrieved, start=1))
        ranked_items = tuple({
            "document_id": doc_id,
            "chunk_id": chunk_id,
            "rank": rank,
            "retrieval_rank": rank,
        } for rank, (doc_id, chunk_id) in enumerate(ranked, start=1))
        records.append(build_process_evidence(
            evaluation_attempt_id="attempt-1",
            schema_version="stage11.wp4.v1",
            kind=ProcessKind.RETRIEVAL,
            producer_id="evaluation.fixture",
            provenance=Provenance.FIXTURE,
            runtime_run_id="attempt-1",
            source_stream_id="rag.artifacts",
            source_event_id=f"retrieval-{index + 1}",
            projection_role="retrieval.snapshot",
            source_schema_ref="rag-evaluation-artifact.v1",
            sensitivity=Sensitivity.SAFE_METADATA,
            payload={
                "retrieval_id": f"retrieval-{index + 1}",
                "status": "SUCCEEDED",
                "retrieval_strategy": strategy,
                "retrieved_items": retrieved_items,
                "ranked_items": ranked_items,
            },
        ))
    trajectory = build_process_trajectory(
        schema_version="stage11.wp4.v1",
        trajectory_id="trajectory://attempt-1",
        project_id="project-1",
        evaluation_run_id="run-1",
        evaluation_attempt_id="attempt-1",
        dataset_id="dataset-1",
        case_ref=CaseVersionRef(case.case_id, case.version),
        execution_request_id="request-1",
        execution_target_ref={"target_id": "target"},
        frozen_subject_ref={"subject": "fixture"},
        sealed=True,
        execution_partial=False,
        body_policy_ref=None,
        source_manifests=(),
        coverage=(CoverageV1(
            ProcessKind.RETRIEVAL,
            "attempt",
            Availability.PRESENT,
            coverage,
            "SOURCE_PARTIAL" if coverage is Completeness.PARTIAL else None,
        ),),
        records=tuple(records),
        edges=(),
    )
    return EvaluationInput(
        case_ref=CaseVersionRef(case.case_id, case.version),
        input_payload=case.input_payload,
        expected_output=None,
        assertion_specs=(),
        process_trajectory=trajectory,
        evidence_refs=(EvidenceRef("process_trajectory", trajectory.trajectory_id),),
        metadata={"case": case.metadata},
    ), trajectory


@pytest.mark.asyncio
async def test_process_only_rag_metrics_match_legacy_artifact_and_preserve_order() -> None:
    dataset = load_dataset(DATASET)
    _catalog, cases = bridge_dataset_to_catalog(dataset, created_at=datetime(2026, 8, 23, tzinfo=timezone.utc))
    case = cases[CaseVersionRef("exact-http-evaluation-v2", "v1")]
    relevant = case.metadata["rag_ground_truth"]["retrieval"]["relevant_chunks"][0]
    identities = [("doc-c", "chunk-c"), (relevant["document_id"], relevant["chunk_id"]), ("doc-b", "chunk-b")]
    process_input, trajectory = _process_rag_input(case, identities)
    legacy_items = [
        {
            **artifact_payload()["retrieved_items"][0],
            "document_id": doc_id,
            "chunk_id": chunk_id,
            "rank": rank,
            "retrieval_rank": rank,
        }
        for rank, (doc_id, chunk_id) in enumerate(identities, start=1)
    ]
    legacy_payload = artifact_payload(
        retrieved_items=legacy_items, ranked_items=legacy_items, selected_items=[], citations=[]
    )
    legacy_ref = EvidenceRef("rag_evaluation_artifact", legacy_payload["artifact_id"], metadata={"payload": legacy_payload})
    legacy_input = EvaluationInput(
        case_ref=CaseVersionRef(case.case_id, case.version),
        input_payload=case.input_payload,
        expected_output=None,
        assertion_specs=(),
        evidence_refs=(legacy_ref,),
        metadata={"case": case.metadata},
    )
    suite_legacy = build_rag_baseline_suite(dataset, process_evidence=False)
    suite_process = build_rag_baseline_suite(dataset, process_evidence=True)
    resolver = RagMetricEvaluatorResolver()
    process_scores = {}
    legacy_scores = {}
    for process_spec, legacy_spec in zip(suite_process.evaluator_specs, suite_legacy.evaluator_specs, strict=True):
        process_requirements = parse_process_requirements(
            process_spec.config_snapshot["process_evidence_requirements"]
        )
        assert validate_process_requirements(trajectory, process_requirements) is None
        process_draft = await resolver.resolve(process_spec).evaluator.evaluate(
            process_input, EvaluatorContext(process_spec)
        )
        legacy_draft = await resolver.resolve(legacy_spec).evaluator.evaluate(
            legacy_input, EvaluatorContext(legacy_spec)
        )
        process_scores[process_spec.evaluator_id] = process_draft.score
        legacy_scores[legacy_spec.evaluator_id] = legacy_draft.score
        assert process_draft.verdict.value == "PASS"
        assert process_draft.evidence_refs == ()
    assert process_scores == legacy_scores
    assert process_scores["recall_at_1"] == 0.0
    assert process_scores["mrr"] == 0.5

    hybrid_ranking = [identities[1], identities[0], identities[2]]
    hybrid_input, _ = _process_rag_input(
        case, identities, ranked=hybrid_ranking, strategy="HYBRID_RRF"
    )
    hybrid_retrieved = [
        {
            **artifact_payload()["retrieved_items"][0],
            "document_id": doc_id,
            "chunk_id": chunk_id,
            "rank": rank,
            "retrieval_rank": rank,
        }
        for rank, (doc_id, chunk_id) in enumerate(identities, start=1)
    ]
    hybrid_ranked = [
        {
            **artifact_payload()["ranked_items"][0],
            "document_id": doc_id,
            "chunk_id": chunk_id,
            "rank": rank,
            "retrieval_rank": rank,
        }
        for rank, (doc_id, chunk_id) in enumerate(hybrid_ranking, start=1)
    ]
    hybrid_payload = artifact_payload(
        retrieval_strategy="HYBRID_RRF",
        retrieved_items=hybrid_retrieved,
        ranked_items=hybrid_ranked,
        selected_items=[],
        citations=[],
    )
    hybrid_legacy = EvaluationInput(
        case_ref=CaseVersionRef(case.case_id, case.version), input_payload=case.input_payload,
        expected_output=None, assertion_specs=(),
        evidence_refs=(EvidenceRef(
            "rag_evaluation_artifact", hybrid_payload["artifact_id"], metadata={"payload": hybrid_payload}
        ),),
        metadata={"case": case.metadata},
    )
    recall_spec = suite_process.evaluator_specs[0]
    hybrid_process_draft = await resolver.resolve(recall_spec).evaluator.evaluate(
        hybrid_input, EvaluatorContext(recall_spec)
    )
    hybrid_legacy_draft = await resolver.resolve(suite_legacy.evaluator_specs[0]).evaluator.evaluate(
        hybrid_legacy, EvaluatorContext(suite_legacy.evaluator_specs[0])
    )
    assert hybrid_process_draft.score == hybrid_legacy_draft.score == 1.0


@pytest.mark.asyncio
async def test_process_rag_partial_coverage_missing_golden_and_multiple_invocations_fail_closed() -> None:
    dataset = load_dataset(DATASET)
    _catalog, cases = bridge_dataset_to_catalog(dataset, created_at=datetime(2026, 8, 23, tzinfo=timezone.utc))
    case = cases[CaseVersionRef("exact-http-evaluation-v2", "v1")]
    relevant = case.metadata["rag_ground_truth"]["retrieval"]["relevant_chunks"][0]
    identities = [(relevant["document_id"], relevant["chunk_id"])]
    partial_input, partial_trajectory = _process_rag_input(case, identities, coverage=Completeness.PARTIAL)
    spec = build_rag_baseline_suite(dataset, process_evidence=True).evaluator_specs[0]
    requirements = parse_process_requirements(spec.config_snapshot["process_evidence_requirements"])
    assert validate_process_requirements(partial_trajectory, requirements) == "REQUIRED_PROCESS_EVIDENCE_INVALID"

    resolver = RagMetricEvaluatorResolver()
    partial_draft = await EvaluationLoopService._evaluate(
        None, resolver.resolve(spec).evaluator, spec, partial_input, None
    )
    assert partial_draft.verdict.value == "ERROR"
    assert partial_draft.reason == "REQUIRED_PROCESS_EVIDENCE_INVALID"
    missing_golden = EvaluationInput(
        case_ref=CaseVersionRef(case.case_id, case.version), input_payload=case.input_payload, expected_output=None, assertion_specs=(),
        process_trajectory=partial_trajectory, metadata={"case": {}},
    )
    missing_draft = await resolver.resolve(spec).evaluator.evaluate(missing_golden, EvaluatorContext(spec))
    assert missing_draft.verdict.value == "INCONCLUSIVE" and missing_draft.score is None

    multiple_input, _ = _process_rag_input(case, identities, duplicate=True)
    multiple_draft = await EvaluationLoopService._evaluate(
        None, resolver.resolve(spec).evaluator, spec, multiple_input, None
    )
    assert multiple_draft.verdict.value == "ERROR"
    assert multiple_draft.metadata["exception_type"] == "ValueError"
