# ruff: noqa: D101, D102, D105, D415

"""Generation correctness / faithfulness 的严格单次 LLM Judge evaluator。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator

from app.core.evaluation.catalog import EvaluatorSpec, ScoreDirection
from app.core.evaluation.generation_evidence import FINAL_ANSWER_EVIDENCE_KIND, FinalAnswerEvidenceV1
from app.core.evaluation.immutable import FrozenJsonValue, freeze_json
from app.core.evaluation.ports import JudgeModelResponse
from app.core.evaluation.rag_artifact import RAG_ARTIFACT_EVIDENCE_KIND, RagEvaluationArtifactV1
from app.core.evaluation.references import EvidenceRef, VersionRef
from app.core.evaluation.results import EvaluationResultDraft, EvaluationVerdict

GENERATION_CORRECTNESS: Final[str] = "generation_correctness"
GENERATION_FAITHFULNESS: Final[str] = "generation_faithfulness"
CORRECTNESS_PROMPT_REF = VersionRef("judge_prompt", "llm-judge-correctness.v1")
FAITHFULNESS_PROMPT_REF = VersionRef("judge_prompt", "llm-judge-faithfulness.v1")
REASON_MAX_CHARS: Final[int] = 2000


class JudgeModelRefusal(Exception):
    """适配器识别到 provider 明确拒绝 Judge 请求。"""


class JudgeMalformedStructuredOutput(Exception):
    """适配器无法取得可解析 JSON 的 structured response。"""


class _JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    reason: StrictStr = Field(min_length=1, max_length=REASON_MAX_CHARS)

    @field_validator("score")
    @classmethod
    def _finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("score must be finite")
        return value


@dataclass(frozen=True, slots=True)
class SelectedContextItem:
    artifact_id: str
    invocation_index: int
    selection_rank: int
    document_id: str
    chunk_id: str
    context_block_id: str
    citation_id: str
    context_content_hash: str
    text: str


@dataclass(frozen=True, slots=True)
class GenerationJudgeInput:
    case_id: str
    question: str
    actual_answer: str
    reference_answer: str | None
    retrieved_context: tuple[SelectedContextItem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_context", tuple(self.retrieved_context))

    def provider_payload(self) -> FrozenJsonValue:
        return freeze_json(
            {
                "case_id": self.case_id,
                "question": self.question,
                "actual_answer": self.actual_answer,
                "reference_answer": self.reference_answer,
                "retrieved_context": [
                    {
                        "artifact_id": item.artifact_id,
                        "invocation_index": item.invocation_index,
                        "selection_rank": item.selection_rank,
                        "document_id": item.document_id,
                        "chunk_id": item.chunk_id,
                        "context_block_id": item.context_block_id,
                        "citation_id": item.citation_id,
                        "context_content_hash": item.context_content_hash,
                        "text": item.text,
                    }
                    for item in self.retrieved_context
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class _JudgeConfig:
    judge_model_ref: VersionRef
    temperature: float
    evaluation_timeout_seconds: float
    max_input_chars: int


def _config(spec: EvaluatorSpec) -> _JudgeConfig:
    if (
        spec.score_range != (0.0, 1.0)
        or spec.score_direction is not ScoreDirection.HIGHER_IS_BETTER
        or spec.threshold is None
        or not 0.0 <= spec.threshold <= 1.0
        or spec.prompt_ref is None
    ):
        raise ValueError("invalid judge evaluator spec")
    value = spec.config_snapshot
    if not isinstance(value, Mapping):
        raise ValueError("invalid judge config")
    model = value.get("judge_model_ref")
    if not isinstance(model, Mapping):
        raise ValueError("invalid judge model config")
    kind, opaque_value = model.get("kind"), model.get("opaque_value")
    temperature = value.get("temperature")
    timeout = value.get("evaluation_timeout_seconds")
    max_input_chars = value.get("max_input_chars")
    if (
        not isinstance(kind, str)
        or not kind
        or not isinstance(opaque_value, str)
        or not opaque_value
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or not isinstance(max_input_chars, int)
        or isinstance(max_input_chars, bool)
        or max_input_chars <= 0
    ):
        raise ValueError("invalid judge config")
    return _JudgeConfig(VersionRef(kind, opaque_value), float(temperature), float(timeout), max_input_chars)


def _failure(spec: EvaluatorSpec, reason: str, *, source_status: str) -> EvaluationResultDraft:
    return EvaluationResultDraft(
        evaluator_id=spec.evaluator_id,
        evaluator_version=spec.evaluator_version,
        config_ref=spec.config_ref,
        prompt_ref=spec.prompt_ref,
        verdict=EvaluationVerdict.ERROR,
        reason=reason,
        metadata={"source_status": source_status},
    )


def _expected_run_id(evaluation_input: object) -> str | None:
    actual = getattr(evaluation_input, "actual_artifact", None)
    artifact_id = getattr(actual, "artifact_id", None)
    prefix = "localagent-run://"
    if not isinstance(artifact_id, str) or not artifact_id.startswith(prefix):
        return None
    return artifact_id.removeprefix(prefix)


def _final_answer(evidence_refs: tuple[EvidenceRef, ...], expected_run_id: str) -> tuple[str, EvidenceRef] | None:
    refs = tuple(item for item in evidence_refs if item.kind == FINAL_ANSWER_EVIDENCE_KIND)
    if len(refs) != 1:
        return None
    ref = refs[0]
    try:
        artifact = FinalAnswerEvidenceV1.model_validate(ref.metadata["payload"])
    except (KeyError, TypeError, ValidationError):
        return None
    if artifact.run_id != expected_run_id or ref.identifier != artifact.evidence_id:
        return None
    return artifact.content, ref


def _selected_context(
    evidence_refs: tuple[EvidenceRef, ...], expected_run_id: str
) -> tuple[tuple[SelectedContextItem, ...], tuple[EvidenceRef, ...]] | None:
    refs = tuple(item for item in evidence_refs if item.kind == RAG_ARTIFACT_EVIDENCE_KIND)
    if not refs:
        return None
    context: list[SelectedContextItem] = []
    valid_refs: list[EvidenceRef] = []
    for ref in refs:
        try:
            artifact = RagEvaluationArtifactV1.model_validate(ref.metadata["payload"])
        except (KeyError, TypeError, ValidationError):
            return None
        if artifact.run_id != expected_run_id or ref.identifier != artifact.artifact_id:
            return None
        valid_refs.append(ref)
        for item in artifact.selected_items:
            context.append(
                SelectedContextItem(
                    artifact_id=artifact.artifact_id,
                    invocation_index=artifact.invocation_index,
                    selection_rank=item.selection_rank,
                    document_id=item.document_id,
                    chunk_id=item.chunk_id,
                    context_block_id=item.context_block_id,
                    citation_id=item.citation_id,
                    context_content_hash=item.context_content_hash,
                    text=item.text,
                )
            )
    context.sort(key=lambda item: (item.invocation_index, item.selection_rank))
    return tuple(context), tuple(valid_refs)


class _GenerationJudgeEvaluator:
    metric_name: str
    expected_prompt_ref: VersionRef

    async def evaluate(self, evaluation_input, context) -> EvaluationResultDraft:
        spec = context.evaluator_spec
        if spec.evaluator_id != self.metric_name or spec.prompt_ref != self.expected_prompt_ref:
            return _failure(spec, "judge_spec_invalid", source_status="JUDGE_SPEC_INVALID")
        try:
            config = _config(spec)
        except ValueError:
            return _failure(spec, "judge_spec_invalid", source_status="JUDGE_SPEC_INVALID")
        if context.judge_model is None:
            return _failure(spec, "judge_input_unavailable", source_status="JUDGE_MODEL_UNAVAILABLE")
        expected_run_id = _expected_run_id(evaluation_input)
        question = evaluation_input.input_payload.get("query") if isinstance(evaluation_input.input_payload, Mapping) else None
        if expected_run_id is None or not isinstance(question, str) or not question.strip():
            return _failure(spec, "judge_input_unavailable", source_status="JUDGE_INPUT_UNAVAILABLE")
        answer = _final_answer(evaluation_input.evidence_refs, expected_run_id)
        if answer is None:
            return _failure(spec, "judge_input_unavailable", source_status="JUDGE_INPUT_UNAVAILABLE")
        actual_answer, answer_ref = answer
        reference_answer: str | None = None
        selected: tuple[SelectedContextItem, ...] = ()
        context_refs: tuple[EvidenceRef, ...] = ()
        if self.metric_name == GENERATION_CORRECTNESS:
            if not isinstance(evaluation_input.expected_output, str) or not evaluation_input.expected_output:
                return _failure(spec, "judge_input_unavailable", source_status="JUDGE_INPUT_UNAVAILABLE")
            reference_answer = evaluation_input.expected_output
        else:
            selected_result = _selected_context(evaluation_input.evidence_refs, expected_run_id)
            if selected_result is None:
                return _failure(spec, "judge_input_unavailable", source_status="JUDGE_CONTEXT_UNAVAILABLE")
            selected, context_refs = selected_result
        judge_input = GenerationJudgeInput(
            case_id=evaluation_input.case_ref.case_id,
            question=question,
            actual_answer=actual_answer,
            reference_answer=reference_answer,
            retrieved_context=selected,
        )
        payload = judge_input.provider_payload()
        if len(_json(payload)) > config.max_input_chars:
            return _failure(spec, "judge_input_too_large", source_status="JUDGE_INPUT_TOO_LARGE")
        try:
            async with asyncio.timeout(config.evaluation_timeout_seconds):
                response = await context.judge_model.structured_generate(
                    prompt_ref=spec.prompt_ref,
                    input_payload=payload,
                    config=spec.config_snapshot,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return _failure(spec, "judge_timeout", source_status="JUDGE_TIMEOUT")
        except JudgeModelRefusal:
            return _failure(spec, "judge_refusal", source_status="JUDGE_REFUSAL")
        except JudgeMalformedStructuredOutput:
            return _failure(spec, "judge_malformed_structured_output", source_status="JUDGE_MALFORMED_STRUCTURED_OUTPUT")
        except Exception:
            return _failure(spec, "judge_provider_failure", source_status="JUDGE_PROVIDER_FAILURE")
        if not isinstance(response, JudgeModelResponse):
            return _failure(spec, "judge_malformed_structured_output", source_status="JUDGE_MALFORMED_STRUCTURED_OUTPUT")
        try:
            output = _JudgeOutput.model_validate(response.payload)
        except ValidationError:
            return _failure(spec, "judge_malformed_structured_output", source_status="JUDGE_MALFORMED_STRUCTURED_OUTPUT")
        verdict = EvaluationVerdict.PASS if output.score >= spec.threshold else EvaluationVerdict.FAIL
        return EvaluationResultDraft(
            evaluator_id=spec.evaluator_id,
            evaluator_version=spec.evaluator_version,
            config_ref=spec.config_ref,
            prompt_ref=spec.prompt_ref,
            verdict=verdict,
            reason=output.reason,
            score=output.score,
            evidence_refs=(answer_ref, *context_refs),
            metadata={
                "source_status": "JUDGE_SUCCESS",
                "judge_model_ref": {"kind": response.model_ref.kind, "opaque_value": response.model_ref.opaque_value},
                "judge_config": {
                    "judge_model_ref": {
                        "kind": config.judge_model_ref.kind,
                        "opaque_value": config.judge_model_ref.opaque_value,
                    },
                    "temperature": config.temperature,
                    "evaluation_timeout_seconds": config.evaluation_timeout_seconds,
                    "max_input_chars": config.max_input_chars,
                },
            },
        )


class GenerationCorrectnessEvaluator(_GenerationJudgeEvaluator):
    metric_name = GENERATION_CORRECTNESS
    expected_prompt_ref = CORRECTNESS_PROMPT_REF


class GenerationFaithfulnessEvaluator(_GenerationJudgeEvaluator):
    metric_name = GENERATION_FAITHFULNESS
    expected_prompt_ref = FAITHFULNESS_PROMPT_REF


def _json(value: FrozenJsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=dict)


__all__ = [
    "CORRECTNESS_PROMPT_REF",
    "FAITHFULNESS_PROMPT_REF",
    "GENERATION_CORRECTNESS",
    "GENERATION_FAITHFULNESS",
    "GenerationCorrectnessEvaluator",
    "GenerationFaithfulnessEvaluator",
    "GenerationJudgeInput",
    "JudgeMalformedStructuredOutput",
    "JudgeModelRefusal",
    "REASON_MAX_CHARS",
    "SelectedContextItem",
]
