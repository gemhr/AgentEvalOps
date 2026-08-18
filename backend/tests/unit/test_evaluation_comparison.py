"""EvaluationComparisonService 最小 Application tests。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

from datetime import datetime, timezone
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
from app.core.evaluation.run_attempts import EvaluationEntityNotFound, EvaluationRun
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
) -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id,
        evaluator_version,
        EvaluatorKind.DETERMINISTIC,
        CONFIG_REF,
        direction,
        config_snapshot={"threshold": 0.5},
        score_range=(0.0, 1.0),
        comparison_tolerance=tolerance,
        prompt_ref=PROMPT_REF,
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
        "prompt_ref": {"kind": value.prompt_ref.kind, "opaque_value": value.prompt_ref.opaque_value},
        "required": value.required,
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
            "selected_cases": (),
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
    config_ref: VersionRef = CONFIG_REF,
    prompt_ref: VersionRef | None = PROMPT_REF,
) -> EvaluationResult:
    return EvaluationResult(
        result_id=result_id or str(uuid4()),
        run_id=str(run_id),
        attempt_id=attempt_id or str(uuid4()),
        dataset_id="dataset",
        dataset_version="d1",
        case_id=case_id,
        case_version=case_version,
        suite_id="suite",
        suite_version="s1",
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
        created_at=NOW,
    )


class FakePersistence:
    def __init__(
        self,
        runs: dict[UUID, EvaluationRun],
        results: dict[UUID, tuple[EvaluationResult, ...]],
    ) -> None:
        self.runs = runs
        self.results = results

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
    assert optional.reason is ComparisonReason.CANDIDATE_MISSING


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
    runs = {baseline_run_id: make_run(baseline_run_id), candidate_run_id: make_run(candidate_run_id)}
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
        **candidate_overrides,
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
    assert slot.reason is ComparisonReason.EVALUATOR_CONFIG_MISMATCH


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.FAILED, RunStatus.OUTCOME_UNKNOWN])
@pytest.mark.asyncio
async def test_non_completed_run_is_rejected(status: RunStatus) -> None:
    baseline_run_id = uuid4()
    candidate_run_id = uuid4()
    runs = {
        baseline_run_id: make_run(baseline_run_id, status=status),
        candidate_run_id: make_run(candidate_run_id),
    }
    with pytest.raises(RunsNotComparable, match="baseline run must be COMPLETED"):
        await compare(service(runs, {}), baseline_run_id, candidate_run_id)


@pytest.mark.parametrize(
    ("baseline_kwargs", "candidate_kwargs", "fragment"),
    [
        ({"dataset_id": "dataset-a"}, {"dataset_id": "dataset-b"}, "dataset identity mismatch"),
        ({"suite_id": "suite-a"}, {"suite_id": "suite-b"}, "suite identity mismatch"),
        ({"target_id": "target-a"}, {"target_id": "target-b"}, "execution target identity mismatch"),
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
    with pytest.raises(ResultAlignmentAmbiguous, match="multiple results"):
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
            ),
        ),
    }
    comparison = await compare(service(runs, results), baseline_run_id, candidate_run_id)
    assert comparison.comparisons[0].classification is RegressionClassification.REGRESSION
    assert comparison.baseline_provenance.dataset_version == "d1"
    assert comparison.candidate_provenance.dataset_version == "d2"
    assert comparison.baseline_provenance.suite_version == "s1"
    assert comparison.candidate_provenance.target_version_ref == VersionRef("git", "def")
