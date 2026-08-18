"""Baseline vs Candidate EvaluationRun 的最小 Application comparison。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from app.core.evaluation.comparison import (
    AlignedResultComparison,
    ComparisonReason,
    EvaluationRunComparison,
    RegressionClassification,
    ResultAlignmentAmbiguous,
    RunComparisonProvenance,
    RunsNotComparable,
)
from app.core.evaluation.results import EvaluationResult, EvaluationVerdict
from app.core.evaluation.run_attempts import EvaluationRun, RunStatus
from app.services.evaluation.persistence import EvaluationPersistenceService

AlignmentKey = tuple[str, str, str, str]
"""跨 Run 对齐键：(case_id, case_version, evaluator_id, evaluator_version)。

run_id / attempt_id / execution_request_id / output_artifact_ref 不得进入该键，
因为 Baseline 与 Candidate 天然属于不同 Run / Attempt。
"""


class EvaluationComparisonService:
    """以两个 COMPLETED EvaluationRun 为输入的纯 Application comparison。"""

    def __init__(self, persistence: EvaluationPersistenceService) -> None:
        self._persistence = persistence

    async def compare_runs(
        self,
        project_id: UUID,
        baseline_run_id: UUID,
        candidate_run_id: UUID,
    ) -> EvaluationRunComparison:
        """对齐并分类两个 Run 的全部 result slots；所有读取按 caller project_id 隔离。"""
        baseline = await self._persistence.get_run(project_id, baseline_run_id)
        candidate = await self._persistence.get_run(project_id, candidate_run_id)
        self._validate_eligibility(baseline, candidate)
        baseline_results = await self._persistence.list_results(project_id, baseline_run_id)
        candidate_results = await self._persistence.list_results(project_id, candidate_run_id)
        baseline_slots = self._index_results(baseline_results, "baseline")
        candidate_slots = self._index_results(candidate_results, "candidate")
        semantics = self._score_semantics(baseline, candidate)
        comparisons = tuple(
            self._compare_slot(key, baseline_slots, candidate_slots, semantics)
            for key in sorted(set(baseline_slots) | set(candidate_slots))
        )
        return EvaluationRunComparison(
            project_id=project_id,
            baseline_run_id=baseline_run_id,
            candidate_run_id=candidate_run_id,
            baseline_provenance=self._provenance(baseline),
            candidate_provenance=self._provenance(candidate),
            comparisons=comparisons,
        )

    @staticmethod
    def _validate_eligibility(baseline: EvaluationRun, candidate: EvaluationRun) -> None:
        if baseline.run_id == candidate.run_id:
            raise RunsNotComparable("baseline and candidate run must differ")
        for label, run in (("baseline", baseline), ("candidate", candidate)):
            if run.status is not RunStatus.COMPLETED:
                raise RunsNotComparable(f"{label} run must be COMPLETED, got {run.status.value}")
        if baseline.project_id != candidate.project_id:
            raise RunsNotComparable("baseline and candidate runs belong to different projects")
        pairs = (
            ("dataset", str(baseline.dataset_snapshot["dataset_id"]), str(candidate.dataset_snapshot["dataset_id"])),
            ("suite", str(baseline.suite_snapshot["suite_id"]), str(candidate.suite_snapshot["suite_id"])),
            ("execution target", baseline.execution_target_ref.target_id, candidate.execution_target_ref.target_id),
        )
        for label, left, right in pairs:
            if left != right:
                raise RunsNotComparable(f"{label} identity mismatch: {left} != {right}")

    @staticmethod
    def _index_results(
        results: tuple[EvaluationResult, ...],
        label: str,
    ) -> dict[AlignmentKey, EvaluationResult]:
        """按跨 Run 对齐键索引结果；同一 Run 内重复键 fail closed，不静默覆盖。"""
        slots: dict[AlignmentKey, EvaluationResult] = {}
        for result in results:
            key = (result.case_id, result.case_version, result.evaluator_id, result.evaluator_version)
            if key in slots:
                raise ResultAlignmentAmbiguous(f"{label} run has multiple results for slot {key}")
            slots[key] = result
        return slots

    @staticmethod
    def _score_semantics(
        baseline: EvaluationRun,
        candidate: EvaluationRun,
    ) -> dict[tuple[str, str], tuple[str | None, float | None]]:
        """从 suite snapshot 提取 (evaluator_id, version) -> (score_direction, tolerance)。"""
        semantics: dict[tuple[str, str], tuple[str | None, float | None]] = {}
        for run in (baseline, candidate):
            for item in run.suite_snapshot["evaluators"]:
                identity = (str(item["evaluator_id"]), str(item["evaluator_version"]))
                semantics.setdefault(
                    identity,
                    (item.get("score_direction"), item.get("comparison_tolerance")),
                )
        return semantics

    def _compare_slot(
        self,
        key: AlignmentKey,
        baseline_slots: Mapping[AlignmentKey, EvaluationResult],
        candidate_slots: Mapping[AlignmentKey, EvaluationResult],
        semantics: Mapping[tuple[str, str], tuple[str | None, float | None]],
    ) -> AlignedResultComparison:
        case_id, case_version, evaluator_id, evaluator_version = key
        baseline = baseline_slots.get(key)
        candidate = candidate_slots.get(key)
        if baseline is None:
            return AlignedResultComparison(
                case_id=case_id,
                case_version=case_version,
                evaluator_id=evaluator_id,
                evaluator_version=evaluator_version,
                candidate_result_id=candidate.result_id,
                classification=RegressionClassification.NOT_COMPARABLE,
                reason=ComparisonReason.BASELINE_MISSING,
            )
        if candidate is None:
            return AlignedResultComparison(
                case_id=case_id,
                case_version=case_version,
                evaluator_id=evaluator_id,
                evaluator_version=evaluator_version,
                baseline_result_id=baseline.result_id,
                classification=RegressionClassification.NOT_COMPARABLE,
                reason=ComparisonReason.CANDIDATE_MISSING,
            )
        if baseline.config_ref != candidate.config_ref or baseline.prompt_ref != candidate.prompt_ref:
            return AlignedResultComparison(
                case_id=case_id,
                case_version=case_version,
                evaluator_id=evaluator_id,
                evaluator_version=evaluator_version,
                baseline_result_id=baseline.result_id,
                candidate_result_id=candidate.result_id,
                classification=RegressionClassification.NOT_COMPARABLE,
                reason=ComparisonReason.EVALUATOR_CONFIG_MISMATCH,
            )
        if (
            EvaluationVerdict.INCONCLUSIVE in (baseline.verdict, candidate.verdict)
            or EvaluationVerdict.ERROR in (baseline.verdict, candidate.verdict)
        ):
            return AlignedResultComparison(
                case_id=case_id,
                case_version=case_version,
                evaluator_id=evaluator_id,
                evaluator_version=evaluator_version,
                baseline_result_id=baseline.result_id,
                candidate_result_id=candidate.result_id,
                classification=RegressionClassification.NOT_COMPARABLE,
                reason=ComparisonReason.INCONCLUSIVE_RESULT,
            )
        classification, reason = self._classify(baseline.verdict, candidate.verdict)
        baseline_score, candidate_score, score_delta, score_regressed = self._score_evidence(
            (evaluator_id, evaluator_version), baseline, candidate, semantics
        )
        return AlignedResultComparison(
            case_id=case_id,
            case_version=case_version,
            evaluator_id=evaluator_id,
            evaluator_version=evaluator_version,
            baseline_result_id=baseline.result_id,
            candidate_result_id=candidate.result_id,
            classification=classification,
            reason=reason,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            score_delta=score_delta,
            score_regressed=score_regressed,
        )

    @staticmethod
    def _classify(
        baseline_verdict: EvaluationVerdict,
        candidate_verdict: EvaluationVerdict,
    ) -> tuple[RegressionClassification, ComparisonReason]:
        if baseline_verdict is EvaluationVerdict.PASS and candidate_verdict is EvaluationVerdict.FAIL:
            return RegressionClassification.REGRESSION, ComparisonReason.VERDICT_REGRESSED
        if baseline_verdict is EvaluationVerdict.FAIL and candidate_verdict is EvaluationVerdict.PASS:
            return RegressionClassification.IMPROVEMENT, ComparisonReason.VERDICT_IMPROVED
        return RegressionClassification.UNCHANGED, ComparisonReason.VERDICT_UNCHANGED

    @staticmethod
    def _score_evidence(
        evaluator_identity: tuple[str, str],
        baseline: EvaluationResult,
        candidate: EvaluationResult,
        semantics: Mapping[tuple[str, str], tuple[str | None, float | None]],
    ) -> tuple[float | None, float | None, float | None, bool | None]:
        """Score 只做附加 evidence；方向/容差缺失时不生成 score_regressed。"""
        baseline_score = baseline.score
        candidate_score = candidate.score
        score_delta = None
        if baseline_score is not None and candidate_score is not None:
            score_delta = candidate_score - baseline_score
        direction, tolerance = semantics.get(evaluator_identity, (None, None))
        score_regressed = None
        if baseline_score is not None and candidate_score is not None and direction in {
            "HIGHER_IS_BETTER",
            "LOWER_IS_BETTER",
        }:
            margin = 0.0 if tolerance is None else tolerance
            if direction == "HIGHER_IS_BETTER":
                score_regressed = candidate_score < baseline_score - margin
            else:
                score_regressed = candidate_score > baseline_score + margin
        return baseline_score, candidate_score, score_delta, score_regressed

    @staticmethod
    def _provenance(run: EvaluationRun) -> RunComparisonProvenance:
        target = run.execution_target_ref
        return RunComparisonProvenance(
            dataset_id=str(run.dataset_snapshot["dataset_id"]),
            dataset_version=run.dataset_ref.opaque_value,
            suite_id=str(run.suite_snapshot["suite_id"]),
            suite_version=run.suite_ref.opaque_value,
            execution_target_id=target.target_id,
            execution_target_kind=target.target_kind,
            target_version_ref=target.target_version_ref,
        )
