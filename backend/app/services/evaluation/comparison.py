"""Baseline 与 Candidate terminal Run 的只读回归派生。"""

# ruff: noqa: D105, D415

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.evaluation.comparison import (
    AlignedResultComparison,
    AttemptAvailability,
    CaseMembership,
    ComparisonCompatibility,
    ComparisonReason,
    ExecutionSubjectSnapshot,
    EvaluationRunComparison,
    EvaluationRunReference,
    RegressionClassification,
    ProvenanceSentinel,
    ResultAlignmentAmbiguous,
    RunComparisonProvenance,
    RunsNotComparable,
)
from app.core.evaluation.references import VersionRef
from app.core.evaluation.execution import OutcomeKind
from app.core.evaluation.immutable import FrozenDict, freeze_json, json_compatible
from app.core.evaluation.results import EvaluationResult, EvaluationVerdict
from app.core.evaluation.run_attempts import TERMINAL_RUN_STATUSES, EvaluationRun, ExecutionAttempt, RunStatus
from app.services.evaluation.loop import _case_from_attempt_snapshot
from app.services.evaluation.persistence import EvaluationPersistenceService

AlignmentKey = tuple[str, str, str]
CONTRACT_VERSION = "stage11.wp3.v1"
_CONDITIONAL_REASONS = {
    ComparisonReason.CASE_VERSION_LABEL_DRIFT.value,
    ComparisonReason.TARGET_BINDING_DRIFT.value,
    ComparisonReason.SUBJECT_BINDING_DRIFT.value,
    ComparisonReason.MODEL_REVISION_UNVERIFIABLE.value,
}


