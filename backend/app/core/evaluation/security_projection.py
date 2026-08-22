"""Prompt Injection Security Regression 的纯投影层（WP6）。

本模块在 persisted ``EvaluationResult`` 之上构建确定性 summary / comparison 投影：

- Verdict Authority 仍属于 ``PromptInjectionSecurityEvaluator`` 与持久化
  ``EvaluationResult``。本模块不重新判定任何安全结论、不调用 Judge / Agent /
  Retrieval；summary 可从持久化事实无重跑重建（DERIVABLE_WITHOUT_RERUN）。
- Ground Truth Authority 仍是 ``EvaluationCase -> GroundTruth.security``；
  case_kind / attack_type / attack_source / severity 只来自 caller 传入的 dataset
  权威 facts，不从 result metadata 或 answer 文本重新推断。
- 不创建第二套 Security Domain / persistence：全部类型是 immutable projection DTO，
  不新增 DB table。

INCONCLUSIVE 是一等公民：执行完成但证据或 Evaluator 无法可靠判断。它与
NOT_EVALUATED（mapped 但未形成有效 Evaluation）和 NOT_MAPPED（当前 ExecutionTarget
没有真实执行映射）严格区分，不合并为单一状态。所有列表输出按稳定 key 排序，
相同输入多次构建结果完全一致。
"""

# ruff: noqa: D105, D415

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.core.evaluation.comparison import AlignedResultComparison, EvaluationRunComparison
from app.core.evaluation.immutable import require_text
from app.core.evaluation.results import EvaluationResult, EvaluationVerdict
from app.core.evaluation.security_evaluator import (
    SECURITY_EVALUATOR_ID,
    SECURITY_REASON_EVIDENCE_UNSUPPORTED,
)


class SecurityProjectionError(ValueError):
    """persisted Result / facts 输入违反 projection contract 时 fail closed。"""


class SecurityCaseStatus(StrEnum):
    """单 case 在一次 Run Security Summary 中的最终状态。"""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_MAPPED = "NOT_MAPPED"


class SecurityTransitionClass(StrEnum):
    """跨 Run 对齐后单个 security slot 的 transition 分类（projection 级语义）。"""

    SECURITY_REGRESSION = "SECURITY_REGRESSION"
    SECURITY_IMPROVEMENT = "SECURITY_IMPROVEMENT"
    OVER_REFUSAL_REGRESSION = "OVER_REFUSAL_REGRESSION"
    CERTAINTY_REGRESSION = "CERTAINTY_REGRESSION"
    EVALUATION_IMPROVEMENT = "EVALUATION_IMPROVEMENT"
    NEWLY_IDENTIFIED_FAILURE = "NEWLY_IDENTIFIED_FAILURE"
    UNCHANGED = "UNCHANGED"
    NOT_COMPARABLE = "NOT_COMPARABLE"


WARNING_JUDGE_PROMPT_CHANGED = "judge_prompt_changed"
WARNING_JUDGE_MODEL_CHANGED = "judge_model_changed"
WARNING_DATASET_VERSION_CHANGED = "dataset_version_changed"

STATUS_REASON_EVALUATION_MISSING = "evaluation_result_missing"

_ATTACK_KIND = "ATTACK"
_BENIGN_KIND = "BENIGN_CONTROL"
_CRITICAL_SEVERITY = "CRITICAL"

_VERDICT_STATUS = {
    EvaluationVerdict.PASS: SecurityCaseStatus.PASS,
    EvaluationVerdict.FAIL: SecurityCaseStatus.FAIL,
    EvaluationVerdict.INCONCLUSIVE: SecurityCaseStatus.INCONCLUSIVE,
}

_EVALUATED_STATUSES = frozenset(
    {SecurityCaseStatus.PASS, SecurityCaseStatus.FAIL, SecurityCaseStatus.INCONCLUSIVE}
)


@dataclass(frozen=True, slots=True)
class UnmappedSecurityCaseRef:
    """Runner 无法映射到真实执行边界的 case 及稳定 gap 原因。"""

    case_id: str
    case_version: str
    gap_reason: str

    def __post_init__(self) -> None:
        require_text(self.case_id, "case_id")
        require_text(self.case_version, "case_version")
        require_text(self.gap_reason, "gap_reason")


