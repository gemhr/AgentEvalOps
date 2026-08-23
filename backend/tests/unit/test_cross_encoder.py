"""Cross-Encoder candidate 消费侧：sidecar 对齐、case 分析、evidence 与机械 gate."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import pytest

from app.services.evaluation import cross_encoder as ce

_METRIC_KEYS = tuple(ce.RRF_SCIFACT_BASELINE)

_EXPECTED_MODEL_REF = "approved/ce@v1"
_EXPECTED_DIGEST = "a" * 64


def _expected() -> ce.CeExpectedConfig:
    return ce.CeExpectedConfig(model_ref=_EXPECTED_MODEL_REF, asset_tree_sha256=_EXPECTED_DIGEST)


def _case(query_id: int, relevant_rank: int | None, chunk_ids: list[list[str]] | None = None) -> dict[str, Any]:
    relevant = f"relevant-{query_id}"
    ranking = [f"doc-{query_id}-{rank}" for rank in range(1, 9)]
    if relevant_rank is not None:
        ranking[relevant_rank - 1] = relevant
    if chunk_ids is None:
        chunk_ids = [[f"local-{query_id}", f"chunk-{query_id}"]]
    return {
        "benchmark_query_id": str(query_id),
        "query": f"query {query_id}",
        "qrels_document_ids": [relevant],
        "ranked_document_ids": ranking,
        "ranked_chunk_ids": chunk_ids,
        "retrieved_chunk_ids": chunk_ids,
        "retrieved_document_ids": ranking,
        "scores": {"document_mrr": 0.0},
    }


def _sidecar_item(document_id: str, chunk_id: str, pre_rank: int, post_rank: int, score: float = 0.5) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "content_hash": "c" * 40,
        "resolved_text_sha256": "0" * 64,
        "pre_ce_rrf_rank": pre_rank,
        "post_ce_rank": post_rank,
        "cross_encoder_score": score,
    }


def _success_row(query: str, items: list[dict[str, Any]], *, load_ms: float | None = 12.0) -> dict[str, Any]:
    return {
        "schema_version": ce.CE_SIDE_CAR_SCHEMA_VERSION,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "algorithm_ref": ce.CE_ALGORITHM_REF,
        "model_ref": _EXPECTED_MODEL_REF,
        "asset_tree_sha256": _EXPECTED_DIGEST,
        "device": "cpu",
        "cache_identity": ce.WP2_DENSE_CACHE_KEY,
        "cache_digests": {"manifest_sha256": "m"},
        "candidate_count": len(items),
        "status": "SUCCEEDED",
        "safe_code": None,
        "items": items,
        "latency_ms": {
            "model_load_latency_ms": load_ms,
            "inference_latency_ms": 1.0,
            "ce_total_latency_ms": 2.0,
        },
    }


def _failure_row(query: str, status: str = "FAILED", code: str = "CROSS_ENCODER_INFERENCE_EXCEPTION") -> dict[str, Any]:
    return {
        "schema_version": ce.CE_SIDE_CAR_SCHEMA_VERSION,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "model_ref": _EXPECTED_MODEL_REF,
        "asset_tree_sha256": _EXPECTED_DIGEST,
        "candidate_count": 8,
        "status": status,
        "safe_code": code,
        "items": [],
    }


def _empty_row(query: str) -> dict[str, Any]:
    return {
        "schema_version": ce.CE_SIDE_CAR_SCHEMA_VERSION,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "algorithm_ref": ce.CE_ALGORITHM_REF,
        "model_ref": _EXPECTED_MODEL_REF,
        "asset_tree_sha256": _EXPECTED_DIGEST,
        "device": "cpu",
        "cache_identity": ce.WP2_DENSE_CACHE_KEY,
        "candidate_count": 0,
        "status": "EMPTY",
        "items": [],
    }


def _ce_metrics(deltas: dict[str, float] | None = None) -> dict[str, float]:
    deltas = deltas or {}
    return {key: ce.RRF_SCIFACT_BASELINE[key] + deltas.get(key, 0.011) for key in _METRIC_KEYS}


def _synthetic_metrics(declines: dict[str, float] | None = None) -> dict[str, float]:
    declines = declines or {}
    return {
        key: ce.RRF_SYNTHETIC_BASELINE[key] + declines.get(key, 0.0) for key in ce.RRF_SYNTHETIC_BASELINE
    }


def _gate_kwargs(**overrides) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "scifact_metrics": _ce_metrics(),
        "synthetic_metrics": _synthetic_metrics(),
        "technical_failure_count": 0,
        "total_queries": 300,
        "case_guardrails_ok": True,
        "rank_transition_ok": True,
        "invariants_ok": True,
        "real_load_latency_present": True,
    }
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# sidecar alignment / cardinality
# ---------------------------------------------------------------------------


def test_sidecar_query_digest_alignment() -> None:
    report = {"case_results": [_case(1, 1), _case(2, 2)]}
    rows = [
        _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)]),
        _success_row("query 2", [_sidecar_item("local-2", "chunk-2", 1, 1)]),
    ]
    result = ce.align_ce_provenance(report, rows, expected=_expected())
    assert set(result["aligned"]) == {"1", "2"}
    assert result["failure_rows"] == ()


def test_alignment_duplicate_digest_fails_closed() -> None:
    report = {"case_results": [_case(1, 1)]}
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    with pytest.raises(ValueError, match="duplicate"):
        ce.align_ce_provenance(report, [row, row], expected=_expected())


def test_alignment_missing_row_fails_closed() -> None:
    report = {"case_results": [_case(1, 1), _case(2, 2)]}
    rows = [_success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])]
    with pytest.raises(ValueError, match="MISSING"):
        ce.align_ce_provenance(report, rows, expected=_expected())


def test_cardinality_mismatch_fails_closed() -> None:
    report = {"case_results": [_case(1, 1)]}
    row = _success_row(
        "query 1",
        [_sidecar_item("local-1", "chunk-1", 1, 1), _sidecar_item("local-1", "chunk-1", 2, 2)],
    )
    row["candidate_count"] = 1  # items 有 2 条
    with pytest.raises(ValueError, match="CARDINALITY"):
        ce.align_ce_provenance(report, [row], expected=_expected())


def test_success_provenance_item_field_validation() -> None:
    report = {"case_results": [_case(1, 1)]}
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    del row["items"][0]["chunk_id"]
    with pytest.raises(ValueError, match="fields mismatch"):
        ce.align_ce_provenance(report, [row], expected=_expected())


def test_success_provenance_identity_mismatch_fails_closed() -> None:
    report = {"case_results": [_case(1, 1)]}
    row = _success_row("query 1", [_sidecar_item("other", "other", 1, 1)])
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        ce.align_ce_provenance(report, [row], expected=_expected())


def test_failure_provenance_rows_are_not_aligned() -> None:
    report = {"case_results": [_case(1, 1)]}
    rows = [
        _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)]),
        _failure_row("query 99"),
    ]
    result = ce.align_ce_provenance(report, rows, expected=_expected())
    assert set(result["aligned"]) == {"1"}
    assert [row["status"] for row in result["failure_rows"]] == ["FAILED"]


def test_case_aligned_to_failure_row_fails_closed() -> None:
    report = {"case_results": [_case(1, 1)]}
    with pytest.raises(ValueError, match="MISMATCH"):
        ce.align_ce_provenance(report, [_failure_row("query 1")], expected=_expected())


# ---------------------------------------------------------------------------
# RRF -> CE analysis
# ---------------------------------------------------------------------------


def _analyze_reports(rrf_ranks: dict[int, int | None], ce_ranks: dict[int, int | None]):
    rrf_cases = [_case(qid, rrf_ranks.get(qid % 6)) for qid in range(1, 301)]
    ce_cases = [_case(qid, ce_ranks.get(qid % 6)) for qid in range(1, 301)]
    return {"case_results": rrf_cases}, {"case_results": ce_cases}


def test_analysis_quantifies_rescue_regression_and_rank_transition() -> None:
    rrf_ranks = {0: 2, 1: None, 2: 1, 3: 1, 4: 3}
    ce_ranks = {0: 1, 1: 1, 2: None, 3: 2, 4: 4}
    rrf_report, ce_report = _analyze_reports(rrf_ranks, ce_ranks)
    analysis = ce.analyze_ce_rerank(rrf_report, ce_report)
    assert analysis["query_count"] == 300
    assert analysis["by_k"]["top1"]["rrf_miss_ce_hit"] == 100
    assert analysis["by_k"]["top1"]["rrf_hit_ce_miss"] == 100
    assert analysis["by_k"]["top3"]["rrf_miss_ce_hit"] == 50
    assert analysis["by_k"]["top3"]["rrf_hit_ce_miss"] == 100
    assert analysis["by_k"]["top5"]["rrf_miss_ce_hit"] == 50
    assert analysis["by_k"]["top5"]["rrf_hit_ce_miss"] == 50
    assert analysis["rank_transition"]["improved"] == 100
    assert analysis["rank_transition"]["degraded"] == 150
    assert analysis["rank_transition"]["unchanged"] == 50
    assert analysis["case_guardrails_ok"] is False
    assert analysis["rank_transition_ok"] is False
    assert analysis["representative_cases"]["rrf_rescue_realized"] != "NOT_AVAILABLE"
    assert analysis["representative_cases"]["rrf_regression_realized"] != "NOT_AVAILABLE"


def test_analysis_noop_ce_keeps_guardrails_true() -> None:
    rrf_report, ce_report = _analyze_reports(
        {0: 1, 1: 1, 2: 1, 3: 1, 4: 1}, {0: 1, 1: 1, 2: 1, 3: 1, 4: 1}
    )
    analysis = ce.analyze_ce_rerank(rrf_report, ce_report)
    assert analysis["rank_transition"]["unchanged"] == 300
    assert analysis["case_guardrails_ok"] is True
    assert analysis["rank_transition_ok"] is True


def test_analysis_fails_closed_on_query_alignment() -> None:
    with pytest.raises(ValueError, match="ALIGNMENT"):
        ce.analyze_ce_rerank(
            {"case_results": [_case(1, 1)]},
            {"case_results": [_case(2, 1)]},
        )


# ---------------------------------------------------------------------------
# Mechanical gate
# ---------------------------------------------------------------------------


def test_gate_accepts_when_all_hard_gates_pass() -> None:
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs())
    assert gate["outcome"] == "ACCEPT"
    assert gate["failure_reasons"] == []


def test_gate_primary_below_threshold_rejects() -> None:
    gate = ce.evaluate_ce_acceptance_gate(
        **_gate_kwargs(scifact_metrics=_ce_metrics({"document_ndcg_at_3": 0.005}))
    )
    assert gate["outcome"] == "REJECT"
    assert "primary_ndcg3_delta_below_threshold" in gate["failure_reasons"]


def test_gate_exact_threshold_equality_passes() -> None:
    deltas = {key: 0.011 for key in _METRIC_KEYS}
    deltas["document_ndcg_at_3"] = ce.NDCG3_REQUIRED_DELTA
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(scifact_metrics=_ce_metrics(deltas)))
    assert gate["outcome"] == "ACCEPT"


def test_gate_epsilon_boundary() -> None:
    deltas = {key: 0.011 for key in _METRIC_KEYS}
    deltas["document_ndcg_at_3"] = ce.NDCG3_REQUIRED_DELTA - 1e-13
    assert ce.evaluate_ce_acceptance_gate(**_gate_kwargs(scifact_metrics=_ce_metrics(deltas)))["outcome"] == "ACCEPT"
    deltas["document_ndcg_at_3"] = ce.NDCG3_REQUIRED_DELTA - 1e-11
    assert ce.evaluate_ce_acceptance_gate(**_gate_kwargs(scifact_metrics=_ce_metrics(deltas)))["outcome"] == "REJECT"


@pytest.mark.parametrize("metric", [key for key in _METRIC_KEYS if key != ce.CE_PRIMARY_METRIC])
def test_gate_each_guardrail_rejects_when_below_bound(metric) -> None:
    deltas = {key: 0.011 for key in _METRIC_KEYS}
    deltas[metric] = -0.006
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(scifact_metrics=_ce_metrics(deltas)))
    assert gate["outcome"] == "REJECT"
    assert any(metric in reason for reason in gate["failure_reasons"])


def test_gate_technical_failure_forces_reject() -> None:
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(technical_failure_count=1))
    assert gate["outcome"] == "REJECT"
    assert "technical_failure_rate_must_be_zero" in gate["failure_reasons"]


def test_gate_candidate_invariant_forces_reject() -> None:
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(invariants_ok=False))
    assert gate["outcome"] == "REJECT"
    assert "candidate_cache_corpus_dataset_invariant_failed" in gate["failure_reasons"]


def test_gate_cache_corpus_invariant_forces_reject() -> None:
    # cache/corpus/Dataset invariant 在 gate 边界折叠为 invariants_ok（runner 单独派生）。
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(invariants_ok=False))
    assert gate["outcome"] == "REJECT"
    assert gate["checks"]["invariants_ok"] is False


def test_gate_case_guardrail_forces_reject() -> None:
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(case_guardrails_ok=False))
    assert gate["outcome"] == "REJECT"


def test_gate_rank_transition_forces_reject() -> None:
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(rank_transition_ok=False))
    assert gate["outcome"] == "REJECT"


def test_gate_synthetic_decline_forces_reject() -> None:
    gate = ce.evaluate_ce_acceptance_gate(
        **_gate_kwargs(synthetic_metrics=_synthetic_metrics({"document_ndcg_at_3": -0.001}))
    )
    assert gate["outcome"] == "REJECT"
    assert any("synthetic" in reason for reason in gate["failure_reasons"])


def test_gate_missing_real_inputs_is_not_evaluated_blocked() -> None:
    for kwargs in (
        _gate_kwargs(scifact_metrics=None),
        _gate_kwargs(synthetic_metrics=None),
        _gate_kwargs(technical_failure_count=None, total_queries=None),
        _gate_kwargs(real_load_latency_present=False),
        _gate_kwargs(case_guardrails_ok=None),
    ):
        gate = ce.evaluate_ce_acceptance_gate(**kwargs)
        assert gate["outcome"] == "NOT_EVALUATED_BLOCKED"


def test_gate_total_queries_incomplete_is_blocked() -> None:
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(total_queries=299))
    assert gate["outcome"] == "NOT_EVALUATED_BLOCKED"


# ---------------------------------------------------------------------------
# evidence / latency
# ---------------------------------------------------------------------------


def test_evidence_latency_is_diagnostic_and_does_not_affect_gate() -> None:
    report = {"case_results": [_case(1, 1)], "benchmark_kind": "BEIR_SCIFACT_LOCALAGENT_ADAPTED"}
    analysis = {"query_count": 300, "by_k": {}, "rank_transition": {}, "case_guardrails_ok": True, "rank_transition_ok": True}
    aligned = {"1": _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)], load_ms=12.0)}
    failure_rows: list[dict[str, Any]] = []
    gate_input = ce.evaluate_ce_acceptance_gate(**_gate_kwargs())
    invariants = {"all_ok": True}
    rrf_report = {"metrics": _ce_metrics()}
    evidence = ce.build_ce_candidate_evidence(
        report=report,
        analysis=analysis,
        aligned=aligned,
        failure_rows=failure_rows,
        invariants=invariants,
        gate_input=gate_input,
        rrf_report=rrf_report,
        expected=_expected(),
    )
    assert evidence["evidence_schema_version"] == ce.CE_EVIDENCE_SCHEMA_VERSION
    assert evidence["model_ref"] == "approved/ce@v1"
    assert evidence["latency_ms"]["model_load_cold_latency_ms"] == 12.0
    assert evidence["latency_ms"]["diagnostic_only"] is True
    assert evidence["technical_failure_rate"] == 0.0

    gate_with_failure = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(technical_failure_count=1))
    evidence_with_failure = ce.build_ce_candidate_evidence(
        report=report,
        analysis=analysis,
        aligned=aligned,
        failure_rows=[_failure_row("query 99")],
        invariants=invariants,
        gate_input=gate_with_failure,
        rrf_report=rrf_report,
        expected=_expected(),
    )
    assert evidence_with_failure["technical_failure_rate"] == 0.5
    # latency 只进 evidence；算法 gate outcome 由 metrics/guardrail/technical_failure 决定。
    assert evidence["gate"]["outcome"] == "ACCEPT"
    assert evidence_with_failure["gate"]["outcome"] == "REJECT"

    # 相同 gate 输入、仅 latency 不同 -> gate outcome 不变。
    gate_input_b = ce.evaluate_ce_acceptance_gate(**_gate_kwargs())
    evidence_latency_b = ce.build_ce_candidate_evidence(
        report=report,
        analysis=analysis,
        aligned={"1": _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)], load_ms=999.0)},
        failure_rows=[],
        invariants=invariants,
        gate_input=gate_input_b,
        rrf_report=rrf_report,
        expected=_expected(),
    )
    assert evidence["latency_ms"] != evidence_latency_b["latency_ms"]
    assert evidence_latency_b["gate"]["outcome"] == "ACCEPT"


# ---------------------------------------------------------------------------
# P1-01 runner success invariant（all_ok 只聚合显式 boolean，不依赖 int truthiness）
# ---------------------------------------------------------------------------


def _runner_invariant_report() -> dict[str, Any]:
    return {"case_results": [_case(1, 1)], "evaluated_retrieval_cases": 300}


def _runner_synthetic_report() -> dict[str, Any]:
    return {"dataset_case_count": 24}


def _runner_aligned() -> dict[str, Mapping[str, Any]]:
    return {"1": _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])}


def test_runner_invariant_all_ok_true_when_technical_failure_zero() -> None:
    invariants = ce.build_runner_invariants(
        dense_cache_key=ce.WP2_DENSE_CACHE_KEY,
        sparse_cache_key=ce.WP2_BM25_CACHE_KEY,
        corpus_checksums_ok=True,
        report=_runner_invariant_report(),
        synthetic_report=_runner_synthetic_report(),
        aligned=_runner_aligned(),
        failure_rows=[],
    )
    assert invariants["technical_failure_count"] == 0
    assert invariants["all_ok"] is True


def test_runner_invariant_all_ok_false_when_technical_failure_positive() -> None:
    invariants = ce.build_runner_invariants(
        dense_cache_key=ce.WP2_DENSE_CACHE_KEY,
        sparse_cache_key=ce.WP2_BM25_CACHE_KEY,
        corpus_checksums_ok=True,
        report=_runner_invariant_report(),
        synthetic_report=_runner_synthetic_report(),
        aligned=_runner_aligned(),
        failure_rows=[_failure_row("query 99")],
    )
    assert invariants["technical_failure_count"] == 1
    assert invariants["all_ok"] is False


def test_gate_accept_path_reachable_with_complete_runner_invariants() -> None:
    invariants = ce.build_runner_invariants(
        dense_cache_key=ce.WP2_DENSE_CACHE_KEY,
        sparse_cache_key=ce.WP2_BM25_CACHE_KEY,
        corpus_checksums_ok=True,
        report=_runner_invariant_report(),
        synthetic_report=_runner_synthetic_report(),
        aligned=_runner_aligned(),
        failure_rows=[],
    )
    assert invariants["all_ok"] is True
    gate = ce.evaluate_ce_acceptance_gate(**_gate_kwargs(invariants_ok=invariants["all_ok"]))
    assert gate["outcome"] == "ACCEPT"


# ---------------------------------------------------------------------------
# P1-03 approved provenance + exact candidate transition
# ---------------------------------------------------------------------------


def _two_chunk_case() -> dict[str, Any]:
    return _case(1, 1, chunk_ids=[["local-1", "chunk-1"], ["local-2", "chunk-2"]])


def _two_chunk_items() -> list[dict[str, Any]]:
    return [
        _sidecar_item("local-1", "chunk-1", 1, 1),
        _sidecar_item("local-2", "chunk-2", 2, 2),
    ]


def _align_fails(row: dict[str, Any], *, case: dict[str, Any] | None = None, match: str) -> None:
    report = {"case_results": [case if case is not None else _case(1, 1)]}
    with pytest.raises(ValueError, match=match):
        ce.align_ce_provenance(report, [row], expected=_expected())


def test_approved_missing_model_ref_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    del row["model_ref"]
    _align_fails(row, match="APPROVED_MISSING.*model_ref")


def test_approved_empty_model_ref_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    row["model_ref"] = ""
    _align_fails(row, match="APPROVED_MISSING.*model_ref")


def test_approved_wrong_model_ref_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    row["model_ref"] = "other/model"
    _align_fails(row, match="APPROVED_MISMATCH.*model_ref")


def test_approved_mixed_model_ref_fails_closed() -> None:
    report = {"case_results": [_case(1, 1), _case(2, 2)]}
    rows = [
        _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)]),
        _success_row("query 2", [_sidecar_item("local-2", "chunk-2", 1, 1)]),
    ]
    rows[1]["model_ref"] = "other/model"
    with pytest.raises(ValueError, match="APPROVED_MISMATCH.*model_ref"):
        ce.align_ce_provenance(report, rows, expected=_expected())


def test_approved_missing_asset_digest_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    del row["asset_tree_sha256"]
    _align_fails(row, match="APPROVED_MISSING.*asset_tree_sha256")


def test_approved_wrong_asset_digest_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    row["asset_tree_sha256"] = "b" * 64
    _align_fails(row, match="APPROVED_MISMATCH.*asset_tree_sha256")


def test_approved_mixed_asset_digest_fails_closed() -> None:
    report = {"case_results": [_case(1, 1), _case(2, 2)]}
    rows = [
        _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)]),
        _success_row("query 2", [_sidecar_item("local-2", "chunk-2", 1, 1)]),
    ]
    rows[1]["asset_tree_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="APPROVED_MISMATCH.*asset_tree_sha256"):
        ce.align_ce_provenance(report, rows, expected=_expected())


def test_approved_wrong_schema_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    row["schema_version"] = "other.v1"
    _align_fails(row, match="APPROVED_MISMATCH.*schema_version")


def test_approved_wrong_algorithm_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    row["algorithm_ref"] = "other-algo"
    _align_fails(row, match="APPROVED_MISMATCH.*algorithm_ref")


def test_approved_wrong_device_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    row["device"] = "cuda"
    _align_fails(row, match="APPROVED_MISMATCH.*device")


def test_approved_wrong_cache_identity_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    row["cache_identity"] = "other-cache-key"
    _align_fails(row, match="APPROVED_MISMATCH.*cache_identity")


def test_pre_order_wrong_fails_closed() -> None:
    row = _success_row(
        "query 1",
        [
            _sidecar_item("local-1", "chunk-1", 2, 2),
            _sidecar_item("local-2", "chunk-2", 1, 1),
        ],
    )
    _align_fails(row, case=_two_chunk_case(), match="TRANSITION_MISMATCH.*pre")


def test_post_order_wrong_fails_closed() -> None:
    row = _success_row(
        "query 1",
        [
            _sidecar_item("local-1", "chunk-1", 1, 2),
            _sidecar_item("local-2", "chunk-2", 2, 1),
        ],
    )
    _align_fails(row, case=_two_chunk_case(), match="TRANSITION_MISMATCH.*post")


def test_missing_candidate_fails_closed() -> None:
    row = _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)])
    _align_fails(row, case=_two_chunk_case(), match="IDENTITY_MISMATCH")


def test_extra_candidate_fails_closed() -> None:
    row = _success_row(
        "query 1",
        [
            _sidecar_item("local-1", "chunk-1", 1, 1),
            _sidecar_item("local-2", "chunk-2", 2, 2),
        ],
    )
    _align_fails(row, match="IDENTITY_MISMATCH")


def test_duplicate_candidate_fails_closed() -> None:
    row = _success_row(
        "query 1",
        [
            _sidecar_item("local-1", "chunk-1", 1, 1),
            _sidecar_item("local-1", "chunk-1", 2, 2),
            _sidecar_item("local-2", "chunk-2", 3, 3),
        ],
    )
    _align_fails(row, case=_two_chunk_case(), match="IDENTITY_DUPLICATE")


def test_fully_valid_provenance_passes() -> None:
    report = {"case_results": [_two_chunk_case()]}
    row = _success_row("query 1", _two_chunk_items())
    result = ce.align_ce_provenance(report, [row], expected=_expected())
    assert set(result["aligned"]) == {"1"}
    assert result["failure_rows"] == ()


# ---------------------------------------------------------------------------
# P1-03-A reject unmatched extra success rows
# ---------------------------------------------------------------------------


def test_extra_approved_success_row_fails_closed() -> None:
    """Report 只有 A；sidecar 含 A(approved) 与 X(approved)。X 未被任何 case 消费，必须拒绝."""
    report = {"case_results": [_case(1, 1)]}
    rows = [
        _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)]),
        _success_row("query 999", [_sidecar_item("local-999", "chunk-999", 1, 1)]),
    ]
    with pytest.raises(ValueError, match="EXTRA_SUCCESS"):
        ce.align_ce_provenance(report, rows, expected=_expected())


def test_extra_unapproved_success_row_fails_closed() -> None:
    """Report 只有 A；sidecar 含 A(approved) 与 X(model_ref=UNAPPROVED)。同样必须拒绝."""
    report = {"case_results": [_case(1, 1)]}
    rows = [
        _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)]),
        _success_row("query 999", [_sidecar_item("local-999", "chunk-999", 1, 1)]),
    ]
    rows[1]["model_ref"] = "UNAPPROVED"
    with pytest.raises(ValueError, match="EXTRA_SUCCESS"):
        ce.align_ce_provenance(report, rows, expected=_expected())


def test_extra_failure_row_is_known_limitation_and_allowed() -> None:
    """Failure row 不被 case 消费属于 P1-02 Accepted Known Limitation；不应被 extra-success 逻辑拒绝."""
    report = {"case_results": [_case(1, 1)]}
    rows = [
        _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)]),
        _failure_row("query 999"),
    ]
    result = ce.align_ce_provenance(report, rows, expected=_expected())
    assert set(result["aligned"]) == {"1"}
    assert [row["status"] for row in result["failure_rows"]] == ["FAILED"]


def test_exact_success_population_passes_order_independent() -> None:
    """Report A、B；sidecar A、B（顺序无关）全部 approved -> 通过."""
    report = {"case_results": [_case(1, 1), _case(2, 2)]}
    rows = [
        _success_row("query 2", [_sidecar_item("local-2", "chunk-2", 1, 1)]),
        _success_row("query 1", [_sidecar_item("local-1", "chunk-1", 1, 1)]),
    ]
    result = ce.align_ce_provenance(report, rows, expected=_expected())
    assert set(result["aligned"]) == {"1", "2"}
    assert result["failure_rows"] == ()


# ---------------------------------------------------------------------------
# P1-03-B EMPTY sidecar row 与 artifact same-N proof
# ---------------------------------------------------------------------------


def test_empty_row_with_nonempty_artifact_fails_closed() -> None:
    """Sidecar EMPTY 但 artifact retrieved/ranked 各含 1 个候选 -> fail closed."""
    case = _case(1, 1, chunk_ids=[["local-1", "chunk-1"]])
    report = {"case_results": [case]}
    with pytest.raises(ValueError, match="EMPTY_ARTIFACT_MISMATCH"):
        ce.align_ce_provenance(report, [_empty_row("query 1")], expected=_expected())


def test_empty_row_with_ranked_only_artifact_fails_closed() -> None:
    """Sidecar EMPTY；artifact retrieved=[]、ranked=[A] -> fail closed."""
    case = {**_case(1, 1, chunk_ids=[]), "ranked_chunk_ids": [["local-1", "chunk-1"]]}
    report = {"case_results": [case]}
    with pytest.raises(ValueError, match="EMPTY_ARTIFACT_MISMATCH"):
        ce.align_ce_provenance(report, [_empty_row("query 1")], expected=_expected())


def test_empty_row_with_retrieved_only_artifact_fails_closed() -> None:
    """Sidecar EMPTY；artifact retrieved=[A]、ranked=[] -> fail closed."""
    case = {**_case(1, 1, chunk_ids=[["local-1", "chunk-1"]]), "ranked_chunk_ids": []}
    report = {"case_results": [case]}
    with pytest.raises(ValueError, match="EMPTY_ARTIFACT_MISMATCH"):
        ce.align_ce_provenance(report, [_empty_row("query 1")], expected=_expected())


def test_empty_row_with_empty_artifact_passes() -> None:
    """Sidecar EMPTY 且 artifact retrieved=[]、ranked=[] -> 合法 EMPTY，通过."""
    case = _case(1, 1, chunk_ids=[])
    report = {"case_results": [case]}
    result = ce.align_ce_provenance(report, [_empty_row("query 1")], expected=_expected())
    assert set(result["aligned"]) == {"1"}
    assert result["failure_rows"] == ()


# ---------------------------------------------------------------------------
# P1-04 shared evidence query/chunk plaintext leakage
# ---------------------------------------------------------------------------


def test_evidence_does_not_leak_query_or_chunk_plaintext() -> None:
    import json as _json

    secret_query = "SECRET_QUERY_P1_04_ABC123"
    secret_chunk_text = "SECRET_CHUNK_P1_04_XYZ789"
    secret_chunk_sha256 = hashlib.sha256(secret_chunk_text.encode("utf-8")).hexdigest()
    rrf_cases: list[dict[str, Any]] = []
    ce_cases: list[dict[str, Any]] = []
    for qid in range(1, 301):
        chunk_ids = [["local-%d" % qid, "chunk-%d" % qid]]
        if qid == 1:
            rrf_cases.append(_case(qid, None, chunk_ids=chunk_ids))
            ce_case = _case(qid, 1, chunk_ids=chunk_ids)
            ce_case["query"] = secret_query
            ce_cases.append(ce_case)
        else:
            rrf_cases.append(_case(qid, 1, chunk_ids=chunk_ids))
            ce_cases.append(_case(qid, 1, chunk_ids=chunk_ids))
    rrf_report = {"case_results": rrf_cases}
    ce_report = {
        "benchmark_kind": "BEIR_SCIFACT_LOCALAGENT_ADAPTED",
        "metrics": _ce_metrics(),
        "case_results": ce_cases,
    }
    analysis = ce.analyze_ce_rerank(rrf_report, ce_report)
    rep = analysis["representative_cases"]["rrf_rescue_realized"]
    assert rep != "NOT_AVAILABLE"
    assert rep["query_id"] == "1"
    assert rep["query_sha256"] == hashlib.sha256(secret_query.encode("utf-8")).hexdigest()
    assert "query" not in rep

    secret_item = _sidecar_item("local-1", "chunk-1", 1, 1)
    secret_item["resolved_text_sha256"] = secret_chunk_sha256
    aligned = {"1": _success_row(secret_query, [secret_item])}
    gate_input = ce.evaluate_ce_acceptance_gate(**_gate_kwargs())
    evidence = ce.build_ce_candidate_evidence(
        report=ce_report,
        analysis=analysis,
        aligned=aligned,
        failure_rows=[],
        invariants={"all_ok": True},
        gate_input=gate_input,
        rrf_report=rrf_report,
        expected=_expected(),
    )
    serialized = _json.dumps(
        {"analysis": analysis, "evidence": evidence}, ensure_ascii=False, sort_keys=True
    )
    assert secret_query not in serialized
    assert secret_chunk_text not in serialized
    assert "query_sha256" in serialized
    assert secret_chunk_sha256 in serialized
    assert "cross_encoder_score" in serialized