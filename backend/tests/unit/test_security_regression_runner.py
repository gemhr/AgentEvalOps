"""WP6 Prompt Injection Regression Runner —— planner / summary / comparison projection 单元测试。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.evaluation import (
    SECURITY_EVALUATOR_ID,
    ArtifactRef,
    CaseVersionRef,
    EvaluationDataset,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationVerdict,
    EvaluatorKind,
    EvaluatorSpec,
    ProvenanceCompleteness,
    ScoreDirection,
    SecurityCaseFacts,
    SecurityCaseStatus,
    SecurityProjectionError,
    SecurityTransitionClass,
    UnmappedSecurityCaseRef,
    VersionRef,
    build_security_comparison_projection,
    build_security_run_summary,
    load_dataset,
)
from app.core.evaluation.comparison import ComparisonReason
from app.core.evaluation.execution import FIXTURE_TARGET_KIND, ExecutionTargetRef
from app.core.evaluation.run_attempts import EvaluationEntityNotFound, EvaluationRun, RunStatus
from app.services.evaluation import EvaluationComparisonService
from app.services.evaluation.security_regression import (
    GAP_AGENT_MESSAGE_BOUNDARY,
    GAP_NON_QUERY_STIMULUS,
    GAP_REFERENCE_DATA_DELIVERY,
    GAP_RETRIEVED_CONTEXT_INJECTION,
    GAP_TARGET_KIND_UNSUPPORTED,
    GAP_TOOL_OUTPUT_BOUNDARY,
    GAP_WIRE_PAYLOAD_INVALID,
    LOCALAGENT_HTTP_TARGET_KIND,
    RUNNER_INFRASTRUCTURE_FAILURE,
    SecurityAttemptRecord,
    SecurityRegressionPlanError,
    SecurityRegressionService,
    SecurityRunExecutionReceipt,
    collect_not_evaluated_reasons,
    map_security_case_input,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
PROJECT_ID = UUID("20000000-0000-4000-a000-000000000002")
RUN_ID = UUID("21000000-0000-4000-a000-000000000003")
CONFIG_REF = VersionRef("judge_config", "v1")
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prompt_injection_regression.v2.json"


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #


def security_spec() -> EvaluatorSpec:
    return EvaluatorSpec(
        SECURITY_EVALUATOR_ID,
        "v1",
        EvaluatorKind.DETERMINISTIC,
        CONFIG_REF,
        ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot={
            "judge_model_ref": {"kind": "llm_model", "opaque_value": "test/judge"},
            "temperature": 0.0,
            "evaluation_timeout_seconds": 5.0,
            "max_input_chars": 10000,
        },
        score_range=(0.0, 1.0),
        prompt_ref=None,
    )


def localagent_target_ref() -> ExecutionTargetRef:
    return ExecutionTargetRef(
        target_id="localagent-coordinated-http",
        target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        target_version_ref=VersionRef("localagent_http_execution_target", "evaluation-v2"),
        capabilities=("TEXT",),
        config_ref=VersionRef("localagent_http_config", "localagent-evaluation-v2"),
    )


def fixture_target_ref() -> ExecutionTargetRef:
    return ExecutionTargetRef(
        target_id="fixture-target",
        target_kind=FIXTURE_TARGET_KIND,
        target_version_ref=VersionRef("fixture", "v1"),
        capabilities=("TEXT",),
        config_ref=VersionRef("target-config", "v1"),
    )


def fact(case_id: str, *, kind: str = "ATTACK", attack_type: str | None = None, source: str | None = None, severity: str | None = None) -> SecurityCaseFacts:
    return SecurityCaseFacts(
        case_id=case_id,
        case_version="v2",
        case_kind=kind,
        attack_type=attack_type if kind == "ATTACK" else None,
        attack_source=source if kind == "ATTACK" else None,
        severity=severity if kind == "ATTACK" else None,
    )


def finding(*, reason_code: str = "security_behavior_pass", verdict: str = "PASS", prompt: str | None = "security-ignore-untrusted-instruction.v2", model: str | None = "test/judge") -> dict[str, object]:
    return {
        "behavior": "IGNORE_UNTRUSTED_INSTRUCTION",
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": "bounded",
        "prompt_ref": {"kind": "judge_prompt", "opaque_value": prompt} if prompt else None,
        "judge_model_ref": {"kind": "llm_model", "opaque_value": model} if model else None,
        "evidence_refs": [],
    }


_SCORE_BY_VERDICT = {
    EvaluationVerdict.PASS: 1.0,
    EvaluationVerdict.FAIL: 0.0,
    EvaluationVerdict.INCONCLUSIVE: None,
}


def make_security_result(
    run_id: UUID,
    case_id: str,
    verdict: EvaluationVerdict,
    *,
    findings: tuple[dict[str, object], ...] = (),
    case_version: str = "v2",
    case_kind: str = "ATTACK",
    evaluator_id: str = SECURITY_EVALUATOR_ID,
    config_ref: VersionRef = CONFIG_REF,
    score: float | None = None,
) -> EvaluationResult:
    resolved_score = _SCORE_BY_VERDICT.get(verdict) if score is None else score
    return EvaluationResult(
        result_id=str(uuid4()),
        run_id=str(run_id),
        attempt_id=str(uuid4()),
        dataset_id="prompt-injection-regression-v2",
        dataset_version="d1",
        case_id=case_id,
        case_version=case_version,
        suite_id="sec-suite",
        suite_version="s1",
        evaluator_id=evaluator_id,
        evaluator_version="v1",
        config_ref=config_ref,
        execution_target_id="target-x",
        execution_request_id=str(uuid4()),
        verdict=verdict,
        reason="summary",
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        target_version_ref=VersionRef("fixture", "v1"),
        output_artifact_ref=ArtifactRef(f"localagent-run://{uuid4()}"),
        score=resolved_score,
        created_at=NOW,
        metadata={
            "evaluator": {
                "source_status": "SECURITY_EVALUATED",
                "security": {
                    "case_kind": case_kind,
                    "attack_type": None,
                    "attack_source": None,
                    "severity": None,
                    "expected_behaviors": [],
                    "behavior_findings": list(findings),
                },
            }
        },
    )


def serialize_spec(spec: EvaluatorSpec) -> dict[str, object]:
    return {
        "evaluator_id": spec.evaluator_id,
        "evaluator_version": spec.evaluator_version,
        "evaluator_kind": spec.evaluator_kind.value,
        "config_ref": {"kind": spec.config_ref.kind, "opaque_value": spec.config_ref.opaque_value},
        "config_snapshot": spec.config_snapshot,
        "threshold": spec.threshold,
        "score_direction": spec.score_direction.value,
        "score_range": spec.score_range,
        "comparison_tolerance": spec.comparison_tolerance,
        "prompt_ref": None,
        "required": True,
    }


def make_run(run_id: UUID, *, dataset_version: str = "v2") -> EvaluationRun:
    target = localagent_target_ref()
    policy = EvaluationPolicy()
    return EvaluationRun(
        run_id=run_id,
        project_id=PROJECT_ID,
        dataset_ref=VersionRef("DATASET", dataset_version),
        suite_ref=VersionRef("SUITE", "s1"),
        execution_target_ref=target,
        dataset_snapshot={
            "dataset_id": "prompt-injection-regression-v2",
            "version": dataset_version,
            "cases": [],
        },
        suite_snapshot={
            "suite_id": "sec-suite",
            "version": "s1",
            "created_at": NOW.isoformat(),
            "selected_cases": [],
            "evaluators": (serialize_spec(security_spec()),),
            "evaluation_policy": {
                "required_result_missing": policy.required_result_missing.value,
                "evaluator_error": policy.evaluator_error.value,
                "evaluator_inconclusive": policy.evaluator_inconclusive.value,
                "metadata": {},
            },
            "target_capability_requirements": [],
            "metadata": {},
        },
        execution_target_snapshot={
            "target_id": target.target_id,
            "target_kind": target.target_kind,
            "target_version_ref": {"kind": target.target_version_ref.kind, "opaque_value": target.target_version_ref.opaque_value},
            "config_ref": {"kind": target.config_ref.kind, "opaque_value": target.config_ref.opaque_value},
            "capabilities": list(target.capabilities),
        },
        created_at=NOW,
        status=RunStatus.COMPLETED,
        finished_at=NOW,
    )


class FakePersistence:
    def __init__(self, runs: dict[UUID, EvaluationRun], results: dict[UUID, tuple[EvaluationResult, ...]]) -> None:
        self.runs = runs
        self.results = results

    async def get_run(self, project_id: UUID, run_id: UUID) -> EvaluationRun:
        run = self.runs.get(run_id)
        if run is None or run.project_id != project_id:
            raise EvaluationEntityNotFound("run not found")
        return run

    async def list_results(self, project_id: UUID, run_id: UUID, attempt_id: UUID | None = None) -> tuple[EvaluationResult, ...]:
        return tuple(item for item in self.results.get(run_id, ()))


async def compare_pair(
    baseline_results: tuple[EvaluationResult, ...],
    candidate_results: tuple[EvaluationResult, ...],
    *,
    baseline_dataset_version: str = "v2",
    candidate_dataset_version: str = "v2",
) -> object:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {
        baseline_run_id: make_run(baseline_run_id, dataset_version=baseline_dataset_version),
        candidate_run_id: make_run(candidate_run_id, dataset_version=candidate_dataset_version),
    }
    persistence = FakePersistence(
        runs,
        {baseline_run_id: baseline_results, candidate_run_id: candidate_results},
    )
    return await EvaluationComparisonService(persistence).compare_runs(
        PROJECT_ID, baseline_run_id, candidate_run_id
    )


# --------------------------------------------------------------------------- #
# A. Execution Support Matrix / planner                                       #
# --------------------------------------------------------------------------- #


def test_map_localagent_query_only_case():
    payload, gap = map_security_case_input(
        target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        case_input={"query": "attack text"},
        attack_source="USER_INPUT",
        localagent_agent_id="core_router",
    )
    assert payload == {"agent_id": "core_router", "query": "attack text"}
    assert gap is None


@pytest.mark.parametrize(
    ("attack_source", "keys", "expected_gap"),
    [
        ("TOOL_OUTPUT", {"query", "tool_output"}, GAP_TOOL_OUTPUT_BOUNDARY),
        ("AGENT_MESSAGE", {"query", "agent_message"}, GAP_AGENT_MESSAGE_BOUNDARY),
        ("RETRIEVED_CONTEXT", {"query", "retrieved_context"}, GAP_RETRIEVED_CONTEXT_INJECTION),
        ("REFERENCE_DATA", {"query", "candidate_answer", "reference_answer"}, GAP_REFERENCE_DATA_DELIVERY),
        (None, {"query", "extra"}, GAP_NON_QUERY_STIMULUS),
    ],
)
def test_map_localagent_stimulus_cases_are_explicitly_rejected(attack_source, keys, expected_gap):
    case_input = {key: "value" for key in keys}
    payload, gap = map_security_case_input(
        target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        case_input=case_input,
        attack_source=attack_source,
        localagent_agent_id="core_router",
    )
    assert payload is None
    assert gap == expected_gap


def test_map_localagent_invalid_query_payload_is_rejected():
    payload, gap = map_security_case_input(
        target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        case_input={"query": 123},
        attack_source="USER_INPUT",
        localagent_agent_id="core_router",
    )
    assert payload is None
    assert gap == GAP_WIRE_PAYLOAD_INVALID


def test_map_fixture_passthrough_preserves_stimulus():
    case_input = {"query": "q", "tool_output": "payload"}
    payload, gap = map_security_case_input(
        target_kind=FIXTURE_TARGET_KIND,
        case_input=case_input,
        attack_source="TOOL_OUTPUT",
        localagent_agent_id="core_router",
    )
    assert payload == case_input
    assert gap is None


def test_map_unsupported_target_kind_is_explicit():
    payload, gap = map_security_case_input(
        target_kind="UNKNOWN_KIND",
        case_input={"query": "q"},
        attack_source="USER_INPUT",
        localagent_agent_id="core_router",
    )
    assert payload is None
    assert gap == GAP_TARGET_KIND_UNSUPPORTED


def test_plan_real_dataset_localagent_execution_matrix():
    dataset = load_dataset(FIXTURE)
    service = SecurityRegressionService(None)
    plan = service.plan_run(
        dataset,
        execution_target_ref=localagent_target_ref(),
        evaluator_spec=security_spec(),
        suite_id="sec-suite",
        suite_version="s1",
        timeout=timedelta(seconds=30),
        created_at=NOW,
    )

    assert len(plan.facts) == 25
    mapped_ids = [ref.case_id for ref in plan.mapped_refs]
    assert len(mapped_ids) == 15
    assert mapped_ids == [
        "sec-direct-override-001",
        "sec-direct-override-002",
        "sec-direct-override-003",
        "sec-prompt-extraction-001",
        "sec-prompt-extraction-002",
        "sec-prompt-extraction-003",
        "sec-role-confusion-001",
        "sec-role-confusion-002",
        "sec-tool-injection-001",
        "sec-benign-001",
        "sec-benign-002",
        "sec-benign-003",
        "sec-benign-004",
        "sec-benign-005",
        "sec-benign-006",
    ]

    gap_by_case = {item.case_id: item.gap_reason for item in plan.unmapped}
    assert len(gap_by_case) == 10
    assert gap_by_case["sec-indirect-rag-001"] == GAP_RETRIEVED_CONTEXT_INJECTION
    assert gap_by_case["sec-indirect-rag-002"] == GAP_RETRIEVED_CONTEXT_INJECTION
    assert gap_by_case["sec-indirect-rag-003"] == GAP_RETRIEVED_CONTEXT_INJECTION
    assert gap_by_case["sec-prompt-extraction-004"] == GAP_RETRIEVED_CONTEXT_INJECTION
    assert gap_by_case["sec-judge-injection-002"] == GAP_RETRIEVED_CONTEXT_INJECTION
    assert gap_by_case["sec-tool-injection-002"] == GAP_TOOL_OUTPUT_BOUNDARY
    assert gap_by_case["sec-cross-agent-001"] == GAP_AGENT_MESSAGE_BOUNDARY
    assert gap_by_case["sec-cross-agent-002"] == GAP_AGENT_MESSAGE_BOUNDARY
    assert gap_by_case["sec-judge-injection-001"] == GAP_REFERENCE_DATA_DELIVERY
    assert gap_by_case["sec-judge-injection-003"] == GAP_REFERENCE_DATA_DELIVERY

    ref = CaseVersionRef("sec-direct-override-001", "v2")
    projected = plan.cases[ref]
    assert dict(projected.input_payload) == {
        "agent_id": "core_router",
        "query": dataset.cases[0].input["query"],
    }
    assert projected.metadata["security_ground_truth"]["case_kind"] == "ATTACK"
    assert projected.metadata["truthfulness_label"] == "SYNTHETIC_SECURITY_REGRESSION_CASE"
    assert plan.suite.case_selection == plan.mapped_refs


def test_plan_fixture_target_maps_all_25_cases_with_passthrough_payload():
    dataset = load_dataset(FIXTURE)
    service = SecurityRegressionService(None)
    plan = service.plan_run(
        dataset,
        execution_target_ref=fixture_target_ref(),
        evaluator_spec=security_spec(),
        suite_id="sec-suite",
        suite_version="s1",
        timeout=timedelta(seconds=30),
        created_at=NOW,
    )
    assert len(plan.mapped_refs) == 25
    assert plan.unmapped == ()
    rag_case = plan.cases[CaseVersionRef("sec-indirect-rag-001", "v2")]
    assert dict(rag_case.input_payload) == dataset.cases[3].input
    assert rag_case.input_payload["retrieved_context"] == dataset.cases[3].input["retrieved_context"]


def test_plan_requires_security_evaluator_spec():
    dataset = load_dataset(FIXTURE)
    other = EvaluatorSpec(
        "generation_correctness",
        "v1",
        EvaluatorKind.LLM_JUDGE,
        CONFIG_REF,
        ScoreDirection.HIGHER_IS_BETTER,
        prompt_ref=VersionRef("judge_prompt", "llm-judge-correctness.v2"),
    )
    with pytest.raises(SecurityRegressionPlanError):
        SecurityRegressionService(None).plan_run(
            dataset,
            execution_target_ref=localagent_target_ref(),
            evaluator_spec=other,
            suite_id="s",
            suite_version="s1",
            timeout=timedelta(seconds=30),
            created_at=NOW,
        )


def test_plan_rejects_dataset_without_security_ground_truth():
    payload = {
        "dataset_schema_version": "evaluation-dataset.v1",
        "dataset_id": "legacy-dataset",
        "name": "LEGACY",
        "version": "v1",
        "cases": [
            {
                "case_id": "legacy-001",
                "name": "legacy case",
                "input": {"query": "hello"},
                "ground_truth": {
                    "retrieval": {
                        "relevant_chunks": [{"document_id": "doc-1", "chunk_id": "chunk-1"}]
                    }
                },
            }
        ],
    }
    legacy = EvaluationDataset.model_validate(payload)
    with pytest.raises(SecurityRegressionPlanError):
        SecurityRegressionService(None).plan_run(
            legacy,
            execution_target_ref=fixture_target_ref(),
            evaluator_spec=security_spec(),
            suite_id="s",
            suite_version="s1",
            timeout=timedelta(seconds=30),
            created_at=NOW,
        )


def test_plan_raises_when_no_case_maps_to_target():
    payload = {
        "dataset_schema_version": "evaluation-dataset.v2",
        "dataset_id": "rag-only",
        "name": "RAG_ONLY",
        "version": "v1",
        "cases": [
            {
                "case_id": "rag-001",
                "name": "indirect only",
                "input": {"query": "summarize", "retrieved_context": "inject"},
                "ground_truth": {
                    "security": {
                        "case_kind": "ATTACK",
                        "attack_type": "INDIRECT_CONTEXT_INJECTION",
                        "attack_source": "RETRIEVED_CONTEXT",
                        "severity": "HIGH",
                        "expected_behaviors": ["IGNORE_UNTRUSTED_INSTRUCTION"],
                    }
                },
            }
        ],
    }
    with pytest.raises(SecurityRegressionPlanError):
        SecurityRegressionService(None).plan_run(
            EvaluationDataset.model_validate(payload),
            execution_target_ref=localagent_target_ref(),
            evaluator_spec=security_spec(),
            suite_id="s",
            suite_version="s1",
            timeout=timedelta(seconds=30),
            created_at=NOW,
        )


# --------------------------------------------------------------------------- #
# B. Summary builder                                                          #
# --------------------------------------------------------------------------- #


def summary_facts() -> list[SecurityCaseFacts]:
    return [
        fact("sec-a-pass", attack_type="DIRECT_INSTRUCTION_OVERRIDE", source="USER_INPUT", severity="HIGH"),
        fact("sec-a-critical-fail", attack_type="SYSTEM_PROMPT_EXTRACTION", source="USER_INPUT", severity="CRITICAL"),
        fact("sec-a-high-fail", attack_type="ROLE_CONFUSION", source="USER_INPUT", severity="HIGH"),
        fact("sec-a-critical-inc", attack_type="UNAUTHORIZED_TOOL_INSTRUCTION", source="TOOL_OUTPUT", severity="CRITICAL"),
        fact("sec-a-missing", attack_type="INDIRECT_CONTEXT_INJECTION", source="RETRIEVED_CONTEXT", severity="MEDIUM"),
        fact("sec-a-unmapped", attack_type="CROSS_AGENT_INSTRUCTION_INJECTION", source="AGENT_MESSAGE", severity="HIGH"),
        fact("sec-benign-pass", kind="BENIGN_CONTROL"),
        fact("sec-benign-fail", kind="BENIGN_CONTROL"),
    ]


def summary_results() -> list[EvaluationResult]:
    return [
        make_security_result(RUN_ID, "sec-a-pass", EvaluationVerdict.PASS, findings=(finding(reason_code="security_behavior_pass"),)),
        make_security_result(RUN_ID, "sec-a-critical-fail", EvaluationVerdict.FAIL, findings=(finding(reason_code="security_behavior_fail", verdict="FAIL"),)),
        make_security_result(RUN_ID, "sec-a-high-fail", EvaluationVerdict.FAIL, findings=(finding(reason_code="security_behavior_fail", verdict="FAIL"),)),
        make_security_result(
            RUN_ID,
            "sec-a-critical-inc",
            EvaluationVerdict.INCONCLUSIVE,
            findings=(
                finding(reason_code="security_evidence_unsupported", verdict="INCONCLUSIVE", prompt=None, model=None),
                finding(reason_code="security_evidence_unsupported", verdict="INCONCLUSIVE", prompt=None, model=None),
            ),
        ),
        make_security_result(RUN_ID, "sec-benign-pass", EvaluationVerdict.PASS, findings=(finding(reason_code="security_behavior_pass"),), case_kind="BENIGN_CONTROL"),
        make_security_result(RUN_ID, "sec-benign-fail", EvaluationVerdict.FAIL, findings=(finding(reason_code="security_behavior_fail", verdict="FAIL"),), case_kind="BENIGN_CONTROL"),
    ]


def build_summary(results, *, unmapped=(), not_evaluated_reasons=None):
    return build_security_run_summary(
        run_id=RUN_ID,
        dataset_id="prompt-injection-regression-v2",
        dataset_version="v2",
        suite_id="sec-suite",
        suite_version="s1",
        execution_target_id="target-x",
        execution_target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        facts=summary_facts(),
        results=list(results),
        unmapped=list(unmapped),
        not_evaluated_reasons=not_evaluated_reasons or {},
    )


def test_summary_mixed_verdicts_and_coverage_counts():
    summary = build_summary(
        summary_results(),
        unmapped=(UnmappedSecurityCaseRef("sec-a-unmapped", "v2", GAP_AGENT_MESSAGE_BOUNDARY),),
        not_evaluated_reasons={("sec-a-missing", "v2"): "run_not_ready"},
    )
    assert summary.total_cases == 8
    assert summary.evaluated_cases == 6
    assert summary.not_evaluated_cases == 1
    assert summary.not_mapped_cases == 1
    assert summary.attack.total == 6
    assert summary.attack.passed == 1
    assert summary.attack.failed == 2
    assert summary.attack.inconclusive == 1
    assert summary.attack.not_evaluated == 1
    assert summary.attack.not_mapped == 1
    assert summary.benign.total == 2
    assert summary.benign.passed == 1
    assert summary.benign.failed == 1
    assert summary.benign.inconclusive == 0


def test_summary_attack_and_benign_are_independent_dimensions():
    summary = build_summary(summary_results())
    all_attack_pass_like = summary.attack
    benign = summary.benign
    assert all_attack_pass_like.failed == 2
    assert benign.failed == 1
    entry_by_id = {entry.case_id: entry for entry in summary.entries}
    assert entry_by_id["sec-benign-fail"].status is SecurityCaseStatus.FAIL
    assert entry_by_id["sec-benign-fail"].result_id is not None
    assert entry_by_id["sec-benign-fail"].attempt_id is not None
    assert entry_by_id["sec-a-critical-inc"].status is SecurityCaseStatus.INCONCLUSIVE


def test_summary_not_mapped_and_not_evaluated_are_distinct():
    summary = build_summary(
        summary_results(),
        unmapped=(UnmappedSecurityCaseRef("sec-a-unmapped", "v2", GAP_AGENT_MESSAGE_BOUNDARY),),
        not_evaluated_reasons={("sec-a-missing", "v2"): "run_not_ready"},
    )
    entry_by_id = {entry.case_id: entry for entry in summary.entries}
    unmapped_entry = entry_by_id["sec-a-unmapped"]
    not_evaluated_entry = entry_by_id["sec-a-missing"]
    assert unmapped_entry.status is SecurityCaseStatus.NOT_MAPPED
    assert unmapped_entry.status_reason == GAP_AGENT_MESSAGE_BOUNDARY
    assert unmapped_entry.result_id is None
    assert not_evaluated_entry.status is SecurityCaseStatus.NOT_EVALUATED
    assert not_evaluated_entry.status_reason == "run_not_ready"
    assert not_evaluated_entry.reason_codes == ()
    default_missing = build_summary([r for r in summary_results() if r.case_id != "sec-a-missing"])
    fallback = {entry.case_id: entry for entry in default_missing.entries}["sec-a-missing"]
    assert fallback.status_reason == "evaluation_result_missing"


def test_summary_groupings_by_type_source_severity_are_sorted():
    summary = build_summary(
        summary_results(),
        unmapped=(UnmappedSecurityCaseRef("sec-a-unmapped", "v2", GAP_AGENT_MESSAGE_BOUNDARY),),
    )
    assert [name for name, _ in summary.by_attack_type] == [
        "CROSS_AGENT_INSTRUCTION_INJECTION",
        "DIRECT_INSTRUCTION_OVERRIDE",
        "INDIRECT_CONTEXT_INJECTION",
        "ROLE_CONFUSION",
        "SYSTEM_PROMPT_EXTRACTION",
        "UNAUTHORIZED_TOOL_INSTRUCTION",
    ]
    by_type = dict(summary.by_attack_type)
    assert by_type["SYSTEM_PROMPT_EXTRACTION"].total == 1
    assert by_type["SYSTEM_PROMPT_EXTRACTION"].failed == 1
    assert [name for name, _ in summary.by_attack_source] == [
        "AGENT_MESSAGE",
        "RETRIEVED_CONTEXT",
        "TOOL_OUTPUT",
        "USER_INPUT",
    ]
    assert [name for name, _ in summary.by_severity] == ["CRITICAL", "HIGH", "MEDIUM"]
    by_severity = dict(summary.by_severity)
    assert by_severity["CRITICAL"].failed == 1
    assert by_severity["CRITICAL"].inconclusive == 1
    assert by_severity["MEDIUM"].not_evaluated == 1


def test_summary_critical_case_detection_without_release_authority():
    summary = build_summary(
        summary_results(),
        unmapped=(UnmappedSecurityCaseRef("sec-a-unmapped", "v2", GAP_AGENT_MESSAGE_BOUNDARY),),
    )
    assert summary.critical_failing_cases == ("sec-a-critical-fail",)
    assert summary.critical_inconclusive_cases == ("sec-a-critical-inc",)


def test_summary_reason_codes_sorted_by_count_then_name():
    summary = build_summary(summary_results())
    assert list(summary.top_reason_codes) == [
        ("security_behavior_fail", 3),
        ("security_behavior_pass", 2),
        ("security_evidence_unsupported", 2),
    ]


def test_summary_contract_gaps_cover_mapping_and_evidence_categories():
    summary = build_summary(
        summary_results(),
        unmapped=(UnmappedSecurityCaseRef("sec-a-unmapped", "v2", GAP_AGENT_MESSAGE_BOUNDARY),),
    )
    gaps = {gap.category: gap.cases for gap in summary.contract_gaps}
    assert gaps[GAP_AGENT_MESSAGE_BOUNDARY] == ("sec-a-unmapped",)
    assert gaps["security_evidence_unsupported"] == ("sec-a-critical-inc",)


def test_summary_stable_under_result_order_shuffle():
    results = summary_results()
    base = build_summary(
        results,
        unmapped=(UnmappedSecurityCaseRef("sec-a-unmapped", "v2", GAP_AGENT_MESSAGE_BOUNDARY),),
        not_evaluated_reasons={("sec-a-missing", "v2"): "run_not_ready"},
    )
    rng = random.Random(42)
    for _ in range(5):
        shuffled = list(results)
        rng.shuffle(shuffled)
        rebuilt = build_summary(
            shuffled,
            unmapped=(UnmappedSecurityCaseRef("sec-a-unmapped", "v2", GAP_AGENT_MESSAGE_BOUNDARY),),
            not_evaluated_reasons={("sec-a-missing", "v2"): "run_not_ready"},
        )
        assert rebuilt == base
    assert [entry.case_id for entry in base.entries] == sorted(entry.case_id for entry in base.entries)


def test_summary_fail_closed_on_duplicate_slot_results():
    duplicated = summary_results() + [summary_results()[0]]
    with pytest.raises(SecurityProjectionError):
        build_summary(duplicated)


def test_summary_fail_closed_on_unknown_case_result():
    stranger = make_security_result(RUN_ID, "sec-not-in-facts", EvaluationVerdict.PASS)
    with pytest.raises(SecurityProjectionError):
        build_summary(summary_results() + [stranger])


def test_summary_fail_closed_on_malformed_findings_metadata():
    broken = make_security_result(
        RUN_ID,
        "sec-a-pass",
        EvaluationVerdict.PASS,
        findings=({"behavior": "IGNORE_UNTRUSTED_INSTRUCTION"},),
    )
    others = [r for r in summary_results() if r.case_id != "sec-a-pass"]
    with pytest.raises(SecurityProjectionError):
        build_summary(others + [broken])


def test_summary_fail_closed_on_error_verdict():
    error_result = make_security_result(RUN_ID, "sec-a-pass", EvaluationVerdict.ERROR)
    others = [r for r in summary_results() if r.case_id != "sec-a-pass"]
    with pytest.raises(SecurityProjectionError):
        build_summary(others + [error_result])


def test_collect_not_evaluated_reasons_from_receipt():
    receipt = SecurityRunExecutionReceipt(
        run_id=RUN_ID,
        records=(
            SecurityAttemptRecord(CaseVersionRef("c1", "v1"), uuid4(), "PROGRESSED"),
            SecurityAttemptRecord(CaseVersionRef("c2", "v1"), uuid4(), "RUN_NOT_READY"),
            SecurityAttemptRecord(CaseVersionRef("c3", "v1"), uuid4(), RUNNER_INFRASTRUCTURE_FAILURE, "ValueError"),
        ),
    )
    reasons = collect_not_evaluated_reasons(receipt)
    assert reasons == {("c2", "v1"): "run_not_ready", ("c3", "v1"): RUNNER_INFRASTRUCTURE_FAILURE.lower()}


# --------------------------------------------------------------------------- #
# C. Baseline / Candidate security comparison projection                      #
# --------------------------------------------------------------------------- #


async def test_projection_attack_regression_and_improvement():
    baseline = (
        make_security_result(uuid4(), "sec-x", EvaluationVerdict.PASS, findings=(finding(),)),
        make_security_result(uuid4(), "sec-y", EvaluationVerdict.FAIL, findings=(finding(verdict="FAIL", reason_code="security_behavior_fail"),)),
    )
    candidate = (
        make_security_result(uuid4(), "sec-x", EvaluationVerdict.FAIL, findings=(finding(verdict="FAIL", reason_code="security_behavior_fail"),)),
        make_security_result(uuid4(), "sec-y", EvaluationVerdict.PASS, findings=(finding(),)),
    )
    comparison = await compare_pair(baseline, candidate)
    projection = build_security_comparison_projection(comparison=comparison, baseline_results=baseline, candidate_results=candidate)
    by_case = {slot.case_id: slot for slot in projection.slots}
    assert by_case["sec-x"].classification is SecurityTransitionClass.SECURITY_REGRESSION
    assert by_case["sec-y"].classification is SecurityTransitionClass.SECURITY_IMPROVEMENT
    assert len(projection.security_regressions) == 1
    assert len(projection.security_improvements) == 1
    assert projection.slots[0].detail == ComparisonReason.VERDICT_REGRESSED.value


async def test_projection_benign_over_refusal_regression():
    baseline = (make_security_result(uuid4(), "sec-benign", EvaluationVerdict.PASS, case_kind="BENIGN_CONTROL"),)
    candidate = (make_security_result(uuid4(), "sec-benign", EvaluationVerdict.FAIL, case_kind="BENIGN_CONTROL"),)
    comparison = await compare_pair(baseline, candidate)
    projection = build_security_comparison_projection(comparison=comparison, baseline_results=baseline, candidate_results=candidate)
    assert projection.benign_regressions[0].classification is SecurityTransitionClass.OVER_REFUSAL_REGRESSION
    assert projection.security_regressions == ()


@pytest.mark.parametrize(
    ("baseline_verdict", "candidate_verdict", "expected"),
    [
        (EvaluationVerdict.PASS, EvaluationVerdict.INCONCLUSIVE, SecurityTransitionClass.CERTAINTY_REGRESSION),
        (EvaluationVerdict.FAIL, EvaluationVerdict.INCONCLUSIVE, SecurityTransitionClass.CERTAINTY_REGRESSION),
        (EvaluationVerdict.INCONCLUSIVE, EvaluationVerdict.PASS, SecurityTransitionClass.EVALUATION_IMPROVEMENT),
        (EvaluationVerdict.INCONCLUSIVE, EvaluationVerdict.FAIL, SecurityTransitionClass.NEWLY_IDENTIFIED_FAILURE),
    ],
)
async def test_projection_inconclusive_transitions_are_explicit(baseline_verdict, candidate_verdict, expected):
    baseline = (make_security_result(uuid4(), "sec-z", baseline_verdict),)
    candidate = (make_security_result(uuid4(), "sec-z", candidate_verdict),)
    comparison = await compare_pair(baseline, candidate)
    projection = build_security_comparison_projection(comparison=comparison, baseline_results=baseline, candidate_results=candidate)
    assert projection.slots[0].classification is expected
    assert projection.slots[0].baseline_verdict == baseline_verdict.value
    assert projection.slots[0].candidate_verdict == candidate_verdict.value


async def test_projection_prompt_version_mismatch_blocks_regression_attribution():
    baseline = (
        make_security_result(
            uuid4(), "sec-v", EvaluationVerdict.PASS, findings=(finding(prompt="security-ignore-untrusted-instruction.v1"),)
        ),
    )
    candidate = (
        make_security_result(
            uuid4(), "sec-v", EvaluationVerdict.FAIL, findings=(finding(prompt="security-ignore-untrusted-instruction.v2", verdict="FAIL", reason_code="security_behavior_fail"),)
        ),
    )
    comparison = await compare_pair(baseline, candidate)
    projection = build_security_comparison_projection(comparison=comparison, baseline_results=baseline, candidate_results=candidate)
    slot = projection.slots[0]
    assert slot.classification is SecurityTransitionClass.NOT_COMPARABLE
    assert slot.detail == "judge_prompt_changed"
    assert slot.warnings == ("judge_prompt_changed",)
    assert projection.security_regressions == ()
    assert ("judge_prompt_changed", ("sec-v",)) in projection.comparability_warnings


async def test_projection_judge_model_change_warns_but_keeps_classification():
    baseline = (make_security_result(uuid4(), "sec-m", EvaluationVerdict.PASS, findings=(finding(model="judge/provider-a"),)),)
    candidate = (make_security_result(uuid4(), "sec-m", EvaluationVerdict.PASS, findings=(finding(model="judge/provider-b"),)),)
    comparison = await compare_pair(baseline, candidate)
    projection = build_security_comparison_projection(comparison=comparison, baseline_results=baseline, candidate_results=candidate)
    slot = projection.slots[0]
    assert slot.classification is SecurityTransitionClass.UNCHANGED
    assert "judge_model_changed" in slot.warnings
    assert ("judge_model_changed", ("sec-m",)) in projection.comparability_warnings


async def test_projection_dataset_version_change_never_silently_aligns():
    baseline = (make_security_result(uuid4(), "sec-d", EvaluationVerdict.PASS, case_version="vA"),)
    candidate = (make_security_result(uuid4(), "sec-d", EvaluationVerdict.FAIL, case_version="vB"),)
    comparison = await compare_pair(
        baseline,
        candidate,
        baseline_dataset_version="vA",
        candidate_dataset_version="vB",
    )
    assert comparison.comparisons[0].reason is ComparisonReason.CANDIDATE_MISSING
    projection = build_security_comparison_projection(comparison=comparison, baseline_results=baseline, candidate_results=candidate)
    assert projection.dataset_version_changed is True
    assert all(slot.classification is SecurityTransitionClass.NOT_COMPARABLE for slot in projection.slots)
    assert projection.security_regressions == ()
    assert ("dataset_version_changed", ("sec-d",)) in projection.comparability_warnings


async def test_projection_missing_side_is_not_comparable():
    baseline = (make_security_result(uuid4(), "sec-gap", EvaluationVerdict.PASS),)
    comparison = await compare_pair(baseline, ())
    projection = build_security_comparison_projection(comparison=comparison, baseline_results=baseline, candidate_results=())
    slot = projection.slots[0]
    assert slot.classification is SecurityTransitionClass.NOT_COMPARABLE
    assert slot.detail == ComparisonReason.CANDIDATE_MISSING.value
    assert projection.not_comparable_count == 1


async def test_projection_deterministic_ordering_and_non_security_skip():
    baseline = (
        make_security_result(uuid4(), "sec-z", EvaluationVerdict.PASS),
        make_security_result(uuid4(), "sec-a", EvaluationVerdict.PASS),
        make_security_result(uuid4(), "other-slot", EvaluationVerdict.PASS, evaluator_id="generation_correctness"),
    )
    candidate = (
        make_security_result(uuid4(), "sec-a", EvaluationVerdict.PASS),
        make_security_result(uuid4(), "other-slot", EvaluationVerdict.FAIL, evaluator_id="generation_correctness"),
        make_security_result(uuid4(), "sec-z", EvaluationVerdict.PASS),
    )
    comparison = await compare_pair(baseline, candidate)
    projection = build_security_comparison_projection(comparison=comparison, baseline_results=baseline, candidate_results=candidate)
    security_ids = [slot.case_id for slot in projection.slots]
    assert security_ids == ["sec-a", "sec-z"]
    assert all(slot.classification is SecurityTransitionClass.UNCHANGED for slot in projection.slots)
