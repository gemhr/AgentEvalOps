"""WP5 Stateful Memory 的 deterministic evaluators（纯函数，只读消费证据）。

所有 evaluator 只做 read-only 比较：

- Ground Truth Authority 完全来自 dataset ``StatefulMemoryScenario``；
- 不要求 runtime ``memory_id`` 与 dataset 固定 ID 相等；alias 与 memory_id 的绑定
  由 caller 在 scenario execution 中建立；
- 事件/快照缺失一律 fail-closed（BLOCKED），绝不默认 0 当作 PASS；
- retrieval 命名诚实为 lexical ``Expected Memory Recall@K`` / ``Hit@K`` /
  ``Irrelevant Memory Rejection Rate``，禁止语义召回术语；
- selected 与 supplied 严格区分；``direct_entry_supplied`` 绝不等于 injected PASS；
- leakage 只基于 selected/injected evidence，不基于最终 LLM 回答复述。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from app.core.evaluation.immutable import require_text
from app.core.evaluation.stateful_assertion import (
    AssertionDimension,
    AssertionStatus,
    BlockReason,
    EvaluationLayer,
    EvidenceGapClassification,
    FailureTaxonomy,
    MemoryAssertion,
)
from app.core.evaluation.stateful_metrics import (
    MetricAggregate,
    RatioMetric,
    RUNTIME_BLOCK_RATE_METRIC,
    EVALUATION_INFRA_FAILURE_RATE_METRIC,
    EXPECTED_EVIDENCE_LIMITATION_BLOCKED_COUNT_METRIC,
    SCENARIO_SUCCESS_RATE_METRIC,
    build_evaluation_infra_failure_rate,
    build_expected_evidence_limitation_blocked_aggregate,
    build_failure_rate_aggregate,
    build_metric_aggregate,
    build_runtime_block_rate,
    required_assertions,
    scenario_success_assertion,
    status_counts,
)
from app.core.evaluation.stateful_memory_dataset import (
    FormationDecision,
    LifecycleOperation,
    MemoryRecordExpectation,
    MemoryStatus,
    PredicateClassification,
    StatefulMemoryScenario,
    StatefulMemoryStep,
    TruthfulnessOrigin,
    REGISTERED_PREDICATES,
)
from app.core.evaluation.stateful_memory_dataset_v2 import (
    IdentityEvidenceRequirement,
    StatefulMemoryScenarioV2,
)
from app.core.evaluation.stateful_projection import (
    CanonicalMemoryRecord,
    MemoryStateSnapshot,
    RedactionState,
    count_active_by_logical_key,
    state_diff,
)
from app.core.evaluation.stateful_journal import JournalEvents

FORMATION_PRECISION_REMEMBER: Final[str] = "formation_decision_precision_remember"
FORMATION_RECALL_REMEMBER: Final[str] = "formation_decision_recall_remember"
FORMATION_PRECISION_IGNORE: Final[str] = "formation_decision_precision_ignore"
FORMATION_RECALL_IGNORE: Final[str] = "formation_decision_recall_ignore"
PREDICATE_CLASSIFICATION_ACCURACY: Final[str] = "registered_vs_open_accuracy"
PREDICATE_ID_ACCURACY: Final[str] = "registered_predicate_id_accuracy"
LIFECYCLE_OPERATION_ACCURACY: Final[str] = "lifecycle_operation_accuracy"
FINAL_STATE_ACCURACY: Final[str] = "memory_final_state_accuracy"
INVARIANT_PASS_RATE: Final[str] = "invariant_pass_rate"
RECALL_AT_K_METRIC: Final[str] = "expected_memory_recall_at_k"
HIT_AT_K_METRIC: Final[str] = "hit_at_k"
REJECTION_RATE_METRIC: Final[str] = "irrelevant_memory_rejection_rate"
INJECTION_SUCCESS_RATE_METRIC: Final[str] = "injection_success_rate"
FORGOTTEN_LEAKAGE_RATE_METRIC: Final[str] = "forgotten_memory_leakage_rate"
SUPERSEDED_LEAKAGE_RATE_METRIC: Final[str] = "superseded_memory_leakage_rate"
SCOPE_LEAKAGE_RATE_METRIC: Final[str] = "scope_leakage_rate"
IRRELEVANT_INJECTION_RATE_METRIC: Final[str] = "irrelevant_memory_injection_rate"
GENERATION_USE_METRIC: Final[str] = "generation_use"

_NA_BLOCKED_REASON = "no formation expectation for this step"


class RetrievalEvidenceSource(StrEnum):
    """retrieval selection 证据来源；identity 级证据缺失时按 BLOCKED 处理。"""

    SELECTION_IDS = "SELECTION_IDS"
    JOURNAL_COUNTS = "JOURNAL_COUNTS"


@dataclass(frozen=True, slots=True)
class RetrievalSelectionEvidence:
    """一次 retrieval step 的 selected/injected 观察（content-minimized counts + optional ids）。"""

    step_id: str
    run_id: str
    retrieval_status: str
    selected_count: int
    context_record_count: int
    planning_injected: bool
    direct_entry_supplied: bool
    registered_selected_count: int
    open_selected_count: int
    selected_memory_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        require_text(self.step_id, "step_id")
        require_text(self.run_id, "run_id")
        require_text(self.retrieval_status, "retrieval_status")
        counts = (
            self.selected_count,
            self.context_record_count,
            self.registered_selected_count,
            self.open_selected_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("retrieval evidence counts must be non-negative")
        if self.selected_memory_ids is not None:
            object.__setattr__(self, "selected_memory_ids", tuple(self.selected_memory_ids))
            if self.selected_count != len(self.selected_memory_ids):
                raise ValueError("selected_count must match selected_memory_ids length")

    @property
    def evidence_source(self) -> RetrievalEvidenceSource:
        """返回证据来源：SELECTION_IDS 或仅 JOURNAL_COUNTS。"""
        if self.selected_memory_ids is not None:
            return RetrievalEvidenceSource.SELECTION_IDS
        return RetrievalEvidenceSource.JOURNAL_COUNTS


@dataclass(frozen=True, slots=True)
class ScenarioEvaluationEvidence:
    """一次 scenario 的全部只读证据投影（由 runner 构建）。"""

    scenario: StatefulMemoryScenario
    journal_by_step: dict[str, JournalEvents] = field(default_factory=dict, compare=False)
    snapshots_by_step: dict[str, tuple[MemoryStateSnapshot, MemoryStateSnapshot]] = field(
        default_factory=dict, compare=False
    )
    final_snapshot: MemoryStateSnapshot | None = None
    outcome_kind_by_step: dict[str, str] = field(default_factory=dict, compare=False)
    run_id_by_step: dict[str, str] = field(default_factory=dict, compare=False)
    selection_by_step: dict[str, RetrievalSelectionEvidence] = field(default_factory=dict, compare=False)
    alias_binding: dict[str, str] = field(default_factory=dict, compare=False)
    final_answer_text: str | None = None
    evaluation_layer: EvaluationLayer = EvaluationLayer.LAYER_1_DETERMINISTIC

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, (StatefulMemoryScenario, StatefulMemoryScenarioV2)):
            raise TypeError("evidence scenario must be StatefulMemoryScenario or StatefulMemoryScenarioV2")
        if not isinstance(self.evaluation_layer, EvaluationLayer):
            raise TypeError("evidence evaluation_layer must be EvaluationLayer")


@dataclass(frozen=True, slots=True)
class _FormationOutcome:
    actual: FormationDecision
    blocked_by: BlockReason | None = None


@dataclass(frozen=True, slots=True)
class _LifecycleOutcome:
    operation: LifecycleOperation | None
    outcome: str | None
    detail: str
    blocked_by: BlockReason | None = None


@dataclass(frozen=True, slots=True)
class ScenarioEvaluation:
    """一次 scenario 的完整 evaluation 结果。"""

    scenario_id: str
    assertions: tuple[MemoryAssertion, ...]
    metrics: dict[str, MetricAggregate | RatioMetric]
    runtime_block_rate: RatioMetric
    evaluation_infra_failure_rate: RatioMetric
    scenario_outcome: AssertionStatus
    scenario_outcome_assertion: MemoryAssertion
    failure_taxonomies: tuple[str, ...]
    deterministic_gate_eligible: bool = False
    truthfulness_origin: str | None = None
    required: bool = True


def _blocked(
    assertion_id: str,
    dimension: AssertionDimension,
    reason: str,
    blocked_by: BlockReason,
    *,
    expected: object = None,
    required: bool = True,
    evidence_gap_classification: EvidenceGapClassification | None = None,
) -> MemoryAssertion:
    return MemoryAssertion(
        assertion_id=assertion_id,
        dimension=dimension,
        status=AssertionStatus.BLOCKED,
        expected=expected,
        blocked_by=blocked_by,
        evidence_gap_classification=evidence_gap_classification,
        reason=reason,
        required=required,
    )


def _na(assertion_id: str, dimension: AssertionDimension, reason: str) -> MemoryAssertion:
    return MemoryAssertion(
        assertion_id=assertion_id,
        dimension=dimension,
        status=AssertionStatus.NOT_APPLICABLE,
        reason=reason,
    )


def _pass(
    assertion_id: str,
    dimension: AssertionDimension,
    reason: str,
    *,
    expected: object = None,
    actual: object = None,
    required: bool = True,
) -> MemoryAssertion:
    return MemoryAssertion(
        assertion_id=assertion_id,
        dimension=dimension,
        status=AssertionStatus.PASS,
        expected=expected,
        actual_evidence=actual,
        reason=reason,
        required=required,
    )


def _fail(
    assertion_id: str,
    dimension: AssertionDimension,
    reason: str,
    taxonomy: FailureTaxonomy,
    *,
    expected: object = None,
    actual: object = None,
    required: bool = True,
) -> MemoryAssertion:
    return MemoryAssertion(
        assertion_id=assertion_id,
        dimension=dimension,
        status=AssertionStatus.FAIL,
        expected=expected,
        actual_evidence=actual,
        failure_taxonomy=taxonomy,
        reason=reason,
        required=required,
    )


def build_alias_binding(
    expected_records: list[MemoryRecordExpectation],
    snapshots: list[MemoryStateSnapshot],
) -> dict[str, str]:
    """把 expected alias 绑定到 runtime memory_id（canonical identity 匹配，不依赖固定 ID）。

    在每个快照中按 (agent, scope, type, logical_key, status, value) 找到唯一匹配记录。
    一个 alias 只允许绑定到一个 memory_id；找不到或重复则跳过（调用方决定是否 BLOCKED）。
    """
    binding: dict[str, str] = {}
    for expectation in expected_records:
        candidates: list[str] = []
        for snapshot in snapshots:
            for record in snapshot.records:
                identity_match = (
                    record.agent_id == expectation.agent_id
                    and record.memory_scope == expectation.memory_scope
                    and record.memory_type == expectation.memory_type
                    and record.logical_key == expectation.logical_key
                    and record.status == expectation.status
                )
                if not identity_match:
                    continue
                if expectation.status != "FORGOTTEN" and record.canonical_value != expectation.value:
                    continue
                candidates.append(record.memory_id)
        unique = list(dict.fromkeys(candidates))
        if len(unique) == 1:
            binding[expectation.alias] = unique[0]
    return binding


def _candidate_outcomes_decision(candidate_outcomes: str) -> FormationDecision | None:
    """从 formation event 的 candidate_outcomes 解析 decision（无 candidate 返回 None）。

    candidate_outcomes 形如 ``"ordinal|OUTCOME|REASON|memory_id"`` 以 ``;`` 连接，
    ``"NONE"`` 表示无 candidate。OUTCOME ∈ ACCEPTED / PERSISTED / REUSED / NO_CHANGE /
    IGNORED。任一 accepted/persisted/reused/no_change candidate → REMEMBER；全部
    ignored → IGNORE。
    """
    if not candidate_outcomes or candidate_outcomes == "NONE":
        return None
    ignored_seen = False
    for part in candidate_outcomes.split(";"):
        tokens = part.split("|")
        if len(tokens) < 2:
            continue
        outcome = tokens[1].strip().upper()
        if outcome in {"ACCEPTED", "PERSISTED", "REUSED", "NO_CHANGE"}:
            return FormationDecision.REMEMBER
        if outcome == "IGNORED":
            ignored_seen = True
    return FormationDecision.IGNORE if ignored_seen else None


def _formation_actual(evidence: ScenarioEvaluationEvidence, step: StatefulMemoryStep) -> _FormationOutcome:
    """真实 formation decision（E1-R2 冻结语义）。

    - accepted/persisted/reused candidate → REMEMBER（``reused_count > 0`` / NO_CHANGE
      的 ``persisted_count=0`` 仍是合法 REMEMBER decision）。
    - accepted=0（无 candidate 或全部 ignored）→ 观察上未记住任何东西，
      IGNORE-equivalent；expected REMEMBER 时由调用方判 FAIL(FORMATION_FALSE_NEGATIVE)。
    - formation event 本身报告 typed runtime failure，或 runtime 在 formation 前终止
      → BLOCKED(runtime)。
    - 只有证据缺失（无 formation event + SUCCESS）才 BLOCKED(evidence_capture)。
    """
    journal = evidence.journal_by_step.get(step.step_id)
    outcome_kind = evidence.outcome_kind_by_step.get(step.step_id, "UNKNOWN")
    if journal is None or not journal.formation:
        if outcome_kind == "SUCCESS":
            return _FormationOutcome(FormationDecision.BLOCKED, BlockReason.EVIDENCE_CAPTURE)
        return _FormationOutcome(FormationDecision.BLOCKED, BlockReason.RUNTIME)
    if any(
        event.safe_error_code is not None or event.status not in {"SUCCEEDED", "OK", "COMPLETED"}
        for event in journal.formation
    ):
        return _FormationOutcome(FormationDecision.BLOCKED, BlockReason.RUNTIME)
    if any(
        event.accepted_count > 0 or event.persisted_count > 0 or event.reused_count > 0 for event in journal.formation
    ):
        return _FormationOutcome(FormationDecision.REMEMBER)
    if all(event.ignored_count > 0 for event in journal.formation):
        return _FormationOutcome(FormationDecision.IGNORE)
    parsed = [_candidate_outcomes_decision(event.candidate_outcomes) for event in journal.formation]
    if any(decision is FormationDecision.REMEMBER for decision in parsed):
        return _FormationOutcome(FormationDecision.REMEMBER)
    return _FormationOutcome(FormationDecision.IGNORE)


def _lifecycle_actual(evidence: ScenarioEvaluationEvidence, step: StatefulMemoryStep) -> _LifecycleOutcome:
    journal = evidence.journal_by_step.get(step.step_id)
    outcome_kind = evidence.outcome_kind_by_step.get(step.step_id, "UNKNOWN")
    if journal is None or not journal.lifecycle:
        if outcome_kind == "SUCCESS":
            return _LifecycleOutcome(None, None, "no lifecycle event", BlockReason.EVIDENCE_CAPTURE)
        return _LifecycleOutcome(None, None, "runtime blocked before lifecycle", BlockReason.RUNTIME)
    event = journal.lifecycle[-1]
    return _LifecycleOutcome(
        LifecycleOperation(event.operation) if event.operation else None,
        event.outcome,
        f"operation={event.operation}; outcome={event.outcome}",
    )


def _policy_ignored_passes(evidence: ScenarioEvaluationEvidence, step: StatefulMemoryStep) -> bool:
    journal = evidence.journal_by_step.get(step.step_id)
    if journal is None:
        return False
    if journal.lifecycle:
        return False
    if any(event.accepted_count > 0 or event.persisted_count > 0 for event in journal.formation):
        return False
    return True


def _post_snapshot(evidence: ScenarioEvaluationEvidence, step_id: str) -> MemoryStateSnapshot | None:
    pair = evidence.snapshots_by_step.get(step_id)
    if pair is None:
        return None
    return pair[1]


def _formed_memory(evidence: ScenarioEvaluationEvidence, step: StatefulMemoryStep) -> CanonicalMemoryRecord | None:
    post = _post_snapshot(evidence, step.step_id)
    if post is None:
        return None
    run_id = evidence.run_id_by_step.get(step.step_id, step.step_id)
    candidates = [
        record
        for record in post.records
        if record.status == "ACTIVE"
        and record.origin_run_id == run_id
        and record.agent_id == step.agent_id
        and record.memory_scope == step.memory_scope
    ]
    if not candidates:
        return None
    return candidates[0]


def evaluate_formation(
    evidence: ScenarioEvaluationEvidence,
) -> tuple[list[MemoryAssertion], list[tuple[str, FormationDecision, FormationDecision | None]]]:
    """Formation Decision Precision / Recall（REMEMBER 与 IGNORE 各自正类）。

    每个 expected formation decision 是一个 unit。planning 在 formation 前失败 → BLOCKED
    （runtime），不是 false negative。
    """
    assertions: list[MemoryAssertion] = []
    decisions: list[tuple[str, FormationDecision, FormationDecision | None]] = []
    for step in evidence.scenario.steps:
        expected = step.expected_formation
        if expected is None:
            continue
        assertion_id = f"{evidence.scenario.scenario_id}.{step.step_id}.formation"
        if expected.decision is FormationDecision.NA:
            assertions.append(_na(assertion_id, AssertionDimension.FORMATION, "formation not asserted"))
            continue
        outcome = _formation_actual(evidence, step)
        if expected.decision is FormationDecision.BLOCKED:
            if outcome.actual is FormationDecision.BLOCKED:
                assertions.append(
                    _pass(
                        assertion_id,
                        AssertionDimension.FORMATION,
                        "formation blocked as expected",
                        expected="BLOCKED",
                        actual="BLOCKED",
                        required=step.required,
                    )
                )
                decisions.append((assertion_id, FormationDecision.BLOCKED, FormationDecision.BLOCKED))
            else:
                assertions.append(
                    _fail(
                        assertion_id,
                        AssertionDimension.FORMATION,
                        "formation happened despite expected BLOCKED",
                        FailureTaxonomy.FORMATION_FALSE_POSITIVE,
                        expected="BLOCKED",
                        actual=outcome.actual.value,
                        required=step.required,
                    )
                )
                decisions.append((assertion_id, FormationDecision.BLOCKED, outcome.actual))
            continue
        if outcome.actual is FormationDecision.BLOCKED:
            assertions.append(
                _blocked(
                    assertion_id,
                    AssertionDimension.FORMATION,
                    "formation evidence is unavailable",
                    outcome.blocked_by or BlockReason.EVIDENCE_CAPTURE,
                    expected=expected.decision.value,
                    required=step.required,
                )
            )
            decisions.append((assertion_id, expected.decision, None))
            continue
        if outcome.actual is expected.decision:
            assertions.append(
                _pass(
                    assertion_id,
                    AssertionDimension.FORMATION,
                    f"formation decision {expected.decision.value} matches",
                    expected=expected.decision.value,
                    actual=outcome.actual.value,
                    required=step.required,
                )
            )
        else:
            taxonomy = (
                FailureTaxonomy.FORMATION_FALSE_POSITIVE
                if outcome.actual is FormationDecision.REMEMBER
                else FailureTaxonomy.FORMATION_FALSE_NEGATIVE
            )
            assertions.append(
                _fail(
                    assertion_id,
                    AssertionDimension.FORMATION,
                    f"expected {expected.decision.value} but actual {outcome.actual.value}",
                    taxonomy,
                    expected=expected.decision.value,
                    actual=outcome.actual.value,
                    required=step.required,
                )
            )
        decisions.append((assertion_id, expected.decision, outcome.actual))
    return assertions, decisions


def _decision_metrics(
    name: str,
    positive_class: FormationDecision,
    decisions: list[tuple[str, FormationDecision, FormationDecision | None]],
) -> tuple[MetricAggregate, MetricAggregate]:
    """Precision/Recall：只有可 evaluable 的实际决策进入 denominator。

    blocked（actual=None）与 NA（expected=NA）不进 quality denominator；blocked 单列。
    """
    evaluable = [
        (assertion_id, expected, actual)
        for assertion_id, expected, actual in decisions
        if expected is not FormationDecision.NA and actual is not None
    ]
    blocked = sum(expected is not FormationDecision.NA and actual is None for _, expected, actual in decisions)
    not_applicable = sum(expected is FormationDecision.NA for _, expected, _ in decisions)
    correct = sum(expected is positive_class and actual is positive_class for _, expected, actual in evaluable)
    false_positive = sum(
        expected is not positive_class and actual is positive_class for _, expected, actual in evaluable
    )
    false_negative = sum(
        expected is positive_class and actual is not positive_class for _, expected, actual in evaluable
    )
    precision = _aggregate(f"{name}_precision", correct, false_positive, blocked, not_applicable)
    recall = _aggregate(f"{name}_recall", correct, false_negative, blocked, not_applicable)
    return precision, recall


def _aggregate(name: str, passed: int, failed: int, blocked: int, not_applicable: int) -> MetricAggregate:
    denominator = passed + failed
    value: float | None = None
    if denominator > 0:
        value = passed / denominator
    return MetricAggregate(
        metric_name=name,
        passed=passed,
        failed=failed,
        blocked=blocked,
        not_applicable=not_applicable,
        evaluable_denominator=denominator,
        value=value,
    )


def evaluate_predicate(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """REGISTERED_vs_OPEN Accuracy + Registered Predicate ID Accuracy。

    formation failed-closed / runtime blocked 是 BLOCKED；绝不从 logical_key 反推 formation。
    """
    assertions: list[MemoryAssertion] = []
    for step in evidence.scenario.steps:
        expected = step.expected_formation
        if expected is None or expected.predicate is None:
            continue
        assertion_id = f"{evidence.scenario.scenario_id}.{step.step_id}.predicate"
        if expected.predicate.classification is PredicateClassification.BLOCKED:
            outcome = _formation_actual(evidence, step)
            if outcome.actual is FormationDecision.BLOCKED:
                assertions.append(
                    _pass(
                        assertion_id,
                        AssertionDimension.PREDICATE,
                        "predicate classification blocked as expected",
                        expected="BLOCKED",
                        actual="BLOCKED",
                        required=step.required,
                    )
                )
            else:
                assertions.append(
                    _fail(
                        assertion_id,
                        AssertionDimension.PREDICATE,
                        "predicate classified despite expected BLOCKED",
                        FailureTaxonomy.PREDICATE_CLASSIFICATION_ERROR,
                        expected="BLOCKED",
                        actual=outcome.actual.value,
                        required=step.required,
                    )
                )
            continue
        if expected.decision is not FormationDecision.REMEMBER:
            assertions.append(
                _na(
                    assertion_id,
                    AssertionDimension.PREDICATE,
                    "predicate only asserted for REMEMBER formation",
                )
            )
            continue
        outcome = _formation_actual(evidence, step)
        if outcome.actual is not FormationDecision.REMEMBER:
            assertions.append(
                _blocked(
                    assertion_id,
                    AssertionDimension.PREDICATE,
                    "formation did not occur; predicate cannot be classified",
                    outcome.blocked_by or BlockReason.PREREQUISITE,
                    expected=expected.predicate.classification.value,
                    required=step.required,
                )
            )
            continue
        formed = _formed_memory(evidence, step)
        if formed is None:
            assertions.append(
                _blocked(
                    assertion_id,
                    AssertionDimension.PREDICATE,
                    "formed memory is not present in post-step snapshot",
                    BlockReason.EVIDENCE_CAPTURE,
                    expected=expected.predicate.classification.value,
                    required=step.required,
                )
            )
            continue
        actual_class = (
            PredicateClassification.REGISTERED
            if formed.logical_key in REGISTERED_PREDICATES
            else PredicateClassification.OPEN
        )
        if actual_class is not expected.predicate.classification:
            assertions.append(
                _fail(
                    assertion_id,
                    AssertionDimension.PREDICATE,
                    f"expected {expected.predicate.classification.value} but actual {actual_class.value}",
                    FailureTaxonomy.PREDICATE_CLASSIFICATION_ERROR,
                    expected=expected.predicate.classification.value,
                    actual={"classification": actual_class.value, "logical_key": formed.logical_key},
                    required=step.required,
                )
            )
            continue
        if expected.predicate.classification is PredicateClassification.REGISTERED:
            expected_id = expected.predicate.predicate_id
            if formed.logical_key != expected_id:
                assertions.append(
                    _fail(
                        assertion_id,
                        AssertionDimension.PREDICATE,
                        f"expected registered predicate {expected_id} but actual {formed.logical_key}",
                        FailureTaxonomy.PREDICATE_CLASSIFICATION_ERROR,
                        expected=expected_id,
                        actual=formed.logical_key,
                        required=step.required,
                    )
                )
                continue
        assertions.append(
            _pass(
                assertion_id,
                AssertionDimension.PREDICATE,
                f"predicate classified as {actual_class.value}",
                expected=expected.predicate.classification.value,
                actual={"classification": actual_class.value, "logical_key": formed.logical_key},
                required=step.required,
            )
        )
    return assertions


def evaluate_lifecycle(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """Exact lifecycle operation accuracy（INSERT/NO_CHANGE/SUPERSEDE/FORGET 等）。"""
    assertions: list[MemoryAssertion] = []
    core_operations = {
        LifecycleOperation.INSERT,
        LifecycleOperation.NO_CHANGE,
        LifecycleOperation.SUPERSEDE,
        LifecycleOperation.FORGET,
    }
    outcome_expectations = {
        LifecycleOperation.NOT_FOUND: "NOT_FOUND",
        LifecycleOperation.ALREADY_FORGOTTEN: "ALREADY_FORGOTTEN",
    }
    for step in evidence.scenario.steps:
        expected = step.expected_lifecycle
        if expected is None:
            continue
        assertion_id = f"{evidence.scenario.scenario_id}.{step.step_id}.lifecycle"
        if expected is LifecycleOperation.POLICY_IGNORED:
            if _policy_ignored_passes(evidence, step):
                assertions.append(
                    _pass(
                        assertion_id,
                        AssertionDimension.LIFECYCLE,
                        "formation policy ignored the candidate as expected",
                        expected=expected.value,
                        actual="POLICY_IGNORED",
                        required=step.required,
                    )
                )
            else:
                assertions.append(
                    _fail(
                        assertion_id,
                        AssertionDimension.LIFECYCLE,
                        "lifecycle ran or candidate was accepted despite POLICY_IGNORED",
                        FailureTaxonomy.LIFECYCLE_OPERATION_MISMATCH,
                        expected="POLICY_IGNORED",
                        actual="LIFECYCLE_RESOLVED",
                        required=step.required,
                    )
                )
            continue
        outcome = _lifecycle_actual(evidence, step)
        if outcome.blocked_by is not None:
            assertions.append(
                _blocked(
                    assertion_id,
                    AssertionDimension.LIFECYCLE,
                    outcome.detail,
                    outcome.blocked_by,
                    expected=expected.value,
                    required=step.required,
                )
            )
            continue
        if expected in core_operations:
            if outcome.operation is expected and outcome.outcome == "OK":
                assertions.append(
                    _pass(
                        assertion_id,
                        AssertionDimension.LIFECYCLE,
                        f"lifecycle {expected.value} matches with outcome OK",
                        expected=expected.value,
                        actual=f"{outcome.operation.value}:{outcome.outcome}",
                        required=step.required,
                    )
                )
                continue
            assertions.append(
                _fail(
                    assertion_id,
                    AssertionDimension.LIFECYCLE,
                    f"expected {expected.value} but actual {outcome.operation or 'NONE'}:{outcome.outcome}",
                    FailureTaxonomy.LIFECYCLE_OPERATION_MISMATCH,
                    expected=expected.value,
                    actual=f"{outcome.operation.value if outcome.operation else 'NONE'}:{outcome.outcome}",
                    required=step.required,
                )
            )
            continue
        expected_outcome = outcome_expectations.get(expected)
        if expected_outcome is not None and outcome.outcome == expected_outcome:
            assertions.append(
                _pass(
                    assertion_id,
                    AssertionDimension.LIFECYCLE,
                    f"lifecycle outcome {expected_outcome} matches",
                    expected=expected.value,
                    actual=outcome.outcome,
                    required=step.required,
                )
            )
            continue
        assertions.append(
            _fail(
                assertion_id,
                AssertionDimension.LIFECYCLE,
                f"expected {expected.value} but actual {outcome.operation or 'NONE'}:{outcome.outcome}",
                FailureTaxonomy.LIFECYCLE_OPERATION_MISMATCH,
                expected=expected.value,
                actual=f"{outcome.operation.value if outcome.operation else 'NONE'}:{outcome.outcome}",
                required=step.required,
            )
        )
    return assertions


def evaluate_final_state(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """Canonical final-state compare：expected records vs actual records。

    若任何 required formation 在 runtime 前被阻断（formation BLOCKED），final state
    依赖未执行 → BLOCKED(prerequisite)，不判为 MISSING FAIL。
    """
    assertion_id = f"{evidence.scenario.scenario_id}.final_state"
    formation_blocked = any(
        step.expected_formation is not None
        and step.expected_formation.decision is FormationDecision.REMEMBER
        and _formation_actual(evidence, step).actual is FormationDecision.BLOCKED
        and step.required
        for step in evidence.scenario.steps
    )
    if formation_blocked:
        return [
            _blocked(
                assertion_id,
                AssertionDimension.FINAL_STATE,
                "final state depends on formation that was blocked by the runtime",
                BlockReason.PREREQUISITE,
            )
        ]
    if evidence.final_snapshot is None:
        return [
            _blocked(
                assertion_id,
                AssertionDimension.FINAL_STATE,
                "final snapshot is unavailable",
                BlockReason.EVIDENCE_CAPTURE,
            )
        ]
    diffs = state_diff(
        [item for item in evidence.scenario.expected_state],
        evidence.final_snapshot.records,
        alias_binding=evidence.alias_binding,
    )
    if not diffs:
        return [
            _pass(
                assertion_id,
                AssertionDimension.FINAL_STATE,
                "final state exactly matches expected canonical projection",
                expected=[item.alias for item in evidence.scenario.expected_state],
                actual=[record.memory_id for record in evidence.final_snapshot.records],
            )
        ]
    return [
        _fail(
            assertion_id,
            AssertionDimension.FINAL_STATE,
            f"final state differs: {len(diffs)} diff(s)",
            FailureTaxonomy.FINAL_STATE_MISMATCH,
            expected=[item.alias for item in evidence.scenario.expected_state],
            actual=[diff.to_dict() for diff in diffs],
        )
    ]


def _keyed_partition_records(
    records: tuple[CanonicalMemoryRecord, ...],
    logical_key: str,
) -> list[CanonicalMemoryRecord]:
    return [record for record in records if record.logical_key == logical_key]


def evaluate_invariants(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """逐项 invariant 检查；每项保留独立 assertion（Invariant Pass Rate 分母）。"""
    assertions: list[MemoryAssertion] = []
    allowed_agent_scopes: dict[tuple[str, str], None] = {}
    for step in evidence.scenario.steps:
        allowed_agent_scopes.setdefault((step.agent_id, step.memory_scope), None)
    for record in evidence.scenario.expected_state:
        allowed_agent_scopes.setdefault((record.agent_id, record.memory_scope), None)
    for record in evidence.scenario.initial_state.records:
        allowed_agent_scopes.setdefault((record.agent_id, record.memory_scope), None)

    for step in evidence.scenario.steps:
        expected = step.expected_formation
        lifecycle = step.expected_lifecycle
        if expected is None or lifecycle is None:
            continue
        pair = evidence.snapshots_by_step.get(step.step_id)
        if pair is None:
            assertions.append(
                _blocked(
                    f"{evidence.scenario.scenario_id}.{step.step_id}.invariant.snapshot",
                    AssertionDimension.INVARIANT,
                    "pre/post snapshots unavailable",
                    BlockReason.EVIDENCE_CAPTURE,
                    required=step.required,
                )
            )
            continue
        pre, post = pair
        base = f"{evidence.scenario.scenario_id}.{step.step_id}.invariant"

        active_counts = count_active_by_logical_key(post.records)
        violations = [key for key, count in active_counts.items() if count > 1]
        if violations:
            assertions.append(
                _fail(
                    f"{base}.keyed_active_le_1",
                    AssertionDimension.INVARIANT,
                    "keyed partition has more than one ACTIVE row",
                    FailureTaxonomy.INVARIANT_VIOLATION,
                    expected=1,
                    actual={str(key): count for key, count in active_counts.items() if count > 1},
                    required=step.required,
                )
            )
        else:
            assertions.append(
                _pass(
                    f"{base}.keyed_active_le_1",
                    AssertionDimension.INVARIANT,
                    "no keyed partition has more than one ACTIVE row",
                    required=step.required,
                )
            )

        keyed_steps = [
            record
            for record in post.records
            if record.status == "ACTIVE"
            and record.agent_id == step.agent_id
            and record.memory_scope == step.memory_scope
        ]
        if lifecycle is LifecycleOperation.NO_CHANGE:
            pre_ids = {record.memory_id for record in pre.records}
            post_ids = {record.memory_id for record in post.records}
            if pre_ids != post_ids:
                assertions.append(
                    _fail(
                        f"{base}.no_change_no_new_row",
                        AssertionDimension.INVARIANT,
                        "NO_CHANGE created or removed a memory row",
                        FailureTaxonomy.INVARIANT_VIOLATION,
                        expected=sorted(pre_ids),
                        actual=sorted(post_ids),
                        required=step.required,
                    )
                )
            else:
                assertions.append(
                    _pass(
                        f"{base}.no_change_no_new_row",
                        AssertionDimension.INVARIANT,
                        "NO_CHANGE created no new row",
                        required=step.required,
                    )
                )
                for winner in keyed_steps:
                    pre_winner = next((r for r in pre.records if r.memory_id == winner.memory_id), None)
                    if pre_winner is None:
                        continue
                    unchanged = (
                        pre_winner.created_at == winner.created_at
                        and pre_winner.updated_at == winner.updated_at
                        and pre_winner.provenance_key() == winner.provenance_key()
                    )
                    if unchanged:
                        assertions.append(
                            _pass(
                                f"{base}.no_change_keeps_winner",
                                AssertionDimension.INVARIANT,
                                "NO_CHANGE kept winner created_at/updated_at/provenance",
                                actual={
                                    "created_at": winner.created_at,
                                    "updated_at": winner.updated_at,
                                    "provenance": winner.provenance_key(),
                                },
                                required=step.required,
                            )
                        )
                    else:
                        assertions.append(
                            _fail(
                                f"{base}.no_change_keeps_winner",
                                AssertionDimension.INVARIANT,
                                "NO_CHANGE mutated winner timestamps or provenance",
                                FailureTaxonomy.INVARIANT_VIOLATION,
                                expected={
                                    "created_at": pre_winner.created_at,
                                    "updated_at": pre_winner.updated_at,
                                    "provenance": pre_winner.provenance_key(),
                                },
                                actual={
                                    "created_at": winner.created_at,
                                    "updated_at": winner.updated_at,
                                    "provenance": winner.provenance_key(),
                                },
                                required=step.required,
                            )
                        )

        if lifecycle is LifecycleOperation.SUPERSEDE:
            keyed_post = [
                record
                for record in post.records
                if record.agent_id == step.agent_id
                and record.memory_scope == step.memory_scope
                and record.logical_key is not None
            ]
            winners = [record for record in keyed_post if record.status == "ACTIVE"]
            superseded = [record for record in keyed_post if record.status == "SUPERSEDED"]
            if len(winners) != 1:
                assertions.append(
                    _fail(
                        f"{base}.supersede_single_winner",
                        AssertionDimension.INVARIANT,
                        "SUPERSEDE did not yield exactly one ACTIVE winner",
                        FailureTaxonomy.INVARIANT_VIOLATION,
                        expected=1,
                        actual=len(winners),
                        required=step.required,
                    )
                )
            else:
                winner = winners[0]
                direct = all(record.superseded_by_memory_id == winner.memory_id for record in superseded)
                if direct:
                    assertions.append(
                        _pass(
                            f"{base}.supersede_direct_to_latest",
                            AssertionDimension.INVARIANT,
                            "SUPERSEDE relations point directly at the ACTIVE winner",
                            required=step.required,
                        )
                    )
                else:
                    assertions.append(
                        _fail(
                            f"{base}.supersede_direct_to_latest",
                            AssertionDimension.INVARIANT,
                            "a SUPERSEDED row does not point directly at the ACTIVE winner",
                            FailureTaxonomy.INVARIANT_VIOLATION,
                            expected=winner.memory_id,
                            actual=[record.superseded_by_memory_id for record in superseded],
                            required=step.required,
                        )
                    )

        if lifecycle is LifecycleOperation.FORGET:
            expected_predicate_id = (
                step.expected_formation.predicate.predicate_id
                if step.expected_formation and step.expected_formation.predicate
                else None
            )
            keyed_post = [
                record
                for record in post.records
                if record.agent_id == step.agent_id
                and record.memory_scope == step.memory_scope
                and record.logical_key == expected_predicate_id
            ]
            forgotten = [record for record in keyed_post if record.status == "FORGOTTEN"]
            if not keyed_post or not forgotten:
                assertions.append(
                    _fail(
                        f"{base}.forget_all_tombstoned",
                        AssertionDimension.INVARIANT,
                        "FORGET did not tombstone the keyed partition",
                        FailureTaxonomy.INVARIANT_VIOLATION,
                        expected="FORGOTTEN",
                        actual=[record.status for record in keyed_post],
                        required=step.required,
                    )
                )
            else:
                redacted = all(record.redaction_state() is RedactionState.REDACTED for record in forgotten)
                if redacted:
                    assertions.append(
                        _pass(
                            f"{base}.forget_redaction",
                            AssertionDimension.INVARIANT,
                            "FORGOTTEN records are fully redacted",
                            required=step.required,
                        )
                    )
                else:
                    assertions.append(
                        _fail(
                            f"{base}.forget_redaction",
                            AssertionDimension.INVARIANT,
                            "a FORGOTTEN record is not fully redacted",
                            FailureTaxonomy.INVARIANT_VIOLATION,
                            expected="[FORGOTTEN]",
                            actual=[
                                record.to_projection_dict()
                                for record in forgotten
                                if record.redaction_state() is not RedactionState.REDACTED
                            ],
                            required=step.required,
                        )
                    )

    foreign = [
        record
        for record in (evidence.final_snapshot.records if evidence.final_snapshot else ())
        if (record.agent_id, record.memory_scope) not in allowed_agent_scopes
    ]
    if foreign:
        assertions.append(
            _fail(
                f"{evidence.scenario.scenario_id}.scope_isolation",
                AssertionDimension.INVARIANT,
                "final state contains records outside declared agent/scope",
                FailureTaxonomy.INVARIANT_VIOLATION,
                expected=[],
                actual=[
                    {"agent_id": r.agent_id, "memory_scope": r.memory_scope, "memory_id": r.memory_id} for r in foreign
                ],
            )
        )
    else:
        assertions.append(
            _pass(
                f"{evidence.scenario.scenario_id}.scope_isolation",
                AssertionDimension.INVARIANT,
                "no records leaked outside declared agent/scope",
                actual=[],
            )
        )
    return assertions


def _selected_alias_ids(evidence: ScenarioEvaluationEvidence, aliases: list[str]) -> tuple[list[str], list[str]]:
    bound: list[str] = []
    unbounded: list[str] = []
    for alias in aliases:
        memory_id = evidence.alias_binding.get(alias)
        if memory_id is None:
            unbounded.append(alias)
        else:
            bound.append(memory_id)
    return bound, unbounded


def _identity_evidence_block(
    evidence: ScenarioEvaluationEvidence,
    expected: object,
) -> tuple[BlockReason, EvidenceGapClassification | None]:
    """按当前 layer + dataset identity policy 判定 identity evidence 缺失时的 BLOCKED 分类。

    - policy 对该 layer 声明 EXPECTED_LIMITATION -> NOT_SUPPORTED_BY_CURRENT_EVIDENCE +
      EXPECTED_EVIDENCE_LIMITATION（accepted limitation；不计 infra failure，仍保留 BLOCKED）。
    - policy 对该 layer 声明 REQUIRED -> EVIDENCE_CAPTURE + 无分类（unexpected evidence
      gap；进入 infra failure numerator，并保持 Layer-1 correctness/hard-gate concern）。
    - 未声明 policy（V1 / A2 最小 contract）-> 保持 R2 legacy：
      NOT_SUPPORTED_BY_CURRENT_EVIDENCE，无 expected-limitation 分类。
    """
    layer = evidence.evaluation_layer
    policy = getattr(expected, "identity_evidence_by_layer", None)
    requirement: IdentityEvidenceRequirement | None = None
    if policy is not None:
        requirement = policy.layer_1 if layer is EvaluationLayer.LAYER_1_DETERMINISTIC else policy.layer_2
    if requirement is IdentityEvidenceRequirement.EXPECTED_LIMITATION:
        return (
            BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE,
            EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION,
        )
    if requirement is IdentityEvidenceRequirement.REQUIRED:
        return BlockReason.EVIDENCE_CAPTURE, None
    return BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE, None


def evaluate_retrieval(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """Expected Memory Recall@K / Hit@K / Irrelevant Memory Rejection Rate。

    identity 级证据（selected memory ids）可用时计算 exact；仅 counts 时做 count 级
    检查，identity 级 assertion 为 BLOCKED（not supported by current evidence）。
    """
    assertions: list[MemoryAssertion] = []
    for step in evidence.scenario.steps:
        expected = step.expected_retrieval
        if expected is None:
            continue
        base = f"{evidence.scenario.scenario_id}.{step.step_id}.retrieval"
        selection = evidence.selection_by_step.get(step.step_id)
        if selection is None:
            assertions.append(
                _blocked(
                    base,
                    AssertionDimension.RETRIEVAL,
                    "retrieval selection evidence is unavailable",
                    BlockReason.EVIDENCE_CAPTURE,
                    expected=expected.expected_selected,
                    required=step.required,
                )
            )
            continue

        expected_ids, unbounded = _selected_alias_ids(evidence, expected.expected_selected)
        if unbounded:
            assertions.append(
                _blocked(
                    f"{base}.alias_binding",
                    AssertionDimension.RETRIEVAL,
                    f"expected aliases unbounded to runtime memory_id: {', '.join(unbounded)}",
                    BlockReason.EVIDENCE_CAPTURE,
                    expected=unbounded,
                    required=step.required,
                )
            )
        else:
            expected_set = set(expected_ids)
            if selection.evidence_source is RetrievalEvidenceSource.SELECTION_IDS:
                assert selection.selected_memory_ids is not None
                selected = list(selection.selected_memory_ids)
                selected_set = set(selected)
                hits = expected_set & selected_set
                recall = len(hits) / len(expected_set)
                assertions.append(
                    _pass(
                        f"{base}.recall_at_k",
                        AssertionDimension.RETRIEVAL,
                        f"Expected Memory Recall@K={recall:.3f}",
                        expected={"aliases": expected.expected_selected, "k": expected.k},
                        actual={"recall": recall, "hits": sorted(hits), "selected": selected},
                        required=step.required,
                    )
                    if len(hits) == len(expected_set)
                    else _fail(
                        f"{base}.recall_at_k",
                        AssertionDimension.RETRIEVAL,
                        f"Expected Memory Recall@K={recall:.3f}",
                        FailureTaxonomy.RETRIEVAL_MISS,
                        expected={"aliases": expected.expected_selected, "k": expected.k},
                        actual={"recall": recall, "hits": sorted(hits), "selected": selected},
                        required=step.required,
                    )
                )
                hit = len(hits) > 0
                assertions.append(
                    _pass(
                        f"{base}.hit_at_k",
                        AssertionDimension.RETRIEVAL,
                        "expected memory hit in selected set",
                        actual={"hit": hit},
                        required=step.required,
                    )
                    if hit
                    else _fail(
                        f"{base}.hit_at_k",
                        AssertionDimension.RETRIEVAL,
                        "expected memory not hit in selected set",
                        FailureTaxonomy.RETRIEVAL_MISS,
                        actual={"hit": False},
                        required=step.required,
                    )
                )
                if expected.expected_excluded:
                    excluded_ids, excluded_unbound = _selected_alias_ids(evidence, expected.expected_excluded)
                    if excluded_unbound:
                        assertions.append(
                            _blocked(
                                f"{base}.rejection",
                                AssertionDimension.RETRIEVAL,
                                "excluded aliases unbounded to runtime memory_id",
                                BlockReason.EVIDENCE_CAPTURE,
                                expected=excluded_unbound,
                                required=step.required,
                            )
                        )
                    else:
                        leaked = selected_set & set(excluded_ids)
                        rejection = 1.0 - (len(leaked) / len(excluded_ids))
                        if leaked:
                            assertions.append(
                                _fail(
                                    f"{base}.rejection",
                                    AssertionDimension.RETRIEVAL,
                                    "excluded memory was selected",
                                    FailureTaxonomy.IRRELEVANT_RETRIEVAL,
                                    expected=[],
                                    actual=sorted(leaked),
                                    required=step.required,
                                )
                            )
                        else:
                            assertions.append(
                                _pass(
                                    f"{base}.rejection",
                                    AssertionDimension.RETRIEVAL,
                                    f"Irrelevant Memory Rejection Rate={rejection:.3f}",
                                    actual={"rejection": rejection},
                                    required=step.required,
                                )
                            )
            else:
                if selection.selected_count == 0 and expected.expected_selected:
                    assertions.append(
                        _fail(
                            f"{base}.selected_count",
                            AssertionDimension.RETRIEVAL,
                            "expected memory selection but selected_count is 0",
                            FailureTaxonomy.RETRIEVAL_MISS,
                            expected=len(expected.expected_selected),
                            actual=0,
                            required=step.required,
                        )
                    )
                else:
                    assertions.append(
                        _pass(
                            f"{base}.selected_count",
                            AssertionDimension.RETRIEVAL,
                            f"selected_count={selection.selected_count} is consistent",
                            expected=len(expected.expected_selected),
                            actual=selection.selected_count,
                            required=step.required,
                        )
                    )
                identity_blocked_by, identity_gap_classification = _identity_evidence_block(evidence, expected)
                assertions.append(
                    _blocked(
                        f"{base}.recall_at_k",
                        AssertionDimension.RETRIEVAL,
                        "identity-level selection evidence is not supported by current runtime journal",
                        identity_blocked_by,
                        expected=expected.expected_selected,
                        required=step.required,
                        evidence_gap_classification=identity_gap_classification,
                    )
                )
                if expected.expected_excluded:
                    assertions.append(
                        _blocked(
                            f"{base}.rejection",
                            AssertionDimension.RETRIEVAL,
                            "identity-level rejection evidence is not supported by current runtime journal",
                            identity_blocked_by,
                            expected=expected.expected_excluded,
                            required=step.required,
                            evidence_gap_classification=identity_gap_classification,
                        )
                    )
    return assertions


def _dcg(ordered: list[float]) -> float:
    return sum((2.0**relevance - 1.0) / math.log2(index + 2) for index, relevance in enumerate(ordered))


def evaluate_ranking(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """只有多有序/分级 relevant Memory 才计算 MRR/NDCG；否则 Hit@K/Recall@K 足够。"""
    assertions: list[MemoryAssertion] = []
    for step in evidence.scenario.steps:
        expected = step.expected_retrieval
        if expected is None or not expected.expected_ranked_order:
            continue
        base = f"{evidence.scenario.scenario_id}.{step.step_id}.ranking"
        selection = evidence.selection_by_step.get(step.step_id)
        if selection is None or selection.evidence_source is not RetrievalEvidenceSource.SELECTION_IDS:
            assertions.append(
                _blocked(
                    base,
                    AssertionDimension.RANKING,
                    "ranked selection evidence is unavailable",
                    BlockReason.EVIDENCE_CAPTURE,
                    required=step.required,
                )
            )
            continue
        assert selection.selected_memory_ids is not None
        bound, unbounded = _selected_alias_ids(evidence, expected.expected_ranked_order)
        if unbounded:
            assertions.append(
                _blocked(
                    base,
                    AssertionDimension.RANKING,
                    "ranked aliases unbounded to runtime memory_id",
                    BlockReason.EVIDENCE_CAPTURE,
                    expected=unbounded,
                    required=step.required,
                )
            )
            continue
        if len(bound) < 2:
            assertions.append(
                _na(
                    base,
                    AssertionDimension.RANKING,
                    "single relevant memory; NDCG not computed",
                )
            )
            continue
        selected = list(selection.selected_memory_ids)
        rank_of: dict[str, int] = {}
        for index, memory_id in enumerate(selected):
            rank_of.setdefault(memory_id, index + 1)
        first_rank = min((rank_of[memory_id] for memory_id in bound if memory_id in rank_of), default=None)
        if first_rank is None:
            mrr = 0.0
        else:
            mrr = 1.0 / first_rank
        expected_order = list(bound)
        actual_order = [memory_id for memory_id in selected if memory_id in set(bound)]
        expected_relevance = [1.0] * len(expected_order)
        actual_relevance = [
            1.0 if memory_id in set(expected_order) else 0.0 for memory_id in selected if memory_id in rank_of
        ]
        ideal = _dcg(expected_relevance)
        ndcg = _dcg(actual_relevance[: len(expected_order)]) / ideal if ideal > 0 else 0.0
        exact = actual_order == expected_order
        assertions.append(
            _pass(
                f"{base}.mrr",
                AssertionDimension.RANKING,
                f"MRR={mrr:.3f}",
                actual={"mrr": mrr, "first_relevant_rank": first_rank},
                required=step.required,
            )
            if exact
            else _fail(
                f"{base}.mrr",
                AssertionDimension.RANKING,
                f"MRR={mrr:.3f}",
                FailureTaxonomy.RETRIEVAL_MISS,
                expected=[memory_id for memory_id in expected_order],
                actual=actual_order,
                required=step.required,
            )
        )
        if exact:
            assertions.append(
                _pass(
                    f"{base}.ndcg_at_k",
                    AssertionDimension.RANKING,
                    f"NDCG@K={ndcg:.3f}",
                    actual={"ndcg": ndcg},
                    required=step.required,
                )
            )
        else:
            assertions.append(
                _fail(
                    f"{base}.ndcg_at_k",
                    AssertionDimension.RANKING,
                    f"NDCG@K={ndcg:.3f}",
                    FailureTaxonomy.RETRIEVAL_MISS,
                    expected=[memory_id for memory_id in expected_order],
                    actual=actual_order,
                    required=step.required,
                )
            )
    return assertions


def evaluate_injection(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """Injection Success Rate：只有 planning_injected + context_record_count 满足才 PASS。

    ``direct_entry_supplied`` 只是 supplied observation，绝不是 injected PASS；若当前
    证据只能证明 supplied，injection assertion 是 BLOCKED / NOT_SUPPORTED_BY_CURRENT_EVIDENCE。
    """
    assertions: list[MemoryAssertion] = []
    for step in evidence.scenario.steps:
        expected = step.expected_injection
        if expected is None:
            continue
        base = f"{evidence.scenario.scenario_id}.{step.step_id}.injection"
        selection = evidence.selection_by_step.get(step.step_id)
        if selection is None:
            assertions.append(
                _blocked(
                    base,
                    AssertionDimension.INJECTION,
                    "injection evidence is unavailable",
                    BlockReason.EVIDENCE_CAPTURE,
                    expected=expected.planner_context_record_count,
                    required=step.required,
                )
            )
            continue
        if expected.planner_context_record_count is not None:
            if not selection.planning_injected:
                assertions.append(
                    _fail(
                        base,
                        AssertionDimension.INJECTION,
                        "planner injection expected but planning_injected is false",
                        FailureTaxonomy.CONTEXT_INJECTION_MISS,
                        expected={
                            "planning_injected": True,
                            "context_record_count": expected.planner_context_record_count,
                        },
                        actual={"planning_injected": False, "context_record_count": selection.context_record_count},
                        required=step.required,
                    )
                )
                continue
            if selection.context_record_count != expected.planner_context_record_count:
                assertions.append(
                    _fail(
                        base,
                        AssertionDimension.INJECTION,
                        "context_record_count does not meet dataset expectation",
                        FailureTaxonomy.CONTEXT_INJECTION_MISS,
                        expected=expected.planner_context_record_count,
                        actual=selection.context_record_count,
                        required=step.required,
                    )
                )
                continue
            assertions.append(
                _pass(
                    base,
                    AssertionDimension.INJECTION,
                    "planner injection accepted with expected context_record_count",
                    expected=expected.planner_context_record_count,
                    actual={
                        "planning_injected": selection.planning_injected,
                        "context_record_count": selection.context_record_count,
                    },
                    required=step.required,
                )
            )
            continue
        if selection.direct_entry_supplied:
            assertions.append(
                _blocked(
                    base,
                    AssertionDimension.INJECTION,
                    "only direct_entry_supplied evidence; Builder acceptance cannot be proven",
                    BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE,
                    expected="direct_entry_supplied",
                    required=step.required,
                )
            )
            continue
        assertions.append(
            _fail(
                base,
                AssertionDimension.INJECTION,
                "no injection acceptance evidence",
                FailureTaxonomy.CONTEXT_INJECTION_MISS,
                expected=[],
                actual={"direct_entry_supplied": selection.direct_entry_supplied},
                required=step.required,
            )
        )
    return assertions


def _leakage_evidence(
    evidence: ScenarioEvaluationEvidence,
) -> list[tuple[StatefulMemoryStep, RetrievalSelectionEvidence, list[CanonicalMemoryRecord]]]:
    """对每个 retrieval step 构建 leakage 检查证据；无暴露时返回空。"""
    if evidence.final_snapshot is None:
        return []
    records = evidence.final_snapshot.records
    checks: list[tuple[StatefulMemoryStep, RetrievalSelectionEvidence, list[CanonicalMemoryRecord]]] = []
    for step in evidence.scenario.steps:
        selection = evidence.selection_by_step.get(step.step_id)
        if selection is None:
            continue
        if selection.selected_count == 0 and selection.context_record_count == 0:
            continue
        if selection.evidence_source is not RetrievalEvidenceSource.SELECTION_IDS:
            continue
        assert selection.selected_memory_ids is not None
        selected = [record for record in records if record.memory_id in set(selection.selected_memory_ids)]
        checks.append((step, selection, selected))
    return checks


def evaluate_leakage(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """Forgotten / Superseded / Scope / Irrelevant leakage（基于 selected/injected evidence）。"""
    assertions: list[MemoryAssertion] = []
    base = f"{evidence.scenario.scenario_id}.leakage"
    checks = _leakage_evidence(evidence)
    forgotten_ids = {
        record.memory_id
        for record in (evidence.final_snapshot.records if evidence.final_snapshot else ())
        if record.status == "FORGOTTEN"
    }
    superseded_ids = {
        record.memory_id
        for record in (evidence.final_snapshot.records if evidence.final_snapshot else ())
        if record.status == "SUPERSEDED"
    }

    if not checks:
        if any(step.expected_retrieval is not None for step in evidence.scenario.steps):
            has_selection = any(
                evidence.selection_by_step.get(step.step_id) is not None
                for step in evidence.scenario.steps
                if step.expected_retrieval is not None
            )
            if has_selection:
                assertions.append(
                    _na(
                        f"{base}.forgotten",
                        AssertionDimension.LEAKAGE,
                        "no retrieval exposure to evaluate",
                    )
                )
            else:
                assertions.append(
                    _blocked(
                        f"{base}.forgotten",
                        AssertionDimension.LEAKAGE,
                        "retrieval selection evidence is unavailable",
                        BlockReason.EVIDENCE_CAPTURE,
                    )
                )
        return assertions

    step, selection, selected_records = checks[0]
    assert selection.selected_memory_ids is not None
    selected_set = set(selection.selected_memory_ids)

    if forgotten_ids:
        leaked = selected_set & forgotten_ids
        if leaked:
            assertions.append(
                _fail(
                    f"{base}.forgotten",
                    AssertionDimension.LEAKAGE,
                    "forgotten memory selected/injected",
                    FailureTaxonomy.FORGOTTEN_LEAKAGE,
                    expected=[],
                    actual=sorted(leaked),
                    required=step.required,
                )
            )
        else:
            assertions.append(
                _pass(
                    f"{base}.forgotten",
                    AssertionDimension.LEAKAGE,
                    "no forgotten memory selected/injected",
                    actual=[],
                    required=step.required,
                )
            )
    else:
        assertions.append(
            _na(
                f"{base}.forgotten",
                AssertionDimension.LEAKAGE,
                "no forgotten memories exist in final state",
            )
        )

    if superseded_ids:
        leaked = selected_set & superseded_ids
        if leaked:
            assertions.append(
                _fail(
                    f"{base}.superseded",
                    AssertionDimension.LEAKAGE,
                    "superseded memory selected/injected",
                    FailureTaxonomy.SUPERSEDED_LEAKAGE,
                    expected=[],
                    actual=sorted(leaked),
                    required=step.required,
                )
            )
        else:
            assertions.append(
                _pass(
                    f"{base}.superseded",
                    AssertionDimension.LEAKAGE,
                    "no superseded memory selected/injected",
                    actual=[],
                    required=step.required,
                )
            )
    else:
        assertions.append(
            _na(
                f"{base}.superseded",
                AssertionDimension.LEAKAGE,
                "no superseded memories exist in final state",
            )
        )

    foreign = [
        record
        for record in selected_records
        if record.agent_id != step.agent_id or record.memory_scope != step.memory_scope
    ]
    if foreign:
        assertions.append(
            _fail(
                f"{base}.scope",
                AssertionDimension.LEAKAGE,
                "memory outside step scope selected/injected",
                FailureTaxonomy.SCOPE_LEAKAGE,
                expected=[],
                actual=[
                    {"memory_id": r.memory_id, "agent_id": r.agent_id, "memory_scope": r.memory_scope} for r in foreign
                ],
                required=step.required,
            )
        )
    else:
        assertions.append(
            _pass(
                f"{base}.scope",
                AssertionDimension.LEAKAGE,
                "no memory outside step scope selected/injected",
                actual=[],
                required=step.required,
            )
        )

    expected_ids = _bound_expected_ids(evidence, step)
    irrelevant = [record.memory_id for record in selected_records if record.memory_id not in expected_ids]
    if irrelevant:
        assertions.append(
            _fail(
                f"{base}.irrelevant",
                AssertionDimension.LEAKAGE,
                "irrelevant memory selected/injected",
                FailureTaxonomy.IRRELEVANT_RETRIEVAL,
                expected=[],
                actual=sorted(irrelevant),
                required=step.required,
            )
        )
    else:
        assertions.append(
            _pass(
                f"{base}.irrelevant",
                AssertionDimension.LEAKAGE,
                "no irrelevant memory selected/injected",
                actual=[],
                required=step.required,
            )
        )
    return assertions


def _bound_expected_ids(evidence: ScenarioEvaluationEvidence, step: StatefulMemoryStep) -> set[str]:
    expected = step.expected_retrieval
    if expected is None:
        return set()
    bound, _ = _selected_alias_ids(evidence, expected.expected_selected)
    return set(bound)


def evaluate_generation(
    evidence: ScenarioEvaluationEvidence,
) -> list[MemoryAssertion]:
    """独立 optional generation 维度；不覆盖 runtime correctness。"""
    expectation = evidence.scenario.generation_expectation
    assertion_id = f"{evidence.scenario.scenario_id}.generation"
    if expectation is None:
        return [_na(assertion_id, AssertionDimension.GENERATION, "no generation expectation")]
    if expectation.kind.value == "EXACT":
        answer = evidence.final_answer_text
        if answer is None:
            return [
                _blocked(
                    assertion_id,
                    AssertionDimension.GENERATION,
                    "final answer evidence is unavailable",
                    BlockReason.EVIDENCE_CAPTURE,
                    required=False,
                )
            ]
        if answer.strip() == (expectation.expected_value or "").strip():
            return [
                _pass(
                    assertion_id,
                    AssertionDimension.GENERATION,
                    "exact final answer matches",
                    expected=expectation.expected_value,
                    actual=answer,
                    required=False,
                )
            ]
        return [
            _fail(
                assertion_id,
                AssertionDimension.GENERATION,
                "final answer does not match expected value",
                FailureTaxonomy.GENERATION_USE_FAILURE,
                expected=expectation.expected_value,
                actual=answer,
                required=False,
            )
        ]
    return [
        _na(
            assertion_id,
            AssertionDimension.GENERATION,
            "open generation requires human adjudication",
        )
    ]


def evaluate_scenario(evidence: ScenarioEvaluationEvidence) -> ScenarioEvaluation:
    """执行一个 scenario 的全部 evaluators 并汇总 metric/denominator/outcome。"""
    scenario = evidence.scenario
    assertions: list[MemoryAssertion] = []
    formation_assertions, decisions = evaluate_formation(evidence)
    assertions.extend(formation_assertions)
    assertions.extend(evaluate_predicate(evidence))
    assertions.extend(evaluate_lifecycle(evidence))
    assertions.extend(evaluate_final_state(evidence))
    assertions.extend(evaluate_invariants(evidence))
    assertions.extend(evaluate_retrieval(evidence))
    assertions.extend(evaluate_ranking(evidence))
    assertions.extend(evaluate_injection(evidence))
    assertions.extend(evaluate_leakage(evidence))
    assertions.extend(evaluate_generation(evidence))

    metrics: dict[str, MetricAggregate] = {}
    precision_remember, recall_remember = _decision_metrics(
        FORMATION_PRECISION_REMEMBER, FormationDecision.REMEMBER, decisions
    )
    metrics[FORMATION_PRECISION_REMEMBER] = precision_remember
    metrics[FORMATION_RECALL_REMEMBER] = recall_remember
    precision_ignore, recall_ignore = _decision_metrics(
        FORMATION_PRECISION_IGNORE, FormationDecision.IGNORE, decisions
    )
    metrics[FORMATION_PRECISION_IGNORE] = precision_ignore
    metrics[FORMATION_RECALL_IGNORE] = recall_ignore
    metrics[PREDICATE_CLASSIFICATION_ACCURACY] = build_metric_aggregate(
        PREDICATE_CLASSIFICATION_ACCURACY,
        [item for item in assertions if item.dimension is AssertionDimension.PREDICATE],
    )
    metrics[PREDICATE_ID_ACCURACY] = build_metric_aggregate(
        PREDICATE_ID_ACCURACY,
        [
            item
            for item in assertions
            if item.dimension is AssertionDimension.PREDICATE and item.assertion_id.endswith("predicate")
        ],
    )
    metrics[LIFECYCLE_OPERATION_ACCURACY] = build_metric_aggregate(
        LIFECYCLE_OPERATION_ACCURACY,
        [item for item in assertions if item.dimension is AssertionDimension.LIFECYCLE],
    )
    metrics[FINAL_STATE_ACCURACY] = build_metric_aggregate(
        FINAL_STATE_ACCURACY,
        [item for item in assertions if item.dimension is AssertionDimension.FINAL_STATE],
    )
    metrics[INVARIANT_PASS_RATE] = build_metric_aggregate(
        INVARIANT_PASS_RATE,
        [item for item in assertions if item.dimension is AssertionDimension.INVARIANT],
    )
    metrics[RECALL_AT_K_METRIC] = build_metric_aggregate(
        RECALL_AT_K_METRIC,
        [item for item in assertions if item.assertion_id.endswith("recall_at_k")],
    )
    metrics[HIT_AT_K_METRIC] = build_metric_aggregate(
        HIT_AT_K_METRIC,
        [item for item in assertions if item.assertion_id.endswith("hit_at_k")],
    )
    metrics[REJECTION_RATE_METRIC] = build_metric_aggregate(
        REJECTION_RATE_METRIC,
        [item for item in assertions if item.assertion_id.endswith("rejection")],
    )
    metrics[INJECTION_SUCCESS_RATE_METRIC] = build_metric_aggregate(
        INJECTION_SUCCESS_RATE_METRIC,
        [item for item in assertions if item.dimension is AssertionDimension.INJECTION],
    )
    for name, dimension in (
        (FORGOTTEN_LEAKAGE_RATE_METRIC, "forgotten"),
        (SUPERSEDED_LEAKAGE_RATE_METRIC, "superseded"),
        (SCOPE_LEAKAGE_RATE_METRIC, "scope"),
        (IRRELEVANT_INJECTION_RATE_METRIC, "irrelevant"),
    ):
        metrics[name] = build_failure_rate_aggregate(
            name,
            [
                item
                for item in assertions
                if item.dimension is AssertionDimension.LEAKAGE and item.assertion_id.endswith(f".{dimension}")
            ],
        )
    metrics[GENERATION_USE_METRIC] = build_metric_aggregate(
        GENERATION_USE_METRIC,
        [item for item in assertions if item.dimension is AssertionDimension.GENERATION],
    )
    metrics[EXPECTED_EVIDENCE_LIMITATION_BLOCKED_COUNT_METRIC] = build_expected_evidence_limitation_blocked_aggregate(
        assertions
    )

    runtime_block_rate = build_runtime_block_rate(assertions)
    metrics[RUNTIME_BLOCK_RATE_METRIC] = runtime_block_rate
    infra_failure_rate = build_evaluation_infra_failure_rate(assertions)
    metrics[EVALUATION_INFRA_FAILURE_RATE_METRIC] = infra_failure_rate

    required = required_assertions(assertions)
    applicable = [item for item in required if item.status is not AssertionStatus.NOT_APPLICABLE]
    required_pass = sum(item.status is AssertionStatus.PASS for item in applicable)
    required_fail = sum(item.status is AssertionStatus.FAIL for item in applicable)
    required_blocked = sum(item.status is AssertionStatus.BLOCKED for item in applicable)
    optional_fail = sum(item.status is AssertionStatus.FAIL for item in assertions if not item.required)
    child_taxonomies = sorted(
        {
            str(item.failure_taxonomy.value)
            for item in assertions
            if item.status is AssertionStatus.FAIL
            and not item.assertion_id.endswith(".outcome")
            and item.failure_taxonomy is not None
        }
    )
    outcome_assertion = scenario_success_assertion(
        f"{scenario.scenario_id}.outcome",
        required_pass,
        required_fail,
        required_blocked,
        optional_fail,
        child_failure_taxonomies=child_taxonomies,
    )
    assertions.append(outcome_assertion)
    metrics[SCENARIO_SUCCESS_RATE_METRIC] = build_metric_aggregate(
        SCENARIO_SUCCESS_RATE_METRIC,
        [outcome_assertion],
    )

    taxonomies = tuple(
        sorted(
            {
                str(item.failure_taxonomy.value)
                for item in assertions
                if item.status is AssertionStatus.FAIL and item.failure_taxonomy is not None
            }
        )
    )
    deterministic_gate_eligible = (
        scenario.required
        and scenario.deterministic_denominator
        and scenario.truthfulness_origin is TruthfulnessOrigin.DETERMINISTIC_GROUND_TRUTH
    )
    return ScenarioEvaluation(
        scenario_id=scenario.scenario_id,
        assertions=tuple(assertions),
        metrics=metrics,
        runtime_block_rate=runtime_block_rate,
        evaluation_infra_failure_rate=infra_failure_rate,
        scenario_outcome=outcome_assertion.status,
        scenario_outcome_assertion=outcome_assertion,
        failure_taxonomies=taxonomies,
        deterministic_gate_eligible=deterministic_gate_eligible,
        truthfulness_origin=scenario.truthfulness_origin.value,
        required=scenario.required,
    )


__all__ = [
    "EVALUATION_INFRA_FAILURE_RATE_METRIC",
    "EXPECTED_EVIDENCE_LIMITATION_BLOCKED_COUNT_METRIC",
    "FINAL_STATE_ACCURACY",
    "FORMATION_PRECISION_IGNORE",
    "FORMATION_PRECISION_REMEMBER",
    "FORMATION_RECALL_IGNORE",
    "FORMATION_RECALL_REMEMBER",
    "FORGOTTEN_LEAKAGE_RATE_METRIC",
    "GENERATION_USE_METRIC",
    "HIT_AT_K_METRIC",
    "INJECTION_SUCCESS_RATE_METRIC",
    "INVARIANT_PASS_RATE",
    "IRRELEVANT_INJECTION_RATE_METRIC",
    "LIFECYCLE_OPERATION_ACCURACY",
    "PREDICATE_CLASSIFICATION_ACCURACY",
    "PREDICATE_ID_ACCURACY",
    "REJECTION_RATE_METRIC",
    "RECALL_AT_K_METRIC",
    "RUNTIME_BLOCK_RATE_METRIC",
    "RetrievalEvidenceSource",
    "RetrievalSelectionEvidence",
    "SCENARIO_SUCCESS_RATE_METRIC",
    "SCOPE_LEAKAGE_RATE_METRIC",
    "SUPERSEDED_LEAKAGE_RATE_METRIC",
    "ScenarioEvaluation",
    "ScenarioEvaluationEvidence",
    "build_alias_binding",
    "evaluate_scenario",
]
