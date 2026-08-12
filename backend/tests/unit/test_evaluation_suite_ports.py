from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

# ruff: noqa: D415

import pytest

from app.core.evaluation import (
    AssertionSpec,
    CapabilityRequirement,
    CaseVersionRef,
    EvaluationInput,
    EvaluationPolicy,
    EvaluationResultDraft,
    EvaluationSuiteVersion,
    EvaluationVerdict,
    Evaluator,
    EvaluatorContext,
    EvaluatorKind,
    EvaluatorSpec,
    EvidenceRef,
    PolicyDisposition,
    ScoreDirection,
    VersionRef,
)

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def make_spec(
    evaluator_id: str = "exact",
    evaluator_version: str = "v1",
    kind: EvaluatorKind = EvaluatorKind.DETERMINISTIC,
) -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        evaluator_kind=kind,
        config_ref=VersionRef("evaluator_config", "config-v1"),
        config_snapshot={"normalize": True, "fields": ["answer"]},
        threshold=0.8,
        score_direction=ScoreDirection.HIGHER_IS_BETTER,
        score_range=(0.0, 1.0),
        comparison_tolerance=0.01,
        prompt_ref=VersionRef("prompt", "prompt-v2") if kind is EvaluatorKind.LLM_JUDGE else None,
        required=True,
    )


def test_evaluator_spec_owns_scoring_snapshot_for_both_kinds() -> None:
    deterministic = make_spec()
    judge = make_spec("judge", "v2", EvaluatorKind.LLM_JUDGE)

    assert deterministic.threshold == 0.8
    assert deterministic.score_direction is ScoreDirection.HIGHER_IS_BETTER
    assert deterministic.score_range == (0.0, 1.0)
    assert deterministic.comparison_tolerance == 0.01
    assert deterministic.prompt_ref is None
    assert judge.prompt_ref == VersionRef("prompt", "prompt-v2")
    with pytest.raises(TypeError):
        deterministic.config_snapshot["new"] = True  # type: ignore[index]


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"score_range": (1.0, 0.0)}, "minimum"),
        ({"comparison_tolerance": -0.1}, "non-negative"),
        ({"threshold": float("inf")}, "finite"),
    ],
)
def test_evaluator_spec_validates_numeric_contract(changes: dict[str, object], match: str) -> None:
    kwargs = {
        "evaluator_id": "exact",
        "evaluator_version": "v1",
        "evaluator_kind": EvaluatorKind.DETERMINISTIC,
        "config_ref": VersionRef("config", "v1"),
        "score_direction": ScoreDirection.HIGHER_IS_BETTER,
    }
    kwargs.update(changes)
    with pytest.raises(ValueError, match=match):
        EvaluatorSpec(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"evaluator_kind": "CUSTOM"}, "unknown evaluator_kind"),
        ({"score_direction": "SIDEWAYS"}, "unknown score_direction"),
        ({"config_snapshot": {"bad": object()}}, "Unsupported JSON"),
    ],
)
def test_evaluator_spec_rejects_unknown_kind_direction_and_invalid_config(
    changes: dict[str, object],
    match: str,
) -> None:
    kwargs = {
        "evaluator_id": "exact",
        "evaluator_version": "v1",
        "evaluator_kind": EvaluatorKind.DETERMINISTIC,
        "config_ref": VersionRef("config", "v1"),
        "score_direction": ScoreDirection.HIGHER_IS_BETTER,
    }
    kwargs.update(changes)
    with pytest.raises((TypeError, ValueError), match=match):
        EvaluatorSpec(**kwargs)  # type: ignore[arg-type]


def test_suite_preserves_order_and_freezes_policy_and_requirements() -> None:
    cases = [CaseVersionRef("case-b", "v1"), CaseVersionRef("case-a", "v1")]
    evaluators = [make_spec("exact"), make_spec("judge", kind=EvaluatorKind.LLM_JUDGE)]
    policy = EvaluationPolicy(
        required_result_missing=PolicyDisposition.FAIL,
        evaluator_error=PolicyDisposition.INCONCLUSIVE,
        metadata={"owner": ["suite"]},
    )
    suite = EvaluationSuiteVersion(
        suite_id="suite-a",
        version="v1",
        case_selection=cases,
        evaluator_specs=evaluators,
        evaluation_policy=policy,
        target_capability_requirements=[
            CapabilityRequirement("TEXT_OUTPUT"),
            CapabilityRequirement("TOOL_EVIDENCE"),
        ],
        metadata={"channels": ["offline"]},
        created_at=NOW,
    )

    cases.reverse()
    evaluators.reverse()
    assert tuple(ref.case_id for ref in suite.case_selection) == ("case-b", "case-a")
    assert tuple(spec.evaluator_id for spec in suite.evaluator_specs) == ("exact", "judge")
    assert suite.evaluation_policy.required_result_missing is PolicyDisposition.FAIL
    assert tuple(item.identifier for item in suite.target_capability_requirements) == (
        "TEXT_OUTPUT",
        "TOOL_EVIDENCE",
    )
    with pytest.raises(FrozenInstanceError):
        suite.version = "v2"  # type: ignore[misc]
    with pytest.raises(TypeError):
        suite.evaluation_policy.metadata["new"] = "value"  # type: ignore[index]


