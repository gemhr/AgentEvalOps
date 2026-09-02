# ruff: noqa: D415
"""WP3 配对 identity、固定指标和 Candidate Gate 合同测试。"""

from copy import deepcopy
from pathlib import Path

from app.core.evaluation.dataset import EvaluationCase
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1
from app.core.evaluation.wp3_candidate_gate import (
    WP3CaseClassification,
    WP3GateStatus,
    WP3IdentityMismatch,
    WP3RewriteFixture,
    WP3RewriteFixtureEntry,
    WP3RunIdentity,
    WP3RunSummary,
    WP3_METRICS,
    aggregate_metrics,
    classify_case,
    evaluated_settings_profile_sha256,
    evaluate_candidate_gate,
    validate_pair_identities,
    dataset_content_sha256,
    metrics_for_artifact,
)
from tests.unit.test_rag_artifact import artifact_payload
from app.core.evaluation.run_attempts import RunStatus
from app.services.evaluation.wp3_coordinator import (
    WP3CaseObservation,
    WP3ExperimentDescriptor,
    WP3PairedCoordinator,
    WP3StrategyRunReceipt,
)
from types import SimpleNamespace
import pytest


def _identity(role: str, strategy: str = "BASELINE") -> WP3RunIdentity:
    return WP3RunIdentity(
        experiment_id="exp-1", pair_id="pair-1", role=role, repeat_index=0,
        retrieval_strategy=strategy, dataset_id="rag-evaluation-dataset", dataset_version="v1",
        dataset_digest="d" * 64, dataset_content_sha256="d" * 64,
        suite_id="rag-baseline-suite", suite_version="v1",
        execution_target_id="localagent-http", execution_target_kind="HTTP",
        execution_target_version="v2", endpoint_contract="/api/runtime/evaluation-execute/v2",
        localagent_version="commit-1", localagent_head_commit="h" * 40,
        working_tree_diff_sha256="w" * 64,
        generation_id="generation-1", provenance_sha256="p" * 64,
        corpus_id="corpus-1", source_manifest_sha256="s" * 64, chunk_policy_sha256="c" * 64,
        chunk_manifest_sha256="m" * 64, document_count=2, chunk_count=4,
        embedding_identity="e" * 64,
        evaluated_settings_profile_sha256=evaluated_settings_profile_sha256({
            "rag_top_k": 3, "rag_min_score": 0.2,
            "knowledge_collection_name": "huawei_wiki_collection",
            "embedding_identity": "e" * 64, "chunk_policy_sha256": "c" * 64,
            "query_rewrite_policy": "fixture-replay.v1", "candidate_limit": 8,
        }),
        rewrite_policy="fixture-replay.v1", rewrite_fixture_id="f" * 64,
        started_at="2026-01-01T00:00:00+00:00", run_id=role.lower(),
    )


def _summary(*, failures: int = 0, completed: int = 24, degraded: int = 0, latency: tuple[float, ...] = (100.0,)) -> WP3RunSummary:
    metrics = {metric: 0.5 for metric in WP3_METRICS}
    return WP3RunSummary(24, completed, failures, degraded, 0, metrics, latency)


def test_pair_identity_allows_role_and_strategy_only() -> None:
    validate_pair_identities(_identity("BASELINE"), _identity("CANDIDATE", "HYBRID_RRF"))


def test_identity_digest_is_deterministic_and_metric_set_is_frozen() -> None:
    assert _identity("BASELINE").identity_sha256 == _identity("BASELINE").identity_sha256
    assert set(aggregate_metrics([{metric: 1.0 for metric in WP3_METRICS}])) == set(WP3_METRICS)


def test_dataset_content_identity_uses_raw_file_bytes(tmp_path: Path) -> None:
    path = tmp_path / "dataset.json"
    path.write_bytes(b'{"b":2, "a":1}\n')
    assert dataset_content_sha256(path) == "ab9b84f4601e1853896d1512348b9d84bf0a4a9e7426eb70fbc980f02b435818"


def test_valid_empty_artifact_contributes_zero_retrieval_metrics() -> None:
    artifact = RagEvaluationArtifactV1.model_validate(
        artifact_payload(retrieval_status="EMPTY", retrieved_items=[], ranked_items=[], selected_items=[])
    )
    case = SimpleNamespace(ground_truth=SimpleNamespace(retrieval=object(), ranking=object()))
    assert metrics_for_artifact(case, artifact) == {metric: 0.0 for metric in WP3_METRICS}


