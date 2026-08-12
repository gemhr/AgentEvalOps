import ast
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.evaluation import (
    ArtifactRef,
    EvaluationResult,
    EvaluationResultDraft,
    EvaluationVerdict,
    EvidenceRef,
    ProvenanceCompleteness,
    VersionRef,
)

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("verdict", list(EvaluationVerdict))
def test_result_draft_supports_every_verdict_and_optional_score(verdict: EvaluationVerdict) -> None:
    draft = EvaluationResultDraft(
        evaluator_id="evaluator-a",
        evaluator_version="v1",
        config_ref=VersionRef("config", "v1"),
        verdict=verdict,
        reason=f"{verdict} reason",
        score=None,
        evidence_refs=[EvidenceRef("log", "log-a")],
        metadata={"details": ["immutable"]},
    )
    assert draft.verdict is verdict
    assert draft.score is None
    assert draft.evidence_refs[0].kind == "log"


def make_result(**changes: object) -> EvaluationResult:
    values = {
        "result_id": "result-a",
        "run_id": "run-a",
        "attempt_id": "attempt-a",
        "dataset_id": "dataset-a",
        "dataset_version": "v1",
        "case_id": "case-a",
        "case_version": "v2",
        "suite_id": "suite-a",
        "suite_version": "v3",
        "evaluator_id": "judge-a",
        "evaluator_version": "v4",
        "config_ref": VersionRef("config", "config-v1"),
        "prompt_ref": VersionRef("prompt", "prompt-v2"),
        "execution_target_id": "target-a",
        "target_version_ref": VersionRef("target", "opaque-target-v1"),
        "execution_request_id": "request-a",
        "output_artifact_ref": ArtifactRef("artifact-a", digest="sha256:abc"),
        "score": 0.9,
        "verdict": EvaluationVerdict.PASS,
        "reason": "requirements satisfied",
        "evidence_refs": [EvidenceRef("observation", "obs-a")],
        "provenance_completeness": ProvenanceCompleteness.COMPLETE,
        "metadata": {"labels": ["critical"]},
        "created_at": NOW,
    }
    values.update(changes)
    return EvaluationResult(**values)  # type: ignore[arg-type]


def test_complete_result_is_trace_independent_and_deeply_immutable() -> None:
    metadata = {"labels": ["critical"]}
    result = make_result(metadata=metadata)
    metadata["labels"].append("mutated")

    assert result.provenance_completeness is ProvenanceCompleteness.COMPLETE
    assert result.output_artifact_ref == ArtifactRef("artifact-a", digest="sha256:abc")
    assert result.evidence_refs == (EvidenceRef("observation", "obs-a"),)
    assert result.metadata["labels"] == ("critical",)
    with pytest.raises(FrozenInstanceError):
        result.score = 0.1  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.metadata["labels"] = ()  # type: ignore[index]


def test_partial_result_makes_missing_target_version_explicit() -> None:
    result = make_result(
        target_version_ref=None,
        output_artifact_ref=None,
        score=None,
        verdict=EvaluationVerdict.INCONCLUSIVE,
        reason="target version unavailable",
        provenance_completeness=ProvenanceCompleteness.PARTIAL,
    )
    assert result.target_version_ref is None
    assert result.provenance_completeness is ProvenanceCompleteness.PARTIAL


def test_complete_result_cannot_hide_missing_required_provenance() -> None:
    with pytest.raises(ValueError, match="target_version_ref"):
        make_result(target_version_ref=None)


def test_result_rejects_unknown_verdict_and_provenance_state() -> None:
    with pytest.raises(ValueError, match="unknown verdict"):
        make_result(verdict="MAYBE")
    with pytest.raises(ValueError, match="unknown provenance_completeness"):
        make_result(provenance_completeness="UNKNOWN")


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_result_rejects_non_finite_score(score: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        make_result(score=score)


def test_evaluation_package_has_no_forbidden_imports() -> None:
    package = Path(__file__).parents[2] / "app" / "core" / "evaluation"
    forbidden_prefixes = (
        "app.infrastructure",
        "app.services",
        "app.core.traces",
        "sqlalchemy",
        "celery",
        "redis",
        "litellm",
    )
    forbidden_symbols = {"Trace", "Span", "TraceModel", "EvalRunModel", "LLMEngine"}
    violations: list[str] = []

    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden_prefixes):
                    violations.append(f"{path.name}: from {module}")
                for alias in node.names:
                    if alias.name in forbidden_symbols:
                        violations.append(f"{path.name}: symbol {alias.name}")

    assert violations == []