def test_suite_rejects_duplicate_cases_evaluators_and_capabilities() -> None:
    case_ref = CaseVersionRef("case-a", "v1")
    spec = make_spec()
    base = {
        "suite_id": "suite-a",
        "version": "v1",
        "case_selection": [case_ref],
        "evaluator_specs": [spec],
        "evaluation_policy": EvaluationPolicy(),
        "created_at": NOW,
    }
    with pytest.raises(ValueError, match="selected case"):
        EvaluationSuiteVersion(**(base | {"case_selection": [case_ref, case_ref]}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="evaluator identity/version"):
        EvaluationSuiteVersion(**(base | {"evaluator_specs": [spec, spec]}))  # type: ignore[arg-type]
    capability = CapabilityRequirement("TEXT_OUTPUT")
    with pytest.raises(ValueError, match="target capability"):
        EvaluationSuiteVersion(
            **base,  # type: ignore[arg-type]
            target_capability_requirements=[capability, capability],
        )


class FakeDeterministicEvaluator:
    """不需要任何外部依赖的 fake evaluator。"""

    async def evaluate(
        self,
        evaluation_input: EvaluationInput,
        context: EvaluatorContext,
    ) -> EvaluationResultDraft:
        """用预期输出执行确定性比较。"""
        passed = evaluation_input.expected_output == "expected"
        return EvaluationResultDraft(
            evaluator_id=context.evaluator_spec.evaluator_id,
            evaluator_version=context.evaluator_spec.evaluator_version,
            config_ref=context.evaluator_spec.config_ref,
            verdict=EvaluationVerdict.PASS if passed else EvaluationVerdict.FAIL,
            reason="deterministic comparison",
            score=1.0 if passed else 0.0,
        )


class FakeJudgeModel:
    """仅实现 JudgeModelPort 最小能力的 fake。"""

    async def structured_generate(self, **_: object) -> object:
        """返回固定结构化判定。"""
        return {"verdict": "PASS"}


class FakeExternalEvaluator:
    """证明外部 Judge 能通过 Protocol 注入的 fake evaluator。"""

    async def evaluate(
        self,
        evaluation_input: EvaluationInput,
        context: EvaluatorContext,
    ) -> EvaluationResultDraft:
        """调用注入的 JudgeModelPort 并生成 draft。"""
        assert context.judge_model is not None
        await context.judge_model.structured_generate(
            prompt_ref=context.evaluator_spec.prompt_ref,
            input_payload=evaluation_input.expected_output,
            config=context.evaluator_spec.config_snapshot,
        )
        return EvaluationResultDraft(
            evaluator_id=context.evaluator_spec.evaluator_id,
            evaluator_version=context.evaluator_spec.evaluator_version,
            config_ref=context.evaluator_spec.config_ref,
            prompt_ref=context.evaluator_spec.prompt_ref,
            verdict=EvaluationVerdict.PASS,
            reason="judge completed",
        )


@pytest.mark.asyncio
async def test_evaluator_port_supports_deterministic_without_external_dependencies() -> None:
    evaluator: Evaluator = FakeDeterministicEvaluator()
    evaluation_input = EvaluationInput(
        case_ref=CaseVersionRef("case-a", "v1"),
        expected_output="expected",
        assertion_specs=[AssertionSpec("exact", "exact", config={})],
        execution_outcome_ref=EvidenceRef("execution_outcome", "outcome-a"),
    )
    result = await evaluator.evaluate(evaluation_input, EvaluatorContext(make_spec()))
    assert result.verdict is EvaluationVerdict.PASS
    assert result.score == 1.0


@pytest.mark.asyncio
async def test_evaluator_port_injects_minimal_judge_protocol() -> None:
    evaluator: Evaluator = FakeExternalEvaluator()
    spec = make_spec("judge", kind=EvaluatorKind.LLM_JUDGE)
    result = await evaluator.evaluate(
        EvaluationInput(CaseVersionRef("case-a", "v1"), "expected", ()),
        EvaluatorContext(spec, judge_model=FakeJudgeModel()),
    )
    assert result.prompt_ref == spec.prompt_ref
    assert result.verdict is EvaluationVerdict.PASS
