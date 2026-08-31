"""WP6-E baseline candidate provenance structure（``WP6_STATEFUL_EPISODIC_BASELINE_V1``）。

本轮只建立 typed provenance structure / candidate builder；不自行宣布
``CANONICAL_BASELINE = YES``。最终 canonicalization 由后续 Codex Gate + Layer2 策略
决定。baseline candidate 至少冻结：

- baseline id / dataset identity + digest；
- AgentEvalOps implementation ref + LocalAgent target implementation ref；
- interpreter / execution policy / experiment lineage；
- 每个 scenario artifact 的 outcome / failure taxonomy / blocked facts。

数字 delta 只在 baseline 与 candidate 的 dataset digest、implementation ref、execution
target semantics 与 model provenance 全部兼容时才可比较（参考 WP5
``CompatibilityFacts`` 语义）；不兼容时只报告 ``BASELINE_INCOMPATIBLE``。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from app.core.evaluation.immutable import require_text


class EpisodicBaselineStatus(StrEnum):
    """baseline 状态：本轮只允许 CANDIDATE / NOT_CREATED。"""

    CANDIDATE = "CANDIDATE"
    INVALIDATED_CANDIDATE = "INVALIDATED_CANDIDATE"
    NOT_CREATED = "NOT_CREATED"


class EpisodicBaselineCompatibility(StrEnum):
    """baseline 与 candidate 的可比较性。"""

    COMPATIBLE = "COMPATIBLE"
    BASELINE_INCOMPATIBLE = "BASELINE_INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class EpisodicBaselineAuthority:
    """不可变候选 baseline authority 的完整 provenance 快照。"""

    baseline_id: str
    status: EpisodicBaselineStatus
    dataset: Mapping[str, str]
    dataset_lineage: Mapping[str, str]
    target_evaluation_implementation_ref: str
    target_source_receipt_digest: str
    agentevalops_evaluation_implementation_ref: str
    agentevalops_source_receipt_digest: str
    execution_policy: str
    scenario_outcomes: Mapping[str, str]
    assertion_summary: Mapping[str, int]
    failure_taxonomy: tuple[str, ...]
    blocked_taxonomy: tuple[str, ...]
    metrics: Mapping[str, Mapping[str, object]]
    layer1_gate: Mapping[str, object]
    environment_provenance: Mapping[str, object]
    experiment_artifact_ref: str
    experiment_artifact_digest: str

    def __post_init__(self) -> None:
        require_text(self.baseline_id, "baseline_id")
        if self.status is EpisodicBaselineStatus.NOT_CREATED:
            raise ValueError("baseline authority status must be CANDIDATE or INVALIDATED_CANDIDATE")
        for name, value in (
            ("dataset_schema", self.dataset.get("schema")),
            ("dataset_id", self.dataset.get("id")),
            ("dataset_version", self.dataset.get("version")),
            ("dataset_digest", self.dataset.get("digest")),
            ("parent_dataset_id", self.dataset_lineage.get("parent_dataset_id")),
            ("parent_dataset_version", self.dataset_lineage.get("parent_dataset_version")),
            ("parent_dataset_digest", self.dataset_lineage.get("parent_dataset_digest")),
            ("remediation_reason", self.dataset_lineage.get("remediation_reason")),
            ("authority_gate", self.dataset_lineage.get("authority_gate")),
            ("target_evaluation_implementation_ref", self.target_evaluation_implementation_ref),
            ("target_source_receipt_digest", self.target_source_receipt_digest),
            ("agentevalops_evaluation_implementation_ref", self.agentevalops_evaluation_implementation_ref),
            ("agentevalops_source_receipt_digest", self.agentevalops_source_receipt_digest),
            ("execution_policy", self.execution_policy),
            ("experiment_artifact_ref", self.experiment_artifact_ref),
            ("experiment_artifact_digest", self.experiment_artifact_digest),
        ):
            require_text(value, name)
        if len(self.scenario_outcomes) != 12:
            raise ValueError("baseline authority requires 12 scenario outcomes")
        if not self.assertion_summary or not self.metrics or not self.layer1_gate or not self.environment_provenance:
            raise ValueError("baseline authority requires assertion, metrics, gate, and environment provenance")

    def compatibility_with(self, candidate: "EpisodicBaselineAuthority") -> EpisodicBaselineCompatibility:
        """仅同一 frozen identity 才允许比较。"""
        keys = (
            self.dataset.get("digest"),
            self.target_evaluation_implementation_ref,
            self.target_source_receipt_digest,
            self.agentevalops_evaluation_implementation_ref,
            self.agentevalops_source_receipt_digest,
            self.execution_policy,
            self.experiment_artifact_digest,
        )
        candidate_keys = (
            candidate.dataset.get("digest"),
            candidate.target_evaluation_implementation_ref,
            candidate.target_source_receipt_digest,
            candidate.agentevalops_evaluation_implementation_ref,
            candidate.agentevalops_source_receipt_digest,
            candidate.execution_policy,
            candidate.experiment_artifact_digest,
        )
        return EpisodicBaselineCompatibility.COMPATIBLE if keys == candidate_keys else EpisodicBaselineCompatibility.BASELINE_INCOMPATIBLE

    @property
    def canonical_eligible(self) -> bool:
        """Only a non-invalidated candidate can enter a final freeze decision."""
        return self.status is EpisodicBaselineStatus.CANDIDATE


@dataclass(frozen=True, slots=True)
class EpisodicBaselineProvenance:
    """baseline 兼容性所需的 typed provenance facts。"""

    baseline_id: str
    dataset_id: str
    dataset_version: str
    dataset_digest: str | None
    agentevalops_implementation_ref: str | None
    target_evaluation_implementation_ref: str | None
    interpreter_ref: str | None = None
    execution_policy: str | None = None

    def __post_init__(self) -> None:
        require_text(self.baseline_id, "baseline_id")
        require_text(self.dataset_id, "dataset_id")
        require_text(self.dataset_version, "dataset_version")


@dataclass(frozen=True, slots=True)
class EpisodicBaselineCandidate:
    """一个 baseline candidate 快照（CANDIDATE；不得宣称 canonical）。"""

    status: EpisodicBaselineStatus
    provenance: EpisodicBaselineProvenance
    scenario_outcomes: dict[str, str] = field(default_factory=dict)
    failure_taxonomy: tuple[str, ...] = ()
    blocked_taxonomy: tuple[str, ...] = ()
    experiment_artifact_ref: str | None = None
    canonical_baseline: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, EpisodicBaselineStatus):
            raise ValueError("baseline status must be EpisodicBaselineStatus")
        if self.canonical_baseline:
            raise ValueError("this round must not declare a canonical baseline")
        if self.status is EpisodicBaselineStatus.NOT_CREATED:
            return
        if not self.provenance.dataset_digest:
            raise ValueError("candidate baseline requires a dataset digest")
        if not self.provenance.agentevalops_implementation_ref:
            raise ValueError("candidate baseline requires AgentEvalOps implementation ref")
        if not self.provenance.target_evaluation_implementation_ref:
            raise ValueError("candidate baseline requires target evaluation implementation ref")

    def to_dict(self) -> dict[str, object]:
        """投影为 JSON-safe candidate 快照。"""
        return {
            "baseline_id": self.provenance.baseline_id,
            "status": self.status.value,
            "provenance": {
                "dataset_id": self.provenance.dataset_id,
                "dataset_version": self.provenance.dataset_version,
                "dataset_digest": self.provenance.dataset_digest,
                "agentevalops_implementation_ref": self.provenance.agentevalops_implementation_ref,
                "target_evaluation_implementation_ref": self.provenance.target_evaluation_implementation_ref,
                "interpreter_ref": self.provenance.interpreter_ref,
                "execution_policy": self.provenance.execution_policy,
            },
            "scenario_outcomes": dict(self.scenario_outcomes),
            "failure_taxonomy": list(self.failure_taxonomy),
            "blocked_taxonomy": list(self.blocked_taxonomy),
            "experiment_artifact_ref": self.experiment_artifact_ref,
            "canonical_baseline": False,
        }


EPISODIC_BASELINE_ID: str = "WP6_STATEFUL_EPISODIC_BASELINE_V1"
EPISODIC_BASELINE_V2_ID: str = "WP6_STATEFUL_EPISODIC_BASELINE_V2"


__all__ = [
    "EPISODIC_BASELINE_ID",
    "EPISODIC_BASELINE_V2_ID",
    "EpisodicBaselineAuthority",
    "EpisodicBaselineCandidate",
    "EpisodicBaselineCompatibility",
    "EpisodicBaselineProvenance",
    "EpisodicBaselineStatus",
]
