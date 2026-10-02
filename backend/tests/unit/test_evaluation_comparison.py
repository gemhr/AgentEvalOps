"""EvaluationComparisonService 最小 Application tests。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.core.evaluation import (
    AlignedResultComparison,
    ArtifactRef,
    ComparisonReason,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationRunComparison,
    EvaluationVerdict,
    EvaluatorKind,
    EvaluatorSpec,
    ProvenanceCompleteness,
    RegressionClassification,
    RunStatus,
    ScoreDirection,
    VersionRef,
)
from app.core.evaluation.comparison import ResultAlignmentAmbiguous, RunsNotComparable
from app.core.evaluation.execution import ExecutionTargetRef
from app.core.evaluation.execution import ExecutionRequest, OutcomeKind
from app.core.evaluation.references import CaseVersionRef
from app.core.evaluation.run_attempts import AttemptStatus, EvaluationEntityNotFound, EvaluationRun, ExecutionAttempt
from app.core.evaluation.catalog import TestCaseVersion as CatalogCase
from app.services.evaluation import EvaluationComparisonService

NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
PROJECT_ID = UUID("10000000-0000-4000-a000-000000000001")
CONFIG_REF = VersionRef("config", "cfg-1")
PROMPT_REF = VersionRef("prompt", "prompt-1")
TARGET_VERSION = VersionRef("git", "abc")


def spec(
    evaluator_id: str,
    *,
    evaluator_version: str = "v1",
    direction: ScoreDirection = ScoreDirection.HIGHER_IS_BETTER,
    tolerance: float | None = None,
    config_ref: VersionRef = CONFIG_REF,
    prompt_ref: VersionRef | None = PROMPT_REF,
) -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id,
        evaluator_version,
        EvaluatorKind.DETERMINISTIC,
        config_ref,
        direction,
        config_snapshot={"threshold": 0.5},
        score_range=(0.0, 1.0),
        comparison_tolerance=tolerance,
        prompt_ref=prompt_ref,
    )


def serialize_spec(value: EvaluatorSpec) -> dict[str, object]:
    return {
        "evaluator_id": value.evaluator_id,
        "evaluator_version": value.evaluator_version,
        "evaluator_kind": value.evaluator_kind.value,
        "config_ref": {"kind": value.config_ref.kind, "opaque_value": value.config_ref.opaque_value},
        "config_snapshot": value.config_snapshot,
        "threshold": value.threshold,
        "score_direction": value.score_direction.value,
        "score_range": value.score_range,
        "comparison_tolerance": value.comparison_tolerance,
        "prompt_ref": None if value.prompt_ref is None else {
            "kind": value.prompt_ref.kind, "opaque_value": value.prompt_ref.opaque_value
        },
        "required": value.required,
        "result_schema_ref": {"kind": value.result_schema_ref.kind, "opaque_value": value.result_schema_ref.opaque_value},
        "comparison_semantics": value.comparison_semantics,
        "required_artifact_kinds": value.required_artifact_kinds,
        "required_evidence_kinds": value.required_evidence_kinds,
    }


def make_run(
    run_id: UUID,
    *,
    project_id: UUID = PROJECT_ID,
    status: RunStatus = RunStatus.COMPLETED,
    dataset_id: str = "dataset",
    dataset_version: str = "d1",
    suite_id: str = "suite",
    suite_version: str = "s1",
    target_id: str = "target",
    target_version: VersionRef | None = TARGET_VERSION,
    specs: tuple[EvaluatorSpec, ...] = (spec("eval"),),
) -> EvaluationRun:
    target = ExecutionTargetRef(target_id, "FIXTURE", target_version, ("TEXT",), VersionRef("target-config", "v1"))
    terminal = status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.OUTCOME_UNKNOWN}
    return EvaluationRun(
        run_id=run_id,
        project_id=project_id,
        dataset_ref=VersionRef("DATASET", dataset_version),
        suite_ref=VersionRef("SUITE", suite_version),
        execution_target_ref=target,
        dataset_snapshot={"dataset_id": dataset_id, "version": dataset_version, "cases": []},
        suite_snapshot={
            "suite_id": suite_id,
            "version": suite_version,
            "created_at": NOW.isoformat(),
            "selected_cases": ({"case_id": "case-a", "version": "v1"},),
            "evaluators": tuple(serialize_spec(value) for value in specs),
            "evaluation_policy": {
                "required_result_missing": EvaluationPolicy().required_result_missing.value,
                "evaluator_error": EvaluationPolicy().evaluator_error.value,
                "evaluator_inconclusive": EvaluationPolicy().evaluator_inconclusive.value,
                "metadata": {},
            },
            "target_capability_requirements": (),
            "metadata": {},
        },
        execution_target_snapshot={
            "target_id": target_id,
            "target_kind": "FIXTURE",
            "target_version_ref": None
            if target_version is None
            else {"kind": target_version.kind, "opaque_value": target_version.opaque_value},
            "config_ref": {"kind": "target-config", "opaque_value": "v1"},
            "capabilities": ("TEXT",),
        },
        subject_ref={"fixture_target_identity": target_id, "fixture_content_identity": "sha256:" + "a" * 64},
        created_at=NOW,
        status=status,
        finished_at=NOW if terminal else None,
    )


def make_result(
    *,
    run_id: UUID,
    case_id: str,
    case_version: str,
    evaluator_id: str,
    evaluator_version: str,
    verdict: EvaluationVerdict,
    score: float | None = None,
    result_id: str | None = None,
    attempt_id: str | None = None,
    dataset_version: str = "d1",
    suite_version: str = "s1",
    config_ref: VersionRef = CONFIG_REF,
    prompt_ref: VersionRef | None = PROMPT_REF,
) -> EvaluationResult:
    return EvaluationResult(
        result_id=result_id or str(uuid4()),
        run_id=str(run_id),
        attempt_id=attempt_id or str(uuid4()),
        dataset_id="dataset",
        dataset_version=dataset_version,
        case_id=case_id,
        case_version=case_version,
        suite_id="suite",
        suite_version=suite_version,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        config_ref=config_ref,
        prompt_ref=prompt_ref,
        execution_target_id="target",
        execution_request_id=str(uuid4()),
        verdict=verdict,
        reason="evaluated",
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        target_version_ref=TARGET_VERSION,
        output_artifact_ref=ArtifactRef("artifact", "sha256:abc", "application/json"),
        score=score,
        metadata={"policy_normalization": {"source": "UNCHANGED", "final_verdict": verdict.value}},
        created_at=NOW,
    )


class FakePersistence:
    def __init__(
        self,
        runs: dict[UUID, EvaluationRun],
        results: dict[UUID, tuple[EvaluationResult, ...]],
    ) -> None:
        self.runs = runs
        self.results = dict(results)
        self.attempts: dict[UUID, tuple[ExecutionAttempt, ...]] = {}
        # 旧的单槽测试在 result fixture 中表达 manifest；投影到冻结 Run/Attempt 夹具。
        evaluator_additions: dict[UUID, dict[tuple[str, str], EvaluatorSpec]] = {}
        case_additions: dict[UUID, dict[str, str]] = {}
        for run_id, run_results in results.items():
            evaluator_additions[run_id] = {}
            case_additions[run_id] = {}
            for result in run_results:
                case_additions[run_id][result.case_id] = result.case_version
                evaluator_additions[run_id][(result.evaluator_id, result.evaluator_version)] = spec(
                    result.evaluator_id, evaluator_version=result.evaluator_version
                )
            attempt_id_by_case = {
                result.case_id: UUID(result.attempt_id) for result in run_results
            }
            self.results[run_id] = tuple(
                replace(result, attempt_id=str(attempt_id_by_case[result.case_id])) for result in run_results
            )
        for run_id, run in tuple(self.runs.items()):
            original_evaluators = tuple(run.suite_snapshot["evaluators"])
            evaluator_specs = {
                (str(item["evaluator_id"]), str(item["evaluator_version"])): dict(item)
                for item in original_evaluators
            }
            for value in evaluator_additions.get(run_id, {}).values():
                evaluator_specs.setdefault((value.evaluator_id, value.evaluator_version), serialize_spec(value))
            selected_cases = {
                (str(item["case_id"]), str(item["version"]))
                for item in run.suite_snapshot["selected_cases"]
            }
            selected_cases.update(case_additions.get(run_id, {}).items())
            # result versions are selected input versions in this fixture.
            selected = tuple({"case_id": case_id, "version": version} for case_id, version in sorted(selected_cases))
            suite_snapshot = dict(run.suite_snapshot)
            suite_snapshot["selected_cases"] = selected
            suite_snapshot["evaluators"] = tuple(evaluator_specs.values())
            self.runs[run_id] = replace(run, suite_snapshot=suite_snapshot)
            results_for_run = self.results.get(run_id, ())
            attempt_id_by_case: dict[str, UUID] = {}
            for item in results_for_run:
                attempt_id_by_case.setdefault(item.case_id, UUID(item.attempt_id))
            for item in selected:
                attempt_id_by_case.setdefault(item["case_id"], uuid4())
            self.attempts[run_id] = tuple(
                _make_attempt(self.runs[run_id], str(item["case_id"]), str(item["version"]),
                              attempt_id_by_case[str(item["case_id"])])
                for item in selected
            )

    async def get_run(self, project_id: UUID, run_id: UUID) -> EvaluationRun:
        run = self.runs.get(run_id)
        if run is None or run.project_id != project_id:
            raise EvaluationEntityNotFound("run not found")
        return run

    async def list_results(
        self,
        project_id: UUID,
        run_id: UUID,
        attempt_id: UUID | None = None,
    ) -> tuple[EvaluationResult, ...]:
        results = self.results.get(run_id, ())
        if attempt_id is None:
            return tuple(results)
        return tuple(item for item in results if item.attempt_id == str(attempt_id))

    async def list_latest_attempts(self, project_id: UUID, run_id: UUID) -> tuple[ExecutionAttempt, ...]:
        return self.attempts.get(run_id, ())


def _make_attempt(run: EvaluationRun, case_id: str, version: str, attempt_id: UUID) -> ExecutionAttempt:
    case = CatalogCase(case_id, version, "case", {}, NOW, expected_output=None)
    target = run.execution_target_ref
    request = ExecutionRequest(
        str(uuid4()), str(run.run_id), str(attempt_id), CaseVersionRef(case_id, version), {},
        timedelta(seconds=30), str(uuid4()),
    )
    return ExecutionAttempt(
        attempt_id=attempt_id, project_id=run.project_id, run_id=run.run_id,
        case_ref=CaseVersionRef(case_id, version), attempt_no=1, execution_target_ref=target,
        execution_request=request,
        request_snapshot={
            "case_snapshot": {
                "case_id": case.case_id, "version": case.version, "name": case.name,
                "input_payload": {}, "expected_output": None, "created_at": NOW.isoformat(),
                "assertion_specs": [], "fixture_refs": [], "evidence_refs": [], "tags": [], "metadata": {},
            }
        },
        created_at=NOW, status=AttemptStatus.TERMINAL, claim_token=uuid4(), finished_at=NOW,
        execution_outcome_kind=OutcomeKind.SUCCESS,
    )


def service(
    runs: dict[UUID, EvaluationRun],
    results: dict[UUID, tuple[EvaluationResult, ...]],
) -> EvaluationComparisonService:
    return EvaluationComparisonService(FakePersistence(runs, results))


async def compare(
    svc: EvaluationComparisonService,
    baseline_run_id: UUID,
    candidate_run_id: UUID,
) -> EvaluationRunComparison:
    return await svc.compare_runs(PROJECT_ID, baseline_run_id, candidate_run_id)


async def one_slot_pair(
    *,
    baseline_verdict: EvaluationVerdict,
    candidate_verdict: EvaluationVerdict,
    baseline_score: float | None = None,
    candidate_score: float | None = None,
    **run_kwargs: object,
) -> tuple[EvaluationRunComparison, AlignedResultComparison]:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {
        baseline_run_id: make_run(baseline_run_id, **run_kwargs),
        candidate_run_id: make_run(candidate_run_id, **run_kwargs),
    }
    results = {
        baseline_run_id: (
                make_result(
                    run_id=baseline_run_id,
                case_id="case-a",
                case_version="v1",
                evaluator_id="eval",
                evaluator_version="v1",
                verdict=baseline_verdict,
                score=baseline_score,
            ),
        ),
        candidate_run_id: (
            make_result(
                run_id=candidate_run_id,
                case_id="case-a",
                case_version="v1",
                evaluator_id="eval",
                evaluator_version="v1",
                verdict=candidate_verdict,
                score=candidate_score,
            ),
        ),
    }
    comparison = await compare(service(runs, results), baseline_run_id, candidate_run_id)
    return comparison, comparison.comparisons[0]


@pytest.mark.asyncio
async def test_pass_to_fail_is_regression() -> None:
    comparison, slot = await one_slot_pair(
        baseline_verdict=EvaluationVerdict.PASS,
        candidate_verdict=EvaluationVerdict.FAIL,
    )
    assert slot.classification is RegressionClassification.REGRESSION
    assert slot.reason is ComparisonReason.VERDICT_REGRESSED
    assert slot.baseline_result_id and slot.candidate_result_id
    assert comparison.comparisons == (slot,)


@pytest.mark.asyncio
async def test_fail_to_pass_is_improvement() -> None:
    _, slot = await one_slot_pair(
        baseline_verdict=EvaluationVerdict.FAIL,
        candidate_verdict=EvaluationVerdict.PASS,
    )
    assert slot.classification is RegressionClassification.IMPROVEMENT
    assert slot.reason is ComparisonReason.VERDICT_IMPROVED


@pytest.mark.asyncio
async def test_pass_to_pass_is_unchanged() -> None:
    _, slot = await one_slot_pair(
        baseline_verdict=EvaluationVerdict.PASS,
        candidate_verdict=EvaluationVerdict.PASS,
    )
    assert slot.classification is RegressionClassification.UNCHANGED
    assert slot.reason is ComparisonReason.VERDICT_UNCHANGED


@pytest.mark.asyncio
async def test_fail_to_fail_is_unchanged() -> None:
    _, slot = await one_slot_pair(
        baseline_verdict=EvaluationVerdict.FAIL,
        candidate_verdict=EvaluationVerdict.FAIL,
    )
    assert slot.classification is RegressionClassification.UNCHANGED
    assert slot.reason is ComparisonReason.VERDICT_UNCHANGED


@pytest.mark.parametrize(
    ("baseline_verdict", "candidate_verdict"),
    [
        (EvaluationVerdict.INCONCLUSIVE, EvaluationVerdict.PASS),
        (EvaluationVerdict.PASS, EvaluationVerdict.INCONCLUSIVE),
        (EvaluationVerdict.ERROR, EvaluationVerdict.PASS),
        (EvaluationVerdict.PASS, EvaluationVerdict.ERROR),
    ],
)
@pytest.mark.asyncio
async def test_inconclusive_or_error_is_not_comparable(
    baseline_verdict: EvaluationVerdict,
    candidate_verdict: EvaluationVerdict,
) -> None:
    _, slot = await one_slot_pair(baseline_verdict=baseline_verdict, candidate_verdict=candidate_verdict)
    assert slot.classification is RegressionClassification.NOT_COMPARABLE
    assert slot.reason is ComparisonReason.INCONCLUSIVE_RESULT


@pytest.mark.asyncio
async def test_single_side_missing_optional_result_is_not_comparable() -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {baseline_run_id: make_run(baseline_run_id), candidate_run_id: make_run(candidate_run_id)}
    shared = {"case_id": "case-a", "case_version": "v1"}
    results = {
        # Baseline 有 required + optional 两个 result；Candidate 只有 required。
        baseline_run_id: (
            make_result(
                run_id=baseline_run_id,
                evaluator_id="eval",
                evaluator_version="v1",
                verdict=EvaluationVerdict.PASS,
                **shared,
            ),
            make_result(
                run_id=baseline_run_id,
                evaluator_id="optional-eval",
                evaluator_version="v1",
                verdict=EvaluationVerdict.PASS,
                **shared,
            ),
        ),
        candidate_run_id: (
            make_result(
                run_id=candidate_run_id,
                evaluator_id="eval",
                evaluator_version="v1",
                verdict=EvaluationVerdict.PASS,
                **shared,
            ),
        ),
    }
    comparison = await compare(service(runs, results), baseline_run_id, candidate_run_id)
    by_key = {(item.case_id, item.evaluator_id): item for item in comparison.comparisons}
    assert by_key[("case-a", "eval")].classification is RegressionClassification.UNCHANGED
    optional = by_key[("case-a", "optional-eval")]
    assert optional.classification is RegressionClassification.NOT_COMPARABLE
    assert ComparisonReason.MISSING_CANDIDATE_RESULT.value in optional.reason_codes


@pytest.mark.parametrize(
    ("candidate_overrides", "fragment"),
    [
        ({"config_ref": VersionRef("config", "other")}, "config"),
        ({"prompt_ref": None}, "prompt"),
    ],
)
@pytest.mark.asyncio
async def test_config_or_prompt_mismatch_is_not_comparable(
    candidate_overrides: dict[str, object],
    fragment: str,
) -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    changed = spec(
        "eval",
        config_ref=candidate_overrides.get("config_ref", CONFIG_REF),
        prompt_ref=candidate_overrides.get("prompt_ref", PROMPT_REF),
    )
    runs = {
        baseline_run_id: make_run(baseline_run_id),
        candidate_run_id: make_run(candidate_run_id, specs=(changed,)),
    }
    baseline_result = make_result(
        run_id=baseline_run_id,
        case_id="case-a",
        case_version="v1",
        evaluator_id="eval",
        evaluator_version="v1",
        verdict=EvaluationVerdict.PASS,
    )
    candidate_result = make_result(
        run_id=candidate_run_id,
        case_id="case-a",
        case_version="v1",
        evaluator_id="eval",
        evaluator_version="v1",
        verdict=EvaluationVerdict.FAIL,
    )
    comparison = await compare(
        service(
            runs,
            {
                baseline_run_id: (baseline_result,),
                candidate_run_id: (candidate_result,),
            },
        ),
        baseline_run_id,
        candidate_run_id,
    )
    slot = comparison.comparisons[0]
    assert slot.classification is RegressionClassification.NOT_COMPARABLE
    assert slot.compatibility.value == "INCOMPARABLE"
    assert any(code.startswith("EVALUATOR_") for code in slot.reason_codes)


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING])
@pytest.mark.asyncio
async def test_non_completed_run_is_rejected(status: RunStatus) -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {
        baseline_run_id: make_run(baseline_run_id, status=status),
        candidate_run_id: make_run(candidate_run_id),
    }
    with pytest.raises(RunsNotComparable, match="baseline run must be terminal"):
        await compare(service(runs, {}), baseline_run_id, candidate_run_id)


@pytest.mark.parametrize(
    ("baseline_kwargs", "candidate_kwargs", "fragment"),
    [
        ({"dataset_id": "dataset-a"}, {"dataset_id": "dataset-b"}, "dataset lineage mismatch"),
        ({"suite_id": "suite-a"}, {"suite_id": "suite-b"}, "suite lineage mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_run_identity_mismatch_is_rejected(
    baseline_kwargs: dict[str, str],
    candidate_kwargs: dict[str, str],
    fragment: str,
) -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {
        baseline_run_id: make_run(baseline_run_id, **baseline_kwargs),
        candidate_run_id: make_run(candidate_run_id, **candidate_kwargs),
    }
    with pytest.raises(RunsNotComparable, match=fragment):
        await compare(service(runs, {}), baseline_run_id, candidate_run_id)


@pytest.mark.asyncio
async def test_same_run_as_baseline_and_candidate_is_rejected() -> None:
    run_id = uuid4()
    with pytest.raises(RunsNotComparable, match="must differ"):
        await compare(service({run_id: make_run(run_id)}, {}), run_id, run_id)


@pytest.mark.asyncio
async def test_cross_tenant_run_is_fail_closed() -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {
        baseline_run_id: make_run(baseline_run_id),
        # Candidate 属于另一个 tenant；以 caller project 查询必须不可见。
        candidate_run_id: make_run(
            candidate_run_id,
            project_id=UUID("20000000-0000-4000-a000-000000000001"),
        ),
    }
    with pytest.raises(EvaluationEntityNotFound, match="run not found"):
        await compare(service(runs, {}), baseline_run_id, candidate_run_id)


@pytest.mark.asyncio
async def test_attempt_id_does_not_participate_in_alignment() -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {baseline_run_id: make_run(baseline_run_id), candidate_run_id: make_run(candidate_run_id)}
    results = {
        baseline_run_id: (
            make_result(
                run_id=baseline_run_id,
                case_id="case-a",
                case_version="v1",
                evaluator_id="eval",
                evaluator_version="v1",
                verdict=EvaluationVerdict.PASS,
                attempt_id=str(UUID("30000000-0000-4000-a000-000000000001")),
            ),
        ),
        candidate_run_id: (
            make_result(
                run_id=candidate_run_id,
                case_id="case-a",
                case_version="v1",
                evaluator_id="eval",
                evaluator_version="v1",
                verdict=EvaluationVerdict.FAIL,
                attempt_id=str(UUID("30000000-0000-4000-a000-000000000002")),
            ),
        ),
    }
    comparison = await compare(service(runs, results), baseline_run_id, candidate_run_id)
    assert comparison.comparisons[0].classification is RegressionClassification.REGRESSION


@pytest.mark.asyncio
async def test_deterministic_ordering() -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {baseline_run_id: make_run(baseline_run_id), candidate_run_id: make_run(candidate_run_id)}
    # 故意以乱序构造两侧 results，期望输出按对齐键稳定排序。
    cases = ("case-c", "case-a", "case-b")
    baseline_results = tuple(
        make_result(
            run_id=baseline_run_id,
            case_id=case_id,
            case_version="v1",
            evaluator_id="eval",
            evaluator_version="v1",
            verdict=EvaluationVerdict.PASS,
        )
        for case_id in cases
    )
    candidate_results = tuple(
        make_result(
            run_id=candidate_run_id,
            case_id=case_id,
            case_version="v1",
            evaluator_id="eval",
            evaluator_version="v1",
            verdict=EvaluationVerdict.PASS,
        )
        for case_id in reversed(cases)
    )
    comparison = await compare(
        service(runs, {baseline_run_id: baseline_results, candidate_run_id: candidate_results}),
        baseline_run_id,
        candidate_run_id,
    )
    assert [item.case_id for item in comparison.comparisons] == ["case-a", "case-b", "case-c"]


@pytest.mark.asyncio
async def test_duplicate_alignment_slot_fails_closed() -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {baseline_run_id: make_run(baseline_run_id), candidate_run_id: make_run(candidate_run_id)}
    duplicate = make_result(
        run_id=baseline_run_id,
        case_id="case-a",
        case_version="v1",
        evaluator_id="eval",
        evaluator_version="v1",
        verdict=EvaluationVerdict.PASS,
    )
    results = {
        baseline_run_id: (
            make_result(
                run_id=baseline_run_id,
                case_id="case-a",
                case_version="v1",
                evaluator_id="eval",
                evaluator_version="v1",
                verdict=EvaluationVerdict.FAIL,
            ),
            duplicate,
        ),
        candidate_run_id: (),
    }
    with pytest.raises(ResultAlignmentAmbiguous, match="duplicate result slot"):
        await compare(service(runs, results), baseline_run_id, candidate_run_id)


@pytest.mark.asyncio
async def test_score_evidence_does_not_flip_classification() -> None:
    _, slot = await one_slot_pair(
        baseline_verdict=EvaluationVerdict.PASS,
        candidate_verdict=EvaluationVerdict.PASS,
        baseline_score=1.0,
        candidate_score=0.2,
        specs=(spec("eval", tolerance=0.1),),
    )
    assert slot.classification is RegressionClassification.UNCHANGED
    assert slot.reason is ComparisonReason.VERDICT_UNCHANGED
    assert slot.baseline_score == 1.0
    assert slot.candidate_score == 0.2
    assert slot.score_delta == pytest.approx(-0.8)
    assert slot.score_regressed is True


@pytest.mark.asyncio
async def test_version_differences_are_preserved_as_provenance() -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {
        baseline_run_id: make_run(
            baseline_run_id,
            dataset_version="d1",
            suite_version="s1",
            target_version=VersionRef("git", "abc"),
        ),
        candidate_run_id: make_run(
            candidate_run_id,
            dataset_version="d2",
            suite_version="s2",
            target_version=VersionRef("git", "def"),
        ),
    }
    results = {
        baseline_run_id: (
            make_result(
                run_id=baseline_run_id,
                case_id="case-a",
                case_version="v1",
                evaluator_id="eval",
                evaluator_version="v1",
                    verdict=EvaluationVerdict.PASS,
                    dataset_version="d1",
                    suite_version="s1",
                ),
        ),
        candidate_run_id: (
            make_result(
                run_id=candidate_run_id,
                case_id="case-a",
                case_version="v1",
                evaluator_id="eval",
                evaluator_version="v1",
                    verdict=EvaluationVerdict.FAIL,
                    dataset_version="d2",
                    suite_version="s2",
                ),
        ),
    }
    comparison = await compare(service(runs, results), baseline_run_id, candidate_run_id)
    assert comparison.comparisons[0].classification is RegressionClassification.NOT_COMPARABLE
    assert comparison.comparisons[0].compatibility.value == "INCOMPARABLE"
    assert comparison.baseline_provenance.dataset_version == "d1"
    assert comparison.candidate_provenance.dataset_version == "d2"
    assert comparison.baseline_provenance.suite_version == "s1"
    assert comparison.candidate_provenance.target_version_ref == VersionRef("git", "def")


@pytest.mark.asyncio
async def test_subject_missing_facts_cannot_be_accepted_as_known():
    from app.core.evaluation.comparison import ComparisonCompatibility

    baseline_id, candidate_id = uuid4(), uuid4()
    left, right = make_run(baseline_id), make_run(candidate_id)
    assert EvaluationComparisonService._subject_compatibility(left, right)[0] is ComparisonCompatibility.COMPARABLE
    for subject in (None, {"fixture_content_identity": "UNKNOWN"}, {"fixture_content_identity": "NOT_APPLICABLE"}):
        candidate = replace(right, subject_ref=subject)
        compat, reasons = EvaluationComparisonService._subject_compatibility(left, candidate)
        assert compat is ComparisonCompatibility.INCOMPARABLE
        assert "INSUFFICIENT_SUBJECT_PROVENANCE" in reasons
    facts = {"subject_kind": "AGENT", "agent_id": "agent", "agent_version": "v1",
             "workflow_id": "workflow", "workflow_version": "v1", "toolset_identity": "tools",
             "provider_binding_identity": "provider", "runtime_version": "v1",
             "deployment_environment": "test", "run_mode": "eval", "profile": "p"}
    target = replace(left.execution_target_ref, target_kind="LOCALAGENT_HTTP")
    agent = replace(left, execution_target_ref=target, subject_ref=facts)
    for version in (None, "UNKNOWN", "NOT_APPLICABLE"):
        incomplete = replace(agent, subject_ref={**facts, "agent_version": version})
        assert EvaluationComparisonService._subject_compatibility(agent, incomplete)[0] is ComparisonCompatibility.INCOMPARABLE
    assert EvaluationComparisonService._subject_compatibility(
        agent, replace(agent, subject_ref={**facts, "agent_version": "v2"})
    )[0] is ComparisonCompatibility.CONDITIONALLY_COMPARABLE


def test_controlled_test_subject_is_comparable_and_missing_subject_is_not():
    from app.core.evaluation.comparison import ComparisonCompatibility

    baseline, candidate = make_run(uuid4()), make_run(uuid4())
    target = replace(baseline.execution_target_ref, target_kind="LOCALAGENT_HTTP")
    frozen_test_subject = {
        "subject_kind": "AGENT",
        "agent_id": "core_router",
        "agent_version": "TEST_FIXTURE:core_router:v1",
        "workflow_id": "episodic_evaluation_layer1",
        "workflow_version": "TEST_FIXTURE:episodic_evaluation_layer1:v1",
        "toolset_identity": {"fixture": "controlled-toolset", "version": "TEST_FIXTURE:v1"},
        "provider_binding_identity": {
            "fixture": "sequenced-openai-compatible-provider",
            "model": "wp3-deterministic-provider-v1",
            "version": "TEST_FIXTURE:v1",
        },
        "runtime_version": "TEST_FIXTURE:evaluation-v2",
        "deployment_environment": "TEST",
        "run_mode": "evaluation",
        "profile": "EPISODIC_EVALUATION_LAYER1",
    }
    baseline = replace(baseline, execution_target_ref=target, subject_ref=frozen_test_subject)
    candidate = replace(candidate, execution_target_ref=target, subject_ref=dict(frozen_test_subject))
    assert EvaluationComparisonService._subject_compatibility(baseline, candidate)[0] is ComparisonCompatibility.COMPARABLE
    compatibility, reasons = EvaluationComparisonService._subject_compatibility(
        baseline, replace(candidate, subject_ref={"subject_kind": "AGENT"}),
    )
    assert compatibility is ComparisonCompatibility.INCOMPARABLE
    assert "INSUFFICIENT_SUBJECT_PROVENANCE" in reasons


def test_fixture_content_identity_is_captured_from_actual_templates():
    from app.adapters.evaluation.fixture import FixtureExecution, FixtureExecutionTarget

    target = make_run(uuid4()).execution_target_ref
    ref = CaseVersionRef("case-a", "v1")
    first = FixtureExecution(OutcomeKind.SUCCESS, NOW, NOW, ArtifactRef("answer", "sha256:a"))
    adapter = FixtureExecutionTarget(target, {ref: first})
    changed = FixtureExecutionTarget(target, {ref: replace(first, output_artifact_ref=ArtifactRef("answer", "sha256:b"))})
    assert adapter.subject_ref != changed.subject_ref
    assert adapter.subject_ref == FixtureExecutionTarget(target, {ref: replace(first, finished_at=NOW + timedelta(seconds=1))}).subject_ref


def test_target_drift_requires_supported_same_contract():
    from app.core.evaluation.comparison import ComparisonCompatibility

    left = make_run(uuid4(), target_version=VersionRef("adapter", "fixture.v1"))
    right = make_run(uuid4(), target_id="other", target_version=VersionRef("adapter", "fixture.v1"))
    assert EvaluationComparisonService._target_compatibility(left, right)[0] is ComparisonCompatibility.CONDITIONALLY_COMPARABLE
    unknown = replace(right, execution_target_ref=replace(right.execution_target_ref, target_version_ref=VersionRef("schema", "unknown-v99")))
    assert EvaluationComparisonService._target_compatibility(left, unknown)[0] is ComparisonCompatibility.INCOMPARABLE
    different_config = replace(right, execution_target_ref=replace(right.execution_target_ref, config_ref=VersionRef("config", "unknown")))
    assert EvaluationComparisonService._target_compatibility(left, different_config)[0] is ComparisonCompatibility.INCOMPARABLE


@pytest.mark.asyncio
async def test_evaluator_schema_semantics_and_required_evidence_are_frozen():
    from app.core.evaluation.comparison import ComparisonCompatibility

    b, c = uuid4(), uuid4()
    value = spec("eval")
    br = make_result(run_id=b, case_id="case-a", case_version="v1", evaluator_id="eval", evaluator_version="v1", verdict=EvaluationVerdict.PASS, score=1.0)
    cr = make_result(run_id=c, case_id="case-a", case_version="v1", evaluator_id="eval", evaluator_version="v1", verdict=EvaluationVerdict.PASS, score=0.2)
    for changed in (replace(value, result_schema_ref=VersionRef("evaluation_result", "v99")),
                    replace(value, comparison_semantics="unsupported.v99"),
                    replace(value, required_evidence_kinds=("final_answer",))):
        assert changed.definition_digest != value.definition_digest
        persistence = FakePersistence({b: make_run(b, specs=(value,)), c: make_run(c, specs=(changed,))}, {b: (br,), c: (cr,)})
        result = await EvaluationComparisonService(persistence).compare_runs(PROJECT_ID, b, c)
        row = result.comparisons[0]
        assert row.compatibility is ComparisonCompatibility.INCOMPARABLE
        assert row.classification is RegressionClassification.NOT_COMPARABLE
        assert row.score_delta is None
    snapshot = serialize_spec(value)
    snapshot.pop("result_schema_ref")
    assert EvaluationComparisonService._evaluator_compatibility(snapshot, snapshot, br, cr)[1] == {"INSUFFICIENT_EVALUATOR_PROVENANCE"}


@pytest.mark.asyncio
async def test_accepted_case_version_drift_report_and_recompute_use_same_frozen_truth():
    from app.core.evaluation.report import ReleaseDecision
    from app.services.evaluation.report import RegressionReportService

    b, c = uuid4(), uuid4()
    left, right = make_run(b), make_run(c)
    right = replace(right, suite_snapshot={**dict(right.suite_snapshot), "selected_cases": ({"case_id": "case-a", "version": "v2"},)})
    results = {run: (make_result(run_id=run, case_id="case-a", case_version=version, evaluator_id="eval",
                                 evaluator_version="v1", verdict=EvaluationVerdict.PASS),)
               for run, version in ((b, "v1"), (c, "v2"))}
    persistence = FakePersistence({b: left, c: right}, results)
    comparator = EvaluationComparisonService(persistence)
    first = await comparator.compare_runs(PROJECT_ID, b, c, accepted_conditional_reason_codes=("CASE_VERSION_LABEL_DRIFT",))
    second = await comparator.compare_runs(PROJECT_ID, b, c, accepted_conditional_reason_codes=("CASE_VERSION_LABEL_DRIFT",))
    assert first.semantic_digest == second.semantic_digest
    assert first.computed_at != second.computed_at
    assert len(first.comparisons) == 1
    for version in ("v1", "v2"):
        assert RegressionReportService().build_report(first, (CaseVersionRef("case-a", version),)).release_decision is ReleaseDecision.PASS
