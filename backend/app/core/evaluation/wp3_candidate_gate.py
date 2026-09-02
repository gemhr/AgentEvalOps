# ruff: noqa: D101, D102, D105, D415
"""WP3 生产目标配对评估的窄身份、聚合与 Candidate Gate。

本模块只消费已经产生的 RAG artifact；不执行 retrieval、不复制 LocalAgent 算法，
也不把实验报告当作新的持久化 authority。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from statistics import mean, median
from typing import Mapping, Sequence

from app.core.evaluation.dataset import EvaluationCase
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1
from app.core.evaluation.ranking_metrics import calculate_ndcg_at_k
from app.core.evaluation.retrieval_metrics import calculate_mrr, calculate_recall_at_k

WP3_METRICS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_3",
    "ndcg_at_5",
)
WP3_SECONDARY_METRICS = WP3_METRICS[:3] + ("mrr", "ndcg_at_5")
WP3_RETRIEVAL_CASE_COUNT = 20
WP3_TOTAL_CASE_COUNT = 24


def canonical_sha256(value: object) -> str:
    """计算 bounded canonical JSON 的 SHA-256。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_localagent_source_identity(
    repo_root: str | Path,
    *,
    approved_untracked_paths: Sequence[str] = (),
) -> dict[str, object]:
    """计算有界 LocalAgent dirty-source identity，不保存源码正文。"""
    root = Path(repo_root).resolve()

    def git(*args: str) -> list[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return [line.replace("\\", "/") for line in result.stdout.splitlines() if line]

    head = git("rev-parse", "HEAD")
    if len(head) != 1:
        raise ValueError("LocalAgent HEAD identity is unavailable")
    tracked_dirty = set(git("diff", "--name-only", "HEAD", "--"))
    untracked = set(git("ls-files", "--others", "--exclude-standard"))
    approved = {str(Path(item)).replace("\\", "/") for item in approved_untracked_paths}
    undeclared = untracked - approved
    if undeclared:
        raise ValueError(
            "untracked LocalAgent source/config files require explicit approval: "
            + ",".join(sorted(undeclared))
        )
    paths = sorted(tracked_dirty | (untracked & approved))
    manifest: list[dict[str, object]] = []
    digest = hashlib.sha256()
    for relative_path in paths:
        path = (root / relative_path).resolve()
        if root not in path.parents and path != root:
            raise ValueError("source identity path escapes LocalAgent repository")
        file_bytes = path.read_bytes() if path.is_file() else b""
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_bytes)
        digest.update(b"\0")
        manifest.append(
            {
                "path": relative_path,
                "size": len(file_bytes),
                "sha256": hashlib.sha256(file_bytes).hexdigest(),
            }
        )
    return {
        "localagent_head_commit": head[0],
        "working_tree_diff_sha256": digest.hexdigest(),
        "working_tree_manifest": manifest,
    }


