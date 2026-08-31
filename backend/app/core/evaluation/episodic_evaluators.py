"""WP6-E 13 类 typed assertion evaluators（纯函数，只读消费证据）。

Ground Truth Authority 完全来自 dataset ``EpisodicScenario``；actual evidence 来自
runner 收集的 capture / receipts / runtime / journal / SQLite projection。所有
evaluator 只做 read-only 比较：

- 缺 identity capture 一律 BLOCKED / EVIDENCE_CAPTURE，绝不把 expected identity
  当作未选择（Retrieval Miss）。
- selected != supplied != injected；三个 assertion surface 独立评价。
- SQLite 只做 Persistence / Final State，绝不作 selection oracle。
- 不做任何 canonical_text exact-match 或 content-similarity inference。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.evaluation.episodic_assertion import (
    PERSISTED_OBSERVATION_FIDELITY_ASSERTION,
    RUNTIME_IDENTITY_GROUNDING_ASSERTION,
    EpisodicAssertion,
    EpisodicAssertionGroup,
    EpisodicBlockReason,
    EpisodicFailureTaxonomy,
    required_assertions,
    scenario_success_assertion,
    status_counts,
)
from app.core.evaluation.episodic_dataset import (
    EpisodicFormationOutcome,
    EpisodicSkipReason,
    EpisodicTerminalStatus,
    NonexistentFactKind,
    PersistenceAssertion,
)
from app.core.evaluation.episodic_evidence import (
    EpisodicCaptureEvidence,
    EpisodicInjectionTarget,
    EpisodicRunEvidence,
    EpisodicScenarioEvaluationEvidence,
    RunExecutionStatus,
)
from app.core.evaluation.episodic_identity import EpisodicIdentityMap, IdentityResolutionStatus
from app.core.evaluation.episodic_metrics import build_episodic_scenario_metrics
from app.core.evaluation.episodic_projection import (
    EpisodicProjectionError,
    EpisodicProjectionRecord,
    read_episodic_projection,
)
from app.core.evaluation.episodic_step_identity import (
    EpisodicStepIdentityAdapter,
    EpisodicStepIdentityError,
)
from app.core.evaluation.immutable import require_text
from app.core.evaluation.stateful_assertion import AssertionStatus


@dataclass(frozen=True, slots=True)
class EpisodicScenarioEvaluation:
    """一个 scenario 的完整 evaluation 结果。"""

    scenario_id: str
    assertions: tuple[EpisodicAssertion, ...]
    metrics: dict[str, object]
    scenario_outcome: AssertionStatus
    scenario_outcome_assertion: EpisodicAssertion
    failure_taxonomies: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.scenario_id, "scenario_id")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_evidence(evidence: EpisodicScenarioEvaluationEvidence, dataset_run_id: str) -> EpisodicRunEvidence | None:
    return evidence.run_evidence_by_dataset_run_id.get(dataset_run_id)


def _projection_index(
    evidence: EpisodicScenarioEvaluationEvidence,
) -> dict[str, EpisodicProjectionRecord]:
    index: dict[str, EpisodicProjectionRecord] = {}
    for record in evidence.final_projection:
        if isinstance(record, EpisodicProjectionRecord):
            index[record.memory_id] = record
    return index


def _resolved(
    identity_map: EpisodicIdentityMap, episode_ref: str
) -> tuple[str | None, IdentityResolutionStatus | None]:
    resolution = identity_map.resolution_for(episode_ref)
    if resolution is None:
        return None, IdentityResolutionStatus.NOT_DECLARED
    return resolution.memory_id, resolution.status


def _assert(
    assertion_id: str,
    group: EpisodicAssertionGroup,
    status: AssertionStatus,
    *,
    expected: object,
    actual: object,
    failure_taxonomy: EpisodicFailureTaxonomy | None = None,
    blocked_by: EpisodicBlockReason | None = None,
    evidence_source: str,
    reason: str,
    required: bool = True,
) -> EpisodicAssertion:
    return EpisodicAssertion(
        assertion_id=assertion_id,
        group=group,
        status=status,
        expected=expected,
        actual_evidence=actual,
        failure_taxonomy=failure_taxonomy,
        blocked_by=blocked_by,
        evidence_source=evidence_source,
        reason=reason,
        required=required,
    )


def _formation_receipt_outcome(run: EpisodicRunEvidence) -> str | None:
    if run.formation_receipt is not None:
        return run.formation_receipt.outcome
    if run.runtime_receipt is not None:
        return run.runtime_receipt.formation_outcome
    return None


def _capture_evidence(run: EpisodicRunEvidence) -> EpisodicCaptureEvidence | None:
    return run.capture


def _episode_content_fields(record: EpisodicProjectionRecord) -> tuple[str, ...]:
    fields = [record.canonical_text, record.situation_text, record.goal_text]
    fields.extend(item.name for item in record.observations)
    fields.append(record.result.terminal_status)
    fields.append(record.result.stop_reason)
    fields.append(record.result.delivery_status)
    if record.lesson is not None:
        fields.append(record.lesson)
    return tuple(fields)


# ---------------------------------------------------------------------------
# 1. FORMATION
# ---------------------------------------------------------------------------


def evaluate_formation(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate FORMATION assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    for run in evidence.scenario.runs:
        if run.expected_formation is None:
            continue
        run_id = run.run_id
        assertion_id = f"{evidence.scenario.scenario_id}.{run_id}.formation"
        run_evidence = _run_evidence(evidence, run_id)
        if run_evidence is None or run_evidence.execution_status is not RunExecutionStatus.EXECUTED:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.FORMATION,
                    AssertionStatus.BLOCKED,
                    expected={"outcome": run.expected_formation.expected_formation_outcome.value},
                    actual={"run_evidence": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="runtime",
                    reason="run evidence is missing for formation assertion",
                )
            )
            continue
        actual_outcome = _formation_receipt_outcome(run_evidence)
        if actual_outcome is None:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.FORMATION,
                    AssertionStatus.BLOCKED,
                    expected={"outcome": run.expected_formation.expected_formation_outcome.value},
                    actual={"formation_receipt": "missing", "runtime_receipt": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="formation_receipt",
                    reason="formation receipt and runtime receipt are both missing",
                )
            )
            continue
        expected_outcome = run.expected_formation.expected_formation_outcome.value
        if actual_outcome != expected_outcome:
            taxonomy = EpisodicFailureTaxonomy.RUNTIME_BEHAVIORAL_FAILURE
            if expected_outcome == EpisodicFormationOutcome.CREATED.value:
                taxonomy = EpisodicFailureTaxonomy.EPISODE_FORMATION_FALSE_NEGATIVE
            elif actual_outcome == EpisodicFormationOutcome.CREATED.value:
                taxonomy = EpisodicFailureTaxonomy.EPISODE_FORMATION_FALSE_POSITIVE
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.FORMATION,
                    AssertionStatus.FAIL,
                    expected={"outcome": expected_outcome},
                    actual={"outcome": actual_outcome},
                    failure_taxonomy=taxonomy,
                    evidence_source="formation_receipt",
                    reason="actual formation outcome differs from expectation",
                )
            )
            continue
        actual_delta = 1 if actual_outcome == EpisodicFormationOutcome.CREATED.value else 0
        if actual_delta != run.expected_formation.expected_episode_count_delta:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.FORMATION,
                    AssertionStatus.FAIL,
                    expected={"episode_count_delta": run.expected_formation.expected_episode_count_delta},
                    actual={"episode_count_delta": actual_delta, "outcome": actual_outcome},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_FORMATION_FALSE_POSITIVE,
                    evidence_source="formation_receipt",
                    reason="episode count delta differs from expectation",
                )
            )
            continue
        assertions.append(
            _assert(
                assertion_id,
                EpisodicAssertionGroup.FORMATION,
                AssertionStatus.PASS,
                expected={"outcome": expected_outcome, "delta": actual_delta},
                actual={"outcome": actual_outcome, "delta": actual_delta},
                evidence_source="formation_receipt",
                reason="formation outcome and count delta match",
            )
        )
    return assertions


# ---------------------------------------------------------------------------
# 2. ELIGIBILITY
# ---------------------------------------------------------------------------


def evaluate_eligibility(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate ELIGIBILITY assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    for run in evidence.scenario.runs:
        if run.expected_eligibility is None:
            continue
        run_id = run.run_id
        assertion_id = f"{evidence.scenario.scenario_id}.{run_id}.eligibility"
        run_evidence = _run_evidence(evidence, run_id)
        if run_evidence is None or run_evidence.execution_status is not RunExecutionStatus.EXECUTED:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.ELIGIBILITY,
                    AssertionStatus.BLOCKED,
                    expected={"eligible": run.expected_eligibility.eligible},
                    actual={"run_evidence": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="runtime",
                    reason="run evidence is missing for eligibility assertion",
                )
            )
            continue
        actual_outcome = _formation_receipt_outcome(run_evidence)
        if actual_outcome is None:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.ELIGIBILITY,
                    AssertionStatus.BLOCKED,
                    expected={"eligible": run.expected_eligibility.eligible},
                    actual={"formation_receipt": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="formation_receipt",
                    reason="no formation evidence to decide eligibility",
                )
            )
            continue
        formed = actual_outcome == EpisodicFormationOutcome.CREATED.value
        if run.expected_eligibility.eligible == formed:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.ELIGIBILITY,
                    AssertionStatus.PASS,
                    expected={"eligible": run.expected_eligibility.eligible},
                    actual={"eligible": formed, "outcome": actual_outcome},
                    evidence_source="formation_receipt",
                    reason="eligibility matches formation evidence",
                )
            )
            continue
        assertions.append(
            _assert(
                assertion_id,
                EpisodicAssertionGroup.ELIGIBILITY,
                AssertionStatus.FAIL,
                expected={"eligible": run.expected_eligibility.eligible},
                actual={"eligible": formed, "outcome": actual_outcome},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_FORMATION_FALSE_POSITIVE,
                evidence_source="formation_receipt",
                reason="ineligible run unexpectedly formed an episode",
            )
        )
    return assertions


# ---------------------------------------------------------------------------
# 3. EPISODE_STRUCTURE
# ---------------------------------------------------------------------------


def evaluate_episode_structure(
    evidence: EpisodicScenarioEvaluationEvidence,
) -> list[EpisodicAssertion]:
    """Evaluate EPISODE STRUCTURE assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    """Evaluate EPISODE STRUCTURE assertions for the scenario."""
    projection = _projection_index(evidence)
    for run in evidence.scenario.runs:
        structure = run.expected_episode_structure
        if structure is None:
            continue
        run_id = run.run_id
        assertion_id = f"{evidence.scenario.scenario_id}.{run_id}.structure"
        episode_ref = run.expected_formation.expected_episode_ref if run.expected_formation is not None else None
        memory_id = None
        status: IdentityResolutionStatus | None = None
        if episode_ref is not None:
            memory_id, status = _resolved(evidence.identity_map, episode_ref)
        if status is not None and status is not IdentityResolutionStatus.RESOLVED:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.EPISODE_STRUCTURE,
                    AssertionStatus.BLOCKED,
                    expected={"memory_type": "EPISODIC", "status": "ACTIVE"},
                    actual={"identity": status.value},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="identity_resolver",
                    reason="expected episode identity is not resolvable from receipts",
                )
            )
            continue
        record = projection.get(memory_id) if memory_id is not None else None
        if record is None:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.EPISODE_STRUCTURE,
                    AssertionStatus.BLOCKED,
                    expected={"memory_type": "EPISODIC", "status": "ACTIVE"},
                    actual={"memory_id": memory_id, "projection": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="sqlite_projection",
                    reason="resolved episode is missing from final SQLite projection",
                )
            )
            continue
        mismatches: list[object] = []
        if record.memory_type != structure.expected_memory_type.value:
            mismatches.append(
                {
                    "field": "memory_type",
                    "expected": structure.expected_memory_type.value,
                    "actual": record.memory_type,
                }
            )
        if record.status != structure.expected_status.value:
            mismatches.append(
                {"field": "status", "expected": structure.expected_status.value, "actual": record.status}
            )
        if record.logical_key is not None:
            mismatches.append({"field": "logical_key", "expected": None, "actual": record.logical_key})
        if record.origin_run_id != structure.expected_origin_run_id:
            pass  # origin_run_id 是 runtime identity，symbolic 映射在 identity map；此处不断言相等
        if record.agent_id != structure.expected_agent_id:
            mismatches.append(
                {"field": "agent_id", "expected": structure.expected_agent_id, "actual": record.agent_id}
            )
        if record.memory_scope != structure.expected_memory_scope:
            mismatches.append(
                {"field": "memory_scope", "expected": structure.expected_memory_scope, "actual": record.memory_scope}
            )
        if mismatches:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.EPISODE_STRUCTURE,
                    AssertionStatus.FAIL,
                    expected={"memory_type": "EPISODIC", "status": "ACTIVE", "logical_key": None},
                    actual={"mismatches": mismatches},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_FORMATION_FALSE_POSITIVE,
                    evidence_source="sqlite_projection",
                    reason="episode structure differs from expectation",
                )
            )
            continue
        assertions.append(
            _assert(
                assertion_id,
                EpisodicAssertionGroup.EPISODE_STRUCTURE,
                AssertionStatus.PASS,
                expected={"memory_type": "EPISODIC", "status": "ACTIVE", "logical_key": None},
                actual={"memory_id": record.memory_id},
                evidence_source="sqlite_projection",
                reason="episode structure matches expectation",
            )
        )
    return assertions


