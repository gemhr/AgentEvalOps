"""Adapters for the existing trace/session metric catalog."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any

from app.core.evaluation.catalog import EvaluatorKind, EvaluatorSpec, ScoreDirection
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.execution import ExecutionOutcome, ExecutionRequest, ExecutionTarget, ExecutionTargetRef, OutcomeKind
from app.core.evaluation.ports import Evaluator
from app.core.evaluation.references import ArtifactRef, VersionRef
from app.core.evaluation.results import EvaluationResultDraft, EvaluationVerdict
from app.core.evaluation.run_attempts import ExecutionAttempt
from app.core.traces.entities import Trace
from app.core.evals.metrics import get_metric, get_session_metric
from app.infrastructure.llm.engine import LLMEngine

LEGACY_TARGET_ID = "stored-evaluation-input"
LEGACY_TARGET_KIND = "STORED_TRACE_OR_SESSION"
LEGACY_TARGET_VERSION = VersionRef("target", "stored-evaluation-input.v1")
LEGACY_CONFIG_VERSION = "legacy-metric-config.v1"
LEGACY_EVALUATOR_VERSION = "legacy-metric.v1"
LEGACY_TRACE_SIGNAL_METRICS = ("confidence", "loop_detection", "tool_correctness", "coherence")


def legacy_target_ref() -> ExecutionTargetRef:
    """Return the stable identity used for persisted evaluation inputs."""
    return ExecutionTargetRef(
        target_id=LEGACY_TARGET_ID,
        target_kind=LEGACY_TARGET_KIND,
        target_version_ref=LEGACY_TARGET_VERSION,
    )


class StoredEvaluationTarget:
    """Record execution of an immutable trace/session input snapshot."""

    def __init__(self, ref: ExecutionTargetRef) -> None:
        self._ref = ref

    @property
    def target_ref(self) -> ExecutionTargetRef:
        """Expose the resolver-bound target identity."""
        return self._ref

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        """Record a successful evaluation-input execution without external IO."""
        now = datetime.now(timezone.utc)
        return ExecutionOutcome(
            request_id=request.request_id,
            kind=OutcomeKind.SUCCESS,
            started_at=now,
            finished_at=now,
            output_artifact_ref=ArtifactRef(
                artifact_id=f"stored-evaluation-input:{request.case_ref.case_id}:{request.case_ref.version}",
                media_type="application/vnd.agent-evalops.stored-input+json",
            ),
            metadata={"input_source": "attempt_request_snapshot"},
        )


class LegacyMetricEvaluator:
    """Run a registered legacy metric while returning a canonical Result draft."""

    def __init__(self, metric_name: str, config: dict[str, Any]) -> None:
        self._metric_name = metric_name
        self._config = config

    async def evaluate(self, value: EvaluationInput, context: EvaluatorContext) -> EvaluationResultDraft:
        """Evaluate the trace/session snapshot and bind its configured identity."""
        payload = value.input_payload
        if not isinstance(payload, Mapping):
            raise ValueError("persisted metric input must be an object")
        threshold = self._config.get("threshold")
        model = self._config.get("model")
        llm = LLMEngine()
        if self._config.get("scope") == "SESSION":
            traces = [Trace.model_validate(item) for item in payload["traces"]]
            signals: dict[str, dict[str, float]] = {}
            for index, trace in enumerate(traces):
                signals[str(trace.trace_id)] = {}
                for signal_name in LEGACY_TRACE_SIGNAL_METRICS:
                    signal_result = await get_metric(signal_name)().evaluate(
                        trace, llm, model=model, session_traces=traces[:index] or None
                    )
                    signals[str(trace.trace_id)][signal_name] = float(signal_result.score)
            result = await get_session_metric(self._metric_name)().evaluate(
                str(payload["session_id"]), traces, llm, model=model,
                signal_weights=self._config.get("signal_weights"),
                precomputed_signals=signals,
            )
        else:
            trace = Trace.model_validate(payload["trace"])
            metric_cls = get_metric(self._metric_name)
            metric = metric_cls()
            result = await metric.evaluate(
                trace, llm, threshold=threshold, model=model,
                session_traces=[Trace.model_validate(item) for item in payload.get("session_traces", [])] or None,
            )
        value_score = float(result.score)
        spec = context.evaluator_spec
        verdict = EvaluationVerdict.PASS if value_score >= (threshold if threshold is not None else 0.5) else EvaluationVerdict.FAIL
        return EvaluationResultDraft(
            evaluator_id=spec.evaluator_id,
            evaluator_version=spec.evaluator_version,
            config_ref=spec.config_ref,
            verdict=verdict,
            reason=result.reason or "legacy metric evaluated",
            score=value_score,
            metadata={"legacy_metric": self._metric_name, **result.metadata},
        )


class LegacyMetricResolver:
    """Resolve only metric identities frozen into the Suite snapshot."""

    def resolve(self, spec: EvaluatorSpec):
        """Resolve the metric implementation from the immutable suite slot."""
        from app.services.evaluation.loop import ResolvedEvaluator

        config = dict(spec.config_snapshot)
        return ResolvedEvaluator(
            spec.evaluator_id,
            spec.evaluator_version,
            LegacyMetricEvaluator(spec.evaluator_id, config),
        )


class LegacyExecutionTargetResolver:
    """Resolve the frozen stored-input target identity."""

    def resolve(self, target_ref: ExecutionTargetRef) -> ExecutionTarget:
        """Reject target identities outside this compatibility adapter."""
        if target_ref != legacy_target_ref():
            raise ValueError("unknown legacy stored-input target")
        return StoredEvaluationTarget(target_ref)


def metric_spec(name: str, *, scope: str, model: str, threshold: float | None = None, signal_weights: dict[str, float] | None = None) -> EvaluatorSpec:
    """Freeze legacy metric configuration into a canonical evaluator slot."""
    if scope == "SESSION":
        metric = get_session_metric(name)()
        default_threshold = metric.threshold
        kind = EvaluatorKind.DETERMINISTIC
    else:
        metric = get_metric(name)()
        default_threshold = metric.threshold
        kind = EvaluatorKind.LLM_JUDGE
    config = {"scope": scope, "model": model, "threshold": default_threshold if threshold is None else threshold}
    if signal_weights:
        config["signal_weights"] = signal_weights
    return EvaluatorSpec(
        evaluator_id=name,
        evaluator_version=LEGACY_EVALUATOR_VERSION,
        evaluator_kind=kind,
        config_ref=VersionRef("legacy_metric_config", LEGACY_CONFIG_VERSION),
        score_direction=ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot=config,
        threshold=config["threshold"],
        score_range=(0.0, 1.0),
    )
