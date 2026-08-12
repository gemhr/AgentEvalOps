"""独立于 legacy Trace/Eval 的 AgentEvalOps Evaluation Domain。"""

# ruff: noqa: D415

from app.core.evaluation.catalog import (
    AssertionSpec,
    DatasetVersion,
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluatorKind,
    EvaluatorSpec,
    PolicyDisposition,
    ScoreDirection,
    TestCaseVersion,
)
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.execution import (
    FIXTURE_TARGET_KIND,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionTarget,
    ExecutionTargetRef,
    OutcomeKind,
    UnsupportedTargetCapabilitiesError,
    validate_target_capabilities,
)
from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue, JsonValue, freeze_json
from app.core.evaluation.ports import Evaluator, JudgeModelPort
from app.core.evaluation.references import (
    ArtifactRef,
    CapabilityRequirement,
    CaseVersionRef,
    EvidenceRef,
    VersionRef,
)
from app.core.evaluation.results import (
    EvaluationResult,
    EvaluationResultDraft,
    EvaluationVerdict,
    ProvenanceCompleteness,
)

__all__ = [
    "ArtifactRef",
    "AssertionSpec",
    "CapabilityRequirement",
    "CaseVersionRef",
    "DatasetVersion",
    "EvaluationInput",
    "EvaluationPolicy",
    "EvaluationResult",
    "EvaluationResultDraft",
    "EvaluationSuiteVersion",
    "EvaluationVerdict",
    "Evaluator",
    "EvaluatorContext",
    "EvaluatorKind",
    "EvaluatorSpec",
    "EvidenceRef",
    "ExecutionOutcome",
    "ExecutionRequest",
    "ExecutionTarget",
    "ExecutionTargetRef",
    "FIXTURE_TARGET_KIND",
    "FrozenDict",
    "FrozenJsonValue",
    "JsonValue",
    "JudgeModelPort",
    "OutcomeKind",
    "PolicyDisposition",
    "ProvenanceCompleteness",
    "ScoreDirection",
    "TestCaseVersion",
    "UnsupportedTargetCapabilitiesError",
    "VersionRef",
    "freeze_json",
    "validate_target_capabilities",
]
