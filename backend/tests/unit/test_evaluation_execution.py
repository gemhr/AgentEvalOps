import ast

# ruff: noqa: D415

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.adapters.evaluation import FixtureExecution, FixtureExecutionTarget
from app.core.evaluation import (
    FIXTURE_TARGET_KIND,
    ArtifactRef,
    AssertionSpec,
    CapabilityRequirement,
    CaseVersionRef,
    EvaluationInput,
    EvaluationResultDraft,
    EvaluationVerdict,
    EvaluatorContext,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionTarget,
    ExecutionTargetRef,
    OutcomeKind,
    ScoreDirection,
    UnsupportedTargetCapabilitiesError,
    VersionRef,
    validate_target_capabilities,
)

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(milliseconds=10)
CASE_REF = CaseVersionRef("case-a", "v1")


def make_request(**changes: object) -> ExecutionRequest:
    values = {
        "request_id": "request-a",
        "run_id": "run-a",
        "attempt_id": "attempt-a",
        "case_ref": CASE_REF,
        "input_payload": {"messages": [{"role": "user", "content": ["question"]}]},
        "timeout": timedelta(seconds=30),
        "idempotency_key": "run-a:case-a:attempt-a",
        "execution_metadata": {"labels": ["offline"]},
    }
    values.update(changes)
    return ExecutionRequest(**values)  # type: ignore[arg-type]


def make_outcome(kind: OutcomeKind, **changes: object) -> ExecutionOutcome:
    values: dict[str, object] = {
        "request_id": "request-a",
        "kind": kind,
        "started_at": NOW,
        "finished_at": LATER,
        "metadata": {"source": ["fixture"]},
    }
    if kind is OutcomeKind.SUCCESS:
        values["output_artifact_ref"] = ArtifactRef("actual-answer")
    else:
        values["error_category"] = kind.value.lower()
        values["reason"] = f"confirmed {kind.value.lower()}"
    values.update(changes)
    return ExecutionOutcome(**values)  # type: ignore[arg-type]


def make_target_ref(**changes: object) -> ExecutionTargetRef:
    values = {
        "target_id": "fixture-target",
        "target_kind": FIXTURE_TARGET_KIND,
        "target_version_ref": VersionRef("fixture_set", "fixtures-v1"),
        "capabilities": ["TEXT_OUTPUT", "JSON_INPUT"],
        "config_ref": VersionRef("fixture_config", "config-v1"),
    }
    values.update(changes)
    return ExecutionTargetRef(**values)  # type: ignore[arg-type]


def test_execution_request_is_valid_and_deeply_immutable() -> None:
    payload = {"messages": [{"parts": ["question"]}]}
    metadata = {"labels": ["critical"]}
    request = make_request(input_payload=payload, execution_metadata=metadata)

    payload["messages"][0]["parts"].append("mutated")
    metadata["labels"].append("mutated")
    assert request.input_payload["messages"][0]["parts"] == ("question",)
    assert request.execution_metadata["labels"] == ("critical",)
    assert request.timeout == timedelta(seconds=30)
    assert request.idempotency_key == "run-a:case-a:attempt-a"
    with pytest.raises(FrozenInstanceError):
        request.idempotency_key = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.execution_metadata["new"] = True  # type: ignore[index]


@pytest.mark.parametrize("timeout", [timedelta(0), timedelta(seconds=-1)])
def test_execution_request_rejects_non_positive_timeout(timeout: timedelta) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        make_request(timeout=timeout)


def test_execution_request_requires_explicit_timedelta_timeout() -> None:
    with pytest.raises(TypeError, match="timedelta"):
        make_request(timeout=30)