@dataclass(frozen=True, slots=True)
class SecurityCaseFacts:
    """Dataset 权威的 security Ground Truth facts（不含任何 verdict）。"""

    case_id: str
    case_version: str
    case_kind: str
    attack_type: str | None = None
    attack_source: str | None = None
    severity: str | None = None

    def __post_init__(self) -> None:
        require_text(self.case_id, "case_id")
        require_text(self.case_version, "case_version")
        if self.case_kind not in {_ATTACK_KIND, _BENIGN_KIND}:
            raise SecurityProjectionError("unknown security case_kind")
        if self.attack_type is not None:
            require_text(self.attack_type, "attack_type")
        if self.attack_source is not None:
            require_text(self.attack_source, "attack_source")
        if self.severity is not None:
            require_text(self.severity, "severity")


@dataclass(frozen=True, slots=True)
class SecurityKindCounters:
    """一个 case 类别（ATTACK / BENIGN_CONTROL）下的完整状态计数。"""

    total: int
    passed: int
    failed: int
    inconclusive: int
    not_evaluated: int
    not_mapped: int

    def __post_init__(self) -> None:
        counts = (
            self.total,
            self.passed,
            self.failed,
            self.inconclusive,
            self.not_evaluated,
            self.not_mapped,
        )
        if any(count < 0 for count in counts):
            raise SecurityProjectionError("security counters must be non-negative")
        if self.passed + self.failed + self.inconclusive + self.not_evaluated + self.not_mapped != self.total:
            raise SecurityProjectionError("security counters must sum to kind total")


@dataclass(frozen=True, slots=True)
class SecurityCaseEntry:
    """一个 selected case 的 summary 条目：dataset facts + persisted verdict 投影。"""

    case_id: str
    case_version: str
    case_kind: str
    attack_type: str | None
    attack_source: str | None
    severity: str | None
    status: SecurityCaseStatus
    evaluator_id: str | None = None
    result_id: str | None = None
    attempt_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    status_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SecurityContractGap:
    """一类 contract gap 及其涉及的全部 case ids（排序稳定）。"""

    category: str
    cases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecurityRunSummary:
    """一次 Run 的 Security Regression Summary（纯 projection，可重建）。"""

    run_id: UUID
    dataset_id: str
    dataset_version: str
    suite_id: str
    suite_version: str
    execution_target_id: str
    execution_target_kind: str
    total_cases: int
    evaluated_cases: int
    not_evaluated_cases: int
    not_mapped_cases: int
    attack: SecurityKindCounters
    benign: SecurityKindCounters
    by_attack_type: tuple[tuple[str, SecurityKindCounters], ...]
    by_attack_source: tuple[tuple[str, SecurityKindCounters], ...]
    by_severity: tuple[tuple[str, SecurityKindCounters], ...]
    critical_failing_cases: tuple[str, ...]
    critical_inconclusive_cases: tuple[str, ...]
    top_reason_codes: tuple[tuple[str, int], ...]
    contract_gaps: tuple[SecurityContractGap, ...]
    entries: tuple[SecurityCaseEntry, ...]

    def __post_init__(self) -> None:
        if sum(counter.total for counter in (self.attack, self.benign)) != self.total_cases:
            raise SecurityProjectionError("kind totals must sum to total_cases")
        evaluated = self.attack.passed + self.attack.failed + self.attack.inconclusive
        benign_evaluated = self.benign.passed + self.benign.failed + self.benign.inconclusive
        if evaluated + benign_evaluated != self.evaluated_cases:
            raise SecurityProjectionError("evaluated counts are inconsistent with kind counters")
        if self.total_cases != len(self.entries):
            raise SecurityProjectionError("total_cases must match entries length")


@dataclass(frozen=True, slots=True)
class SecuritySlotProjection:
    """跨 Run 对齐后一个 security slot 的 transition 投影。"""

    case_id: str
    case_version: str
    evaluator_id: str
    evaluator_version: str
    classification: SecurityTransitionClass
    detail: str
    warnings: tuple[str, ...] = ()
    baseline_result_id: str | None = None
    candidate_result_id: str | None = None
    baseline_verdict: str | None = None
    candidate_verdict: str | None = None
    baseline_score: float | None = None
    candidate_score: float | None = None
    case_kind: str | None = None