def test_settings_profile_is_deterministic_and_excludes_allowed_strategy_difference() -> None:
    profile = {
        "rag_top_k": 3, "rag_min_score": 0.2,
        "knowledge_collection_name": "huawei_wiki_collection",
        "embedding_identity": "e" * 64, "chunk_policy_sha256": "c" * 64,
        "query_rewrite_policy": "fixture-replay.v1", "candidate_limit": 8,
    }
    assert evaluated_settings_profile_sha256(profile) == evaluated_settings_profile_sha256(dict(reversed(profile.items())))


def test_baseline_failure_is_inconclusive_but_candidate_failure_is_fail() -> None:
    counts = {item: 0 for item in WP3CaseClassification}
    baseline_failed = evaluate_candidate_gate(
        baseline=_summary(failures=1), candidate=_summary(), pair_valid=True,
        provenance_valid=True, regression_counts=counts,
    )
    assert baseline_failed["HYBRID_CANDIDATE_GATE"] is WP3GateStatus.INCONCLUSIVE
    candidate_failed = evaluate_candidate_gate(
        baseline=_summary(), candidate=_summary(failures=1), pair_valid=True,
        provenance_valid=True, regression_counts=counts,
    )
    assert candidate_failed["EXECUTION_RELIABILITY_GATE"] is WP3GateStatus.FAIL


def test_invalid_pair_short_circuits_all_downstream_gates() -> None:
    counts = {item: 0 for item in WP3CaseClassification}
    gates = evaluate_candidate_gate(
        baseline=_summary(), candidate=_summary(), pair_valid=False,
        provenance_valid=True, regression_counts=counts,
    )
    assert all(
        gates[name] is WP3GateStatus.INCONCLUSIVE
        for name in (
            "FAIRNESS_GATE", "PROVENANCE_CONSISTENCY_GATE", "EXECUTION_RELIABILITY_GATE",
            "QUALITY_GATE", "PER_CASE_REGRESSION_GATE", "LATENCY_GATE", "HYBRID_CANDIDATE_GATE",
        )
    )


def test_rewrite_fixture_digest_is_immutable_and_complete() -> None:
    entries = tuple(
        WP3RewriteFixtureEntry.build(f"case-{index}", f"query-{index}", f"rewritten-{index}")
        for index in range(24)
    )
    fixture = WP3RewriteFixture("v1", entries)
    assert fixture.resolve(case_id="case-3", query="query-3").rewritten_query == "rewritten-3"


@pytest.mark.asyncio
async def test_invalid_baseline_never_starts_candidate() -> None:
    descriptor = WP3ExperimentDescriptor(
        "exp-1", "pair-1", "rag-evaluation-dataset", "v1", "d" * 64, "d" * 64,
        "rag-baseline-suite", "v1", "target", "HTTP", "v2", "p" * 64, "f" * 64,
    )
    calls: list[str] = []

    async def execute(role, _identity):
        calls.append(role)
        return WP3StrategyRunReceipt(
            run=SimpleNamespace(status=RunStatus.FAILED), cases=(), identity_persisted=False,
            generation_rewrite_valid=False, shutdown_clean=False, port_released=False,
            writable_state_isolated=False,
        )

    with pytest.raises(Exception, match="candidate must not start"):
        await WP3PairedCoordinator(descriptor).run(
            baseline_identity=_identity("BASELINE"),
            candidate_identity=_identity("CANDIDATE", "HYBRID_RRF"),
            run_strategy=execute,
        )
    assert calls == ["BASELINE"]


def test_hidden_retrieval_setting_mismatch_is_rejected() -> None:
    left = _identity("BASELINE")
    right = _identity("CANDIDATE", "HYBRID_RRF")
    object.__setattr__(
        right,
        "evaluated_settings_profile_sha256",
        evaluated_settings_profile_sha256({
            "rag_top_k": 8, "rag_min_score": 0.2,
            "knowledge_collection_name": "huawei_wiki_collection",
            "embedding_identity": "e" * 64, "chunk_policy_sha256": "c" * 64,
            "query_rewrite_policy": "fixture-replay.v1", "candidate_limit": 8,
        }),
    )
    with pytest.raises(WP3IdentityMismatch, match="evaluated_settings_profile_sha256"):
        validate_pair_identities(left, right)


