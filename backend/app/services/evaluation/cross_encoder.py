"""Stage5 Phase3 WP3 Cross-Encoder candidate 评估、evidence 与机械 acceptance gate.

本模块只做消费侧：fail-closed sidecar 对齐、provenance 基数校验、RRF->CE case 分析、
versioned evidence 构造与机械 gate。不做任何 ML inference，不修改数据库 schema，
不新增 Evaluation Domain。

Gate 常量在读取任何 CE result 之前冻结（``CE_CANDIDATE_ACCEPTANCE_GATE.v1``）；
不允许根据结果修改 threshold。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import mean, median
from typing import Any

CE_CANDIDATE_ACCEPTANCE_GATE = "CE_CANDIDATE_ACCEPTANCE_GATE.v1"
CE_EVIDENCE_SCHEMA_VERSION = "beir-scifact-cross-encoder-candidate.v1"
CE_SIDE_CAR_SCHEMA_VERSION = "localagent-cross-encoder-provenance.v1"
CE_ALGORITHM_REF = "cross-encoder-rerank.v1"
CE_PRIMARY_METRIC = "document_ndcg_at_3"

NUMERIC_COMPARISON_EPSILON = 1e-12
NDCG3_REQUIRED_DELTA = 0.0100000000
GUARDRAIL_MIN_DELTA = -0.0050000000

# WP2 RRF actual（Codex Decision §13.2），真实冻结基线。
RRF_SCIFACT_BASELINE: dict[str, float] = {
    "document_recall_at_1": 0.5566666666666666,
    "document_recall_at_3": 0.7084444444444444,
    "document_recall_at_5": 0.7878333333333334,
    "document_mrr": 0.6668928571428572,
    "document_ndcg_at_3": 0.6552275721605366,
    "document_ndcg_at_5": 0.6888253015619742,
}

# WP2 RRF synthetic（Codex Decision §13.3）。
RRF_SYNTHETIC_BASELINE: dict[str, float] = {
    "document_recall_at_1": 0.8666666666666667,
    "document_recall_at_3": 1.0,
    "document_recall_at_5": 1.0,
    "document_mrr": 1.0,
    "document_ndcg_at_3": 0.9876856010773168,
    "document_ndcg_at_5": 0.9876856010773168,
}

_METRIC_KEYS: tuple[str, ...] = tuple(RRF_SCIFACT_BASELINE)
_CE_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "EMPTY"})
_CE_FAILURE_STATUSES = frozenset({"FAILED", "TIMED_OUT", "CANCELLED", "DEGRADED"})
_CE_ITEM_FIELDS = frozenset(
    {
        "document_id",
        "chunk_id",
        "content_hash",
        "resolved_text_sha256",
        "pre_ce_rrf_rank",
        "post_ce_rank",
        "cross_encoder_score",
    }
)
_SCIFACT_QUERY_COUNT = 300

# WP2 冻结的 Dense/BM25 cache identity（candidate/cache invariant 校验目标）。
WP2_DENSE_CACHE_KEY = "b63c0bbd115150da766b84a80331b332010812bbb757a6782df9ae2224ca8f46"
WP2_BM25_CACHE_KEY = "594c9c95a3b6f29bcd2fcd738e23ee345427d06b49a70ac3149544a1a4f8f84b"


@dataclass(frozen=True, slots=True)
class CeExpectedConfig:
    """批准 run 的 expected provenance facts；来自批准配置或冻结常量，不由 sidecar 自证."""

    model_ref: str
    asset_tree_sha256: str
    schema_version: str = CE_SIDE_CAR_SCHEMA_VERSION
    algorithm_ref: str = CE_ALGORITHM_REF
    device: str = "cpu"
    cache_identity: str = WP2_DENSE_CACHE_KEY

    def __post_init__(self) -> None:
        """校验必需批准字段非空."""
        for name in ("model_ref", "asset_tree_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"CE_EXPECTED_CONFIG_INVALID: {name} must be non-empty")


# ---------------------------------------------------------------------------
# 对齐与基数校验（fail closed）
# ---------------------------------------------------------------------------


def _query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _ordered_query_ids(values: Mapping[str, Any]) -> list[str]:
    return sorted(values, key=lambda value: (int(value), value))


def _validate_approved_provenance(row: Mapping[str, Any], expected: CeExpectedConfig) -> None:
    """每个 success row 的批准 provenance facts 必须 exact match expected；缺失/空/错误/mixed 均 fail closed."""
    for field, expected_value in (
        ("schema_version", expected.schema_version),
        ("algorithm_ref", expected.algorithm_ref),
        ("model_ref", expected.model_ref),
        ("asset_tree_sha256", expected.asset_tree_sha256),
        ("device", expected.device),
        ("cache_identity", expected.cache_identity),
    ):
        value = row.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"CE_PROVENANCE_APPROVED_MISSING: {field}")
        if value != expected_value:
            raise ValueError(f"CE_PROVENANCE_APPROVED_MISMATCH: {field}")


def _exact_identity_sequence(
    items: Sequence[Mapping[str, Any]],
    rank_field: str,
    artifact_ids: Any,
    *,
    label: str,
) -> None:
    """按 rank 排序 items 后，逐项与 artifact id 序列 exact match；顺序错误/长度不同均 fail closed."""
    if not isinstance(artifact_ids, list):
        raise ValueError(f"CE_PROVENANCE_TRANSITION_MISSING: artifact {label} ids unavailable")
    if len(artifact_ids) != len(items):
        raise ValueError(f"CE_PROVENANCE_TRANSITION_LENGTH: {label} ids length differs")
    ordered = sorted(items, key=lambda item: int(item[rank_field]))
    actual = [(str(item["document_id"]), str(item["chunk_id"])) for item in ordered]
    expected = [(str(entry[0]), str(entry[1])) for entry in artifact_ids]
    if actual != expected:
        raise ValueError(f"CE_PROVENANCE_TRANSITION_MISMATCH: {label} order does not match artifact")


def _validate_success_row(
    row: Mapping[str, Any], case: Mapping[str, Any], expected: CeExpectedConfig
) -> None:
    """校验成功 case 的 CE sidecar row：schema、基数、identity set、score、rank 与 exact transition."""
    _validate_approved_provenance(row, expected)
    if row.get("schema_version") != CE_SIDE_CAR_SCHEMA_VERSION:
        raise ValueError("CE_PROVENANCE_INVALID: unexpected schema_version")
    status = row.get("status")
    if status not in _CE_SUCCESS_STATUSES:
        raise ValueError("CE_PROVENANCE_INVALID: unexpected status")
    candidate_count = row.get("candidate_count")
    items = row.get("items")
    if not isinstance(candidate_count, int) or not isinstance(items, list):
        raise ValueError("CE_PROVENANCE_INVALID: candidate_count/items malformed")
    if status == "EMPTY":
        if candidate_count != 0 or items:
            raise ValueError("CE_PROVENANCE_CARDINALITY_FAILED: EMPTY row must have 0 items")
        for field, label in (("retrieved_chunk_ids", "pre"), ("ranked_chunk_ids", "post")):
            artifact_ids = case.get(field)
            if not isinstance(artifact_ids, list):
                raise ValueError(f"CE_PROVENANCE_TRANSITION_MISSING: artifact {label} ids unavailable")
            if artifact_ids:
                raise ValueError(
                    f"CE_PROVENANCE_EMPTY_ARTIFACT_MISMATCH: EMPTY row but artifact {label} ids non-empty"
                )
        return
    if candidate_count != len(items):
        raise ValueError("CE_PROVENANCE_CARDINALITY_FAILED: candidate_count does not match items")
    if candidate_count < 1 or candidate_count > 8:
        raise ValueError("CE_PROVENANCE_CARDINALITY_FAILED: candidate_count out of range")
    for item in items:
        if not isinstance(item, Mapping) or set(item) != _CE_ITEM_FIELDS:
            raise ValueError("CE_PROVENANCE_INVALID: item fields mismatch")
        for field in ("document_id", "chunk_id", "content_hash", "resolved_text_sha256"):
            if not isinstance(item.get(field), str) or not item.get(field):
                raise ValueError("CE_PROVENANCE_INVALID: item string field empty")
        for field in ("pre_ce_rrf_rank", "post_ce_rank"):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("CE_PROVENANCE_INVALID: invalid rank")
        score = item.get("cross_encoder_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            raise ValueError("CE_PROVENANCE_INVALID: invalid cross_encoder_score")
    identities = {(str(item["document_id"]), str(item["chunk_id"])) for item in items}
    if len(identities) != len(items):
        raise ValueError("CE_PROVENANCE_IDENTITY_DUPLICATE: duplicate candidate identity")
    for field, label in (("retrieved_chunk_ids", "pre"), ("ranked_chunk_ids", "post")):
        artifact_ids = case.get(field)
        if not isinstance(artifact_ids, list):
            raise ValueError(f"CE_PROVENANCE_TRANSITION_MISSING: artifact {label} ids unavailable")
        expected_identities = {(str(entry[0]), str(entry[1])) for entry in artifact_ids}
        if expected_identities != identities:
            raise ValueError(f"CE_PROVENANCE_IDENTITY_MISMATCH: CE items differ from artifact {label} ids")
    pre_ranks = [int(item["pre_ce_rrf_rank"]) for item in items]
    if sorted(pre_ranks) != list(range(1, candidate_count + 1)):
        raise ValueError("CE_PROVENANCE_RANK_INVALID: pre_ce_rrf_rank not a 1..N permutation")
    post_ranks = [int(item["post_ce_rank"]) for item in items]
    if sorted(post_ranks) != list(range(1, candidate_count + 1)):
        raise ValueError("CE_PROVENANCE_RANK_INVALID: post_ce_rank not a 1..N permutation")
    _exact_identity_sequence(items, "pre_ce_rrf_rank", case.get("retrieved_chunk_ids"), label="pre")
    _exact_identity_sequence(items, "post_ce_rank", case.get("ranked_chunk_ids"), label="post")


def align_ce_provenance(
    report: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    expected: CeExpectedConfig,
) -> dict[str, object]:
    """按 query digest fail-closed 对齐；每个 success row 必须 exact match approved provenance 与候选转换."""
    by_digest: dict[str, Mapping[str, Any]] = {}
    failure_rows: list[Mapping[str, Any]] = []
    for row in rows:
        digest = row.get("query_sha256")
        status = row.get("status")
        if not isinstance(digest, str) or not digest or digest in by_digest:
            raise ValueError("CE_PROVENANCE_INVALID: missing or duplicate query_sha256")
        by_digest[digest] = row
        if status in _CE_FAILURE_STATUSES:
            failure_rows.append(row)
        elif status not in _CE_SUCCESS_STATUSES:
            raise ValueError("CE_PROVENANCE_INVALID: unknown status")
    aligned: dict[str, Mapping[str, Any]] = {}
    for case in report["case_results"]:
        digest = _query_digest(str(case["query"]))
        row = by_digest.get(digest)
        if row is None:
            raise ValueError("CE_PROVENANCE_MISSING: sidecar row missing for case")
        if row.get("status") not in _CE_SUCCESS_STATUSES:
            raise ValueError("CE_PROVENANCE_MISMATCH: case aligned to failure row")
        _validate_success_row(row, case, expected)
        aligned[str(case["benchmark_query_id"])] = row
    expected_success_digests = {
        _query_digest(str(case["query"])) for case in report["case_results"]
    }
    actual_success_digests = {
        row["query_sha256"]
        for row in by_digest.values()
        if row.get("status") in _CE_SUCCESS_STATUSES
    }
    extra = actual_success_digests - expected_success_digests
    if extra:
        raise ValueError(
            f"CE_PROVENANCE_EXTRA_SUCCESS: unmatched success row count={len(extra)}"
        )
    if len(aligned) != len(report["case_results"]):
        raise ValueError("CE_PROVENANCE_ALIGNMENT_FAILED")
    return {"aligned": aligned, "failure_rows": tuple(failure_rows)}


def _cases(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item["benchmark_query_id"]): item for item in report["case_results"]}


def _hit(case: Mapping[str, Any], k: int) -> bool:
    return bool(set(case["qrels_document_ids"]) & set(case["ranked_document_ids"][:k]))


def _first_relevant_rank(case: Mapping[str, Any]) -> int | None:
    relevant = set(case["qrels_document_ids"])
    for rank, document_id in enumerate(case["ranked_document_ids"], 1):
        if document_id in relevant:
            return rank
    return None


def _representative(
    rrf: Mapping[str, Mapping[str, Any]],
    ce: Mapping[str, Mapping[str, Any]],
) -> dict[str, object]:
    predicates = {
        "rrf_rescue_realized": lambda r, c: not _hit(r, 5) and _hit(c, 5),
        "rrf_regression_realized": lambda r, c: _hit(r, 5) and not _hit(c, 5),
        "both_hit_ce_improved": lambda r, c: _hit(r, 5)
        and _hit(c, 5)
        and (_first_relevant_rank(c) or 10**9) < (_first_relevant_rank(r) or 10**9),
        "both_miss": lambda r, c: not _hit(r, 5) and not _hit(c, 5),
    }
    result: dict[str, object] = {}
    for category, predicate in predicates.items():
        result[category] = "NOT_AVAILABLE"
        for query_id in _ordered_query_ids(rrf):
            if predicate(rrf[query_id], ce[query_id]):
                result[category] = {
                    "query_id": query_id,
                    "query_sha256": _query_digest(str(ce[query_id]["query"])),
                    "qrels_document_ids": ce[query_id]["qrels_document_ids"],
                    "rrf_ranked_document_ids": rrf[query_id]["ranked_document_ids"],
                    "ce_ranked_document_ids": ce[query_id]["ranked_document_ids"],
                    "truthfulness": "真实",
                }
                break
    return result


def analyze_ce_rerank(
    rrf_report: Mapping[str, Any], ce_report: Mapping[str, Any]
) -> dict[str, object]:
    """量化 RRF -> CE：rescue/regression、rank transition、guardrail 布尔."""
    rrf = _cases(rrf_report)
    ce = _cases(ce_report)
    if rrf.keys() != ce.keys() or len(rrf) != _SCIFACT_QUERY_COUNT:
        raise ValueError("CE_RERANK_QUERY_ALIGNMENT_FAILED")

    by_k: dict[str, object] = {}
    for k in (1, 3, 5):
        counts = {
            "rrf_miss_ce_hit": 0,
            "rrf_hit_ce_miss": 0,
            "rrf_hit_ce_hit": 0,
            "rrf_miss_ce_miss": 0,
        }
        for query_id in _ordered_query_ids(rrf):
            rrf_hit = _hit(rrf[query_id], k)
            ce_hit = _hit(ce[query_id], k)
            counts[f"rrf_{'hit' if rrf_hit else 'miss'}_ce_{'hit' if ce_hit else 'miss'}"] += 1
        by_k[f"top{k}"] = counts

    transitions = {"improved": 0, "degraded": 0, "unchanged": 0}
    for query_id in _ordered_query_ids(rrf):
        rrf_rank = _first_relevant_rank(rrf[query_id])
        ce_rank = _first_relevant_rank(ce[query_id])
        if ce_rank is not None and (rrf_rank is None or ce_rank < rrf_rank):
            transitions["improved"] += 1
        elif rrf_rank is not None and (ce_rank is None or ce_rank > rrf_rank):
            transitions["degraded"] += 1
        else:
            transitions["unchanged"] += 1

    case_guardrails_ok = all(
        by_k[f"top{k}"]["rrf_miss_ce_hit"] >= by_k[f"top{k}"]["rrf_hit_ce_miss"]
        for k in (1, 3, 5)
    )
    rank_transition_ok = transitions["improved"] >= transitions["degraded"]
    return {
        "query_count": len(rrf),
        "by_k": by_k,
        "rank_transition": transitions,
        "case_guardrails_ok": case_guardrails_ok,
        "rank_transition_ok": rank_transition_ok,
        "representative_cases": _representative(rrf, ce),
    }


def _at_least(value: float, threshold: float) -> bool:
    """Epsilon 只吸收序列化误差；不是业务 tolerance."""
    return value >= threshold - NUMERIC_COMPARISON_EPSILON


def build_runner_invariants(
    *,
    dense_cache_key: str,
    sparse_cache_key: str,
    corpus_checksums_ok: bool,
    report: Mapping[str, Any],
    synthetic_report: Mapping[str, Any],
    aligned: Mapping[str, Mapping[str, Any]],
    failure_rows: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """纯函数聚合 runner 的 candidate/cache/corpus/Dataset invariant.

    ``all_ok`` 只聚合显式 boolean invariant，并显式包含 ``technical_failure_count == 0``；
    不对数值 ``0`` 使用 truthiness。``technical_failure_count`` 作为数值事实单独保留。
    """
    budget_ok = True
    for case in report["case_results"]:
        for field in ("retrieved_chunk_ids", "ranked_chunk_ids"):
            value = case.get(field)
            if isinstance(value, list) and len(value) > 8:
                budget_ok = False
    identity_permutation_ok = True
    for row in aligned.values():
        items = row.get("items")
        if isinstance(items, list) and items:
            pre = sorted(int(item["pre_ce_rrf_rank"]) for item in items)
            post = sorted(int(item["post_ce_rank"]) for item in items)
            if pre != list(range(1, len(items) + 1)) or post != list(range(1, len(items) + 1)):
                identity_permutation_ok = False
    dataset_complete = (
        report.get("evaluated_retrieval_cases") == _SCIFACT_QUERY_COUNT
        and synthetic_report.get("dataset_case_count") == 24
    )
    technical_failure_count = len(failure_rows)
    boolean_checks = [
        dense_cache_key == WP2_DENSE_CACHE_KEY,
        sparse_cache_key == WP2_BM25_CACHE_KEY,
        bool(corpus_checksums_ok),
        bool(budget_ok),
        bool(identity_permutation_ok),
        bool(dataset_complete),
        technical_failure_count == 0,
    ]
    invariants: dict[str, object] = {
        "dense_cache_identity_ok": boolean_checks[0],
        "sparse_cache_identity_ok": boolean_checks[1],
        "corpus_checksums_ok": bool(corpus_checksums_ok),
        "candidate_budget_ok": bool(budget_ok),
        "identity_permutation_ok": bool(identity_permutation_ok),
        "dataset_complete": bool(dataset_complete),
        "technical_failure_count": technical_failure_count,
    }
    return {**invariants, "all_ok": all(boolean_checks)}


def _metric_deltas(ce_metrics: Mapping[str, float]) -> dict[str, float]:
    return {key: float(ce_metrics[key]) - RRF_SCIFACT_BASELINE[key] for key in _METRIC_KEYS}


def evaluate_ce_acceptance_gate(
    *,
    scifact_metrics: Mapping[str, float] | None,
    synthetic_metrics: Mapping[str, float] | None,
    technical_failure_count: int | None,
    total_queries: int | None,
    case_guardrails_ok: bool | None,
    rank_transition_ok: bool | None,
    invariants_ok: bool | None,
    real_load_latency_present: bool,
) -> dict[str, object]:
    """输出 ACCEPT / REJECT / NOT_EVALUATED_BLOCKED；所有 hard gate 已冻结."""
    blocked: list[str] = []
    if scifact_metrics is None:
        blocked.append("scifact_300_not_completed")
    if synthetic_metrics is None:
        blocked.append("synthetic_24_not_completed")
    if technical_failure_count is None or total_queries is None:
        blocked.append("ce_evidence_missing")
    elif total_queries != _SCIFACT_QUERY_COUNT:
        blocked.append("scifact_300_incomplete")
    if not real_load_latency_present:
        blocked.append("real_model_load_latency_evidence_missing")
    if case_guardrails_ok is None or rank_transition_ok is None or invariants_ok is None:
        blocked.append("analysis_or_invariants_missing")

    base: dict[str, object] = {
        "gate": CE_CANDIDATE_ACCEPTANCE_GATE,
        "primary_metric": CE_PRIMARY_METRIC,
        "comparison_baseline": "WP2 RRF actual",
        "numeric_comparison_epsilon": NUMERIC_COMPARISON_EPSILON,
        "outcome": "NOT_EVALUATED_BLOCKED",
        "blocked_reasons": blocked,
        "failure_reasons": [],
    }
    if blocked:
        return base

    failures: list[str] = []
    if technical_failure_count is not None and technical_failure_count > 0:
        failures.append("technical_failure_rate_must_be_zero")
    if case_guardrails_ok is not True:
        failures.append("case_guardrail_miss_vs_hit_guardrail")
    if rank_transition_ok is not True:
        failures.append("rank_transition_improved_vs_degraded_guardrail")
    if invariants_ok is not True:
        failures.append("candidate_cache_corpus_dataset_invariant_failed")

    assert scifact_metrics is not None and synthetic_metrics is not None
    deltas = _metric_deltas(scifact_metrics)
    for key, delta in deltas.items():
        if key == CE_PRIMARY_METRIC:
            if not _at_least(delta, NDCG3_REQUIRED_DELTA):
                failures.append("primary_ndcg3_delta_below_threshold")
        elif not _at_least(delta, GUARDRAIL_MIN_DELTA):
            failures.append(f"guardrail_{key}_delta_below_threshold")

    synthetic_checks: dict[str, bool] = {}
    for key, baseline in RRF_SYNTHETIC_BASELINE.items():
        value = float(synthetic_metrics[key])
        ok = _at_least(value, baseline)
        synthetic_checks[key] = ok
        if not ok:
            failures.append(f"synthetic_{key}_declined")

    checks = {
        "primary": {
            "metric": CE_PRIMARY_METRIC,
            "rrf_baseline": RRF_SCIFACT_BASELINE[CE_PRIMARY_METRIC],
            "ce_value": float(scifact_metrics[CE_PRIMARY_METRIC]),
            "delta": deltas[CE_PRIMARY_METRIC],
            "required_min_delta": NDCG3_REQUIRED_DELTA,
            "passed": _at_least(deltas[CE_PRIMARY_METRIC], NDCG3_REQUIRED_DELTA),
        },
        "guardrails": {
            key: {
                "rrf_baseline": RRF_SCIFACT_BASELINE[key],
                "ce_value": float(scifact_metrics[key]),
                "delta": deltas[key],
                "allowed_min_delta": GUARDRAIL_MIN_DELTA,
                "passed": _at_least(deltas[key], GUARDRAIL_MIN_DELTA),
            }
            for key in _METRIC_KEYS
            if key != CE_PRIMARY_METRIC
        },
        "case_guardrails_ok": case_guardrails_ok,
        "rank_transition_ok": rank_transition_ok,
        "invariants_ok": invariants_ok,
        "synthetic_checks": synthetic_checks,
        "technical_failure_count": technical_failure_count,
    }
    outcome = "ACCEPT" if not failures else "REJECT"
    return {
        **base,
        "outcome": outcome,
        "blocked_reasons": [],
        "failure_reasons": failures,
        "scifact_deltas": deltas,
        "checks": checks,
        "technical_failure_count": technical_failure_count,
    }


# ---------------------------------------------------------------------------
# Latency（DIAGNOSTIC_ONLY，不参与算法 gate）
# ---------------------------------------------------------------------------


def _latency(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "p50": None, "p95": None}
    ordered = sorted(float(value) for value in values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "mean": float(mean(ordered)),
        "p50": float(median(ordered)),
        "p95": float(ordered[p95_index]),
    }


def _aggregate_ce_latency(rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    load_values: list[float] = []
    infer_values: list[float] = []
    total_values: list[float] = []
    for row in rows:
        latency = row.get("latency_ms") if isinstance(row.get("latency_ms"), Mapping) else {}
        load = latency.get("model_load_latency_ms")
        infer = latency.get("inference_latency_ms")
        total = latency.get("ce_total_latency_ms")
        if isinstance(load, (int, float)):
            load_values.append(float(load))
        if isinstance(infer, (int, float)):
            infer_values.append(float(infer))
        if isinstance(total, (int, float)):
            total_values.append(float(total))
    return {
        "model_load_cold_latency_ms": min(load_values) if load_values else None,
        "first_inference_latency_ms": infer_values[0] if infer_values else None,
        "warm_inference_mean_ms": _latency(infer_values[1:])["mean"] if len(infer_values) > 1 else None,
        "warm_inference_ms": _latency(infer_values[1:]),
        "ce_total_wall_ms": _latency(total_values),
        "diagnostic_only": True,
    }


# ---------------------------------------------------------------------------
# versioned evidence builder
# ---------------------------------------------------------------------------


def build_ce_candidate_evidence(
    *,
    report: Mapping[str, Any],
    analysis: Mapping[str, Any],
    aligned: Mapping[str, Mapping[str, Any]],
    failure_rows: Sequence[Mapping[str, Any]],
    invariants: Mapping[str, Any],
    gate_input: Mapping[str, Any],
    rrf_report: Mapping[str, Any],
    expected: CeExpectedConfig,
) -> dict[str, object]:
    """生成不复制 SciFact corpus 的 case-level CE evidence（beir-scifact-cross-encoder-candidate.v1）."""
    success_rows = [row for row in aligned.values()]
    latency = _aggregate_ce_latency([*success_rows, *failure_rows])
    total_queries = len(report["case_results"]) + len(failure_rows)
    cases = []
    for item in report["case_results"]:
        query_id = str(item["benchmark_query_id"])
        row = aligned.get(query_id)
        cases.append(
            {
                "query_id": query_id,
                "qrels_document_ids": item["qrels_document_ids"],
                "retrieved_document_ids": item["retrieved_document_ids"],
                "ranked_document_ids": item["ranked_document_ids"],
                "retrieved_chunk_ids": item.get("retrieved_chunk_ids"),
                "ranked_chunk_ids": item.get("ranked_chunk_ids"),
                "metrics": item["scores"],
                "ce_items": list(row.get("items") or []) if row is not None else [],
                "ce_status": row.get("status") if row is not None else None,
            }
        )
    rrf_metrics = rrf_report.get("metrics") if isinstance(rrf_report.get("metrics"), Mapping) else {}
    return {
        "evidence_schema_version": CE_EVIDENCE_SCHEMA_VERSION,
        "benchmark_kind": report.get("benchmark_kind"),
        "algorithm_ref": expected.algorithm_ref,
        "model_ref": expected.model_ref,
        "asset_tree_sha256": expected.asset_tree_sha256,
        "device": expected.device,
        "cache_identity": expected.cache_identity,
        "metrics": report.get("metrics"),
        "rrf_baseline_metrics": dict(rrf_metrics),
        "latency_ms": latency,
        "analysis": dict(analysis),
        "failure_count": len(failure_rows),
        "technical_failure_rate": (
            len(failure_rows) / total_queries if total_queries else None
        ),
        "invariants": dict(invariants),
        "gate": dict(gate_input),
        "cases": cases,
    }


__all__ = [
    "CE_ALGORITHM_REF",
    "CE_CANDIDATE_ACCEPTANCE_GATE",
    "CE_EVIDENCE_SCHEMA_VERSION",
    "CE_PRIMARY_METRIC",
    "CE_SIDE_CAR_SCHEMA_VERSION",
    "GUARDRAIL_MIN_DELTA",
    "NDCG3_REQUIRED_DELTA",
    "NUMERIC_COMPARISON_EPSILON",
    "RRF_SCIFACT_BASELINE",
    "RRF_SYNTHETIC_BASELINE",
    "WP2_BM25_CACHE_KEY",
    "WP2_DENSE_CACHE_KEY",
    "CeExpectedConfig",
    "align_ce_provenance",
    "analyze_ce_rerank",
    "build_ce_candidate_evidence",
    "build_runner_invariants",
    "evaluate_ce_acceptance_gate",
]