@dataclass(frozen=True, slots=True)
class SecurityComparisonProjection:
    """Baseline vs Candidate 的 Security transition 投影（消费既有 comparison 事实）。"""

    project_id: UUID
    baseline_run_id: UUID
    candidate_run_id: UUID
    dataset_version_changed: bool
    suite_version_changed: bool
    slots: tuple[SecuritySlotProjection, ...]
    security_regressions: tuple[SecuritySlotProjection, ...]
    benign_regressions: tuple[SecuritySlotProjection, ...]
    security_improvements: tuple[SecuritySlotProjection, ...]
    certainty_regressions: tuple[SecuritySlotProjection, ...]
    newly_identified_failures: tuple[SecuritySlotProjection, ...]
    evaluation_improvements: tuple[SecuritySlotProjection, ...]
    unchanged_count: int
    not_comparable_count: int
    comparability_warnings: tuple[tuple[str, tuple[str, ...]], ...]


def _verdict_status(verdict: EvaluationVerdict) -> SecurityCaseStatus:
    status = _VERDICT_STATUS.get(verdict)
    if status is None:
        raise SecurityProjectionError(f"unexpected persisted verdict: {verdict}")
    return status


def _security_block(result: EvaluationResult) -> Mapping[str, object] | None:
    """读取 result metadata 中的 security block；缺失返回 None，畸形 fail closed。"""
    evaluator_meta = result.metadata.get("evaluator")
    if evaluator_meta is None:
        return None
    if not isinstance(evaluator_meta, Mapping):
        raise SecurityProjectionError("invalid persisted evaluator metadata")
    block = evaluator_meta.get("security")
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise SecurityProjectionError("invalid persisted security metadata")
    return block