def test_settings_and_rewrite_mismatch_short_circuit_inconclusive() -> None:
    counts = {item: 0 for item in WP3CaseClassification}
    settings = evaluate_candidate_gate(
        baseline=_summary(), candidate=_summary(), pair_valid=True,
        provenance_valid=True, regression_counts=counts, settings_valid=False,
    )
    rewrite = evaluate_candidate_gate(
        baseline=_summary(), candidate=_summary(), pair_valid=True,
        provenance_valid=True, regression_counts=counts, rewrite_valid=False,
    )
    provenance = evaluate_candidate_gate(
        baseline=_summary(), candidate=_summary(), pair_valid=True,
        provenance_valid=False, regression_counts=counts,
    )
    for gates in (settings, rewrite, provenance):
        assert gates["QUALITY_GATE"] is WP3GateStatus.INCONCLUSIVE
        assert gates["PER_CASE_REGRESSION_GATE"] is WP3GateStatus.INCONCLUSIVE
        assert gates["HYBRID_CANDIDATE_GATE"] is WP3GateStatus.INCONCLUSIVE


def test_alignment_not_comparable_is_inconclusive_not_fail() -> None:
    counts = {item: 0 for item in WP3CaseClassification}
    counts[WP3CaseClassification.NOT_COMPARABLE] = 1
    gates = evaluate_candidate_gate(
        baseline=_summary(), candidate=_summary(), pair_valid=True,
        provenance_valid=True, regression_counts=counts,
    )
    assert gates["HYBRID_CANDIDATE_GATE"] is WP3GateStatus.INCONCLUSIVE
    assert gates["QUALITY_GATE"] is WP3GateStatus.INCONCLUSIVE
    assert gates["PER_CASE_REGRESSION_GATE"] is WP3GateStatus.INCONCLUSIVE


def test_candidate_execution_failure_is_reliability_fail_even_with_not_comparable() -> None:
    counts = {item: 0 for item in WP3CaseClassification}
    counts[WP3CaseClassification.NOT_COMPARABLE] = 1
    gates = evaluate_candidate_gate(
        baseline=_summary(), candidate=_summary(failures=1), pair_valid=True,
        provenance_valid=True, regression_counts=counts,
    )
    assert gates["EXECUTION_RELIABILITY_GATE"] is WP3GateStatus.FAIL
    assert gates["HYBRID_CANDIDATE_GATE"] is WP3GateStatus.FAIL


def test_valid_quality_regression_is_fail_and_valid_evidence_is_pass() -> None:
    counts = {item: 0 for item in WP3CaseClassification}
    weak_candidate = WP3RunSummary(
        24, 24, 0, 0, 0, {metric: 0.4 for metric in WP3_METRICS}, (100.0,),
    )
    failed = evaluate_candidate_gate(
        baseline=_summary(), candidate=weak_candidate, pair_valid=True,
        provenance_valid=True, regression_counts=counts,
    )
    assert failed["QUALITY_GATE"] is WP3GateStatus.FAIL
    assert failed["HYBRID_CANDIDATE_GATE"] is WP3GateStatus.FAIL
    stronger = WP3RunSummary(
        24, 24, 0, 0, 0,
        {metric: 0.6 if metric != "ndcg_at_3" else 0.55 for metric in WP3_METRICS},
        (110.0,),
    )
    passed = evaluate_candidate_gate(
        baseline=_summary(), candidate=stronger, pair_valid=True,
        provenance_valid=True, regression_counts=counts,
    )
    assert passed["HYBRID_CANDIDATE_GATE"] is WP3GateStatus.PASS


def _ranked(document_id: str, chunk_id: str, rank: int) -> dict[str, object]:
    item = deepcopy(artifact_payload()["retrieved_items"][0])
    item.update({"document_id": document_id, "chunk_id": chunk_id, "rank": rank, "retrieval_rank": rank, "rerank_rank": rank, "selected": rank == 1})
    return item


def _case() -> EvaluationCase:
    return EvaluationCase.model_validate(
        {
            "case_id": "case-identity",
            "name": "identity severe",
            "input": {"agent_id": "knowledge_expert", "query": "q"},
            "ground_truth": {
                "retrieval": {
                    "relevant_chunks": [
                        {"document_id": "doc-a", "chunk_id": "c1"},
                        {"document_id": "doc-b", "chunk_id": "c2"},
                    ]
                },
                "ranking": {
                    "graded_relevance": [
                        {"document_id": "doc-a", "chunk_id": "c1", "relevance": 3},
                        {"document_id": "doc-b", "chunk_id": "c2", "relevance": 2},
                    ]
                },
            },
        }
    )