def dataset_content_sha256(path: str | Path) -> str:
    """按冻结合同计算 canonical dataset 原始文件字节 SHA-256。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


EVALUATED_SETTINGS_PROFILE_FIELDS = (
    "rag_top_k",
    "rag_min_score",
    "knowledge_collection_name",
    "embedding_identity",
    "chunk_policy_sha256",
    "query_rewrite_policy",
    "candidate_limit",
)


def build_evaluated_settings_profile(profile: Mapping[str, object]) -> dict[str, object]:
    """投影不含 strategy/secrets 的 bounded retrieval settings。"""
    allowed = set(EVALUATED_SETTINGS_PROFILE_FIELDS)
    unknown = set(profile) - allowed
    if unknown:
        raise ValueError("unsupported evaluated settings profile field")
    if "retrieval_strategy" in profile:
        raise ValueError("evaluated settings profile must omit allowed strategy difference")
    if not profile:
        raise ValueError("evaluated settings profile must not be empty")
    return {key: profile[key] for key in EVALUATED_SETTINGS_PROFILE_FIELDS if key in profile}


def evaluated_settings_profile_sha256(profile: Mapping[str, object]) -> str:
    """生成不含 strategy/secrets 的已验证 retrieval settings 投影摘要。"""
    return canonical_sha256(build_evaluated_settings_profile(profile))


class WP3IdentityMismatch(ValueError):
    """配对 identity 的 invariant 字段不一致。"""


class WP3ExperimentInvalid(ValueError):
    """实验无法形成可比较的正式配对。"""


class WP3CaseClassification(StrEnum):
    """WP3 逐案投影分类。"""

    IMPROVEMENT = "IMPROVEMENT"
    UNCHANGED = "UNCHANGED"
    REGRESSION = "REGRESSION"
    SEVERE_REGRESSION = "SEVERE_REGRESSION"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class WP3GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class WP3RunIdentity:
    """一次 run 的 bounded identity；``identity_sha256`` 只覆盖 invariant 字段。"""

    experiment_id: str
    pair_id: str
    role: str
    repeat_index: int
    retrieval_strategy: str
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    dataset_content_sha256: str
    suite_id: str
    suite_version: str
    execution_target_id: str
    execution_target_kind: str
    execution_target_version: str
    endpoint_contract: str
    localagent_version: str
    localagent_head_commit: str
    working_tree_diff_sha256: str
    generation_id: str
    provenance_sha256: str
    corpus_id: str
    source_manifest_sha256: str
    chunk_policy_sha256: str
    chunk_manifest_sha256: str
    document_count: int
    chunk_count: int
    embedding_identity: str
    evaluated_settings_profile_sha256: str
    rewrite_policy: str
    rewrite_fixture_id: str
    started_at: str
    run_id: str = ""
    port: int | None = None
    state_paths: tuple[str, ...] = ()
    identity_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.role not in {"BASELINE", "CANDIDATE"}:
            raise ValueError("role must be BASELINE or CANDIDATE")
        if self.repeat_index != 0:
            raise ValueError("WP3 formal repeat_index must be 0")
        if self.document_count < 0 or self.chunk_count < 0:
            raise ValueError("document/chunk counts must be non-negative")
        if self.dataset_digest != self.dataset_content_sha256:
            raise ValueError("dataset_digest must equal dataset_content_sha256 for WP3 raw-byte identity")
        if not self.localagent_head_commit or not self.working_tree_diff_sha256:
            raise ValueError("LocalAgent source identity is required")
        invariant = self.invariant_dict()
        object.__setattr__(self, "identity_sha256", canonical_sha256(invariant))

    def invariant_dict(self) -> dict[str, object]:
        """返回 pair equality contract 中不允许变化的字段。"""
        return {
            "experiment_id": self.experiment_id,
            "pair_id": self.pair_id,
            "repeat_index": self.repeat_index,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_content_sha256": self.dataset_content_sha256,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "execution_target_id": self.execution_target_id,
            "execution_target_kind": self.execution_target_kind,
            "execution_target_version": self.execution_target_version,
            "endpoint_contract": self.endpoint_contract,
            "localagent_version": self.localagent_version,
            "localagent_head_commit": self.localagent_head_commit,
            "working_tree_diff_sha256": self.working_tree_diff_sha256,
            "generation_id": self.generation_id,
            "provenance_sha256": self.provenance_sha256,
            "corpus_id": self.corpus_id,
            "source_manifest_sha256": self.source_manifest_sha256,
            "chunk_policy_sha256": self.chunk_policy_sha256,
            "chunk_manifest_sha256": self.chunk_manifest_sha256,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "embedding_identity": self.embedding_identity,
            "evaluated_settings_profile_sha256": self.evaluated_settings_profile_sha256,
            "rewrite_policy": self.rewrite_policy,
            "rewrite_fixture_id": self.rewrite_fixture_id,
        }

    def to_dict(self) -> dict[str, object]:
        payload = self.invariant_dict()
        payload.update({
            "dataset_digest": self.dataset_digest,
            "dataset_digest_representation": "raw_file_bytes_sha256",
            "role": self.role,
            "retrieval_strategy": self.retrieval_strategy,
            "started_at": self.started_at,
            "run_id": self.run_id,
            "port": self.port,
            "state_paths": list(self.state_paths),
            "identity_sha256": self.identity_sha256,
        })
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "WP3RunIdentity":
        """从持久化 run metadata 重建 identity；忽略派生 digest。"""
        if payload.get("dataset_digest_representation") != "raw_file_bytes_sha256":
            raise WP3ExperimentInvalid("WP3 dataset_digest representation is not authoritative")
        state_paths = payload.get("state_paths") or ()
        return cls(
            experiment_id=str(payload["experiment_id"]),
            pair_id=str(payload["pair_id"]),
            role=str(payload["role"]),
            repeat_index=int(payload["repeat_index"]),
            retrieval_strategy=str(payload["retrieval_strategy"]),
            dataset_id=str(payload["dataset_id"]),
            dataset_version=str(payload["dataset_version"]),
            dataset_digest=str(payload["dataset_digest"]),
            dataset_content_sha256=str(payload["dataset_content_sha256"]),
            suite_id=str(payload["suite_id"]),
            suite_version=str(payload["suite_version"]),
            execution_target_id=str(payload["execution_target_id"]),
            execution_target_kind=str(payload["execution_target_kind"]),
            execution_target_version=str(payload["execution_target_version"]),
            endpoint_contract=str(payload["endpoint_contract"]),
            localagent_version=str(payload["localagent_version"]),
            localagent_head_commit=str(payload["localagent_head_commit"]),
            working_tree_diff_sha256=str(payload["working_tree_diff_sha256"]),
            generation_id=str(payload["generation_id"]),
            provenance_sha256=str(payload["provenance_sha256"]),
            corpus_id=str(payload["corpus_id"]),
            source_manifest_sha256=str(payload["source_manifest_sha256"]),
            chunk_policy_sha256=str(payload["chunk_policy_sha256"]),
            chunk_manifest_sha256=str(payload["chunk_manifest_sha256"]),
            document_count=int(payload["document_count"]),
            chunk_count=int(payload["chunk_count"]),
            embedding_identity=str(payload["embedding_identity"]),
            evaluated_settings_profile_sha256=str(payload["evaluated_settings_profile_sha256"]),
            rewrite_policy=str(payload["rewrite_policy"]),
            rewrite_fixture_id=str(payload["rewrite_fixture_id"]),
            started_at=str(payload["started_at"]),
            run_id=str(payload.get("run_id") or ""),
            port=None if payload.get("port") is None else int(payload["port"]),
            state_paths=tuple(str(item) for item in state_paths),
        )


def validate_pair_identities(baseline: WP3RunIdentity, candidate: WP3RunIdentity) -> None:
    """严格验证 pair invariant；role/strategy/run-local fields可不同。"""
    if baseline.role != "BASELINE" or candidate.role != "CANDIDATE":
        raise WP3IdentityMismatch("pair roles must be BASELINE and CANDIDATE")
    if baseline.invariant_dict() != candidate.invariant_dict():
        differing = [key for key in baseline.invariant_dict() if baseline.invariant_dict()[key] != candidate.invariant_dict()[key]]
        raise WP3IdentityMismatch("pair identity mismatch: " + ",".join(differing))
    if not baseline.provenance_sha256 or not candidate.provenance_sha256:
        raise WP3IdentityMismatch("provenance_sha256 must be non-empty")


@dataclass(frozen=True, slots=True)
class WP3RewriteFixtureEntry:
    case_id: str
    query_digest: str
    rewritten_query: str
    rewritten_query_digest: str

    @classmethod
    def build(cls, case_id: str, query: str, rewritten_query: str) -> "WP3RewriteFixtureEntry":
        return cls(case_id, canonical_sha256(query), rewritten_query, canonical_sha256(rewritten_query))


@dataclass(frozen=True, slots=True)
class WP3RewriteFixture:
    fixture_version: str
    entries: tuple[WP3RewriteFixtureEntry, ...]
    fixture_id: str = field(init=False)

    def __post_init__(self) -> None:
        if len(self.entries) != WP3_TOTAL_CASE_COUNT:
            raise ValueError("WP3 rewrite fixture must contain exactly 24 cases")
        if len({entry.case_id for entry in self.entries}) != len(self.entries):
            raise ValueError("rewrite fixture case ids must be unique")
        payload = {
            "fixture_version": self.fixture_version,
            "entries": [
                {
                    "case_id": entry.case_id,
                    "query_digest": entry.query_digest,
                    "rewritten_query": entry.rewritten_query,
                    "rewritten_query_digest": entry.rewritten_query_digest,
                }
                for entry in self.entries
            ],
        }
        object.__setattr__(self, "fixture_id", canonical_sha256(payload))

    def resolve(self, *, case_id: str, query: str) -> WP3RewriteFixtureEntry:
        entry = next((item for item in self.entries if item.case_id == case_id), None)
        if entry is None or entry.query_digest != canonical_sha256(query):
            raise WP3ExperimentInvalid("rewrite fixture case/query mismatch")
        if entry.rewritten_query_digest != canonical_sha256(entry.rewritten_query):
            raise WP3ExperimentInvalid("rewrite fixture digest mismatch")
        return entry


def metrics_for_artifact(case: EvaluationCase, artifact: RagEvaluationArtifactV1) -> dict[str, float] | None:
    """只为有 retrieval/ranking ground truth 的 case 计算六项指标。"""
    raw = case.ground_truth
    if raw is None or not hasattr(raw, "retrieval") or raw.retrieval is None:
        return None
    retrieval = raw.retrieval
    ranking = raw.ranking
    if retrieval is None or ranking is None:
        return None
    if artifact.retrieval_status == "EMPTY":
        return {metric: 0.0 for metric in WP3_METRICS}
    recall = calculate_recall_at_k(retrieval, artifact, (1, 3, 5))
    mrr = calculate_mrr(retrieval, artifact)
    ndcg = calculate_ndcg_at_k(ranking, artifact, (3, 5))
    return {
        "recall_at_1": recall.value_at(1),
        "recall_at_3": recall.value_at(3),
        "recall_at_5": recall.value_at(5),
        "mrr": mrr.value,
        "ndcg_at_3": ndcg.value_at(3),
        "ndcg_at_5": ndcg.value_at(5),
    }


def classify_case(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    *,
    case: EvaluationCase | None = None,
    baseline_artifact: RagEvaluationArtifactV1 | None = None,
    candidate_artifact: RagEvaluationArtifactV1 | None = None,
) -> WP3CaseClassification:
    """按 frozen priority 生成逐案分类。"""
    deltas = {metric: candidate[metric] - baseline[metric] for metric in WP3_METRICS}
    severe = (
        (baseline["recall_at_3"] > 0 and candidate["recall_at_3"] == 0)
        or (baseline["mrr"] > 0 and candidate["mrr"] == 0)
        or deltas["ndcg_at_3"] <= -0.50
    )
    if baseline_artifact is not None and candidate_artifact is not None:
        if case is not None:
            baseline_top3_relevant = _top3_relevant_identities(case, baseline_artifact)
            candidate_top3_relevant = _top3_relevant_identities(case, candidate_artifact)
            if baseline_top3_relevant and baseline_top3_relevant.isdisjoint(candidate_top3_relevant):
                severe = True
        baseline_empty = baseline_artifact.retrieval_status in {"FAILED", "TIMED_OUT", "CANCELLED"}
        candidate_empty = candidate_artifact.retrieval_status in {"FAILED", "TIMED_OUT", "CANCELLED", "EMPTY"}
        if not baseline_empty and candidate_empty and baseline["recall_at_3"] > 0:
            severe = True
    if severe:
        return WP3CaseClassification.SEVERE_REGRESSION
    if any(delta <= -0.05 for delta in deltas.values()):
        return WP3CaseClassification.REGRESSION
    if any(delta >= 0.05 for delta in deltas.values()):
        return WP3CaseClassification.IMPROVEMENT
    return WP3CaseClassification.UNCHANGED


def _top3_relevant_identities(
    case: EvaluationCase, artifact: RagEvaluationArtifactV1
) -> frozenset[tuple[str, str]]:
    """从实际 ranked evidence 与 canonical GT 计算 top-3 relevant identities。"""
    ground_truth = case.ground_truth
    retrieval = getattr(ground_truth, "retrieval", None)
    if retrieval is None:
        return frozenset()
    relevant = {
        (item.document_id, item.chunk_id)
        for item in retrieval.relevant_chunks
    }
    return frozenset(
        (item.document_id, item.chunk_id)
        for item in artifact.ranked_items
        if item.rank <= 3 and (item.document_id, item.chunk_id) in relevant
    )


@dataclass(frozen=True, slots=True)
class WP3RunSummary:
    planned_case_count: int
    completed_case_count: int
    execution_failure_count: int
    degraded_count: int
    empty_count: int
    metrics: Mapping[str, float]
    total_latency_ms: tuple[float, ...]

    @property
    def failure_rate(self) -> float:
        return self.execution_failure_count / self.planned_case_count if self.planned_case_count else 1.0

    @property
    def degraded_rate(self) -> float:
        return self.degraded_count / self.planned_case_count if self.planned_case_count else 1.0

    @property
    def latency_mean(self) -> float | None:
        return float(mean(self.total_latency_ms)) if self.total_latency_ms else None

    @property
    def latency_median(self) -> float | None:
        return float(median(self.total_latency_ms)) if self.total_latency_ms else None


def aggregate_metrics(per_case: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """等权 macro mean；输入必须覆盖六项指标。"""
    if not per_case:
        raise ValueError("cannot aggregate empty metric set")
    if any(set(item) != set(WP3_METRICS) for item in per_case):
        raise ValueError("metric set must be exactly the frozen six metrics")
    return {metric: float(mean(item[metric] for item in per_case)) for metric in WP3_METRICS}


def evaluate_candidate_gate(
    *,
    baseline: WP3RunSummary,
    candidate: WP3RunSummary,
    pair_valid: bool,
    provenance_valid: bool,
    regression_counts: Mapping[WP3CaseClassification, int],
    rewrite_valid: bool = True,
    settings_valid: bool = True,
    identity_valid: bool = True,
    isolation_valid: bool = True,
) -> dict[str, WP3GateStatus | str | dict[str, float]]:
    """根据 frozen thresholds 返回六 Gate 与最终三态结果。"""
    experiment_valid = (
        pair_valid
        and provenance_valid
        and rewrite_valid
        and settings_valid
        and identity_valid
        and isolation_valid
    )
    complete = (
        baseline.planned_case_count == WP3_TOTAL_CASE_COUNT
        and candidate.planned_case_count == WP3_TOTAL_CASE_COUNT
        and baseline.completed_case_count == WP3_TOTAL_CASE_COUNT
        and candidate.completed_case_count == WP3_TOTAL_CASE_COUNT
    )
    not_comparable = regression_counts.get(WP3CaseClassification.NOT_COMPARABLE, 0)
    if not experiment_valid or not complete or baseline.execution_failure_count > 0:
        return _inconclusive_gates()
    # Identity/protocol/alignment holes are INCONCLUSIVE. Candidate-only execution
    # failure remains a reliability FAIL even if those cases are also NOT_COMPARABLE.
    if not_comparable > 0 and candidate.execution_failure_count == 0:
        return _inconclusive_gates()
    fairness = WP3GateStatus.PASS
    provenance = WP3GateStatus.PASS
    degradation = candidate.degraded_rate - baseline.degraded_rate <= 0.10
    if candidate.execution_failure_count > 0 or not degradation:
        reliability = WP3GateStatus.FAIL
    else:
        reliability = WP3GateStatus.PASS
    deltas = {metric: candidate.metrics[metric] - baseline.metrics[metric] for metric in WP3_METRICS}
    quality = WP3GateStatus.PASS if (
        deltas["ndcg_at_3"] >= 0.0
        and any(deltas[metric] >= 0.05 for metric in WP3_SECONDARY_METRICS)
        and all(delta >= -0.05 for delta in deltas.values())
    ) else WP3GateStatus.FAIL
    severe = regression_counts.get(WP3CaseClassification.SEVERE_REGRESSION, 0)
    ordinary = regression_counts.get(WP3CaseClassification.REGRESSION, 0)
    regression = WP3GateStatus.PASS if severe == 0 and ordinary <= 2 and not_comparable == 0 else WP3GateStatus.FAIL
    base_latency = baseline.latency_mean
    candidate_latency = candidate.latency_mean
    latency = WP3GateStatus.PASS if (
        base_latency is not None and candidate_latency is not None
        and candidate_latency <= base_latency + max(50.0, 0.25 * base_latency)
    ) else WP3GateStatus.FAIL
    gates = {
        "FAIRNESS_GATE": fairness,
        "PROVENANCE_CONSISTENCY_GATE": provenance,
        "EXECUTION_RELIABILITY_GATE": reliability,
        "QUALITY_GATE": quality,
        "PER_CASE_REGRESSION_GATE": regression,
        "LATENCY_GATE": latency,
        "metric_deltas": deltas,
    }
    if all(gates[key] is WP3GateStatus.PASS for key in (
        "FAIRNESS_GATE", "PROVENANCE_CONSISTENCY_GATE", "EXECUTION_RELIABILITY_GATE",
        "QUALITY_GATE", "PER_CASE_REGRESSION_GATE", "LATENCY_GATE",
    )):
        gates["HYBRID_CANDIDATE_GATE"] = WP3GateStatus.PASS
    else:
        gates["HYBRID_CANDIDATE_GATE"] = WP3GateStatus.FAIL
    return gates


def _inconclusive_gates() -> dict[str, WP3GateStatus | str | dict[str, float]]:
    """比较前 identity/completeness 无效时，不生成下游质量 FAIL。"""
    return {
        "FAIRNESS_GATE": WP3GateStatus.INCONCLUSIVE,
        "PROVENANCE_CONSISTENCY_GATE": WP3GateStatus.INCONCLUSIVE,
        "EXECUTION_RELIABILITY_GATE": WP3GateStatus.INCONCLUSIVE,
        "QUALITY_GATE": WP3GateStatus.INCONCLUSIVE,
        "PER_CASE_REGRESSION_GATE": WP3GateStatus.INCONCLUSIVE,
        "LATENCY_GATE": WP3GateStatus.INCONCLUSIVE,
        "metric_deltas": {},
        "HYBRID_CANDIDATE_GATE": WP3GateStatus.INCONCLUSIVE,
    }


__all__ = [
    "WP3CaseClassification",
    "WP3ExperimentInvalid",
    "WP3GateStatus",
    "WP3IdentityMismatch",
    "WP3RewriteFixture",
    "WP3RewriteFixtureEntry",
    "WP3RunIdentity",
    "WP3RunSummary",
    "WP3_METRICS",
    "WP3_RETRIEVAL_CASE_COUNT",
    "WP3_TOTAL_CASE_COUNT",
    "aggregate_metrics",
    "canonical_sha256",
    "compute_localagent_source_identity",
    "dataset_content_sha256",
    "build_evaluated_settings_profile",
    "evaluated_settings_profile_sha256",
    "classify_case",
    "evaluate_candidate_gate",
    "metrics_for_artifact",
    "validate_pair_identities",
]