def _behavior_findings(block: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    findings = block.get("behavior_findings")
    if findings is None:
        return ()
    if not isinstance(findings, (list, tuple)):
        raise SecurityProjectionError("invalid persisted behavior findings")
    validated: list[Mapping[str, object]] = []
    for item in findings:
        if not isinstance(item, Mapping):
            raise SecurityProjectionError("invalid persisted behavior finding")
        reason_code = item.get("reason_code")
        if not isinstance(reason_code, str) or not reason_code:
            raise SecurityProjectionError("invalid persisted behavior finding reason_code")
        validated.append(item)
    return tuple(validated)


def _finding_reason_codes(findings: tuple[Mapping[str, object], ...]) -> tuple[str, ...]:
    return tuple(str(item["reason_code"]) for item in findings)


def _ref_values(findings: tuple[Mapping[str, object], ...], key: str) -> frozenset[str]:
    values: set[str] = set()
    for item in findings:
        ref = item.get(key)
        if ref is None:
            continue
        if not isinstance(ref, Mapping):
            raise SecurityProjectionError(f"invalid persisted behavior finding {key}")
        opaque = ref.get("opaque_value")
        if not isinstance(opaque, str) or not opaque:
            raise SecurityProjectionError(f"invalid persisted behavior finding {key} value")
        values.add(opaque)
    return frozenset(values)


def _index_security_results(
    results: Sequence[EvaluationResult],
    label: str,
    known_slots: set[tuple[str, str]],
    evaluator_id: str,
) -> dict[tuple[str, str], EvaluationResult]:
    indexed: dict[tuple[str, str], EvaluationResult] = {}
    for result in results:
        if result.evaluator_id != evaluator_id:
            continue
        key = (result.case_id, result.case_version)
        if key not in known_slots:
            raise SecurityProjectionError(f"{label} result references unknown security case {key}")
        if key in indexed:
            raise SecurityProjectionError(f"{label} has multiple {evaluator_id} results for slot {key}")
        indexed[key] = result
    return indexed


def _kind_counters(entries: Sequence[SecurityCaseEntry]) -> SecurityKindCounters:
    statuses = [entry.status for entry in entries]
    return SecurityKindCounters(
        total=len(statuses),
        passed=sum(status is SecurityCaseStatus.PASS for status in statuses),
        failed=sum(status is SecurityCaseStatus.FAIL for status in statuses),
        inconclusive=sum(status is SecurityCaseStatus.INCONCLUSIVE for status in statuses),
        not_evaluated=sum(status is SecurityCaseStatus.NOT_EVALUATED for status in statuses),
        not_mapped=sum(status is SecurityCaseStatus.NOT_MAPPED for status in statuses),
    )


def _grouped_counters(
    entries: Sequence[SecurityCaseEntry],
    fact_by_slot: Mapping[tuple[str, str], SecurityCaseFacts],
    attribute: str,
) -> tuple[tuple[str, SecurityKindCounters], ...]:
    groups: dict[str, list[SecurityCaseEntry]] = {}
    for entry in entries:
        value = getattr(fact_by_slot[(entry.case_id, entry.case_version)], attribute)
        if value is None:
            continue
        groups.setdefault(value, []).append(entry)
    return tuple(
        (name, _kind_counters(group)) for name, group in sorted(groups.items())
    )


def build_security_run_summary(
    *,
    run_id: UUID,
    dataset_id: str,
    dataset_version: str,
    suite_id: str,
    suite_version: str,
    execution_target_id: str,
    execution_target_kind: str,
    facts: Sequence[SecurityCaseFacts],
    results: Sequence[EvaluationResult],
    unmapped: Sequence[UnmappedSecurityCaseRef] = (),
    not_evaluated_reasons: Mapping[tuple[str, str], str] | None = None,
    evaluator_id: str = SECURITY_EVALUATOR_ID,
) -> SecurityRunSummary:
    """从 dataset 权威 facts + persisted Results 构建确定性 Security Run Summary。

    只消费持久化事实：verdict 来自 ``EvaluationResult``，taxonomy 来自 ``facts``，
    mapping gap 来自 ``unmapped``。不调用 Judge / Agent / Retrieval。
    """
    fact_by_slot: dict[tuple[str, str], SecurityCaseFacts] = {}
    for fact in facts:
        key = (fact.case_id, fact.case_version)
        if key in fact_by_slot:
            raise SecurityProjectionError(f"duplicate security case facts for slot {key}")
        fact_by_slot[key] = fact
    unmapped_index: dict[tuple[str, str], str] = {}
    for item in unmapped:
        key = (item.case_id, item.case_version)
        if key not in fact_by_slot:
            raise SecurityProjectionError(f"unmapped case {key} is outside the selected facts")
        if key in unmapped_index:
            raise SecurityProjectionError(f"duplicate unmapped case ref for slot {key}")
        unmapped_index[key] = item.gap_reason
    reasons = dict(not_evaluated_reasons or {})
    result_by_slot = _index_security_results(results, "run", set(fact_by_slot), evaluator_id)

    entries: list[SecurityCaseEntry] = []
    for key in sorted(fact_by_slot):
        fact = fact_by_slot[key]
        unmapped_reason = unmapped_index.get(key)
        if unmapped_reason is not None:
            entries.append(
                SecurityCaseEntry(
                    case_id=fact.case_id,
                    case_version=fact.case_version,
                    case_kind=fact.case_kind,
                    attack_type=fact.attack_type,
                    attack_source=fact.attack_source,
                    severity=fact.severity,
                    status=SecurityCaseStatus.NOT_MAPPED,
                    status_reason=unmapped_reason,
                )
            )
            continue
        result = result_by_slot.get(key)
        if result is None:
            entries.append(
                SecurityCaseEntry(
                    case_id=fact.case_id,
                    case_version=fact.case_version,
                    case_kind=fact.case_kind,
                    attack_type=fact.attack_type,
                    attack_source=fact.attack_source,
                    severity=fact.severity,
                    status=SecurityCaseStatus.NOT_EVALUATED,
                    status_reason=reasons.get(key, STATUS_REASON_EVALUATION_MISSING),
                )
            )
            continue
        block = _security_block(result)
        findings = _behavior_findings(block) if block is not None else ()
        entries.append(
            SecurityCaseEntry(
                case_id=fact.case_id,
                case_version=fact.case_version,
                case_kind=fact.case_kind,
                attack_type=fact.attack_type,
                attack_source=fact.attack_source,
                severity=fact.severity,
                status=_verdict_status(result.verdict),
                evaluator_id=result.evaluator_id,
                result_id=result.result_id,
                attempt_id=result.attempt_id,
                reason_codes=_finding_reason_codes(findings),
            )
        )

    attack_entries = [entry for entry in entries if entry.case_kind == _ATTACK_KIND]
    benign_entries = [entry for entry in entries if entry.case_kind == _BENIGN_KIND]

    gap_cases: dict[str, set[str]] = {}
    for entry in entries:
        if entry.status is SecurityCaseStatus.NOT_MAPPED:
            gap_cases.setdefault(str(entry.status_reason), set()).add(entry.case_id)
        elif SECURITY_REASON_EVIDENCE_UNSUPPORTED in entry.reason_codes:
            gap_cases.setdefault(SECURITY_REASON_EVIDENCE_UNSUPPORTED, set()).add(entry.case_id)

    reason_counter: Counter[str] = Counter()
    for entry in entries:
        reason_counter.update(entry.reason_codes)

    critical_failing = tuple(
        entry.case_id
        for entry in entries
        if entry.severity == _CRITICAL_SEVERITY and entry.status is SecurityCaseStatus.FAIL
    )
    critical_inconclusive = tuple(
        entry.case_id
        for entry in entries
        if entry.severity == _CRITICAL_SEVERITY and entry.status is SecurityCaseStatus.INCONCLUSIVE
    )

    return SecurityRunSummary(
        run_id=run_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        suite_id=suite_id,
        suite_version=suite_version,
        execution_target_id=execution_target_id,
        execution_target_kind=execution_target_kind,
        total_cases=len(entries),
        evaluated_cases=sum(entry.status in _EVALUATED_STATUSES for entry in entries),
        not_evaluated_cases=sum(entry.status is SecurityCaseStatus.NOT_EVALUATED for entry in entries),
        not_mapped_cases=sum(entry.status is SecurityCaseStatus.NOT_MAPPED for entry in entries),
        attack=_kind_counters(attack_entries),
        benign=_kind_counters(benign_entries),
        by_attack_type=_grouped_counters(attack_entries, fact_by_slot, "attack_type"),
        by_attack_source=_grouped_counters(attack_entries, fact_by_slot, "attack_source"),
        by_severity=_grouped_counters(attack_entries, fact_by_slot, "severity"),
        critical_failing_cases=critical_failing,
        critical_inconclusive_cases=critical_inconclusive,
        top_reason_codes=tuple(sorted(reason_counter.items(), key=lambda item: (-item[1], item[0]))),
        contract_gaps=tuple(
            SecurityContractGap(category=category, cases=tuple(sorted(cases)))
            for category, cases in sorted(gap_cases.items())
        ),
        entries=tuple(entries),
    )


def _classify_transition(
    baseline_verdict: EvaluationVerdict,
    candidate_verdict: EvaluationVerdict,
    case_kind: str | None,
) -> SecurityTransitionClass:
    for verdict in (baseline_verdict, candidate_verdict):
        if verdict not in _VERDICT_STATUS:
            raise SecurityProjectionError(f"unexpected persisted verdict: {verdict}")
    if baseline_verdict is EvaluationVerdict.PASS and candidate_verdict is EvaluationVerdict.FAIL:
        if case_kind == _BENIGN_KIND:
            return SecurityTransitionClass.OVER_REFUSAL_REGRESSION
        return SecurityTransitionClass.SECURITY_REGRESSION
    if baseline_verdict is EvaluationVerdict.FAIL and candidate_verdict is EvaluationVerdict.PASS:
        return SecurityTransitionClass.SECURITY_IMPROVEMENT
    if (
        candidate_verdict is EvaluationVerdict.INCONCLUSIVE
        and baseline_verdict in {EvaluationVerdict.PASS, EvaluationVerdict.FAIL}
    ):
        return SecurityTransitionClass.CERTAINTY_REGRESSION
    if baseline_verdict is EvaluationVerdict.INCONCLUSIVE and candidate_verdict is EvaluationVerdict.PASS:
        return SecurityTransitionClass.EVALUATION_IMPROVEMENT
    if baseline_verdict is EvaluationVerdict.INCONCLUSIVE and candidate_verdict is EvaluationVerdict.FAIL:
        return SecurityTransitionClass.NEWLY_IDENTIFIED_FAILURE
    return SecurityTransitionClass.UNCHANGED


def _slot_case_kind(result: EvaluationResult | None) -> str | None:
    if result is None:
        return None
    block = _security_block(result)
    if block is None:
        return None
    kind = block.get("case_kind")
    if kind is not None and not isinstance(kind, str):
        raise SecurityProjectionError("invalid persisted security case_kind")
    return kind


def build_security_comparison_projection(
    *,
    comparison: EvaluationRunComparison,
    baseline_results: Sequence[EvaluationResult],
    candidate_results: Sequence[EvaluationResult],
    evaluator_id: str = SECURITY_EVALUATOR_ID,
) -> SecurityComparisonProjection:
    """在既有 ``EvaluationRunComparison`` 上构建 security transition 投影。

    对齐与 eligibility 完全复用 generic comparison 事实；本函数只补充：

    - INCONCLUSIVE 显式 transition 语义（generic 层为 NOT_COMPARABLE/inconclusive_result）；
    - per-behavior judge prompt_ref v1/v2 差异 → NOT_COMPARABLE + comparability warning；
    - judge_model_ref 差异 → warning only（model alias 不能证明 immutable weights）；
    - dataset/suite version 漂移 → comparability warning（版本差异由 alignment key 自然隔离）。
    """
    known_slots = {(item.case_id, item.case_version) for item in comparison.comparisons}
    baseline_slots = _index_security_results(baseline_results, "baseline", known_slots, evaluator_id)
    candidate_slots = _index_security_results(candidate_results, "candidate", known_slots, evaluator_id)

    dataset_version_changed = (
        comparison.baseline_provenance.dataset_version != comparison.candidate_provenance.dataset_version
    )
    suite_version_changed = (
        comparison.baseline_provenance.suite_version != comparison.candidate_provenance.suite_version
    )

    slots: list[SecuritySlotProjection] = []
    for item in comparison.comparisons:
        key = (item.case_id, item.case_version)
        baseline = baseline_slots.get(key)
        candidate = candidate_slots.get(key)
        if baseline is None and candidate is None:
            continue
        warnings: list[str] = []
        if baseline is None or candidate is None:
            slots.append(
                SecuritySlotProjection(
                    case_id=item.case_id,
                    case_version=item.case_version,
                    evaluator_id=item.evaluator_id,
                    evaluator_version=item.evaluator_version,
                    classification=SecurityTransitionClass.NOT_COMPARABLE,
                    detail=item.reason.value,
                    baseline_result_id=baseline.result_id if baseline else None,
                    candidate_result_id=candidate.result_id if candidate else None,
                    baseline_score=baseline.score if baseline else None,
                    candidate_score=candidate.score if candidate else None,
                    case_kind=_slot_case_kind(baseline or candidate),
                )
            )
            continue
        if baseline.config_ref != candidate.config_ref:
            slots.append(_not_comparable_slot(item, baseline, candidate, "evaluator_config_mismatch"))
            continue
        baseline_block = _security_block(baseline)
        candidate_block = _security_block(candidate)
        baseline_findings = _behavior_findings(baseline_block) if baseline_block is not None else ()
        candidate_findings = _behavior_findings(candidate_block) if candidate_block is not None else ()
        if _ref_values(baseline_findings, "prompt_ref") != _ref_values(candidate_findings, "prompt_ref"):
            warnings.append(WARNING_JUDGE_PROMPT_CHANGED)
            slots.append(
                _not_comparable_slot(
                    item, baseline, candidate, "judge_prompt_changed", warnings=tuple(warnings)
                )
            )
            continue
        if _ref_values(baseline_findings, "judge_model_ref") != _ref_values(
            candidate_findings, "judge_model_ref"
        ):
            warnings.append(WARNING_JUDGE_MODEL_CHANGED)
        if dataset_version_changed:
            warnings.append(WARNING_DATASET_VERSION_CHANGED)
        slots.append(
            SecuritySlotProjection(
                case_id=item.case_id,
                case_version=item.case_version,
                evaluator_id=item.evaluator_id,
                evaluator_version=item.evaluator_version,
                classification=_classify_transition(baseline.verdict, candidate.verdict, _slot_case_kind(baseline)),
                detail=item.reason.value,
                warnings=tuple(warnings),
                baseline_result_id=baseline.result_id,
                candidate_result_id=candidate.result_id,
                baseline_verdict=baseline.verdict.value,
                candidate_verdict=candidate.verdict.value,
                baseline_score=baseline.score,
                candidate_score=candidate.score,
                case_kind=_slot_case_kind(baseline),
            )
        )

    def _subset(class_: SecurityTransitionClass) -> tuple[SecuritySlotProjection, ...]:
        return tuple(slot for slot in slots if slot.classification is class_)

    warning_map: dict[str, set[str]] = {}
    for slot in slots:
        for code in slot.warnings:
            warning_map.setdefault(code, set()).add(slot.case_id)
    if dataset_version_changed:
        warning_map.setdefault(WARNING_DATASET_VERSION_CHANGED, set()).update(
            slot.case_id for slot in slots
        )

    return SecurityComparisonProjection(
        project_id=comparison.project_id,
        baseline_run_id=comparison.baseline_run_id,
        candidate_run_id=comparison.candidate_run_id,
        dataset_version_changed=dataset_version_changed,
        suite_version_changed=suite_version_changed,
        slots=tuple(slots),
        security_regressions=_subset(SecurityTransitionClass.SECURITY_REGRESSION),
        benign_regressions=_subset(SecurityTransitionClass.OVER_REFUSAL_REGRESSION),
        security_improvements=_subset(SecurityTransitionClass.SECURITY_IMPROVEMENT),
        certainty_regressions=_subset(SecurityTransitionClass.CERTAINTY_REGRESSION),
        newly_identified_failures=_subset(SecurityTransitionClass.NEWLY_IDENTIFIED_FAILURE),
        evaluation_improvements=_subset(SecurityTransitionClass.EVALUATION_IMPROVEMENT),
        unchanged_count=sum(slot.classification is SecurityTransitionClass.UNCHANGED for slot in slots),
        not_comparable_count=sum(slot.classification is SecurityTransitionClass.NOT_COMPARABLE for slot in slots),
        comparability_warnings=tuple(
            (code, tuple(sorted(cases))) for code, cases in sorted(warning_map.items())
        ),
    )


def _not_comparable_slot(
    item: AlignedResultComparison,
    baseline: EvaluationResult,
    candidate: EvaluationResult,
    detail: str,
    *,
    warnings: tuple[str, ...] = (),
) -> SecuritySlotProjection:
    return SecuritySlotProjection(
        case_id=item.case_id,
        case_version=item.case_version,
        evaluator_id=item.evaluator_id,
        evaluator_version=item.evaluator_version,
        classification=SecurityTransitionClass.NOT_COMPARABLE,
        detail=detail,
        warnings=warnings,
        baseline_result_id=baseline.result_id,
        candidate_result_id=candidate.result_id,
        baseline_verdict=baseline.verdict.value,
        candidate_verdict=candidate.verdict.value,
        baseline_score=baseline.score,
        candidate_score=candidate.score,
        case_kind=_slot_case_kind(baseline),
    )


__all__ = [
    "STATUS_REASON_EVALUATION_MISSING",
    "SECURITY_REASON_EVIDENCE_UNSUPPORTED",
    "SECURITY_EVALUATOR_ID",
    "SecurityCaseEntry",
    "SecurityCaseFacts",
    "SecurityCaseStatus",
    "SecurityComparisonProjection",
    "SecurityContractGap",
    "SecurityKindCounters",
    "SecurityProjectionError",
    "SecurityRunSummary",
    "SecuritySlotProjection",
    "SecurityTransitionClass",
    "UnmappedSecurityCaseRef",
    "WARNING_DATASET_VERSION_CHANGED",
    "WARNING_JUDGE_MODEL_CHANGED",
    "WARNING_JUDGE_PROMPT_CHANGED",
    "build_security_comparison_projection",
    "build_security_run_summary",
]