def _artifact_for(identities: list[tuple[str, str]]) -> RagEvaluationArtifactV1:
    items = [_ranked(document_id, chunk_id, index) for index, (document_id, chunk_id) in enumerate(identities, start=1)]
    first = items[0]
    payload = artifact_payload(
        retrieved_items=items,
        ranked_items=items,
        selected_items=[{
            "document_id": first["document_id"],
            "chunk_id": first["chunk_id"],
            "selection_rank": 1,
            "context_block_id": "context-1",
            "citation_id": "Rr1-1",
            "context_content_hash": "xyz",
            "text": "evidence",
        }],
        citations=[{
            "citation_id": "Rr1-1",
            "document_id": first["document_id"],
            "chunk_id": first["chunk_id"],
            "context_block_id": "context-1",
            "context_content_hash": "xyz",
            "display_label": "x.md",
            "page": 1,
            "section": "S",
        }],
    )
    return RagEvaluationArtifactV1.model_validate(payload)


def test_identity_level_top3_loss_is_severe_and_mixed_rank_is_severe() -> None:
    metrics = {metric: 1.0 for metric in WP3_METRICS}
    candidate_metrics = {metric: 1.0 for metric in WP3_METRICS}
    lost = classify_case(
        metrics, candidate_metrics, case=_case(),
        baseline_artifact=_artifact_for([("doc-a", "c1")]),
        candidate_artifact=_artifact_for([("doc-other", "c9")]),
    )
    assert lost is WP3CaseClassification.SEVERE_REGRESSION
    mixed = classify_case(
        metrics, candidate_metrics, case=_case(),
        baseline_artifact=_artifact_for([("doc-a", "c1"), ("doc-b", "c2")]),
        candidate_artifact=_artifact_for([("doc-b", "c2")]),
    )
    assert mixed is not WP3CaseClassification.SEVERE_REGRESSION
    swapped = classify_case(
        metrics, candidate_metrics, case=_case(),
        baseline_artifact=_artifact_for([("doc-a", "c1")]),
        candidate_artifact=_artifact_for([("doc-b", "c2")]),
    )
    assert swapped is WP3CaseClassification.SEVERE_REGRESSION


def test_no_false_severe_when_baseline_top3_identity_is_retained() -> None:
    metrics = {metric: 0.5 for metric in WP3_METRICS}
    classification = classify_case(
        metrics, metrics, case=_case(),
        baseline_artifact=_artifact_for([("doc-a", "c1")]),
        candidate_artifact=_artifact_for([("doc-a", "c1"), ("doc-other", "c9")]),
    )
    assert classification is WP3CaseClassification.UNCHANGED


@pytest.mark.asyncio
async def test_invalid_baseline_identity_and_cleanup_never_start_candidate() -> None:
    descriptor = WP3ExperimentDescriptor(
        "exp-1", "pair-1", "rag-evaluation-dataset", "v1", "d" * 64, "d" * 64,
        "rag-baseline-suite", "v1", "target", "HTTP", "v2", "p" * 64, "f" * 64,
    )

    async def invalid_identity(role, _identity):
        return WP3StrategyRunReceipt(
            run=SimpleNamespace(status=RunStatus.COMPLETED),
            cases=tuple(WP3CaseObservation(f"c{index}", None, "SUCCEEDED", 1.0) for index in range(24)),
            identity_persisted=False, generation_rewrite_valid=True,
            shutdown_clean=True, port_released=True, writable_state_isolated=True,
        )

    calls: list[str] = []

    async def tracked(role, identity):
        calls.append(role)
        return await invalid_identity(role, identity)

    with pytest.raises(Exception, match="candidate must not start"):
        await WP3PairedCoordinator(descriptor).run(
            baseline_identity=_identity("BASELINE"),
            candidate_identity=_identity("CANDIDATE", "HYBRID_RRF"),
            run_strategy=tracked,
        )
    assert calls == ["BASELINE"]

    async def failed_cleanup(role, _identity):
        calls.append(f"cleanup-{role}")
        return WP3StrategyRunReceipt(
            run=SimpleNamespace(status=RunStatus.COMPLETED),
            cases=tuple(WP3CaseObservation(f"c{index}", None, "SUCCEEDED", 1.0) for index in range(24)),
            identity_persisted=True, generation_rewrite_valid=True,
            shutdown_clean=True, port_released=False, writable_state_isolated=True,
        )

    calls.clear()
    with pytest.raises(Exception, match="candidate must not start"):
        await WP3PairedCoordinator(descriptor).run(
            baseline_identity=_identity("BASELINE"),
            candidate_identity=_identity("CANDIDATE", "HYBRID_RRF"),
            run_strategy=failed_cleanup,
        )
    assert calls == ["cleanup-BASELINE"]