@pytest.mark.parametrize("field_name", ["request_id", "run_id", "attempt_id", "idempotency_key"])
def test_execution_request_rejects_empty_identity(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        make_request(**{field_name: " "})


def test_execution_target_ref_preserves_opaque_version_capabilities_and_immutability() -> None:
    capabilities = ["TEXT_OUTPUT", "JSON_INPUT"]
    target_ref = make_target_ref(capabilities=capabilities)
    capabilities.reverse()

    assert target_ref.target_id == "fixture-target"
    assert target_ref.target_kind == FIXTURE_TARGET_KIND
    assert target_ref.target_version_ref == VersionRef("fixture_set", "fixtures-v1")
    assert target_ref.capabilities == ("TEXT_OUTPUT", "JSON_INPUT")
    with pytest.raises(FrozenInstanceError):
        target_ref.target_kind = "changed"  # type: ignore[misc]


def test_execution_target_ref_rejects_empty_or_duplicate_declarations() -> None:
    with pytest.raises(ValueError, match="target_id"):
        make_target_ref(target_id="")
    with pytest.raises(ValueError, match="target_kind"):
        make_target_ref(target_kind=" ")
    with pytest.raises(ValueError, match="duplicate capability"):
        make_target_ref(capabilities=["TEXT_OUTPUT", "TEXT_OUTPUT"])


def test_success_requires_confirmed_artifact_and_excludes_failure_category() -> None:
    outcome = make_outcome(OutcomeKind.SUCCESS)
    assert outcome.output_artifact_ref == ArtifactRef("actual-answer")
    assert outcome.error_category is None
    with pytest.raises(ValueError, match="requires output_artifact_ref"):
        make_outcome(OutcomeKind.SUCCESS, output_artifact_ref=None)
    with pytest.raises(ValueError, match="must not include error_category"):
        make_outcome(OutcomeKind.SUCCESS, error_category="execution_error")


@pytest.mark.parametrize(
    "kind",
    [
        OutcomeKind.FAILURE,
        OutcomeKind.TIMEOUT,
        OutcomeKind.CANCELLED,
        OutcomeKind.OUTCOME_UNKNOWN,
    ],
)
def test_non_success_outcomes_require_reason_and_never_claim_success_artifact(kind: OutcomeKind) -> None:
    outcome = make_outcome(kind)
    assert outcome.output_artifact_ref is None
    assert outcome.error_category == kind.value.lower()
    assert outcome.reason == f"confirmed {kind.value.lower()}"
    with pytest.raises(ValueError, match="must not include output_artifact_ref"):
        make_outcome(kind, output_artifact_ref=ArtifactRef("contradiction"))
    with pytest.raises(ValueError, match="requires reason"):
        make_outcome(kind, reason=None)


def test_outcome_unknown_remains_distinct_from_confirmed_failure() -> None:
    unknown = make_outcome(
        OutcomeKind.OUTCOME_UNKNOWN,
        error_category="remote_finality_unknown",
        reason="connection lost after target accepted the request",
    )
    assert unknown.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert unknown.kind is not OutcomeKind.FAILURE


def test_outcome_is_terminal_timezone_aware_and_deeply_immutable() -> None:
    metadata = {"details": ["confirmed"]}
    outcome = make_outcome(OutcomeKind.TIMEOUT, metadata=metadata)
    metadata["details"].append("mutated")
    assert outcome.finished_at >= outcome.started_at
    assert outcome.metadata["details"] == ("confirmed",)
    with pytest.raises(FrozenInstanceError):
        outcome.kind = OutcomeKind.FAILURE  # type: ignore[misc]
    with pytest.raises(ValueError, match="timezone-aware"):
        make_outcome(OutcomeKind.SUCCESS, started_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="before started_at"):
        make_outcome(OutcomeKind.SUCCESS, finished_at=NOW - timedelta(microseconds=1))


def fixture_for(kind: OutcomeKind) -> FixtureExecution:
    if kind is OutcomeKind.SUCCESS:
        return FixtureExecution(kind, NOW, LATER, output_artifact_ref=ArtifactRef("fixture-actual-answer"))
    return FixtureExecution(
        kind,
        NOW,
        LATER,
        error_category=kind.value.lower(),
        reason=f"configured {kind.value.lower()}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(OutcomeKind))
async def test_fixture_target_returns_every_configured_terminal_outcome(kind: OutcomeKind) -> None:
    target: ExecutionTarget = FixtureExecutionTarget(make_target_ref(), {CASE_REF: fixture_for(kind)})
    outcome = await target.execute(make_request())

    assert outcome.kind is kind
    assert outcome.request_id == "request-a"
    assert outcome.metadata["idempotency_key"] == "run-a:case-a:attempt-a"


@pytest.mark.asyncio
async def test_fixture_target_repeated_request_is_deterministic_and_keeps_business_key() -> None:
    registry = {CASE_REF: fixture_for(OutcomeKind.SUCCESS)}
    target = FixtureExecutionTarget(make_target_ref(), registry)
    registry[CASE_REF] = fixture_for(OutcomeKind.FAILURE)
    request = make_request()

    first = await target.execute(request)
    second = await target.execute(request)
    assert first == second
    assert first.kind is OutcomeKind.SUCCESS
    assert first.metadata["idempotency_key"] == request.idempotency_key


@pytest.mark.asyncio
async def test_fixture_target_fails_closed_for_unknown_fixture_or_unsupported_kind() -> None:
    target = FixtureExecutionTarget(make_target_ref(), {})
    with pytest.raises(LookupError, match="no fixture configured"):
        await target.execute(make_request())
    with pytest.raises(ValueError, match="unsupported target kind"):
        FixtureExecutionTarget(make_target_ref(target_kind="FUTURE_ADAPTER"), {})


def test_capability_validation_accepts_complete_target_and_fails_closed_on_missing() -> None:
    requirements = [CapabilityRequirement("TEXT_OUTPUT"), CapabilityRequirement("JSON_INPUT")]
    validate_target_capabilities(requirements, ["JSON_INPUT", "TEXT_OUTPUT", "EXTRA"])
    with pytest.raises(UnsupportedTargetCapabilitiesError, match="JSON_INPUT"):
        validate_target_capabilities(requirements, ["TEXT_OUTPUT"])


class ArtifactIdentityEvaluator:
    """只用于证明 Execution 成功和 Evaluation 通过是两套代数。"""

    async def evaluate(
        self,
        evaluation_input: EvaluationInput,
        context: EvaluatorContext,
    ) -> EvaluationResultDraft:
        """把 expected string 与 actual artifact identity 作确定性比较。"""
        actual = evaluation_input.actual_artifact
        passed = actual is not None and actual.artifact_id == evaluation_input.expected_output
        return EvaluationResultDraft(
            evaluator_id=context.evaluator_spec.evaluator_id,
            evaluator_version=context.evaluator_spec.evaluator_version,
            config_ref=context.evaluator_spec.config_ref,
            verdict=EvaluationVerdict.PASS if passed else EvaluationVerdict.FAIL,
            reason="artifact identity comparison",
        )


@pytest.mark.asyncio
async def test_execution_success_does_not_imply_evaluation_pass() -> None:
    outcome = make_outcome(
        OutcomeKind.SUCCESS,
        output_artifact_ref=ArtifactRef("wrong-answer-artifact"),
    )
    evaluation_input = EvaluationInput(
        case_ref=CASE_REF,
        expected_output="expected-answer-artifact",
        assertion_specs=[AssertionSpec("artifact-match", "artifact_identity")],
        actual_artifact=outcome.output_artifact_ref,
    )
    spec = EvaluatorSpec(
        evaluator_id="artifact-identity",
        evaluator_version="v1",
        evaluator_kind=EvaluatorKind.DETERMINISTIC,
        config_ref=VersionRef("evaluator_config", "v1"),
        score_direction=ScoreDirection.NOT_APPLICABLE,
    )

    result = await ArtifactIdentityEvaluator().evaluate(evaluation_input, EvaluatorContext(spec))
    assert outcome.kind is OutcomeKind.SUCCESS
    assert result.verdict is EvaluationVerdict.FAIL


def test_execution_contract_and_fixture_adapter_have_no_forbidden_dependencies_or_schemas() -> None:
    backend = Path(__file__).parents[2]
    paths = [
        backend / "app" / "core" / "evaluation" / "execution.py",
        backend / "app" / "adapters" / "evaluation" / "fixture.py",
    ]
    forbidden_prefixes = (
        "app.core.traces",
        "app.infrastructure",
        "app.services",
        "sqlalchemy",
        "celery",
        "redis",
        "litellm",
    )
    forbidden_symbols = {
        "AgentState",
        "LLMEngine",
        "LocalAgentFinalOutput",
        "RetrievalResult",
        "RunContext",
        "RuntimeEvent",
        "Span",
        "ToolResult",
        "Trace",
    }
    violations: list[str] = []

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                violations.extend(
                    f"{path.name}: import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith(forbidden_prefixes)
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden_prefixes):
                    violations.append(f"{path.name}: from {module}")
            elif isinstance(node, ast.Name) and node.id in forbidden_symbols:
                violations.append(f"{path.name}: symbol {node.id}")

    assert violations == []