class EvaluationComparisonService:
    """从冻结 Run、WP1 权威 Attempt 与 finalized Result 构建完整 Comparison。"""

    def __init__(self, persistence: EvaluationPersistenceService) -> None:
        self._persistence = persistence

    async def compare_runs(
        self,
        project_id: UUID,
        baseline_run_id: UUID,
        candidate_run_id: UUID,
        *,
        accepted_conditional_reason_codes: tuple[str, ...] = (),
    ) -> EvaluationRunComparison:
        """从两个 terminal Run 的持久化 facts 生成完整只读比较。"""
        baseline = await self._persistence.get_run(project_id, baseline_run_id)
        candidate = await self._persistence.get_run(project_id, candidate_run_id)
        self._validate_eligibility(baseline, candidate)
        accepted = tuple(sorted(set(accepted_conditional_reason_codes)))
        invalid = set(accepted) - _CONDITIONAL_REASONS
        if invalid:
            raise RunsNotComparable(f"conditional acceptance contains non-conditional reasons: {sorted(invalid)}")

        baseline_attempts = await self._persistence.list_latest_attempts(project_id, baseline_run_id)
        candidate_attempts = await self._persistence.list_latest_attempts(project_id, candidate_run_id)
        baseline_cases, baseline_evaluators = self._manifest(baseline)
        candidate_cases, candidate_evaluators = self._manifest(candidate)
        baseline_case_facts = self._case_facts(baseline_cases, baseline_attempts, "baseline")
        candidate_case_facts = self._case_facts(candidate_cases, candidate_attempts, "candidate")
        baseline_attempt_map = self._attempt_map(baseline_attempts, "baseline")
        candidate_attempt_map = self._attempt_map(candidate_attempts, "candidate")
        for label, run, attempts in (
            ("baseline", baseline, baseline_attempt_map), ("candidate", candidate, candidate_attempt_map)
        ):
            if any(item.run_id != run.run_id or item.project_id != project_id for item in attempts.values()):
                raise ResultAlignmentAmbiguous(f"{label} authoritative attempt view contains a foreign attempt")
        baseline_results = await self._persistence.list_results(project_id, baseline_run_id)
        candidate_results = await self._persistence.list_results(project_id, candidate_run_id)
        baseline_slots = self._result_slots(baseline, baseline_evaluators, baseline_attempt_map, baseline_results, "baseline")
        candidate_slots = self._result_slots(candidate, candidate_evaluators, candidate_attempt_map, candidate_results, "candidate")

        all_case_ids = sorted(set(baseline_cases) | set(candidate_cases))
        rows: list[AlignedResultComparison] = []
        for case_id in all_case_ids:
            case_membership = (
                CaseMembership.BOTH if case_id in baseline_cases and case_id in candidate_cases
                else CaseMembership.NEW_CASE if case_id in candidate_cases
                else CaseMembership.REMOVED_CASE
            )
            evaluator_ids = sorted(
                ({spec["evaluator_id"] for spec in baseline_evaluators.values()} if case_id in baseline_cases else set())
                | ({spec["evaluator_id"] for spec in candidate_evaluators.values()} if case_id in candidate_cases else set())
            )
            for evaluator_id in evaluator_ids:
                rows.append(self._compare_slot(
                    case_id,
                    evaluator_id,
                    case_membership,
                    baseline_cases.get(case_id),
                    candidate_cases.get(case_id),
                    baseline_evaluators,
                    candidate_evaluators,
                    baseline_case_facts.get(case_id),
                    candidate_case_facts.get(case_id),
                    baseline_attempt_map.get(case_id),
                    candidate_attempt_map.get(case_id),
                    baseline_slots.get((case_id, evaluator_id)),
                    candidate_slots.get((case_id, evaluator_id)),
                    baseline,
                    candidate,
                    accepted,
                ))

        baseline_reference = self._reference(
            baseline, baseline_cases, baseline_evaluators, baseline_attempt_map, baseline_results
        )
        candidate_reference = self._reference(
            candidate, candidate_cases, candidate_evaluators, candidate_attempt_map, candidate_results
        )
        computed_at = datetime.now(UTC)
        semantic = {
            "contract": CONTRACT_VERSION,
            "baseline": self._canonical(baseline_reference),
            "candidate": self._canonical(candidate_reference),
            "rows": [self._canonical(row) for row in rows],
            "accepted_conditional_reason_codes": accepted,
        }
        encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return EvaluationRunComparison(
            project_id=project_id,
            baseline_run_id=baseline_run_id,
            candidate_run_id=candidate_run_id,
            baseline_provenance=self._provenance(baseline),
            candidate_provenance=self._provenance(candidate),
            comparisons=tuple(rows),
            comparison_contract_version=CONTRACT_VERSION,
            baseline_reference=baseline_reference,
            candidate_reference=candidate_reference,
            accepted_conditional_reasons=accepted,
            computed_at=computed_at,
            semantic_digest=digest,
        )

    @staticmethod
    def _validate_eligibility(baseline: EvaluationRun, candidate: EvaluationRun) -> None:
        if baseline.run_id == candidate.run_id:
            raise RunsNotComparable("baseline and candidate run must differ")
        for label, run in (("baseline", baseline), ("candidate", candidate)):
            if run.status not in TERMINAL_RUN_STATUSES:
                raise RunsNotComparable(f"{label} run must be terminal, got {run.status.value}")
        if baseline.project_id != candidate.project_id:
            raise RunsNotComparable("baseline and candidate runs belong to different projects")
        for label, left, right in (
            ("dataset", baseline.dataset_snapshot.get("dataset_id"), candidate.dataset_snapshot.get("dataset_id")),
            ("suite", baseline.suite_snapshot.get("suite_id"), candidate.suite_snapshot.get("suite_id")),
        ):
            if left != right:
                raise RunsNotComparable(f"{label} lineage mismatch: {left} != {right}")

    @staticmethod
    def _manifest(run: EvaluationRun) -> tuple[dict[str, str], dict[tuple[str, str], Mapping[str, Any]]]:
        try:
            selected_cases = run.suite_snapshot["selected_cases"]
            evaluator_items = run.suite_snapshot["evaluators"]
            case_versions: dict[str, set[str]] = {}
            for item in selected_cases:
                case_id, version = str(item["case_id"]), str(item["version"])
                versions = case_versions.setdefault(case_id, set())
                if version in versions:
                    raise RunsNotComparable(f"duplicate selected case slot: {case_id}@{version}")
                versions.add(version)
            if any(len(versions) != 1 for versions in case_versions.values()):
                raise RunsNotComparable("AMBIGUOUS_CASE_VERSION")
            evaluators: dict[tuple[str, str], Mapping[str, Any]] = {}
            evaluator_versions: dict[str, set[str]] = {}
            for item in evaluator_items:
                identity = (str(item["evaluator_id"]), str(item["evaluator_version"]))
                if identity in evaluators:
                    raise RunsNotComparable(f"duplicate selected evaluator slot: {identity}")
                evaluators[identity] = item
                evaluator_versions.setdefault(identity[0], set()).add(identity[1])
            if any(len(versions) != 1 for versions in evaluator_versions.values()):
                raise RunsNotComparable("AMBIGUOUS_EVALUATOR_VERSION")
            cases = {case_id: next(iter(versions)) for case_id, versions in case_versions.items()}
            return cases, evaluators
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, RunsNotComparable):
                raise
            raise RunsNotComparable("frozen run manifest is malformed") from exc

    @staticmethod
    def _attempt_map(attempts: tuple[ExecutionAttempt, ...], label: str) -> dict[str, ExecutionAttempt]:
        result: dict[str, ExecutionAttempt] = {}
        for attempt in attempts:
            case_id = attempt.case_ref.case_id
            if case_id in result:
                raise ResultAlignmentAmbiguous(f"{label} authoritative attempt view contains duplicate case {case_id}")
            result[case_id] = attempt
        return result

    @staticmethod
    def _case_facts(
        cases: Mapping[str, str], attempts: tuple[ExecutionAttempt, ...], label: str
    ) -> dict[str, tuple[str, str]]:
        attempts_by_case = EvaluationComparisonService._attempt_map(attempts, label)
        facts: dict[str, tuple[str, str]] = {}
        for case_id, version in cases.items():
            attempt = attempts_by_case.get(case_id)
            if attempt is None or attempt.case_ref.version != version:
                continue
            try:
                case = _case_from_attempt_snapshot(attempt)
            except Exception as exc:
                raise RunsNotComparable(f"{label} case snapshot unavailable for {case_id}") from exc
            facts[case_id] = (case.version, case.semantic_digest)
        return facts

    @staticmethod
    def _result_slots(
        run: EvaluationRun,
        evaluators: Mapping[tuple[str, str], Mapping[str, Any]],
        attempts: Mapping[str, ExecutionAttempt],
        results: tuple[EvaluationResult, ...],
        label: str,
    ) -> dict[tuple[str, str], EvaluationResult]:
        cases = {str(item["case_id"]): str(item["version"]) for item in run.suite_snapshot["selected_cases"]}
        allowed_evaluators = {identity for identity in evaluators}
        slots: dict[tuple[str, str], EvaluationResult] = {}
        for result in results:
            if result.run_id != str(run.run_id):
                raise ResultAlignmentAmbiguous(f"{label} result belongs to a foreign run")
            if (
                result.dataset_id != str(run.dataset_snapshot["dataset_id"])
                or result.dataset_version != run.dataset_ref.opaque_value
                or result.suite_id != str(run.suite_snapshot["suite_id"])
                or result.suite_version != run.suite_ref.opaque_value
            ):
                raise ResultAlignmentAmbiguous(f"{label} result provenance does not match its frozen Run")
            if result.case_id not in cases or result.case_version != cases[result.case_id]:
                raise ResultAlignmentAmbiguous(f"{label} result is outside the frozen case manifest")
            identity = (result.evaluator_id, result.evaluator_version)
            if identity not in allowed_evaluators:
                raise ResultAlignmentAmbiguous(f"{label} result is outside the frozen evaluator manifest")
            attempt = attempts.get(result.case_id)
            if attempt is None or str(attempt.attempt_id) != result.attempt_id or attempt.case_ref.version != result.case_version:
                raise ResultAlignmentAmbiguous(f"{label} result does not match the authoritative attempt")
            if attempt.execution_outcome_kind is not OutcomeKind.SUCCESS:
                raise ResultAlignmentAmbiguous(f"{label} result is attached to a non-success attempt")
            key = (result.case_id, result.evaluator_id)
            if key in slots:
                raise ResultAlignmentAmbiguous(f"{label} has duplicate result slot {key}")
            slots[key] = result
        return slots

    def _compare_slot(
        self,
        case_id: str,
        evaluator_id: str,
        membership: CaseMembership,
        baseline_case_version: str | None,
        candidate_case_version: str | None,
        baseline_evaluators: Mapping[tuple[str, str], Mapping[str, Any]],
        candidate_evaluators: Mapping[tuple[str, str], Mapping[str, Any]],
        baseline_case_fact: tuple[str, str] | None,
        candidate_case_fact: tuple[str, str] | None,
        baseline_attempt: ExecutionAttempt | None,
        candidate_attempt: ExecutionAttempt | None,
        baseline: EvaluationResult | None,
        candidate: EvaluationResult | None,
        baseline_run: EvaluationRun,
        candidate_run: EvaluationRun,
        accepted: tuple[str, ...],
    ) -> AlignedResultComparison:
        b_spec = self._single_spec(baseline_evaluators, evaluator_id)
        c_spec = self._single_spec(candidate_evaluators, evaluator_id)
        reasons: set[str] = set()
        compatibility = ComparisonCompatibility.COMPARABLE
        if membership is not CaseMembership.BOTH:
            reasons.add(ComparisonReason.NEW_CASE.value if membership is CaseMembership.NEW_CASE
                        else ComparisonReason.REMOVED_CASE.value)
            compatibility = ComparisonCompatibility.INCOMPARABLE
        else:
            compatibility, case_reasons = self._case_compatibility(
                baseline_case_version, candidate_case_version, baseline_case_fact, candidate_case_fact
            )
            reasons.update(case_reasons)
        if b_spec is None:
            reasons.add(ComparisonReason.EVALUATOR_ADDED.value)
            compatibility = ComparisonCompatibility.INCOMPARABLE
        elif c_spec is None:
            reasons.add(ComparisonReason.EVALUATOR_REMOVED.value)
            compatibility = ComparisonCompatibility.INCOMPARABLE
        else:
            evaluator_compat, evaluator_reasons = self._evaluator_compatibility(b_spec, c_spec, baseline, candidate)
            compatibility = self._combine(compatibility, evaluator_compat)
            reasons.update(evaluator_reasons)
        target_compat, target_reasons = self._target_compatibility(baseline_run, candidate_run)
        subject_compat, subject_reasons = self._subject_compatibility(baseline_run, candidate_run)
        compatibility = self._combine(compatibility, target_compat)
        compatibility = self._combine(compatibility, subject_compat)
        reasons.update(target_reasons)
        reasons.update(subject_reasons)

        b_outcome = self._availability(baseline_attempt)
        c_outcome = self._availability(candidate_attempt)
        if b_outcome is AttemptAvailability.FAILURE:
            reasons.add(ComparisonReason.ATTEMPT_FAILURE.value)
        elif b_outcome is AttemptAvailability.TIMEOUT:
            reasons.add(ComparisonReason.ATTEMPT_TIMEOUT.value)
        elif b_outcome is AttemptAvailability.CANCELLED:
            reasons.add(ComparisonReason.ATTEMPT_CANCELLED.value)
        elif b_outcome is AttemptAvailability.OUTCOME_UNKNOWN:
            reasons.add(ComparisonReason.OUTCOME_UNKNOWN.value)
        if c_outcome is AttemptAvailability.FAILURE:
            reasons.add(ComparisonReason.ATTEMPT_FAILURE.value)
        elif c_outcome is AttemptAvailability.TIMEOUT:
            reasons.add(ComparisonReason.ATTEMPT_TIMEOUT.value)
        elif c_outcome is AttemptAvailability.CANCELLED:
            reasons.add(ComparisonReason.ATTEMPT_CANCELLED.value)
        elif c_outcome is AttemptAvailability.OUTCOME_UNKNOWN:
            reasons.add(ComparisonReason.OUTCOME_UNKNOWN.value)

        if b_spec is None or c_spec is None:
            pass
        if baseline is None and candidate is None:
            reasons.add(ComparisonReason.BOTH_RESULTS_MISSING.value)
        elif baseline is None:
            reasons.add(ComparisonReason.MISSING_BASELINE_RESULT.value)
        elif candidate is None:
            reasons.add(ComparisonReason.MISSING_CANDIDATE_RESULT.value)

        reason = self._primary_reason(reasons)
        classification = RegressionClassification.NOT_COMPARABLE
        baseline_score = candidate_score = score_delta = None
        score_regressed = None
        score_transition = None
        if baseline is not None and candidate is not None:
            b_norm = self._normalization_reason(baseline)
            c_norm = self._normalization_reason(candidate)
            if b_norm:
                reasons.add(b_norm)
            if c_norm:
                reasons.add(c_norm)
            if baseline.verdict in {EvaluationVerdict.ERROR, EvaluationVerdict.INCONCLUSIVE} or candidate.verdict in {
                EvaluationVerdict.ERROR, EvaluationVerdict.INCONCLUSIVE
            }:
                reasons.add(ComparisonReason.INCONCLUSIVE_RESULT.value)
            if baseline.provenance_completeness.value != "COMPLETE" or candidate.provenance_completeness.value != "COMPLETE":
                reasons.add(ComparisonReason.LEGACY_INSUFFICIENT_PROVENANCE.value)
            allowed = compatibility is ComparisonCompatibility.COMPARABLE or (
                compatibility is ComparisonCompatibility.CONDITIONALLY_COMPARABLE
                and reasons.intersection(_CONDITIONAL_REASONS).issubset(accepted)
            )
            if b_outcome is AttemptAvailability.SUCCESS and c_outcome is AttemptAvailability.SUCCESS and allowed:
                if b_norm or c_norm:
                    pass
                elif baseline.verdict in {EvaluationVerdict.PASS, EvaluationVerdict.FAIL} and candidate.verdict in {
                    EvaluationVerdict.PASS, EvaluationVerdict.FAIL
                }:
                    classification, reason = self._classify(baseline.verdict, candidate.verdict)
                if b_spec is not None and c_spec is not None and self._score_compatible(b_spec, c_spec):
                    if baseline.score is not None and candidate.score is not None:
                        baseline_score, candidate_score = baseline.score, candidate.score
                        score_delta = candidate_score - baseline_score
                        score_transition = (
                            "INCREASED" if score_delta > 0 else "DECREASED" if score_delta < 0 else "UNCHANGED"
                        )
                        tolerance = float(b_spec.get("comparison_tolerance") or 0.0)
                        direction = b_spec.get("score_direction")
                        if direction == "HIGHER_IS_BETTER":
                            score_regressed = score_delta < -tolerance
                        elif direction == "LOWER_IS_BETTER":
                            score_regressed = score_delta > tolerance

        reasons = set(reasons)
        reason = reason if reason is not ComparisonReason.BASELINE_MISSING or not reasons else self._primary_reason(reasons)
        if reason is not ComparisonReason.BASELINE_MISSING:
            reasons.add(reason.value)
        return AlignedResultComparison(
            case_id=case_id,
            case_version=candidate_case_version or baseline_case_version or "unknown",
            evaluator_id=evaluator_id,
            evaluator_version=(str(c_spec["evaluator_version"]) if c_spec else str(b_spec["evaluator_version"])),
            baseline_result_id=baseline.result_id if baseline else None,
            candidate_result_id=candidate.result_id if candidate else None,
            classification=classification,
            reason=reason,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            score_delta=score_delta,
            score_regressed=score_regressed,
            compatibility=compatibility,
            reason_codes=tuple(sorted(reasons)),
            case_membership=membership,
            baseline_case_version=baseline_case_version,
            candidate_case_version=candidate_case_version,
            baseline_evaluator_version=str(b_spec["evaluator_version"]) if b_spec else None,
            candidate_evaluator_version=str(c_spec["evaluator_version"]) if c_spec else None,
            baseline_expected=b_spec is not None and baseline_case_version is not None,
            candidate_expected=c_spec is not None and candidate_case_version is not None,
            baseline_required=bool(b_spec.get("required")) if b_spec else False,
            candidate_required=bool(c_spec.get("required")) if c_spec else False,
            baseline_attempt_id=str(baseline_attempt.attempt_id) if baseline_attempt else None,
            candidate_attempt_id=str(candidate_attempt.attempt_id) if candidate_attempt else None,
            baseline_attempt_outcome=b_outcome,
            candidate_attempt_outcome=c_outcome,
            score_transition=score_transition,
        )

    @staticmethod
    def _single_spec(specs: Mapping[tuple[str, str], Mapping[str, Any]], evaluator_id: str) -> Mapping[str, Any] | None:
        return next((spec for (identity, _), spec in specs.items() if identity == evaluator_id), None)

    @staticmethod
    def _case_compatibility(
        baseline_version: str | None,
        candidate_version: str | None,
        baseline_fact: tuple[str, str] | None,
        candidate_fact: tuple[str, str] | None,
    ) -> tuple[ComparisonCompatibility, set[str]]:
        if baseline_fact is None or candidate_fact is None:
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.LEGACY_INSUFFICIENT_PROVENANCE.value}
        same_content = baseline_fact[1] == candidate_fact[1]
        if baseline_version == candidate_version and same_content:
            return ComparisonCompatibility.COMPARABLE, set()
        if baseline_version == candidate_version:
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.CASE_IDENTITY_COLLISION.value}
        if same_content:
            return ComparisonCompatibility.CONDITIONALLY_COMPARABLE, {ComparisonReason.CASE_VERSION_LABEL_DRIFT.value}
        return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.CASE_CONTENT_CHANGED.value}

    @staticmethod
    def _evaluator_compatibility(
        baseline: Mapping[str, Any],
        candidate: Mapping[str, Any],
        baseline_result: EvaluationResult | None,
        candidate_result: EvaluationResult | None,
    ) -> tuple[ComparisonCompatibility, set[str]]:
        reasons: set[str] = set()
        descriptor_fields = ("result_schema_ref", "comparison_semantics", "required_artifact_kinds", "required_evidence_kinds")
        if any(field not in spec for spec in (baseline, candidate) for field in descriptor_fields):
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.INSUFFICIENT_EVALUATOR_PROVENANCE.value}
        if any(not isinstance(spec[field], (tuple, list))
               or any(not isinstance(kind, str) or not kind.strip() for kind in spec[field])
               for spec in (baseline, candidate) for field in ("required_artifact_kinds", "required_evidence_kinds")):
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.INSUFFICIENT_EVALUATOR_PROVENANCE.value}
        supported_schema = {"kind": "evaluation_result", "opaque_value": "v1"}
        if any(json_compatible(spec["result_schema_ref"]) != supported_schema
               or spec["comparison_semantics"] != "verdict_transition.v1" for spec in (baseline, candidate)):
            return ComparisonCompatibility.INCOMPARABLE, {
                ComparisonReason.EVALUATOR_CONFIG_CHANGED.value,
                ComparisonReason.INSUFFICIENT_EVALUATOR_PROVENANCE.value,
            }
        for spec, result in ((baseline, baseline_result), (candidate, candidate_result)):
            if result is not None and (
                not set(spec["required_evidence_kinds"]).issubset({ref.kind for ref in result.evidence_refs})
                or spec["required_artifact_kinds"] and (
                    result.output_artifact_ref is None
                    or result.output_artifact_ref.media_type not in spec["required_artifact_kinds"]
                )
            ):
                return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.REQUIRED_EVIDENCE_MISSING.value}
        if baseline["evaluator_version"] != candidate["evaluator_version"]:
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.EVALUATOR_VERSION_CHANGED.value}
        for field, reason in (
            ("config_ref", ComparisonReason.EVALUATOR_CONFIG_CHANGED),
            ("config_snapshot", ComparisonReason.EVALUATOR_CONFIG_CHANGED),
            ("prompt_ref", ComparisonReason.EVALUATOR_PROMPT_CHANGED),
            ("threshold", ComparisonReason.EVALUATOR_THRESHOLD_CHANGED),
            ("score_direction", ComparisonReason.EVALUATOR_DIRECTION_CHANGED),
            ("comparison_tolerance", ComparisonReason.EVALUATOR_TOLERANCE_CHANGED),
            ("score_range", ComparisonReason.EVALUATOR_CONFIG_CHANGED),
            ("evaluator_kind", ComparisonReason.EVALUATOR_CONFIG_CHANGED),
        ):
            if json_compatible(baseline.get(field)) != json_compatible(candidate.get(field)):
                reasons.add(reason.value)
        excluded = {
            "evaluator_id", "evaluator_version", "evaluator_kind", "config_ref", "config_snapshot",
            "prompt_ref", "threshold", "score_direction", "score_range", "comparison_tolerance",
            "required",
        }
        other_fields = (set(baseline) | set(candidate)) - excluded
        if any(json_compatible(baseline.get(field)) != json_compatible(candidate.get(field)) for field in other_fields):
            reasons.add(ComparisonReason.EVALUATOR_CONFIG_CHANGED.value)
        if reasons:
            reasons.add(ComparisonReason.EVALUATOR_IDENTITY_COLLISION.value)
            return ComparisonCompatibility.INCOMPARABLE, reasons
        judge_compat, judge_reasons = EvaluationComparisonService._judge_compatibility(
            baseline, candidate, baseline_result, candidate_result
        )
        reasons.update(judge_reasons)
        return judge_compat, reasons

    @staticmethod
    def _judge_compatibility(
        baseline_spec: Mapping[str, Any],
        candidate_spec: Mapping[str, Any],
        baseline_result: EvaluationResult | None,
        candidate_result: EvaluationResult | None,
    ) -> tuple[ComparisonCompatibility, set[str]]:
        baseline_models = EvaluationComparisonService._actual_judge_models(baseline_result) if baseline_result else ()
        candidate_models = EvaluationComparisonService._actual_judge_models(candidate_result) if candidate_result else ()
        baseline_prompts = EvaluationComparisonService._actual_judge_prompts(baseline_result) if baseline_result else ()
        candidate_prompts = EvaluationComparisonService._actual_judge_prompts(candidate_result) if candidate_result else ()
        judge_bound = baseline_spec.get("evaluator_kind") == "LLM_JUDGE" or bool(
            baseline_models or candidate_models or baseline_prompts or candidate_prompts
        )
        if not judge_bound:
            return ComparisonCompatibility.COMPARABLE, set()
        if baseline_result is None or candidate_result is None:
            return ComparisonCompatibility.COMPARABLE, set()
        if bool(baseline_prompts) != bool(candidate_prompts):
            return ComparisonCompatibility.INCOMPARABLE, {
                ComparisonReason.LEGACY_INSUFFICIENT_PROVENANCE.value
            }
        if baseline_prompts and set(map(json.dumps, baseline_prompts)) != set(map(json.dumps, candidate_prompts)):
            return ComparisonCompatibility.INCOMPARABLE, {
                ComparisonReason.EVALUATOR_PROMPT_CHANGED.value
            }
        if not baseline_models or not candidate_models:
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.LEGACY_INSUFFICIENT_PROVENANCE.value}
        baseline_requested = baseline_spec.get("config_snapshot", {}).get("judge_model_ref")
        candidate_requested = candidate_spec.get("config_snapshot", {}).get("judge_model_ref")
        if any(model != json_compatible(baseline_requested) for model in baseline_models) or any(
            model != json_compatible(candidate_requested) for model in candidate_models
        ):
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.JUDGE_BINDING_CHANGED.value}
        if set(map(json.dumps, baseline_models)) != set(map(json.dumps, candidate_models)):
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.JUDGE_BINDING_CHANGED.value}
        return ComparisonCompatibility.CONDITIONALLY_COMPARABLE, {
            ComparisonReason.MODEL_REVISION_UNVERIFIABLE.value
        }

    @staticmethod
    def _actual_judge_models(result: EvaluationResult) -> tuple[dict[str, str], ...]:
        evaluator = result.metadata.get("evaluator")
        if not isinstance(evaluator, Mapping):
            return ()
        direct = evaluator.get("judge_model_ref")
        if isinstance(direct, Mapping) and isinstance(direct.get("kind"), str) and isinstance(
            direct.get("opaque_value"), str
        ):
            return ({"kind": direct["kind"], "opaque_value": direct["opaque_value"]},)
        security = evaluator.get("security")
        findings = security.get("behavior_findings") if isinstance(security, Mapping) else None
        models: set[tuple[str, str]] = set()
        if isinstance(findings, (tuple, list)):
            for finding in findings:
                ref = finding.get("judge_model_ref") if isinstance(finding, Mapping) else None
                if isinstance(ref, Mapping) and isinstance(ref.get("kind"), str) and isinstance(
                    ref.get("opaque_value"), str
                ):
                    models.add((ref["kind"], ref["opaque_value"]))
        return tuple({"kind": kind, "opaque_value": value} for kind, value in sorted(models))

    @staticmethod
    def _actual_judge_prompts(result: EvaluationResult) -> tuple[dict[str, str], ...]:
        evaluator = result.metadata.get("evaluator")
        security = evaluator.get("security") if isinstance(evaluator, Mapping) else None
        findings = security.get("behavior_findings") if isinstance(security, Mapping) else None
        prompts: set[tuple[str, str]] = set()
        if isinstance(findings, (tuple, list)):
            for finding in findings:
                ref = finding.get("prompt_ref") if isinstance(finding, Mapping) else None
                if isinstance(ref, Mapping) and isinstance(ref.get("kind"), str) and isinstance(
                    ref.get("opaque_value"), str
                ):
                    prompts.add((ref["kind"], ref["opaque_value"]))
        return tuple({"kind": kind, "opaque_value": value} for kind, value in sorted(prompts))

    @staticmethod
    def _target_compatibility(
        baseline: EvaluationRun, candidate: EvaluationRun
    ) -> tuple[ComparisonCompatibility, set[str]]:
        left, right = baseline.execution_target_ref, candidate.execution_target_ref
        if left.target_kind != right.target_kind:
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.TARGET_KIND_MISMATCH.value}
        if (left.target_id, left.config_ref, left.target_version_ref) == (
            right.target_id, right.config_ref, right.target_version_ref
        ):
            return ComparisonCompatibility.COMPARABLE, set()
        # 只有已支持且相同的 wire 合同、相同配置下的 target binding 变化有证明。
        from app.adapters.evaluation.http_localagent import LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION

        supported_version = (
            VersionRef("adapter", "fixture.v1") if left.target_kind == "FIXTURE" else
            LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION if left.target_kind == "LOCALAGENT_HTTP" else None
        )
        complete = (
            supported_version is not None and left.target_version_ref == supported_version
            and left.target_version_ref == right.target_version_ref
            and left.config_ref is not None and left.config_ref == right.config_ref
        )
        if complete:
            return ComparisonCompatibility.CONDITIONALLY_COMPARABLE, {ComparisonReason.TARGET_BINDING_DRIFT.value}
        return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.TARGET_BINDING_DRIFT.value}

    @staticmethod
    def _subject_compatibility(
        baseline: EvaluationRun, candidate: EvaluationRun
    ) -> tuple[ComparisonCompatibility, set[str]]:
        left = EvaluationComparisonService._subject_snapshot(baseline)
        right = EvaluationComparisonService._subject_snapshot(candidate)
        if left.subject_kind != right.subject_kind:
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.TARGET_KIND_MISMATCH.value}
        if left.subject_kind == "FIXTURE":
            if (
                left.fixture_target_identity is ProvenanceSentinel.UNKNOWN
                or right.fixture_target_identity is ProvenanceSentinel.UNKNOWN
                or left.fixture_content_identity is ProvenanceSentinel.UNKNOWN
                or right.fixture_content_identity is ProvenanceSentinel.UNKNOWN
            ):
                return ComparisonCompatibility.INCOMPARABLE, {
                    ComparisonReason.INSUFFICIENT_SUBJECT_PROVENANCE.value
                }
            fixture_identity = (left.fixture_target_identity, left.fixture_content_identity)
            candidate_identity = (right.fixture_target_identity, right.fixture_content_identity)
            return (ComparisonCompatibility.COMPARABLE, set()) if fixture_identity == candidate_identity else (
                ComparisonCompatibility.CONDITIONALLY_COMPARABLE,
                {ComparisonReason.SUBJECT_BINDING_DRIFT.value},
            )
        left_agent, right_agent = left.agent_id, right.agent_id
        if any(value in (ProvenanceSentinel.UNKNOWN, ProvenanceSentinel.NOT_APPLICABLE)
               for value in (left_agent, right_agent, left.agent_version, right.agent_version)):
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.INSUFFICIENT_SUBJECT_PROVENANCE.value}
        if left_agent != right_agent:
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.SUBJECT_LINEAGE_MISMATCH.value}
        drift_keys = (
            "agent_version", "workflow_id", "workflow_version", "toolset_identity", "provider_binding_identity",
            "runtime_version", "deployment_environment", "run_mode", "profile",
        )
        if any(
            getattr(left, key) is ProvenanceSentinel.UNKNOWN or getattr(right, key) is ProvenanceSentinel.UNKNOWN
            for key in drift_keys
        ):
            return ComparisonCompatibility.INCOMPARABLE, {ComparisonReason.INSUFFICIENT_SUBJECT_PROVENANCE.value}
        drift = any(getattr(left, key) != getattr(right, key) for key in drift_keys)
        return (
            (ComparisonCompatibility.CONDITIONALLY_COMPARABLE, {ComparisonReason.SUBJECT_BINDING_DRIFT.value})
            if drift else (ComparisonCompatibility.COMPARABLE, set())
        )

    @staticmethod
    def _subject_snapshot(run: EvaluationRun) -> ExecutionSubjectSnapshot:
        target = run.execution_target_ref
        if target.target_kind == "FIXTURE":
            value = run.subject_ref if isinstance(run.subject_ref, Mapping) else {}
            content = value.get("fixture_content_identity")
            if not isinstance(content, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", content) is None:
                content = ProvenanceSentinel.UNKNOWN
            return ExecutionSubjectSnapshot(
                subject_kind="FIXTURE",
                agent_id=ProvenanceSentinel.NOT_APPLICABLE,
                agent_version=ProvenanceSentinel.NOT_APPLICABLE,
                workflow_id=ProvenanceSentinel.NOT_APPLICABLE,
                workflow_version=ProvenanceSentinel.NOT_APPLICABLE,
                toolset_identity=ProvenanceSentinel.NOT_APPLICABLE,
                provider_binding_identity=ProvenanceSentinel.NOT_APPLICABLE,
                runtime_version=ProvenanceSentinel.NOT_APPLICABLE,
                deployment_environment=ProvenanceSentinel.NOT_APPLICABLE,
                run_mode=ProvenanceSentinel.NOT_APPLICABLE,
                profile=ProvenanceSentinel.NOT_APPLICABLE,
                fixture_target_identity=(
                    value.get("fixture_target_identity")
                    if isinstance(value.get("fixture_target_identity"), str)
                    and value["fixture_target_identity"].strip()
                    and value["fixture_target_identity"] not in {"UNKNOWN", "NOT_APPLICABLE"}
                    else ProvenanceSentinel.UNKNOWN
                ),
                fixture_content_identity=(
                    content if isinstance(content, ProvenanceSentinel) else freeze_json(content)
                ),
            )
        values = run.subject_ref if isinstance(run.subject_ref, Mapping) else {}

        def scalar(name: str) -> str | ProvenanceSentinel:
            value = values.get(name)
            if value is None or value == "UNKNOWN":
                return ProvenanceSentinel.UNKNOWN
            if value == "NOT_APPLICABLE":
                return ProvenanceSentinel.NOT_APPLICABLE
            if not isinstance(value, str) or not value.strip():
                return ProvenanceSentinel.UNKNOWN
            return value

        def identity(name: str) -> Any:
            value = values.get(name)
            if value is None or value == "UNKNOWN":
                return ProvenanceSentinel.UNKNOWN
            if value == "NOT_APPLICABLE":
                return ProvenanceSentinel.NOT_APPLICABLE
            try:
                return freeze_json(value)
            except (TypeError, ValueError):
                return ProvenanceSentinel.UNKNOWN

        return ExecutionSubjectSnapshot(
            subject_kind=str(values.get("subject_kind", target.target_kind)),
            agent_id=scalar("agent_id"),
            agent_version=scalar("agent_version"),
            workflow_id=scalar("workflow_id"),
            workflow_version=scalar("workflow_version"),
            toolset_identity=identity("toolset_identity"),
            provider_binding_identity=identity("provider_binding_identity"),
            runtime_version=scalar("runtime_version"),
            deployment_environment=scalar("deployment_environment"),
            run_mode=scalar("run_mode"),
            profile=scalar("profile"),
        )

    @staticmethod
    def _availability(attempt: ExecutionAttempt | None) -> AttemptAvailability:
        if attempt is None or attempt.execution_outcome_kind is None:
            return AttemptAvailability.MISSING
        return {
            OutcomeKind.SUCCESS: AttemptAvailability.SUCCESS,
            OutcomeKind.FAILURE: AttemptAvailability.FAILURE,
            OutcomeKind.TIMEOUT: AttemptAvailability.TIMEOUT,
            OutcomeKind.CANCELLED: AttemptAvailability.CANCELLED,
            OutcomeKind.OUTCOME_UNKNOWN: AttemptAvailability.OUTCOME_UNKNOWN,
        }[attempt.execution_outcome_kind]

    @staticmethod
    def _normalization_reason(result: EvaluationResult) -> str | None:
        policy = result.metadata.get("policy_normalization")
        if not isinstance(policy, Mapping):
            return ComparisonReason.LEGACY_INSUFFICIENT_PROVENANCE.value
        source = policy.get("source")
        if source == "EVALUATOR_ERROR":
            return ComparisonReason.EVALUATOR_ERROR.value
        if source == "EVALUATOR_INCONCLUSIVE":
            return ComparisonReason.EVALUATOR_INCONCLUSIVE.value
        if source != "UNCHANGED":
            return ComparisonReason.LEGACY_INSUFFICIENT_PROVENANCE.value
        return None

    @staticmethod
    def _score_compatible(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
        return all(
            json_compatible(baseline.get(field)) == json_compatible(candidate.get(field))
            for field in ("result_schema_ref", "comparison_semantics", "score_direction", "score_range", "comparison_tolerance")
        )

    @staticmethod
    def _classify(
        baseline_verdict: EvaluationVerdict, candidate_verdict: EvaluationVerdict
    ) -> tuple[RegressionClassification, ComparisonReason]:
        if baseline_verdict is EvaluationVerdict.PASS and candidate_verdict is EvaluationVerdict.FAIL:
            return RegressionClassification.REGRESSION, ComparisonReason.VERDICT_REGRESSED
        if baseline_verdict is EvaluationVerdict.FAIL and candidate_verdict is EvaluationVerdict.PASS:
            return RegressionClassification.IMPROVEMENT, ComparisonReason.VERDICT_IMPROVED
        return RegressionClassification.UNCHANGED, ComparisonReason.VERDICT_UNCHANGED

    @staticmethod
    def _combine(left: ComparisonCompatibility, right: ComparisonCompatibility) -> ComparisonCompatibility:
        order = {
            ComparisonCompatibility.COMPARABLE: 0,
            ComparisonCompatibility.CONDITIONALLY_COMPARABLE: 1,
            ComparisonCompatibility.INCOMPARABLE: 2,
        }
        return left if order[left] >= order[right] else right

    @staticmethod
    def _primary_reason(reasons: set[str]) -> ComparisonReason:
        priority = (
            ComparisonReason.BOTH_RESULTS_MISSING,
            ComparisonReason.MISSING_BASELINE_RESULT,
            ComparisonReason.MISSING_CANDIDATE_RESULT,
            ComparisonReason.OUTCOME_UNKNOWN,
            ComparisonReason.ATTEMPT_TIMEOUT,
            ComparisonReason.ATTEMPT_FAILURE,
            ComparisonReason.ATTEMPT_CANCELLED,
            ComparisonReason.EVALUATOR_ERROR,
            ComparisonReason.EVALUATOR_INCONCLUSIVE,
            ComparisonReason.INCONCLUSIVE_RESULT,
            ComparisonReason.LEGACY_INSUFFICIENT_PROVENANCE,
            ComparisonReason.INSUFFICIENT_EVALUATOR_PROVENANCE,
            ComparisonReason.REQUIRED_EVIDENCE_MISSING,
            ComparisonReason.CASE_IDENTITY_COLLISION,
            ComparisonReason.CASE_CONTENT_CHANGED,
            ComparisonReason.EVALUATOR_VERSION_CHANGED,
            ComparisonReason.EVALUATOR_CONFIG_CHANGED,
            ComparisonReason.EVALUATOR_PROMPT_CHANGED,
            ComparisonReason.EVALUATOR_THRESHOLD_CHANGED,
            ComparisonReason.EVALUATOR_DIRECTION_CHANGED,
            ComparisonReason.EVALUATOR_TOLERANCE_CHANGED,
            ComparisonReason.JUDGE_BINDING_CHANGED,
            ComparisonReason.EVALUATOR_IDENTITY_COLLISION,
            ComparisonReason.TARGET_KIND_MISMATCH,
            ComparisonReason.TARGET_BINDING_DRIFT,
            ComparisonReason.SUBJECT_LINEAGE_MISMATCH,
            ComparisonReason.INSUFFICIENT_SUBJECT_PROVENANCE,
            ComparisonReason.SUBJECT_BINDING_DRIFT,
            ComparisonReason.EVALUATOR_ADDED,
            ComparisonReason.EVALUATOR_REMOVED,
            ComparisonReason.CASE_VERSION_LABEL_DRIFT,
            ComparisonReason.NEW_CASE,
            ComparisonReason.REMOVED_CASE,
        )
        for item in priority:
            if item.value in reasons:
                return item
        return ComparisonReason.BASELINE_MISSING

    def _reference(
        self,
        run: EvaluationRun,
        cases: Mapping[str, str],
        evaluators: Mapping[tuple[str, str], Mapping[str, Any]],
        attempts: Mapping[str, ExecutionAttempt],
        results: tuple[EvaluationResult, ...],
    ) -> EvaluationRunReference:
        assert run.finished_at is not None
        case_manifest = tuple(
            freeze_json({"case_id": case_id, "case_version": version,
                         "semantic_digest": self._case_facts(cases, tuple(attempts.values()), "reference").get(case_id, (None, None))[1]})
            for case_id, version in sorted(cases.items())
        )
        evaluator_manifest = tuple(freeze_json(json_compatible(item)) for _, item in sorted(evaluators.items()))
        attempt_refs = tuple(freeze_json({
            "attempt_id": str(item.attempt_id), "case_id": item.case_ref.case_id,
            "case_version": item.case_ref.version,
            "outcome": item.execution_outcome_kind.value if item.execution_outcome_kind else "MISSING",
        }) for _, item in sorted(attempts.items()))
        result_refs = tuple(freeze_json({
            "result_id": item.result_id, "attempt_id": item.attempt_id, "case_id": item.case_id,
            "case_version": item.case_version, "evaluator_id": item.evaluator_id,
            "evaluator_version": item.evaluator_version,
        }) for item in sorted(results, key=lambda value: (value.case_id, value.evaluator_id, value.result_id)))
        subject = self._subject_snapshot(run)
        return EvaluationRunReference(
            project_id=run.project_id,
            run_id=run.run_id,
            status=run.status.value,
            finished_at=run.finished_at,
            dataset_snapshot=freeze_json(json_compatible(run.dataset_snapshot)),
            suite_snapshot=freeze_json(json_compatible(run.suite_snapshot)),
            target_snapshot=freeze_json(json_compatible(run.execution_target_snapshot)),
            subject_snapshot=subject,
            case_manifest=case_manifest,
            evaluator_manifest=evaluator_manifest,
            attempt_refs=attempt_refs,
            result_refs=result_refs,
        )

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

    @classmethod
    def _canonical(cls, value: Any) -> Any:
        if is_dataclass(value):
            return {item.name: cls._canonical(getattr(value, item.name)) for item in fields(value)}
        if isinstance(value, Mapping):
            return {str(key): cls._canonical(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [cls._canonical(item) for item in value]
        return json_compatible(value)