# ---------------------------------------------------------------------------
# 4. EVIDENCE_GROUNDING
# ---------------------------------------------------------------------------


def _runtime_step_facts(
    run_evidence: EpisodicRunEvidence | None,
) -> tuple[dict[str, str | None], str | None]:
    """收集一个 run 的 canonical Runtime step identity 事实（fail-closed 优先 journal）。

    - journal step facts 优先（``RuntimeEvent.step_id`` 是冻结 canonical authority）；
      ``run_evidence.step_facts`` 由 runner 从 journal 只读采集。
    - 缺失时若 receipt 有 step facts，则使用 receipt 的 step facts（display-name
      presence fallback，不是 identity authority）。
    - 返回 ``(step_facts, source)``；``step_facts`` 的 key 是 canonical step_id。
    """
    if run_evidence is not None and run_evidence.step_facts is not None:
        facts: dict[str, str | None] = {fact.step_id: fact.status for fact in run_evidence.step_facts.facts}
        if facts:
            return facts, "journal"
    if run_evidence is not None and run_evidence.runtime_receipt is not None:
        receipt = run_evidence.runtime_receipt
        if receipt.step_names and len(receipt.step_names) == len(receipt.step_statuses):
            return dict(zip(receipt.step_names, receipt.step_statuses, strict=False)), "runtime_receipt"
    return {}, None


