# ruff: noqa: D101, D102
"""Stage5 Phase3 WP0B BEIR SciFact Dataset Adapter、document projection 与 metrics tests."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.adapters.evaluation.rag_metrics import (
    DOCUMENT_EVALUATOR_VERSION,
    RagMetricEvaluator,
    RagMetricEvaluatorResolver,
)
from app.core.evaluation.beir_scifact import (
    BEIR_SCIFACT_DATASET_ID,
    BEIR_SCIFACT_DATASET_VERSION,
    CHECKSUM_MISMATCH,
    INTEGRITY_GAP,
    build_beir_scifact_dataset,
    load_beir_scifact_asset,
)
from app.core.evaluation.catalog import EvaluatorKind, EvaluatorSpec, ScoreDirection
from app.core.evaluation.dataset import (
    EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION,
    EVALUATION_DATASET_SCHEMA_VERSION,
    DocumentRelevance,
    DocumentRetrievalGroundTruth,
    EvaluationDataset,
    EvaluationCase,
    GroundTruth,
)
from app.core.evaluation.dataset_bridge import bridge_dataset_to_catalog
from app.core.evaluation.document_metrics import (
    DocumentProjection,
    UnknownBenchmarkDocumentError,
    project_artifact_to_documents,
)
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.execution import ExecutionOutcome, OutcomeKind
from app.core.evaluation.process_trajectory import (
    Availability, Completeness, CoverageV1, ProcessKind, Provenance, Sensitivity,
    build_process_evidence, build_process_trajectory, parse_process_requirements, validate_process_requirements,
)
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef, VersionRef
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1, build_rag_artifact_evidence
from app.services.evaluation.beir_scifact_baseline import build_beir_scifact_suite, execute_beir_scifact_baseline
from tests.unit.test_rag_artifact import artifact_payload
from tests.unit.test_wp3_coordinator import memory_persistence


def _write_asset(root: Path, *, qrels_rows: list[tuple[str, str, int]] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    corpus = [
        {"_id": "1", "title": "Doc One", "text": "Text one."},
        {"_id": "2", "title": "Doc Two", "text": "Text two."},
        {"_id": "3", "title": "Doc Three", "text": "Text three."},
    ]
    queries = [
        {"_id": "10", "text": "query one"},
        {"_id": "20", "text": "query two"},
        {"_id": "30", "text": "unused query"},
    ]
    rows = qrels_rows if qrels_rows is not None else [("10", "1", 1), ("20", "2", 1), ("20", "3", 1)]
    (root / "corpus.jsonl").write_text(
        "\n".join(json.dumps(item) for item in corpus) + "\n", encoding="utf-8"
    )
    (root / "queries.jsonl").write_text(
        "\n".join(json.dumps(item) for item in queries) + "\n", encoding="utf-8"
    )
    qrels_dir = root / "qrels"
    qrels_dir.mkdir()
    (qrels_dir / "test.tsv").write_text(
        "query-id\tcorpus-id\tscore\n"
        + "\n".join(f"{q}\t{c}\t{s}" for q, c, s in rows)
        + "\n",
        encoding="utf-8",
    )
    return root


def _item(document_id: str, chunk_id: str, *, rank: int, retrieval_rank: int) -> dict[str, object]:
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "rank": rank,
        "retrieval_rank": retrieval_rank,
        "rerank_rank": rank,
        "retrieval_score": 0.9,
        "retrieval_score_kind": "VECTOR_NORMALIZED_RELEVANCE",
        "retrieval_channels": ["VECTOR_ORIGINAL_AND_REWRITTEN"],
        "rerank_score": 0.8,
        "rerank_score_kind": "HEURISTIC_RERANK",
        "source": {
            "source_type": "md",
            "collection": "beir_scifact_eval_v1",
            "display_name": f"{document_id}.md",
            "document_version": "v1",
        },
        "page": None,
        "section": None,
        "sheet": None,
        "content_hash": chunk_id,
        "selected": False,
    }


def _artifact(
    retrieved: list[dict[str, object]],
    ranked: list[dict[str, object]] | None = None,
    selected: list[dict[str, object]] | None = None,
) -> RagEvaluationArtifactV1:
    payload = artifact_payload()
    payload["retrieved_items"] = retrieved
    payload["ranked_items"] = ranked if ranked is not None else retrieved
    payload["selected_items"] = selected or []
    payload["citations"] = []
    return RagEvaluationArtifactV1.model_validate(payload)


def _tiny_projection() -> DocumentProjection:
    return DocumentProjection({"local-d1": "1", "local-d2": "2", "local-d3": "3"})


@pytest.mark.asyncio
async def test_default_beir_baseline_runs_from_safe_process_evidence(tmp_path: Path, monkeypatch) -> None:
    asset = load_beir_scifact_asset(_write_asset(tmp_path), verify_checksums=False)

    class FakeTarget:
        def __init__(self, target_ref, base_url, *, bearer_token):
            self.target_ref = target_ref

        async def execute(self, request):
            query = request.input_payload["query"]
            document = "local-d1" if query == "query one" else "local-d2"
            item = _item(document, "chunk-1", rank=1, retrieval_rank=1)
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

    monkeypatch.setattr("app.services.evaluation.beir_scifact_baseline.LocalAgentHttpExecutionTarget", FakeTarget)
    persistence = memory_persistence()
    project_id = UUID("20000000-0000-4000-a000-000000000002")
    report = await execute_beir_scifact_baseline(
        persistence=persistence, project_id=project_id, asset=asset,
        base_url="http://localhost", bearer_token="test", document_projection=_tiny_projection(),
    )
    assert report["evaluated_retrieval_cases"] == 2
    assert report["metrics"]["document_recall_at_1"] == 0.75
    assert "private-query" not in str(report)
    attempts = await persistence.list_attempts(project_id, UUID(report["run_id"]))
    assert all([ref.kind for ref in attempt.outcome_evidence_refs] == ["process_trajectory"] for attempt in attempts)


class TestAssetLoading:
    def test_parses_tiny_asset_with_exact_statistics(self, tmp_path: Path) -> None:
        asset = load_beir_scifact_asset(_write_asset(tmp_path), verify_checksums=False)

        assert asset.statistics["corpus_document_count"] == 3
        assert asset.statistics["query_file_count"] == 3
        assert asset.statistics["test_query_count"] == 2
        assert asset.statistics["qrels_rows"] == 3
        assert asset.statistics["unique_relevant_document_count"] == 3
        assert asset.statistics["relevance_score_distribution"] == {1: 3}
        assert asset.statistics["relevant_documents_per_query_distribution"] == {1: 1, 2: 1}
        assert asset.statistics["mean_relevant_documents_per_query"] == 1.5
        assert asset.test_query_ids == ["10", "20"]

    def test_checksum_mismatch_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=CHECKSUM_MISMATCH):
            load_beir_scifact_asset(_write_asset(tmp_path), verify_checksums=True)

    def test_qrels_referencing_missing_corpus_id_is_integrity_gap(self, tmp_path: Path) -> None:
        root = _write_asset(tmp_path, qrels_rows=[("10", "999", 1)])
        with pytest.raises(ValueError, match=INTEGRITY_GAP):
            load_beir_scifact_asset(root, verify_checksums=False)

    def test_qrels_referencing_missing_query_id_is_integrity_gap(self, tmp_path: Path) -> None:
        root = _write_asset(tmp_path, qrels_rows=[("999", "1", 1)])
        with pytest.raises(ValueError, match=INTEGRITY_GAP):
            load_beir_scifact_asset(root, verify_checksums=False)

    def test_duplicate_qrels_follow_official_last_wins_semantics(self, tmp_path: Path) -> None:
        root = _write_asset(
            tmp_path, qrels_rows=[("10", "1", 1), ("10", "1", 1), ("20", "2", 1)]
        )
        asset = load_beir_scifact_asset(root, verify_checksums=False)

        assert asset.qrels["10"] == {"1": 1}
        assert asset.statistics["qrels_rows"] == 3
        assert asset.statistics["qrels_duplicate_rows"] == 1

    def test_conflicting_duplicate_qrels_fails_closed(self, tmp_path: Path) -> None:
        root = _write_asset(tmp_path, qrels_rows=[("10", "1", 1), ("10", "1", 2)])
        with pytest.raises(ValueError, match="conflicts"):
            load_beir_scifact_asset(root, verify_checksums=False)


class TestDatasetBuild:
    def test_dataset_uses_qrels_as_document_level_authority(self, tmp_path: Path) -> None:
        asset = load_beir_scifact_asset(_write_asset(tmp_path), verify_checksums=False)

        dataset = build_beir_scifact_dataset(asset)

        assert dataset.dataset_id == BEIR_SCIFACT_DATASET_ID
        assert dataset.version == BEIR_SCIFACT_DATASET_VERSION
        assert dataset.dataset_schema_version == EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION
        assert len(dataset) == 2
        first = dataset.cases[0]
        assert first.case_id == "scifact-test-10"
        assert first.input["query"] == "query one"
        truth = first.ground_truth.document_retrieval
        assert truth is not None
        assert truth.relevance_map() == {"1": 1}
        second = dataset.cases[1]
        assert second.ground_truth.document_retrieval is not None
        assert second.ground_truth.document_retrieval.relevance_map() == {"2": 1, "3": 1}
        assert first.metadata["benchmark_query_id"] == "10"
        assert first.metadata["relevance_kind"] == "binary"
        assert first.ground_truth.retrieval is None
        assert first.ground_truth.ranking is None

    def test_bridge_projects_document_ground_truth(self, tmp_path: Path) -> None:
        asset = load_beir_scifact_asset(_write_asset(tmp_path), verify_checksums=False)
        dataset = build_beir_scifact_dataset(asset)

        _catalog, cases = bridge_dataset_to_catalog(
            dataset, created_at=datetime(2026, 8, 23, tzinfo=timezone.utc)
        )

        case = cases[CaseVersionRef("scifact-test-20", "v1")]
        truth = case.metadata["rag_ground_truth"]["document_retrieval"]
        assert [dict(item) for item in truth["relevant_documents"]] == [
            {"document_id": "2", "relevance": 1},
            {"document_id": "3", "relevance": 1},
        ]

    def test_v1_dataset_must_not_declare_document_ground_truth(self) -> None:
        with pytest.raises(ValidationError, match="document ground truth"):
            EvaluationDataset(
                dataset_schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
                dataset_id="d",
                name="n",
                version="v1",
                cases=[
                    EvaluationCase(
                        case_id="c1",
                        name="case",
                        input={"agent_id": "knowledge_expert", "query": "q"},
                        ground_truth=GroundTruth(
                            document_retrieval=DocumentRetrievalGroundTruth(
                                relevant_documents=[DocumentRelevance(document_id="1", relevance=1)]
                            )
                        ),
                    )
                ],
            )

    def test_suite_uses_frozen_document_evaluator_identity(self, tmp_path: Path) -> None:
        asset = load_beir_scifact_asset(_write_asset(tmp_path), verify_checksums=False)
        dataset = build_beir_scifact_dataset(asset)

        suite = build_beir_scifact_suite(dataset)

        assert [spec.evaluator_id for spec in suite.evaluator_specs] == [
            "document_recall_at_1",
            "document_recall_at_3",
            "document_recall_at_5",
            "document_mrr",
            "document_ndcg_at_3",
            "document_ndcg_at_5",
        ]
        assert all(
            spec.evaluator_version == DOCUMENT_EVALUATOR_VERSION
            for spec in suite.evaluator_specs
        )
        assert all(spec.required_evidence_kinds == ("process_trajectory",) for spec in suite.evaluator_specs)


class TestDocumentProjection:
    def test_manifest_projection_mapping(self) -> None:
        manifest = {
            "documents": [
                {"document_id": "local-d1", "benchmark_document_id": "1", "source": "1.md"},
                {"document_id": "local-d2", "benchmark_document_id": "2", "source": "2.md"},
            ]
        }
        projection = DocumentProjection.from_manifest(manifest)

        assert projection.benchmark_document_id("local-d1") == "1"
        assert projection.localagent_document_id("2") == "local-d2"
        assert len(projection) == 2
        with pytest.raises(UnknownBenchmarkDocumentError):
            projection.benchmark_document_id("unknown")

    def test_duplicate_benchmark_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            DocumentProjection({"a": "1", "b": "1"})

    def test_projection_deduplicates_same_document_chunks(self) -> None:
        projection = _tiny_projection()
        artifact = _artifact(
            [
                _item("local-d1", "chunk-a", rank=1, retrieval_rank=1),
                _item("local-d2", "chunk-b", rank=2, retrieval_rank=2),
                _item("local-d1", "chunk-c", rank=3, retrieval_rank=3),
                _item("local-d3", "chunk-d", rank=4, retrieval_rank=4),
            ]
        )

        projected = project_artifact_to_documents(artifact, projection)

        retrieved = sorted(projected.retrieved_items, key=lambda item: item.retrieval_rank)
        assert [(item.document_id, item.retrieval_rank) for item in retrieved] == [
            ("1", 1),
            ("2", 2),
            ("3", 3),
        ]
        assert [(item.document_id, item.chunk_id) for item in projected.selected_items] == []

    def test_projection_fail_closed_on_unknown_document(self) -> None:
        projection = _tiny_projection()
        artifact = _artifact(
            [
                _item("local-d1", "chunk-a", rank=1, retrieval_rank=1),
                _item("unmapped", "chunk-x", rank=2, retrieval_rank=2),
            ]
        )
        with pytest.raises(UnknownBenchmarkDocumentError):
            project_artifact_to_documents(artifact, projection)

    def test_projection_preserves_layer_invariant(self) -> None:
        projection = _tiny_projection()
        retrieved = [
            _item("local-d1", "chunk-a", rank=1, retrieval_rank=1),
            _item("local-d2", "chunk-b", rank=2, retrieval_rank=2),
        ]
        ranked = [
            _item("local-d2", "chunk-b", rank=1, retrieval_rank=2),
            _item("local-d1", "chunk-a", rank=2, retrieval_rank=1),
        ]
        selected = [
            {
                "document_id": "local-d2",
                "chunk_id": "chunk-b",
                "selection_rank": 1,
                "context_block_id": "context-1",
                "citation_id": "Rr1-1",
                "context_content_hash": "xyz",
                "text": "evidence",
            }
        ]
        artifact = _artifact(retrieved, ranked, selected)

        projected = project_artifact_to_documents(artifact, projection)

        selected_ids = {(item.document_id, item.chunk_id) for item in projected.selected_items}
        ranked_ids = {(item.document_id, item.chunk_id) for item in projected.ranked_items}
        assert selected_ids <= ranked_ids
        assert projected.ranked_items[0].document_id == "2"
        assert projected.selected_items[0].document_id == "2"


class TestDocumentMetricsEvaluator:
    def _evaluation_input(self, relevant: dict[str, int]) -> EvaluationInput:
        truth = DocumentRetrievalGroundTruth(
            relevant_documents=[
                DocumentRelevance(document_id=doc_id, relevance=score)
                for doc_id, score in sorted(relevant.items())
            ]
        )
        case = EvaluationCase(
            case_id="scifact-test-10",
            name="case",
            input={"agent_id": "knowledge_expert", "query": "q"},
            ground_truth=GroundTruth(document_retrieval=truth),
            metadata={"benchmark_query_id": "10"},
        )
        _catalog, cases = bridge_dataset_to_catalog(
            EvaluationDataset(
                dataset_schema_version=EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION,
                dataset_id="d",
                name="n",
                version="v1",
                cases=[case],
            ),
            created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
        bridged = cases[CaseVersionRef("scifact-test-10", "v1")]
        artifact = _artifact(
            [
                _item("local-d1", "chunk-a", rank=1, retrieval_rank=1),
                _item("local-d2", "chunk-b", rank=2, retrieval_rank=2),
                _item("local-d1", "chunk-c", rank=3, retrieval_rank=3),
                _item("local-d3", "chunk-d", rank=4, retrieval_rank=4),
            ]
        )
        evidence = EvidenceRef(
            kind="rag_evaluation_artifact",
            identifier=artifact.artifact_id,
            metadata={"payload": artifact.model_dump(mode="json")},
        )
        return EvaluationInput(
            case_ref=CaseVersionRef("scifact-test-10", "v1"),
            input_payload=bridged.input_payload,
            expected_output=None,
            assertion_specs=(),
            actual_artifact=ArtifactRef("output", "localagent-run://test"),
            evidence_refs=(evidence,),
            metadata={"case": bridged.metadata},
        )

    def _spec(self, evaluator_id: str) -> EvaluatorSpec:
        return EvaluatorSpec(
            evaluator_id=evaluator_id,
            evaluator_version=DOCUMENT_EVALUATOR_VERSION,
            evaluator_kind=EvaluatorKind.DETERMINISTIC,
            config_ref=VersionRef("rag_document_metric_config", "test"),
            score_direction=ScoreDirection.HIGHER_IS_BETTER,
            config_snapshot={},
            required_evidence_kinds=("rag_evaluation_artifact",),
            score_range=(0.0, 1.0),
        )

    @pytest.mark.asyncio
    async def test_document_metrics_computed_on_projected_ranking(self) -> None:
        resolver = RagMetricEvaluatorResolver(document_projection=_tiny_projection())
        evaluation_input = self._evaluation_input({"1": 1, "3": 1})
        scores = {}
        for evaluator_id in (
            "document_recall_at_1",
            "document_recall_at_3",
            "document_recall_at_5",
            "document_mrr",
            "document_ndcg_at_3",
            "document_ndcg_at_5",
        ):
            spec = self._spec(evaluator_id)
            draft = await resolver.resolve(spec).evaluator.evaluate(
                evaluation_input, EvaluatorContext(spec)
            )
            scores[evaluator_id] = draft.score

        assert scores["document_recall_at_1"] == 0.5
        assert scores["document_recall_at_3"] == 1.0
        assert scores["document_recall_at_5"] == 1.0
        assert scores["document_mrr"] == 1.0
        expected_ndcg = (1.0 + 1.0 / 2.0) / (1.0 + 1.0 / math.log2(3))
        assert scores["document_ndcg_at_3"] == pytest.approx(expected_ndcg)
        assert scores["document_ndcg_at_5"] == pytest.approx(expected_ndcg)

    @pytest.mark.asyncio
    async def test_formal_document_metrics_match_legacy_without_rag_artifact(self, tmp_path: Path) -> None:
        asset = load_beir_scifact_asset(_write_asset(tmp_path), verify_checksums=False)
        suite = build_beir_scifact_suite(build_beir_scifact_dataset(asset))
        legacy_input = self._evaluation_input({"1": 1, "3": 1})
        artifact = RagEvaluationArtifactV1.model_validate(legacy_input.evidence_refs[0].metadata["payload"])
        record = build_process_evidence(
            evaluation_attempt_id="attempt-1", schema_version="stage11.wp4.v1", kind=ProcessKind.RETRIEVAL,
            producer_id="localagent.http.evaluation_v2", provenance=Provenance.RUNTIME,
            runtime_run_id="runtime-1", source_stream_id="rag.artifacts", source_event_id=artifact.artifact_id,
            projection_role="retrieval.snapshot", source_schema_ref=artifact.schema_version,
            sensitivity=Sensitivity.SAFE_METADATA,
            payload={
                "retrieval_id": artifact.retrieval_id, "retrieval_status": artifact.retrieval_status,
                "retrieved_items": tuple({
                    "document_id": item.document_id, "chunk_id": item.chunk_id,
                    "rank": item.rank, "retrieval_rank": item.retrieval_rank,
                } for item in artifact.retrieved_items),
                "ranked_items": tuple({
                    "document_id": item.document_id, "chunk_id": item.chunk_id,
                    "rank": item.rank, "retrieval_rank": item.retrieval_rank,
                } for item in artifact.ranked_items),
                "selected_items": (),
            },
        )
        trajectory = build_process_trajectory(
            schema_version="stage11.wp4.v1", trajectory_id="trajectory://attempt-1", project_id="project-1",
            evaluation_run_id="run-1", evaluation_attempt_id="attempt-1", dataset_id="d",
            case_ref=legacy_input.case_ref, execution_request_id="request-1",
            execution_target_ref={"target_id": "localagent"}, frozen_subject_ref={"subject": "s1"},
            sealed=True, execution_partial=False, body_policy_ref=None, source_manifests=(),
            coverage=(CoverageV1(kind=ProcessKind.RETRIEVAL, scope="attempt", availability=Availability.PRESENT,
                                 completeness=Completeness.COMPLETE),),
            records=(record,), edges=(),
        )
        formal_input = EvaluationInput(
            case_ref=legacy_input.case_ref, input_payload=legacy_input.input_payload,
            expected_output=None, assertion_specs=(), process_trajectory=trajectory,
            metadata=legacy_input.metadata,
        )
        resolver = RagMetricEvaluatorResolver(document_projection=_tiny_projection())
        for formal_spec in suite.evaluator_specs:
            requirements = parse_process_requirements(formal_spec.config_snapshot["process_evidence_requirements"])
            assert validate_process_requirements(trajectory, requirements) is None
            legacy_spec = self._spec(formal_spec.evaluator_id)
            formal = await resolver.resolve(formal_spec).evaluator.evaluate(formal_input, EvaluatorContext(formal_spec))
            legacy = await resolver.resolve(legacy_spec).evaluator.evaluate(legacy_input, EvaluatorContext(legacy_spec))
            assert formal.verdict == legacy.verdict
            assert formal.score == pytest.approx(legacy.score)

    @pytest.mark.asyncio
    async def test_relevant_doc_second_chunk_still_hits_document(self) -> None:
        resolver = RagMetricEvaluatorResolver(document_projection=_tiny_projection())
        # relevant doc "1" 只通过 rank 3 的第二个 chunk 命中，document rank 仍为 1。
        evaluation_input = self._evaluation_input({"1": 1})
        spec = self._spec("document_recall_at_1")
        draft = await resolver.resolve(spec).evaluator.evaluate(
            evaluation_input, EvaluatorContext(spec)
        )
        assert draft.score == 1.0

    @pytest.mark.asyncio
    async def test_missing_document_ground_truth_is_inconclusive(self) -> None:
        evaluator = RagMetricEvaluator(
            "document_recall_at_1", 1, document_projection=_tiny_projection()
        )
        spec = self._spec("document_recall_at_1")
        artifact = _artifact([_item("local-d1", "chunk-a", rank=1, retrieval_rank=1)])
        evidence = EvidenceRef(
            kind="rag_evaluation_artifact",
            identifier=artifact.artifact_id,
            metadata={"payload": artifact.model_dump(mode="json")},
        )
        evaluation_input = EvaluationInput(
            case_ref=CaseVersionRef("c", "v1"),
            input_payload={"agent_id": "a", "query": "q"},
            expected_output=None,
            assertion_specs=(),
            actual_artifact=ArtifactRef("output", "localagent-run://test"),
            evidence_refs=(evidence,),
            metadata={"case": {"rag_ground_truth": {}}},
        )
        draft = await evaluator.evaluate(evaluation_input, EvaluatorContext(spec))
        assert draft.verdict.value == "INCONCLUSIVE"
        assert draft.reason == "document_ground_truth_unavailable"

    @pytest.mark.asyncio
    async def test_unknown_projection_fails_closed_as_inconclusive(self) -> None:
        projection = DocumentProjection({"local-d1": "1"})
        evaluator = RagMetricEvaluator("document_recall_at_1", 1, document_projection=projection)
        artifact = _artifact([_item("unmapped", "chunk-x", rank=1, retrieval_rank=1)])
        evidence = EvidenceRef(
            kind="rag_evaluation_artifact",
            identifier=artifact.artifact_id,
            metadata={"payload": artifact.model_dump(mode="json")},
        )
        source = self._evaluation_input({"1": 1})
        evaluation_input = EvaluationInput(
            case_ref=source.case_ref,
            input_payload=source.input_payload,
            expected_output=None,
            assertion_specs=(),
            actual_artifact=source.actual_artifact,
            evidence_refs=(evidence,),
            metadata=source.metadata,
        )
        spec = self._spec("document_recall_at_1")
        draft = await evaluator.evaluate(evaluation_input, EvaluatorContext(spec))
        assert draft.verdict.value == "INCONCLUSIVE"
        assert draft.reason == "document_projection_unavailable"
        assert draft.metadata["source_status"] == "PROJECTION_FAILED"

    @pytest.mark.asyncio
    async def test_resolver_without_projection_is_inconclusive(self) -> None:
        resolver = RagMetricEvaluatorResolver()
        spec = self._spec("document_mrr")
        resolved = resolver.resolve(spec)
        evaluation_input = self._evaluation_input({"1": 1})
        draft = await resolved.evaluator.evaluate(evaluation_input, EvaluatorContext(spec))
        assert draft.verdict.value == "INCONCLUSIVE"
        assert draft.reason == "document_projection_unavailable"
