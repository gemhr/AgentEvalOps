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
from app.core.evaluation.run_attempts import (
    AttemptClaimLost,
    AttemptNotClaimed,
    AttemptStatus,
    EvaluationEntityNotFound,
    EvaluationPersistenceError,
    EvaluationRun,
    ExecutionAttempt,
    ResultAlreadyFinalized,
    RetryAlreadyCreated,
    RunNotFinishable,
    RunStatus,
)

__all__ = [
    "ArtifactRef",
    "AttemptClaimLost",
    "AttemptNotClaimed",
    "AttemptStatus",
    "AssertionSpec",
    "CapabilityRequirement",
    "CaseVersionRef",
    "DatasetVersion",
    "EvaluationInput",
    "EvaluationEntityNotFound",
    "EvaluationPersistenceError",
    "EvaluationRun",
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
    "ExecutionAttempt",
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
    "ResultAlreadyFinalized",
    "RetryAlreadyCreated",
    "RunNotFinishable",
    "RunStatus",
    "ScoreDirection",
    "TestCaseVersion",
    "UnsupportedTargetCapabilitiesError",
    "VersionRef",
    "freeze_json",
    "validate_target_capabilities",
]