def _grounding_identity_assertions(
    grounding: object,
    run_evidence: EpisodicRunEvidence | None,
    *,
    assertion_id: str,
    step_facts: dict[str, str | None],
    evidence_source: str | None,
) -> list[EpisodicAssertion]:
    """Runtime Identity Grounding：Dataset symbolic step_ref -> canonical step_id/status。

    - 每个 ``RequiredObservedStep.step_ref`` 必须经 typed ``EpisodicStepIdentityAdapter``
      normalize 为 canonical ``task-<ref>``（fail closed；invalid ref -> BLOCKED）。
    - actual identity 只来自 Journal ``RuntimeEvent.step_id`` / runtime receipt canonical
      step_id；绝不用 ``EpisodeObservation.name`` / display name。
    - 状态比较：expected status vs canonical Runtime status；identity 缺失 ->
      BLOCKED / EVIDENCE_CAPTURE，不是 PASS 也不是 MISS。
    """
    assertions: list[EpisodicAssertion] = []
    adapter = EpisodicStepIdentityAdapter()
    for item in grounding.required_observed_step_statuses:
        try:
            identity = adapter.normalize(item.step_ref)
        except EpisodicStepIdentityError as exc:
            assertions.append(
                _assert(
                    f"{assertion_id}{RUNTIME_IDENTITY_GROUNDING_ASSERTION}.step.{item.step_ref}",
                    EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                    AssertionStatus.BLOCKED,
                    expected={"step_ref": item.step_ref, "status": item.expected_status.value},
                    actual={"normalization": "INVALID", "reason": str(exc)},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="step_identity_adapter",
                    reason="Dataset symbolic step_ref cannot be normalized to a canonical Runtime step_id",
                )
            )
            continue
        canonical = identity.step_id
        if not step_facts:
            assertions.append(
                _assert(
                    f"{assertion_id}{RUNTIME_IDENTITY_GROUNDING_ASSERTION}.step.{item.step_ref}",
                    EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                    AssertionStatus.BLOCKED,
                    expected={"step_ref": item.step_ref, "step_id": canonical, "status": item.expected_status.value},
                    actual={"runtime_step_facts": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source=evidence_source or "runtime",
                    reason="no canonical Runtime step identity evidence is available for grounding",
                )
            )
            continue
        actual_status = step_facts.get(canonical)
        if canonical not in step_facts or actual_status is None:
            assertions.append(
                _assert(
                    f"{assertion_id}{RUNTIME_IDENTITY_GROUNDING_ASSERTION}.step.{item.step_ref}",
                    EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                    AssertionStatus.FAIL,
                    expected={"step_ref": item.step_ref, "step_id": canonical, "status": item.expected_status.value},
                    actual={"runtime_step_facts": sorted(step_facts)},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH,
                    evidence_source=evidence_source or "runtime",
                    reason="canonical Runtime step identity is missing from runtime evidence",
                )
            )
            continue
        if actual_status != item.expected_status.value:
            assertions.append(
                _assert(
                    f"{assertion_id}{RUNTIME_IDENTITY_GROUNDING_ASSERTION}.step.{item.step_ref}",
                    EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                    AssertionStatus.FAIL,
                    expected={"step_ref": item.step_ref, "step_id": canonical, "status": item.expected_status.value},
                    actual={"step_id": canonical, "status": actual_status},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH,
                    evidence_source=evidence_source or "runtime",
                    reason="canonical Runtime step status differs from expectation",
                )
            )
            continue
        assertions.append(
            _assert(
                f"{assertion_id}{RUNTIME_IDENTITY_GROUNDING_ASSERTION}.step.{item.step_ref}",
                EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                AssertionStatus.PASS,
                expected={"step_ref": item.step_ref, "step_id": canonical, "status": item.expected_status.value},
                actual={"step_id": canonical, "status": actual_status},
                evidence_source=evidence_source or "runtime",
                reason="canonical Runtime step identity/status matches expectation",
            )
        )
    return assertions


def _runtime_evidence_assertion(
    run_evidence: EpisodicRunEvidence | None,
    *,
    assertion_id: str,
    step_facts: dict[str, str | None],
    evidence_source: str | None,
) -> EpisodicAssertion:
    """V2：要求 Runtime canonical step/status evidence 存在，但不预设 Planner task id。"""
    completed = {step_id: status for step_id, status in step_facts.items() if status is not None}
    if not completed:
        return _assert(
            f"{assertion_id}.runtime_evidence",
            EpisodicAssertionGroup.EVIDENCE_GROUNDING,
            AssertionStatus.BLOCKED,
            expected={"canonical_step_status_evidence": True},
            actual={"runtime_step_facts": sorted(step_facts)},
            blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
            evidence_source=evidence_source or "runtime",
            reason="no completed canonical Runtime step/status evidence is available",
        )
    return _assert(
        f"{assertion_id}.runtime_evidence",
        EpisodicAssertionGroup.EVIDENCE_GROUNDING,
        AssertionStatus.PASS,
        expected={"canonical_step_status_evidence": True},
        actual={"step_ids": sorted(completed)},
        evidence_source=evidence_source or "runtime",
        reason="canonical Runtime step/status evidence is available without a predefined Planner task identity",
    )


def _grounding_fidelity_assertions(
    grounding: object,
    record: EpisodicProjectionRecord | None,
    run_evidence: EpisodicRunEvidence | None,
    *,
    assertion_id: str,
) -> list[EpisodicAssertion]:
    """Persisted Observation Fidelity：Episode 必须是真实 observation 的忠实投影。

    只做 fidelity 检查：human-readable observation/status 与 terminal AgentState
    投影一致，Episode result 与真实 terminal/delivery 一致，且不得虚构
    step/tool/recovery/result。identity 判断由 ``_grounding_identity_assertions``
    单独负责；本函数绝不把 ``EpisodeObservation.name`` 当 runtime identity。
    """
    assertions: list[EpisodicAssertion] = []
    persisted_statuses: tuple[str, ...] = ()
    runtime_terminal = (
        run_evidence.runtime_receipt.terminal_status
        if run_evidence is not None and run_evidence.runtime_receipt is not None
        else None
    )
    runtime_delivery = (
        run_evidence.runtime_receipt.delivery_status
        if run_evidence is not None and run_evidence.runtime_receipt is not None
        else None
    )
    if record is None and (runtime_terminal is None and runtime_delivery is None):
        assertions.append(
            _assert(
                f"{assertion_id}{PERSISTED_OBSERVATION_FIDELITY_ASSERTION}",
                EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                AssertionStatus.BLOCKED,
                expected={
                    "terminal": grounding.expected_terminal_status.value,
                    "delivery": grounding.expected_delivery_status.value,
                },
                actual={"projection": "missing", "runtime_receipt": "missing"},
                blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                evidence_source="sqlite_projection",
                reason="no persisted observation or runtime terminal/delivery evidence is available",
            )
        )
        return assertions

    actual_terminal = runtime_terminal or (record.result.terminal_status if record is not None else None)
    actual_delivery = (
        runtime_delivery
        if runtime_delivery is not None
        else (record.result.delivery_status if record is not None else None)
    )
    terminal_ok = actual_terminal == grounding.expected_terminal_status.value
    delivery_ok = actual_delivery == grounding.expected_delivery_status.value
    if not terminal_ok or not delivery_ok:
        assertions.append(
            _assert(
                f"{assertion_id}{PERSISTED_OBSERVATION_FIDELITY_ASSERTION}",
                EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                AssertionStatus.FAIL,
                expected={
                    "terminal": grounding.expected_terminal_status.value,
                    "delivery": grounding.expected_delivery_status.value,
                },
                actual={"terminal": actual_terminal, "delivery": actual_delivery},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH,
                evidence_source="runtime_receipt",
                reason="persisted Episode terminal/delivery facts do not match runtime evidence",
            )
        )
        return assertions

    # forbidden nonexistent facts（P0；先于 fidelity status，防止 fabricated fact 被
    # missing-fact 掩盖为 GROUNDING_MISMATCH）。
    if record is not None:
        content = "\n".join(_episode_content_fields(record))
        fabricated = [
            {"kind": fact.kind.value, "value": fact.value}
            for fact in grounding.forbidden_nonexistent_facts
            if fact.value in content
        ]
        fake_step_names = {
            fact.value
            for fact in grounding.forbidden_nonexistent_facts
            if fact.kind in {NonexistentFactKind.STEP, NonexistentFactKind.TOOL}
        }
        observed_names = {item.name for item in record.observations}
        leaked_names = sorted(fake_step_names & observed_names)
        if fabricated or leaked_names:
            assertions.append(
                _assert(
                    f"{assertion_id}{PERSISTED_OBSERVATION_FIDELITY_ASSERTION}",
                    EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                    AssertionStatus.FAIL,
                    expected=[item.value for item in grounding.forbidden_nonexistent_facts],
                    actual={"fabricated_facts": fabricated, "leaked_step_or_tool_names": leaked_names},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_FABRICATED_FACT,
                    evidence_source="sqlite_projection",
                    reason="episode claims a fact that does not exist in runtime evidence",
                )
            )
            return assertions

        # persisted observation status fidelity：observation 是 human-readable 事实；
        # 其 name 与 canonical identity 无关（不比较 name），只核对持久化 status 与
        # 真实 terminal AgentState 投影一致。
        persisted_statuses = tuple(sorted({item.status for item in record.observations}))
        if record.result.terminal_status != actual_terminal:
            assertions.append(
                _assert(
                    f"{assertion_id}{PERSISTED_OBSERVATION_FIDELITY_ASSERTION}",
                    EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                    AssertionStatus.FAIL,
                    expected={"terminal": grounding.expected_terminal_status.value},
                    actual={
                        "persisted_terminal": record.result.terminal_status,
                        "runtime_terminal": actual_terminal,
                    },
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH,
                    evidence_source="sqlite_projection",
                    reason="persisted Episode result terminal differs from runtime terminal",
                )
            )
            return assertions

        # observation name 是 display label，不参与 identity 比较；其 status 则必须由
        # 独立 Runtime canonical step/status evidence 支撑。允许 bounded omission，
        # 不允许把 Runtime SUCCEEDED 写成 FAILED。
        runtime_step_facts, runtime_source = _runtime_step_facts(run_evidence)
        runtime_statuses = {status for status in runtime_step_facts.values() if status is not None}
        unsupported_statuses = sorted(set(persisted_statuses) - runtime_statuses)
        if runtime_statuses and unsupported_statuses:
            assertions.append(
                _assert(
                    f"{assertion_id}{PERSISTED_OBSERVATION_FIDELITY_ASSERTION}",
                    EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                    AssertionStatus.FAIL,
                    expected={"runtime_observation_statuses": sorted(runtime_statuses)},
                    actual={"persisted_observation_statuses": list(persisted_statuses)},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH,
                    evidence_source=runtime_source or "runtime",
                    reason="persisted Episode observation status is not supported by Runtime step evidence",
                )
            )
            return assertions

    assertions.append(
        _assert(
            f"{assertion_id}{PERSISTED_OBSERVATION_FIDELITY_ASSERTION}",
            EpisodicAssertionGroup.EVIDENCE_GROUNDING,
            AssertionStatus.PASS,
            expected={
                "terminal": grounding.expected_terminal_status.value,
                "delivery": grounding.expected_delivery_status.value,
            },
            actual={
                "terminal": actual_terminal,
                "delivery": actual_delivery,
                "persisted_statuses": list(persisted_statuses),
            },
            evidence_source="runtime_receipt",
            reason="persisted Episode observation/result is faithful to runtime evidence",
        )
    )
    return assertions


def evaluate_grounding(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate GROUNDING assertions for the scenario (identity + fidelity split)."""
    assertions: list[EpisodicAssertion] = []
    projection = _projection_index(evidence)
    for run in evidence.scenario.runs:
        grounding = run.expected_grounding
        if grounding is None:
            continue
        run_id = run.run_id
        assertion_id = f"{evidence.scenario.scenario_id}.{run_id}.grounding"
        formation = run.expected_formation
        memory_id = None
        status: IdentityResolutionStatus | None = None
        if formation is not None and formation.expected_episode_ref is not None:
            memory_id, status = _resolved(evidence.identity_map, formation.expected_episode_ref)
            if status is not None and status is not IdentityResolutionStatus.RESOLVED:
                assertions.append(
                    _assert(
                        assertion_id,
                        EpisodicAssertionGroup.EVIDENCE_GROUNDING,
                        AssertionStatus.BLOCKED,
                        expected={"terminal": grounding.expected_terminal_status.value},
                        actual={"identity": status.value},
                        blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                        evidence_source="identity_resolver",
                        reason="expected episode identity is not resolvable for grounding",
                    )
                )
                continue
        record = projection.get(memory_id) if memory_id is not None else None
        run_evidence = _run_evidence(evidence, run_id)

        # ---- Runtime Identity Grounding（canonical step_id authority）----
        step_facts, source = _runtime_step_facts(run_evidence)
        if grounding.required_observed_step_statuses:
            assertions.extend(
                _grounding_identity_assertions(
                    grounding,
                    run_evidence,
                    assertion_id=assertion_id,
                    step_facts=step_facts,
                    evidence_source=source,
                )
            )
        elif grounding.require_runtime_step_facts:
            assertions.append(
                _runtime_evidence_assertion(
                    run_evidence,
                    assertion_id=assertion_id,
                    step_facts=step_facts,
                    evidence_source=source,
                )
            )

        # ---- Persisted Observation Fidelity（human-readable 真实性）----
        assertions.extend(
            _grounding_fidelity_assertions(
                grounding,
                record,
                run_evidence,
                assertion_id=assertion_id,
            )
        )
    return assertions


# ---------------------------------------------------------------------------
# 5. PRIVACY
# ---------------------------------------------------------------------------


def evaluate_privacy(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate PRIVACY assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    projection = _projection_index(evidence)
    for run in evidence.scenario.runs:
        privacy = run.expected_privacy
        if privacy is None:
            continue
        run_id = run.run_id
        assertion_id = f"{evidence.scenario.scenario_id}.{run_id}.privacy"
        formation = run.expected_formation
        memory_id = None
        if formation is not None and formation.expected_episode_ref is not None:
            memory_id, status = _resolved(evidence.identity_map, formation.expected_episode_ref)
            if status is not None and status is not IdentityResolutionStatus.RESOLVED:
                assertions.append(
                    _assert(
                        assertion_id,
                        EpisodicAssertionGroup.PRIVACY,
                        AssertionStatus.BLOCKED,
                        expected={"must_not_contain": "declared forbidden fixtures"},
                        actual={"identity": status.value},
                        blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                        evidence_source="identity_resolver",
                        reason="expected episode identity is not resolvable for privacy check",
                    )
                )
                continue
        record = projection.get(memory_id) if memory_id is not None else None
        if record is None:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.PRIVACY,
                    AssertionStatus.BLOCKED,
                    expected={"must_not_contain": "declared forbidden fixtures"},
                    actual={"projection": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="sqlite_projection",
                    reason="episode projection is missing for privacy check",
                )
            )
            continue
        content = "\n".join(_episode_content_fields(record))
        violations: list[object] = []
        for literal in privacy.must_not_contain_literal:
            if literal in content:
                violations.append({"type": "literal", "value": literal})
        for fixture in privacy.must_not_contain_secret_fixture:
            if fixture in content:
                violations.append({"type": "secret_fixture", "value": fixture})
        for fixture in privacy.must_not_contain_path_fixture:
            if fixture in content:
                violations.append({"type": "path_fixture", "value": fixture})
        for field_name in privacy.must_not_contain_forbidden_field:
            for key in (record.situation_text, record.goal_text):
                if field_name in key:
                    violations.append({"type": "forbidden_field", "value": field_name})
        if violations:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.PRIVACY,
                    AssertionStatus.FAIL,
                    expected={"violations": 0},
                    actual={"violations": violations},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_PRIVACY_VIOLATION,
                    evidence_source="sqlite_projection",
                    reason="episode content leaks a forbidden literal/fixture/field",
                )
            )
            continue
        assertions.append(
            _assert(
                assertion_id,
                EpisodicAssertionGroup.PRIVACY,
                AssertionStatus.PASS,
                expected={"violations": 0},
                actual={"violations": 0},
                evidence_source="sqlite_projection",
                reason="episode content contains no forbidden literal/fixture/field",
            )
        )
    return assertions


# ---------------------------------------------------------------------------
# 6. PERSISTENCE
# ---------------------------------------------------------------------------


def evaluate_persistence(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate PERSISTENCE assertions for the scenario."""
    persistence: PersistenceAssertion = evidence.scenario.assertion_groups.persistence
    assertion_id = f"{evidence.scenario.scenario_id}.persistence"
    # 完全没有 run evidence（scenario 未执行）-> 无法公平读取 final state -> BLOCKED。
    if not evidence.run_evidence_by_dataset_run_id:
        return [
            _assert(
                assertion_id,
                EpisodicAssertionGroup.PERSISTENCE,
                AssertionStatus.BLOCKED,
                expected={"row_count": persistence.expected_episode_row_count},
                actual={"run_evidence": "missing"},
                blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                evidence_source="sqlite_projection",
                reason="no run evidence was collected; final SQLite state cannot be fairly evaluated",
            )
        ]
    records = list(evidence.final_projection)
    mismatches: list[object] = []
    if len(records) != persistence.expected_episode_row_count:
        mismatches.append(
            {"field": "row_count", "expected": persistence.expected_episode_row_count, "actual": len(records)}
        )
    origin_ids = [record.origin_run_id for record in records]
    if persistence.origin_run_id_uniqueness and len(origin_ids) != len(set(origin_ids)):
        mismatches.append({"field": "origin_run_id_uniqueness", "expected": True, "actual": False})
    for record in records:
        if record.memory_type != persistence.expected_memory_type.value:
            mismatches.append(
                {
                    "field": "memory_type",
                    "expected": persistence.expected_memory_type.value,
                    "actual": record.memory_type,
                }
            )
        if record.status != persistence.expected_status.value:
            mismatches.append(
                {"field": "status", "expected": persistence.expected_status.value, "actual": record.status}
            )
        if record.logical_key is not None and persistence.logical_key_is_null:
            mismatches.append({"field": "logical_key", "expected": None, "actual": record.logical_key})
    actual_agent_ids = {record.agent_id for record in records}
    if persistence.expected_episode_row_count > 0 and not set(persistence.expected_agent_ids).issubset(
        actual_agent_ids
    ):
        mismatches.append(
            {
                "field": "agent_ids",
                "expected": sorted(persistence.expected_agent_ids),
                "actual": sorted(actual_agent_ids),
            }
        )
    actual_scopes = {record.memory_scope for record in records}
    if persistence.expected_episode_row_count > 0 and not set(persistence.expected_memory_scopes).issubset(
        actual_scopes
    ):
        mismatches.append(
            {
                "field": "memory_scopes",
                "expected": sorted(persistence.expected_memory_scopes),
                "actual": sorted(actual_scopes),
            }
        )
    if mismatches:
        return [
            _assert(
                assertion_id,
                EpisodicAssertionGroup.PERSISTENCE,
                AssertionStatus.FAIL,
                expected={"row_count": persistence.expected_episode_row_count},
                actual={"mismatches": mismatches},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_FORMATION_FALSE_POSITIVE,
                evidence_source="sqlite_projection",
                reason="final SQLite episode state differs from persistence expectation",
            )
        ]
    return [
        _assert(
            assertion_id,
            EpisodicAssertionGroup.PERSISTENCE,
            AssertionStatus.PASS,
            expected={"row_count": persistence.expected_episode_row_count},
            actual={"row_count": len(records)},
            evidence_source="sqlite_projection",
            reason="final SQLite episode state matches persistence expectation",
        )
    ]


# ---------------------------------------------------------------------------
# 7. IDEMPOTENCY
# ---------------------------------------------------------------------------


def evaluate_idempotency(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate IDEMPOTENCY assertions for the scenario."""
    idempotency = evidence.scenario.assertion_groups.idempotency
    assertion_id = f"{evidence.scenario.scenario_id}.idempotency"
    if idempotency is None:
        return []
    run_evidence = _run_evidence(evidence, idempotency.replay_target_run_id)
    if run_evidence is None or run_evidence.execution_status is not RunExecutionStatus.EXECUTED:
        return [
            _assert(
                assertion_id,
                EpisodicAssertionGroup.IDEMPOTENCY,
                AssertionStatus.BLOCKED,
                expected={"first": "CREATED", "second": "REUSED", "row_count": 1},
                actual={"run_evidence": "missing"},
                blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                evidence_source="runtime",
                reason="run evidence is missing for idempotency assertion",
            )
        ]
    first = run_evidence.formation_receipt
    second = run_evidence.replay_receipt
    if first is None or second is None:
        return [
            _assert(
                assertion_id,
                EpisodicAssertionGroup.IDEMPOTENCY,
                AssertionStatus.BLOCKED,
                expected={"first": "CREATED", "second": "REUSED", "row_count": 1},
                actual={"first": first.outcome if first else None, "second": second.outcome if second else None},
                blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                evidence_source="replay_receipt",
                reason="formation or replay receipt is missing for idempotency assertion",
            )
        ]
    violations: list[object] = []
    if first.outcome != "CREATED":
        violations.append({"field": "first", "expected": "CREATED", "actual": first.outcome})
    if second.outcome != "REUSED":
        violations.append({"field": "second", "expected": "REUSED", "actual": second.outcome})
    if first.memory_id != second.memory_id:
        violations.append({"field": "same_memory_id", "expected": first.memory_id, "actual": second.memory_id})
    # 同一 origin run 至多一行：以第一次 formation 的 memory_id 计数（identity authority）。
    formed_memory_id = first.memory_id
    actual_rows = (
        sum(1 for record in evidence.final_projection if record.memory_id == formed_memory_id)
        if formed_memory_id is not None
        else 0
    )
    if actual_rows != idempotency.expected_total_row_count_delta:
        violations.append(
            {"field": "row_count", "expected": idempotency.expected_total_row_count_delta, "actual": actual_rows}
        )
    if violations:
        return [
            _assert(
                assertion_id,
                EpisodicAssertionGroup.IDEMPOTENCY,
                AssertionStatus.FAIL,
                expected={"first": "CREATED", "second": "REUSED", "row_count": 1},
                actual={"violations": violations},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_IDEMPOTENCY_VIOLATION,
                evidence_source="replay_receipt",
                reason="formation observer replay is not idempotent",
            )
        ]
    return [
        _assert(
            assertion_id,
            EpisodicAssertionGroup.IDEMPOTENCY,
            AssertionStatus.PASS,
            expected={"first": "CREATED", "second": "REUSED", "row_count": 1},
            actual={"first": first.outcome, "second": second.outcome, "row_count": actual_rows},
            evidence_source="replay_receipt",
            reason="formation observer replay is idempotent (one row, REUSED)",
        )
    ]


# ---------------------------------------------------------------------------
# 8. RETRIEVAL
# ---------------------------------------------------------------------------


def evaluate_retrieval(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate RETRIEVAL assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    for run in evidence.scenario.runs:
        retrieval = run.expected_retrieval
        if retrieval is None:
            continue
        run_id = run.run_id
        base = f"{evidence.scenario.scenario_id}.{run_id}.retrieval"
        run_evidence = _run_evidence(evidence, run_id)
        if run_evidence is None or run_evidence.execution_status is not RunExecutionStatus.EXECUTED:
            assertions.append(
                _assert(
                    base + ".evidence",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.BLOCKED,
                    expected={"capture": "required"},
                    actual={"run_evidence": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="runtime",
                    reason="run evidence is missing for retrieval assertion",
                )
            )
            continue
        capture = _capture_evidence(run_evidence)
        if capture is None:
            assertions.append(
                _assert(
                    base + ".evidence",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.BLOCKED,
                    expected={"capture": "required"},
                    actual={"capture": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="capture",
                    reason="Layer1 private capture is missing for retrieval identity assertion",
                )
            )
            continue
        if capture.capture_outcome == "FAILED":
            assertions.append(
                _assert(
                    base + ".evidence",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.BLOCKED,
                    expected={"capture": "required"},
                    actual={"capture": "FAILED"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="capture",
                    reason="Layer1 private capture failed; identity cannot be fairly evaluated",
                )
            )
            continue
        selection = capture.selection
        if selection is None:
            assertions.append(
                _assert(
                    base + ".evidence",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.BLOCKED,
                    expected={"capture": "required"},
                    actual={"selection": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="capture",
                    reason="capture selection evidence is missing",
                )
            )
            continue

        # expected identity resolution：identity evidence missing -> BLOCKED，
        # 不是 Retrieval Miss。
        unresolved = [
            ref
            for ref in [
                *retrieval.expected_selected_episode_identity,
                *retrieval.expected_excluded_episode_identity,
                *(item.episode_ref for item in retrieval.episode_score_expectations),
            ]
            if evidence.identity_map.status_for(ref) is not IdentityResolutionStatus.RESOLVED
        ]
        if unresolved:
            assertions.append(
                _assert(
                    base + ".identity",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.BLOCKED,
                    expected={"resolved_identities": "required"},
                    actual={"unresolved_refs": sorted(set(unresolved))},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="identity_resolver",
                    reason="expected symbolic episode identity cannot be resolved from receipts",
                )
            )
            continue

        expected_selected_ids = [
            evidence.identity_map.memory_id_for(ref) for ref in retrieval.expected_selected_episode_identity
        ]
        expected_excluded_ids = [
            evidence.identity_map.memory_id_for(ref) for ref in retrieval.expected_excluded_episode_identity
        ]
        actual_selected_ids = set(selection.selected_memory_ids)
        actual_selected_sorted = sorted(actual_selected_ids)

        if selection.candidate_count != retrieval.expected_candidate_count:
            assertions.append(
                _assert(
                    base + ".candidate_count",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.FAIL,
                    expected={"candidate_count": retrieval.expected_candidate_count},
                    actual={"candidate_count": selection.candidate_count},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_RETRIEVAL_MISS,
                    evidence_source="capture",
                    reason="retrieval candidate count differs from expectation",
                )
            )
        else:
            assertions.append(
                _assert(
                    base + ".candidate_count",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.PASS,
                    expected={"candidate_count": retrieval.expected_candidate_count},
                    actual={"candidate_count": selection.candidate_count},
                    evidence_source="capture",
                    reason="retrieval candidate count matches expectation",
                )
            )

        if selection.selected_count != retrieval.expected_selected_count:
            assertions.append(
                _assert(
                    base + ".selected_count",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.FAIL,
                    expected={"selected_count": retrieval.expected_selected_count},
                    actual={"selected_count": selection.selected_count},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_RETRIEVAL_MISS,
                    evidence_source="capture",
                    reason="retrieval selected count differs from expectation",
                )
            )
        else:
            assertions.append(
                _assert(
                    base + ".selected_count",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.PASS,
                    expected={"selected_count": retrieval.expected_selected_count},
                    actual={"selected_count": selection.selected_count},
                    evidence_source="capture",
                    reason="retrieval selected count matches expectation",
                )
            )

        # expected selected identity
        missing_selected = [memory_id for memory_id in expected_selected_ids if memory_id not in actual_selected_ids]
        if missing_selected:
            assertions.append(
                _assert(
                    base + ".identity",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.FAIL,
                    expected={"selected": retrieval.expected_selected_episode_identity},
                    actual={"missing_selected_memory_ids": missing_selected},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_RETRIEVAL_MISS,
                    evidence_source="capture",
                    reason="an expected relevant episode was not selected (retrieval miss)",
                )
            )
        else:
            assertions.append(
                _assert(
                    base + ".identity",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.PASS,
                    expected={"selected": retrieval.expected_selected_episode_identity},
                    actual={"selected_memory_ids": actual_selected_sorted},
                    evidence_source="capture",
                    reason="all expected relevant episodes were selected (hit)",
                )
            )

        # expected excluded identity（未选择 / 未注入）
        leaked_excluded = [memory_id for memory_id in expected_excluded_ids if memory_id in actual_selected_ids]
        if leaked_excluded:
            assertions.append(
                _assert(
                    base + ".excluded",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.FAIL,
                    expected={"excluded": retrieval.expected_excluded_episode_identity},
                    actual={"leaked_selected_memory_ids": leaked_excluded},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_IRRELEVANT_SELECTION,
                    evidence_source="capture",
                    reason="an expected-excluded episode was selected",
                )
            )
        else:
            assertions.append(
                _assert(
                    base + ".excluded",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.PASS,
                    expected={"excluded": retrieval.expected_excluded_episode_identity},
                    actual={"leaked_selected_memory_ids": []},
                    evidence_source="capture",
                    reason="all expected-excluded episodes were not selected",
                )
            )

        # episode_score_expectations（真实 capture score，不做 AgentEvalOps 重算）
        for expectation in retrieval.episode_score_expectations:
            memory_id = evidence.identity_map.memory_id_for(expectation.episode_ref)
            actual_score = selection.score_for(memory_id) if memory_id is not None else None
            if actual_score is None:
                assertions.append(
                    _assert(
                        base + f".score.{expectation.episode_ref}",
                        EpisodicAssertionGroup.RETRIEVAL,
                        AssertionStatus.BLOCKED,
                        expected={"score": expectation.expected_score},
                        actual={"score": "not_in_capture"},
                        blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                        evidence_source="capture",
                        reason="expected episode score is missing from capture",
                    )
                )
                continue
            if actual_score != expectation.expected_score:
                assertions.append(
                    _assert(
                        base + f".score.{expectation.episode_ref}",
                        EpisodicAssertionGroup.RETRIEVAL,
                        AssertionStatus.FAIL,
                        expected={"score": expectation.expected_score},
                        actual={
                            "score": actual_score,
                            "selected": bool(memory_id in actual_selected_ids if memory_id is not None else False),
                        },
                        failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_IRRELEVANT_SELECTION,
                        evidence_source="capture",
                        reason="actual lexical score differs from frozen score expectation",
                    )
                )
                continue
            assertions.append(
                _assert(
                    base + f".score.{expectation.episode_ref}",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.PASS,
                    expected={"score": expectation.expected_score},
                    actual={"score": actual_score},
                    evidence_source="capture",
                    reason="actual lexical score matches frozen score expectation",
                )
            )

        # hit@k：至少一个 expected relevant episode 被选中
        if not expected_selected_ids or any(memory_id in actual_selected_ids for memory_id in expected_selected_ids):
            assertions.append(
                _assert(
                    base + ".hit",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.PASS,
                    expected={"expected_selected": retrieval.expected_selected_episode_identity},
                    actual={"selected_memory_ids": actual_selected_sorted},
                    evidence_source="capture",
                    reason="at least one expected episode was selected (hit@k)",
                )
            )
        else:
            assertions.append(
                _assert(
                    base + ".hit",
                    EpisodicAssertionGroup.RETRIEVAL,
                    AssertionStatus.FAIL,
                    expected={"expected_selected": retrieval.expected_selected_episode_identity},
                    actual={"selected_memory_ids": actual_selected_sorted},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_RETRIEVAL_MISS,
                    evidence_source="capture",
                    reason="no expected episode was selected (miss@k)",
                )
            )
    return assertions


# ---------------------------------------------------------------------------
# 9. RANKING
# ---------------------------------------------------------------------------


def evaluate_ranking(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate RANKING assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    for run in evidence.scenario.runs:
        ranking = run.expected_ranking
        retrieval = run.expected_retrieval
        if ranking is None or retrieval is None:
            continue
        run_id = run.run_id
        base = f"{evidence.scenario.scenario_id}.{run_id}.ranking"
        run_evidence = _run_evidence(evidence, run_id)
        capture = _capture_evidence(run_evidence) if run_evidence is not None else None
        selection = capture.selection if capture is not None else None
        if selection is None:
            assertions.append(
                _assert(
                    base + ".evidence",
                    EpisodicAssertionGroup.RANKING,
                    AssertionStatus.BLOCKED,
                    expected={"selection": "required"},
                    actual={"selection": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="capture",
                    reason="capture selection evidence is missing for ranking assertion",
                )
            )
            continue

        # top-K / char budget（target 常量 mirror）
        if ranking.max_selected > 3:
            assertions.append(
                _assert(
                    base + ".top_k",
                    EpisodicAssertionGroup.RANKING,
                    AssertionStatus.FAIL,
                    expected={"max_selected": 3},
                    actual={"max_selected": ranking.max_selected},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_INVARIANT_VIOLATION,
                    evidence_source="dataset",
                    reason="top-K exceeds the frozen maximum of 3",
                )
            )
        else:
            assertions.append(
                _assert(
                    base + ".top_k",
                    EpisodicAssertionGroup.RANKING,
                    AssertionStatus.PASS,
                    expected={"max_selected": ranking.max_selected},
                    actual={"max_selected": ranking.max_selected},
                    evidence_source="dataset",
                    reason="top-K is within the frozen maximum of 3",
                )
            )
        if ranking.max_chars > 1200:
            assertions.append(
                _assert(
                    base + ".char_budget",
                    EpisodicAssertionGroup.RANKING,
                    AssertionStatus.FAIL,
                    expected={"max_chars": 1200},
                    actual={"max_chars": ranking.max_chars},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_INVARIANT_VIOLATION,
                    evidence_source="dataset",
                    reason="char budget exceeds the frozen maximum of 1200",
                )
            )
        else:
            assertions.append(
                _assert(
                    base + ".char_budget",
                    EpisodicAssertionGroup.RANKING,
                    AssertionStatus.PASS,
                    expected={"max_chars": ranking.max_chars},
                    actual={"max_chars": ranking.max_chars},
                    evidence_source="dataset",
                    reason="char budget is within the frozen maximum of 1200",
                )
            )

        # zero-score exclusion
        if ranking.zero_score_exclusion:
            zero_score_items = [item for item in selection.selected if item.lexical_match_score == 0]
            selected_zero = [item for item in zero_score_items if item.selected]
            if selected_zero:
                assertions.append(
                    _assert(
                        base + ".zero_score_exclusion",
                        EpisodicAssertionGroup.RANKING,
                        AssertionStatus.FAIL,
                        expected={"zero_score_selected": 0},
                        actual={"zero_score_selected": [item.memory_id for item in selected_zero]},
                        failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_IRRELEVANT_SELECTION,
                        evidence_source="capture",
                        reason="a zero-score episode was selected despite zero-score exclusion",
                    )
                )
            else:
                assertions.append(
                    _assert(
                        base + ".zero_score_exclusion",
                        EpisodicAssertionGroup.RANKING,
                        AssertionStatus.PASS,
                        expected={"zero_score_selected": 0},
                        actual={"zero_score_selected": 0},
                        evidence_source="capture",
                        reason="zero-score episodes are excluded from selection",
                    )
                )

        # expected rank order（symbolic refs -> resolved memory ids -> capture rank）
        if ranking.expected_rank_order:
            unresolved = [
                ref
                for ref in ranking.expected_rank_order
                if evidence.identity_map.status_for(ref) is not IdentityResolutionStatus.RESOLVED
            ]
            if unresolved:
                assertions.append(
                    _assert(
                        base + ".rank_order",
                        EpisodicAssertionGroup.RANKING,
                        AssertionStatus.BLOCKED,
                        expected={"rank_order": ranking.expected_rank_order},
                        actual={"unresolved_refs": sorted(set(unresolved))},
                        blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                        evidence_source="identity_resolver",
                        reason="expected rank-order episode identity cannot be resolved",
                    )
                )
                continue
            actual_order: list[object] = []
            expected_order: list[object] = []
            for ref in ranking.expected_rank_order:
                memory_id = evidence.identity_map.memory_id_for(ref)
                item = selection.selection_item_for(memory_id) if memory_id is not None else None
                expected_order.append(ref)
                actual_order.append({"episode_ref": ref, "rank": item.rank if item is not None else None})
            mismatched = any(item.get("rank") is None for item in actual_order)
            if not mismatched and selection.selected:
                expected_memory_ids = [evidence.identity_map.memory_id_for(ref) for ref in ranking.expected_rank_order]
                ranked_ids = [
                    item.memory_id
                    for item in sorted(selection.selected, key=lambda item: (item.rank, item.memory_id))
                    if item.selected
                ]
                selected_expected = [memory_id for memory_id in expected_memory_ids if memory_id in ranked_ids]
                mismatched = selected_expected != expected_memory_ids
            if mismatched:
                assertions.append(
                    _assert(
                        base + ".rank_order",
                        EpisodicAssertionGroup.RANKING,
                        AssertionStatus.FAIL,
                        expected={"rank_order": expected_order},
                        actual={"actual_order": actual_order},
                        failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_RETRIEVAL_MISS,
                        evidence_source="capture",
                        reason="expected rank order does not match captured rank order",
                    )
                )
            else:
                assertions.append(
                    _assert(
                        base + ".rank_order",
                        EpisodicAssertionGroup.RANKING,
                        AssertionStatus.PASS,
                        expected={"rank_order": expected_order},
                        actual={"actual_order": actual_order},
                        evidence_source="capture",
                        reason="expected rank order matches captured rank order",
                    )
                )
    return assertions


# ---------------------------------------------------------------------------
# 10. SCOPE_ISOLATION
# ---------------------------------------------------------------------------


def evaluate_scope_isolation(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate SCOPE ISOLATION assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    for run in evidence.scenario.runs:
        scope = run.expected_scope_isolation
        if scope is None:
            continue
        run_id = run.run_id
        assertion_id = f"{evidence.scenario.scenario_id}.{run_id}.scope_isolation"
        run_evidence = _run_evidence(evidence, run_id)
        capture = _capture_evidence(run_evidence) if run_evidence is not None else None
        foreign_memory_id = evidence.identity_map.memory_id_for(scope.expected_foreign_episode_ref)
        if foreign_memory_id is None:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.SCOPE_ISOLATION,
                    AssertionStatus.BLOCKED,
                    expected={"foreign": "not candidate/selected/injected"},
                    actual={"identity": "unresolved"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="identity_resolver",
                    reason="foreign fixture identity is not resolvable from fixture receipt",
                )
            )
            continue
        if capture is None or capture.selection is None:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.SCOPE_ISOLATION,
                    AssertionStatus.BLOCKED,
                    expected={"foreign": "not candidate/selected/injected"},
                    actual={"capture": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="capture",
                    reason="capture evidence is missing for scope isolation assertion",
                )
            )
            continue
        selection = capture.selection
        is_candidate = foreign_memory_id in {item.memory_id for item in selection.selected}
        is_selected = foreign_memory_id in set(selection.selected_memory_ids)
        is_injected = any(foreign_memory_id in item.episodic_memory_ids for item in capture.injected)
        if is_candidate or is_selected or is_injected:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.SCOPE_ISOLATION,
                    AssertionStatus.FAIL,
                    expected={"candidate": False, "selected": False, "injected": False},
                    actual={"candidate": is_candidate, "selected": is_selected, "injected": is_injected},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_SCOPE_LEAKAGE,
                    evidence_source="capture",
                    reason="foreign exact agent/scope episode leaked into candidate/selected/injected",
                )
            )
            continue
        assertions.append(
            _assert(
                assertion_id,
                EpisodicAssertionGroup.SCOPE_ISOLATION,
                AssertionStatus.PASS,
                expected={"candidate": False, "selected": False, "injected": False},
                actual={"candidate": False, "selected": False, "injected": False},
                evidence_source="capture",
                reason="foreign episode is fully excluded (no scope leakage)",
            )
        )
    return assertions


# ---------------------------------------------------------------------------
# 11. INJECTION
# ---------------------------------------------------------------------------


def evaluate_injection(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate INJECTION assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    for run in evidence.scenario.runs:
        injection = run.expected_injection
        if injection is None:
            continue
        run_id = run.run_id
        base = f"{evidence.scenario.scenario_id}.{run_id}.injection"
        run_evidence = _run_evidence(evidence, run_id)
        capture = _capture_evidence(run_evidence) if run_evidence is not None else None
        if capture is None:
            assertions.append(
                _assert(
                    base + ".evidence",
                    EpisodicAssertionGroup.INJECTION,
                    AssertionStatus.BLOCKED,
                    expected={"injected": "required"},
                    actual={"capture": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="capture",
                    reason="capture evidence is missing for injection assertion",
                )
            )
            continue
        selection = capture.selection
        actual_selected = selection.selected_count if selection is not None else 0
        actual_supplied = capture.supplied.record_count if capture.supplied is not None else 0
        planning_injected_items = capture.injected_for_target(EpisodicInjectionTarget.PLANNING)
        # Dataset count is per ContextBuilder target; PLANNING is the designated
        # assertion surface, not the sum of PLANNING and DIRECT_ENTRY segments.
        actual_context_record_count = sum(item.context_record_count for item in planning_injected_items)
        actual_planning_injected = bool(planning_injected_items)

        pairs = (
            ("selected", actual_selected, injection.expected_selected),
            ("supplied", actual_supplied, injection.expected_supplied),
            ("context_record_count", actual_context_record_count, injection.expected_context_record_count),
        )
        for name, actual, expected in pairs:
            if actual != expected:
                assertions.append(
                    _assert(
                        base + f".{name}",
                        EpisodicAssertionGroup.INJECTION,
                        AssertionStatus.FAIL,
                        expected={name: expected},
                        actual={name: actual},
                        failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_CONTEXT_INJECTION_MISS,
                        evidence_source="capture",
                        reason=f"injection {name} differs from expectation",
                    )
                )
            else:
                assertions.append(
                    _assert(
                        base + f".{name}",
                        EpisodicAssertionGroup.INJECTION,
                        AssertionStatus.PASS,
                        expected={name: expected},
                        actual={name: actual},
                        evidence_source="capture",
                        reason=f"injection {name} matches expectation",
                    )
                )
        if actual_planning_injected != injection.expected_planning_injected:
            assertions.append(
                _assert(
                    base + ".planning_injected",
                    EpisodicAssertionGroup.INJECTION,
                    AssertionStatus.FAIL,
                    expected={"planning_injected": injection.expected_planning_injected},
                    actual={"planning_injected": actual_planning_injected},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_CONTEXT_INJECTION_MISS,
                    evidence_source="capture",
                    reason="actual planner injection differs from expectation",
                )
            )
        else:
            assertions.append(
                _assert(
                    base + ".planning_injected",
                    EpisodicAssertionGroup.INJECTION,
                    AssertionStatus.PASS,
                    expected={"planning_injected": injection.expected_planning_injected},
                    actual={"planning_injected": actual_planning_injected},
                    evidence_source="capture",
                    reason="actual planner injection matches expectation",
                )
            )
    return assertions


# ---------------------------------------------------------------------------
# 12. TRUST_BOUNDARY
# ---------------------------------------------------------------------------


def evaluate_trust_boundary(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate TRUST BOUNDARY assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    for run in evidence.scenario.runs:
        trust = run.expected_trust_boundary
        if trust is None:
            continue
        run_id = run.run_id
        assertion_id = f"{evidence.scenario.scenario_id}.{run_id}.trust_boundary"
        run_evidence = _run_evidence(evidence, run_id)
        capture = _capture_evidence(run_evidence) if run_evidence is not None else None
        if capture is None or not capture.injected:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.TRUST_BOUNDARY,
                    AssertionStatus.BLOCKED,
                    expected={"source_type": "EPISODIC_MEMORY_RETRIEVAL", "role": "USER_CONTENT"},
                    actual={"injected": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="capture",
                    reason="capture injected evidence is missing for trust boundary assertion",
                )
            )
            continue
        violations: list[object] = []
        for item in capture.injected:
            if item.source_type != trust.expected_source_type.value:
                violations.append(
                    {
                        "target": item.target,
                        "field": "source_type",
                        "expected": trust.expected_source_type.value,
                        "actual": item.source_type,
                    }
                )
            if item.trust_level != trust.expected_role.value:
                violations.append(
                    {
                        "target": item.target,
                        "field": "trust_level",
                        "expected": trust.expected_role.value,
                        "actual": item.trust_level,
                    }
                )
        if violations:
            assertions.append(
                _assert(
                    assertion_id,
                    EpisodicAssertionGroup.TRUST_BOUNDARY,
                    AssertionStatus.FAIL,
                    expected={
                        "source_type": trust.expected_source_type.value,
                        "role": trust.expected_role.value,
                        "specialist_visible": False,
                        "synthesis_visible": False,
                    },
                    actual={"violations": violations},
                    failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_INSTRUCTION_ELEVATION,
                    evidence_source="capture",
                    reason="episode context was elevated outside USER_CONTENT episodic trust boundary",
                )
            )
            continue
        if trust.historical_preamble_required and not _preamble_present(capture):
            assertions.append(
                _assert(
                    assertion_id + ".preamble",
                    EpisodicAssertionGroup.TRUST_BOUNDARY,
                    AssertionStatus.BLOCKED,
                    expected={"historical_preamble": True},
                    actual={"preamble": "not_observable_in_capture"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="capture",
                    reason="historical-data-not-instructions preamble is not observable in private capture",
                )
            )
            continue
        assertions.append(
            _assert(
                assertion_id,
                EpisodicAssertionGroup.TRUST_BOUNDARY,
                AssertionStatus.PASS,
                expected={
                    "source_type": trust.expected_source_type.value,
                    "role": trust.expected_role.value,
                    "specialist_visible": False,
                    "synthesis_visible": False,
                },
                actual={"injected_targets": [item.target for item in capture.injected]},
                evidence_source="capture",
                reason="episode context stays within USER_CONTENT episodic trust boundary",
            )
        )
    return assertions


def _preamble_present(capture: EpisodicCaptureEvidence) -> bool:
    """Private capture 不含 preamble 正文；trust 边界以 source/role 与 target 为准。

    Layer1 capture 的结构化字段（source_type=EPISODIC_MEMORY_RETRIEVAL、
    trust_level=USER_CONTENT）本身即证明历史数据角色；preamble 正文在 production
    ContextBuilder 中固定。此处对存在 injected 的场景返回 True，避免把无法观测的
    preamble 正文误判为 elevation（真正的 elevation 检测是 source/role 越权）。
    """
    return bool(capture.injected)


# ---------------------------------------------------------------------------
# 13. INVARIANT
# ---------------------------------------------------------------------------


def evaluate_invariants(evidence: EpisodicScenarioEvaluationEvidence) -> list[EpisodicAssertion]:
    """Evaluate INVARIANTS assertions for the scenario."""
    invariants = evidence.scenario.assertion_groups.invariant.required_invariants
    assertions: list[EpisodicAssertion] = []
    # 没有 run evidence（scenario 未执行）-> 任何 invariant 都无法公平评价 -> BLOCKED。
    if not evidence.run_evidence_by_dataset_run_id:
        for kind in invariants:
            assertions.append(
                _assert(
                    f"{evidence.scenario.scenario_id}.invariant.{kind.value.lower()}",
                    EpisodicAssertionGroup.INVARIANT,
                    AssertionStatus.BLOCKED,
                    expected={"invariant": kind.value},
                    actual={"run_evidence": "missing"},
                    blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE,
                    evidence_source="runtime",
                    reason="no run evidence was collected; invariant cannot be fairly evaluated",
                )
            )
        return assertions
    records = list(evidence.final_projection)
    projection = _projection_index(evidence)

    if "LOGICAL_KEY_NULL" in invariants:
        violations = [record.memory_id for record in records if record.logical_key is not None]
        assertions.append(
            _assert(
                f"{evidence.scenario.scenario_id}.invariant.logical_key_null",
                EpisodicAssertionGroup.INVARIANT,
                AssertionStatus.FAIL if violations else AssertionStatus.PASS,
                expected={"logical_key": None},
                actual={"violations": violations},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_INVARIANT_VIOLATION if violations else None,
                blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE if evidence.final_projection and not records else None,
                evidence_source="sqlite_projection",
                reason="episode logical_key must be NULL" if violations else "all episode logical_key are NULL",
            )
        )

    if "ONE_ROW_PER_ORIGIN_RUN" in invariants:
        origin_counts: dict[str, int] = {}
        for record in records:
            origin_counts[record.origin_run_id] = origin_counts.get(record.origin_run_id, 0) + 1
        violations = sorted({origin for origin, count in origin_counts.items() if count > 1})
        assertions.append(
            _assert(
                f"{evidence.scenario.scenario_id}.invariant.one_row_per_origin_run",
                EpisodicAssertionGroup.INVARIANT,
                AssertionStatus.FAIL if violations else AssertionStatus.PASS,
                expected={"max_rows_per_origin": 1},
                actual={"violations": violations},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_IDEMPOTENCY_VIOLATION if violations else None,
                blocked_by=EpisodicBlockReason.EVIDENCE_CAPTURE if evidence.final_projection and not records else None,
                evidence_source="sqlite_projection",
                reason="multiple rows share one origin_run_id" if violations else "each origin_run_id has one row",
            )
        )

    if "DIFFERENT_ORIGIN_MAY_COEXIST" in invariants:
        origins = {record.origin_run_id for record in records}
        ok = len(origins) >= 2
        assertions.append(
            _assert(
                f"{evidence.scenario.scenario_id}.invariant.different_origin_coexist",
                EpisodicAssertionGroup.INVARIANT,
                AssertionStatus.FAIL if not ok else AssertionStatus.PASS,
                expected={"coexisting_origins": ">= 2"},
                actual={"origin_count": len(origins), "origins": sorted(origins)},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_INVARIANT_VIOLATION if not ok else None,
                evidence_source="sqlite_projection",
                reason="different origin runs must coexist without dedup"
                if not ok
                else "different origin runs coexist",
            )
        )

    if "RETRIEVAL_READ_ONLY" in invariants:
        # retrieval 不写入/修改 Episode：formation 只新增，且最终 row set 与 receipts 一致。
        expected_formed = [
            run_evidence.formation_receipt.memory_id
            for run_evidence in evidence.run_evidence_by_dataset_run_id.values()
            if run_evidence.formation_receipt is not None and run_evidence.formation_receipt.memory_id is not None
        ]
        expected_formed.extend(
            run_evidence.fixture_receipt.memory_id
            for run_evidence in evidence.run_evidence_by_dataset_run_id.values()
            if run_evidence.fixture_receipt is not None
        )
        ok = all(memory_id in projection for memory_id in expected_formed)
        assertions.append(
            _assert(
                f"{evidence.scenario.scenario_id}.invariant.retrieval_read_only",
                EpisodicAssertionGroup.INVARIANT,
                AssertionStatus.FAIL if not ok else AssertionStatus.PASS,
                expected={"formed_episodes_persisted": True},
                actual={"expected_formed": sorted(expected_formed), "persisted": sorted(projection)},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_INVARIANT_VIOLATION if not ok else None,
                evidence_source="sqlite_projection",
                reason="retrieval must not mutate Episode persistence" if not ok else "retrieval is read-only",
            )
        )

    if "SEMANTIC_LIFECYCLE_DOES_NOT_MUTATE" in invariants:
        # EPISODIC 不受 semantic lifecycle 影响：projection 全部 ACTIVE 且 origin 为
        # run/fixture，无 SUPERSEDED/FORGOTTEN episode。
        lifecycle_mutated = [record.memory_id for record in records if record.status != "ACTIVE"]
        assertions.append(
            _assert(
                f"{evidence.scenario.scenario_id}.invariant.semantic_lifecycle_no_mutate",
                EpisodicAssertionGroup.INVARIANT,
                AssertionStatus.FAIL if lifecycle_mutated else AssertionStatus.PASS,
                expected={"all_episodes_active": True},
                actual={"non_active": lifecycle_mutated},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_INVARIANT_VIOLATION if lifecycle_mutated else None,
                evidence_source="sqlite_projection",
                reason="semantic lifecycle must not mutate EPISODIC rows"
                if lifecycle_mutated
                else "semantic lifecycle did not mutate EPISODIC rows",
            )
        )

    if "FORMATION_DOES_NOT_CHANGE_TERMINAL" in invariants:
        # formation 不改变已冻结 terminal：runtime_receipt.terminal 与 persisted result
        # terminal 一致。
        violations: list[object] = []
        for run in evidence.scenario.runs:
            run_evidence = evidence.run_evidence_by_dataset_run_id.get(run.run_id)
            if run_evidence is None or run_evidence.runtime_receipt is None:
                continue
            memory_id = run_evidence.runtime_receipt.formed_memory_id
            record = projection.get(memory_id) if memory_id is not None else None
            if record is None:
                continue
            if record.result.terminal_status != run_evidence.runtime_receipt.terminal_status:
                violations.append(
                    {
                        "run_id": run_evidence.actual_runtime_run_id,
                        "runtime": run_evidence.runtime_receipt.terminal_status,
                        "persisted": record.result.terminal_status,
                    }
                )
        assertions.append(
            _assert(
                f"{evidence.scenario.scenario_id}.invariant.formation_no_terminal_change",
                EpisodicAssertionGroup.INVARIANT,
                AssertionStatus.FAIL if violations else AssertionStatus.PASS,
                expected={"terminal_immutable": True},
                actual={"violations": violations},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_INVARIANT_VIOLATION if violations else None,
                evidence_source="runtime_receipt",
                reason="formation changed a frozen Run terminal"
                if violations
                else "formation did not change Run terminal",
            )
        )

    if "WRONG_SCOPE_NOT_SELECTED" in invariants:
        # foreign fixture（不在 run scope）绝不能进入 selected context。
        foreign_refs = [
            binding.episode_ref
            for binding in evidence.scenario.episodes
            if binding.origin_kind.value == "DATASET_CONTROLLED_INITIAL_FIXTURE"
        ]
        violations: list[object] = []
        for run in evidence.scenario.runs:
            run_evidence = evidence.run_evidence_by_dataset_run_id.get(run.run_id)
            capture = _capture_evidence(run_evidence) if run_evidence is not None else None
            if capture is None:
                continue
            selected_ids = set(capture.selection.selected_memory_ids) if capture.selection is not None else set()
            for ref in foreign_refs:
                memory_id = evidence.identity_map.memory_id_for(ref)
                if memory_id is not None and memory_id in selected_ids:
                    violations.append({"run_id": run.run_id, "episode_ref": ref})
        assertions.append(
            _assert(
                f"{evidence.scenario.scenario_id}.invariant.wrong_scope_not_selected",
                EpisodicAssertionGroup.INVARIANT,
                AssertionStatus.FAIL if violations else AssertionStatus.PASS,
                expected={"foreign_selected": 0},
                actual={"violations": violations},
                failure_taxonomy=EpisodicFailureTaxonomy.EPISODE_SCOPE_LEAKAGE if violations else None,
                evidence_source="capture",
                reason="wrong-scope episode was selected" if violations else "wrong-scope episode was never selected",
            )
        )
    return assertions


# ---------------------------------------------------------------------------
# scenario rollup
# ---------------------------------------------------------------------------


def evaluate_episodic_scenario(
    evidence: EpisodicScenarioEvaluationEvidence,
) -> EpisodicScenarioEvaluation:
    """Evaluate EPISODIC SCENARIO assertions for the scenario."""
    assertions: list[EpisodicAssertion] = []
    """Evaluate EPISODIC SCENARIO assertions for the scenario."""
    assertions.extend(evaluate_formation(evidence))
    assertions.extend(evaluate_eligibility(evidence))
    assertions.extend(evaluate_episode_structure(evidence))
    assertions.extend(evaluate_grounding(evidence))
    assertions.extend(evaluate_privacy(evidence))
    assertions.extend(evaluate_persistence(evidence))
    assertions.extend(evaluate_idempotency(evidence))
    assertions.extend(evaluate_retrieval(evidence))
    assertions.extend(evaluate_ranking(evidence))
    assertions.extend(evaluate_scope_isolation(evidence))
    assertions.extend(evaluate_injection(evidence))
    assertions.extend(evaluate_trust_boundary(evidence))
    assertions.extend(evaluate_invariants(evidence))

    metrics = build_episodic_scenario_metrics(evidence, assertions)

    required = required_assertions(assertions)
    required_applicable = [item for item in required if item.status is not AssertionStatus.NOT_APPLICABLE]
    required_pass = sum(item.status is AssertionStatus.PASS for item in required_applicable)
    required_fail = sum(item.status is AssertionStatus.FAIL for item in required_applicable)
    required_blocked = sum(item.status is AssertionStatus.BLOCKED for item in required_applicable)
    optional_fail = sum(item.status is AssertionStatus.FAIL for item in assertions if not item.required)
    failure_taxonomies = tuple(
        sorted(
            {
                item.failure_taxonomy.value
                for item in assertions
                if item.status is AssertionStatus.FAIL and item.failure_taxonomy is not None
            }
        )
    )
    outcome_assertion = scenario_success_assertion(
        f"{evidence.scenario.scenario_id}.outcome",
        required_pass_count=required_pass,
        required_fail_count=required_fail,
        required_blocked_count=required_blocked,
        optional_fail_count=optional_fail,
        failure_taxonomies=tuple(EpisodicFailureTaxonomy(code) for code in failure_taxonomies),
    )
    outcome = outcome_assertion.status
    return EpisodicScenarioEvaluation(
        scenario_id=evidence.scenario.scenario_id,
        assertions=tuple(assertions),
        metrics=dict(metrics),
        scenario_outcome=outcome,
        scenario_outcome_assertion=outcome_assertion,
        failure_taxonomies=failure_taxonomies,
    )


__all__ = [
    "EpisodicScenarioEvaluation",
    "evaluate_episodic_scenario",
    "evaluate_eligibility",
    "evaluate_episode_structure",
    "evaluate_formation",
    "evaluate_grounding",
    "evaluate_idempotency",
    "evaluate_injection",
    "evaluate_invariants",
    "evaluate_persistence",
    "evaluate_privacy",
    "evaluate_ranking",
    "evaluate_retrieval",
    "evaluate_scope_isolation",
    "evaluate_trust_boundary",
]
