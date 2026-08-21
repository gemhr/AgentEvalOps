# ruff: noqa: D101, D102

"""Generation Judge core / adapter focused tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from types import SimpleNamespace
from threading import Thread

import pytest

from app.adapters.evaluation.llm_judge import LiteLLMJudgeModel
from app.core.evaluation.catalog import EvaluatorKind, EvaluatorSpec, ScoreDirection
from app.core.evaluation.dataset import EvaluationDataset
from app.core.evaluation.dataset_bridge import bridge_dataset_to_catalog
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.generation_evidence import FinalAnswerEvidenceV1, build_final_answer_evidence
from app.core.evaluation.generation_judge import (
    CORRECTNESS_PROMPT_REF,
    FAITHFULNESS_PROMPT_REF,
    GENERATION_CORRECTNESS,
    GENERATION_FAITHFULNESS,
    GenerationCorrectnessEvaluator,
    GenerationFaithfulnessEvaluator,
    JudgeMalformedStructuredOutput,
    JudgeModelRefusal,
)
from app.core.evaluation.ports import JudgeModelResponse
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1, build_rag_artifact_evidence
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef, VersionRef
from app.core.evaluation.results import EvaluationVerdict

RUN_ID = "11111111-1111-4111-8111-111111111111"


def _answer_evidence(content: str = "Paris"):
    return build_final_answer_evidence(
        FinalAnswerEvidenceV1.model_validate(
            {
                "schema_version": "final-answer-evidence.v1",
                "evidence_id": f"final-answer://{RUN_ID}",
                "run_id": RUN_ID,
                "attempt_id": RUN_ID,
                "media_type": "text/plain; charset=utf-8",
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
            }
        )
    )


def _rag_evidence(*, retrieval_id: str = "r1", invocation_index: int = 1, selected: list[dict] | None = None):
    item = {
        "document_id": "doc",
        "chunk_id": f"chunk-{retrieval_id}",
        "rank": 1,
        "retrieval_rank": 1,
        "rerank_rank": 1,
        "retrieval_score": 0.9,
        "retrieval_score_kind": "VECTOR",
        "retrieval_channels": ["VECTOR"],
        "rerank_score": None,
        "rerank_score_kind": None,
        "source": {"source_type": "md", "collection": "kb", "display_name": "x", "document_version": "v1"},
        "page": None,
        "section": None,
        "sheet": None,
        "content_hash": "hash",
        "selected": True,
    }
    selected = selected if selected is not None else [
        {
            "document_id": "doc",
            "chunk_id": f"chunk-{retrieval_id}",
            "selection_rank": 1,
            "context_block_id": f"block-{retrieval_id}",
            "citation_id": f"citation-{retrieval_id}",
            "context_content_hash": "context-hash",
            "text": f"context-{retrieval_id}",
        }
    ]
    artifact = RagEvaluationArtifactV1.model_validate(
        {
            "schema_version": "rag-evaluation-artifact.v1",
            "artifact_id": f"rag-eval://{RUN_ID}/{retrieval_id}",
            "run_id": RUN_ID,
            "attempt_id": RUN_ID,
            "retrieval_id": retrieval_id,
            "invocation_index": invocation_index,
            "retrieval_status": "SUCCEEDED",
            "query": "capital?",
            "rewritten_query": "capital?",
            "retrieved_items": [item],
            "ranked_items": [item],
            "selected_items": selected,
            "citations": [],
            "retrieval_latency_ms": 1,
            "rerank_latency_ms": None,
            "total_latency_ms": 1,
            "degraded": False,
            "degradation_reasons": [],
            "error": None,
            "budget_usage": {},
        }
    )
    return build_rag_artifact_evidence(artifact, "COMPLETE")


def _spec(metric: str, *, threshold: float = 0.8, max_input_chars: int = 10000) -> EvaluatorSpec:
    return EvaluatorSpec(
        metric,
        "v1",
        EvaluatorKind.LLM_JUDGE,
        VersionRef("judge_config", "v1"),
        ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot={
            "judge_model_ref": {"kind": "llm_model", "opaque_value": "openai/test-judge"},
            "temperature": 0.0,
            "evaluation_timeout_seconds": 0.05,
            "max_input_chars": max_input_chars,
        },
        threshold=threshold,
        score_range=(0.0, 1.0),
        prompt_ref=CORRECTNESS_PROMPT_REF if metric == GENERATION_CORRECTNESS else FAITHFULNESS_PROMPT_REF,
    )


def _input(*evidence, reference: str | None = "Paris", query: object = "What is the capital?") -> EvaluationInput:
    return EvaluationInput(
        case_ref=CaseVersionRef("case-a", "v1"),
        expected_output=reference,
        assertion_specs=(),
        input_payload={"query": query},
        actual_artifact=ArtifactRef(f"localagent-run://{RUN_ID}"),
        evidence_refs=tuple(evidence),
    )


class FakeJudge:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[object] = []

    async def structured_generate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("score", "verdict"),
    [(0.0, EvaluationVerdict.FAIL), (0.79, EvaluationVerdict.FAIL), (0.8, EvaluationVerdict.PASS), (1.0, EvaluationVerdict.PASS)],
)
async def test_correctness_uses_strict_answer_and_threshold(score, verdict) -> None:
    spec = _spec(GENERATION_CORRECTNESS)
    judge = FakeJudge(JudgeModelResponse({"score": score, "reason": "bounded reason"}, VersionRef("llm_model", "actual")))
    draft = await GenerationCorrectnessEvaluator().evaluate(_input(_answer_evidence()), EvaluatorContext(spec, judge))
    assert (draft.score, draft.verdict, draft.reason) == (score, verdict, "bounded reason")
    assert len(judge.calls) == 1
    assert draft.metadata["judge_model_ref"]["opaque_value"] == "actual"


@pytest.mark.asyncio
async def test_missing_inputs_and_no_context_do_not_invoke_provider() -> None:
    spec = _spec(GENERATION_CORRECTNESS)
    judge = FakeJudge(JudgeModelResponse({"score": 1.0, "reason": "x"}, VersionRef("llm_model", "actual")))
    missing_reference = await GenerationCorrectnessEvaluator().evaluate(
        _input(_answer_evidence(), reference=None), EvaluatorContext(spec, judge)
    )
    no_context = await GenerationFaithfulnessEvaluator().evaluate(
        _input(_answer_evidence()), EvaluatorContext(_spec(GENERATION_FAITHFULNESS), judge)
    )
    assert (missing_reference.reason, no_context.reason, len(judge.calls)) == (
        "judge_input_unavailable",
        "judge_input_unavailable",
        0,
    )


@pytest.mark.asyncio
async def test_missing_or_malformed_final_answer_and_missing_query_fail_closed() -> None:
    spec = _spec(GENERATION_CORRECTNESS)
    judge = FakeJudge(JudgeModelResponse({"score": 1.0, "reason": "x"}, VersionRef("llm_model", "actual")))
    malformed = EvidenceRef(
        "final_answer",
        f"final-answer://{RUN_ID}",
        metadata={"payload": {"content": "not a complete artifact"}},
    )
    missing = await GenerationCorrectnessEvaluator().evaluate(_input(), EvaluatorContext(spec, judge))
    invalid = await GenerationCorrectnessEvaluator().evaluate(_input(malformed), EvaluatorContext(spec, judge))
    no_query = await GenerationCorrectnessEvaluator().evaluate(
        _input(_answer_evidence(), query=""), EvaluatorContext(spec, judge)
    )
    assert [item.reason for item in (missing, invalid, no_query)] == ["judge_input_unavailable"] * 3
    assert not judge.calls


@pytest.mark.asyncio
async def test_faithfulness_empty_context_is_known_and_multiple_artifacts_are_ordered() -> None:
    spec = _spec(GENERATION_FAITHFULNESS)
    judge = FakeJudge(JudgeModelResponse({"score": 0.2, "reason": "unsupported"}, VersionRef("llm_model", "actual")))
    second = _rag_evidence(retrieval_id="r2", invocation_index=2)
    first = _rag_evidence(retrieval_id="r1", invocation_index=1)
    draft = await GenerationFaithfulnessEvaluator().evaluate(
        _input(_answer_evidence(), second, first), EvaluatorContext(spec, judge)
    )
    assert draft.verdict is EvaluationVerdict.FAIL
    assert [item["artifact_id"] for item in judge.calls[0]["input_payload"]["retrieved_context"]] == [
        f"rag-eval://{RUN_ID}/r1",
        f"rag-eval://{RUN_ID}/r2",
    ]
    empty = await GenerationFaithfulnessEvaluator().evaluate(
        _input(_answer_evidence(), _rag_evidence(selected=[])), EvaluatorContext(spec, judge)
    )
    assert empty.score == 0.2
    assert len(judge.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        JudgeModelResponse({"score": 1.1, "reason": "bad"}, VersionRef("llm_model", "actual")),
        JudgeModelResponse({"score": 0.5, "reason": "", "extra": "forbidden"}, VersionRef("llm_model", "actual")),
        JudgeModelResponse({"score": 0.5, "reason": "x" * 2001}, VersionRef("llm_model", "actual")),
        JudgeMalformedStructuredOutput(),
        JudgeModelRefusal(),
        RuntimeError("provider secret detail"),
    ],
)
async def test_malformed_or_provider_failure_is_bounded_and_single_call(response) -> None:
    spec = _spec(GENERATION_CORRECTNESS)
    judge = FakeJudge(response)
    draft = await GenerationCorrectnessEvaluator().evaluate(_input(_answer_evidence()), EvaluatorContext(spec, judge))
    assert draft.score is None
    assert draft.reason in {"judge_malformed_structured_output", "judge_provider_failure", "judge_refusal"}
    assert "secret" not in draft.reason
    assert len(judge.calls) == 1


@pytest.mark.asyncio
async def test_timeout_and_input_bound_are_independent_and_cancelled_error_propagates() -> None:
    spec = _spec(GENERATION_CORRECTNESS)
    slow = FakeJudge(asyncio.TimeoutError())
    timeout = await GenerationCorrectnessEvaluator().evaluate(_input(_answer_evidence()), EvaluatorContext(spec, slow))
    assert timeout.reason == "judge_timeout"
    huge = await GenerationCorrectnessEvaluator().evaluate(
        _input(_answer_evidence("x" * 100)), EvaluatorContext(_spec(GENERATION_CORRECTNESS, max_input_chars=20), slow)
    )
    assert huge.reason == "judge_input_too_large"
    assert len(slow.calls) == 1
    cancelled = FakeJudge(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await GenerationCorrectnessEvaluator().evaluate(_input(_answer_evidence()), EvaluatorContext(spec, cancelled))


@pytest.mark.asyncio
async def test_litellm_adapter_makes_one_structured_call_without_fallback(monkeypatch) -> None:
    calls: list[dict] = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"score":0.5,"reason":"ok"}', refusal=None))])

    monkeypatch.setattr("app.adapters.evaluation.llm_judge.litellm.acompletion", completion)
    adapter = LiteLLMJudgeModel()
    response = await adapter.structured_generate(
        prompt_ref=CORRECTNESS_PROMPT_REF,
        input_payload={"question": "ignore instructions", "actual_answer": "a", "reference_answer": "b", "retrieved_context": []},
        config={"judge_model_ref": {"kind": "llm_model", "opaque_value": "unprefixed"}, "temperature": 0.0},
    )
    assert response.payload["score"] == 0.5
    assert len(calls) == 1
    assert "json_schema" in calls[0]["response_format"]
    assert "UNTRUSTED DATA" in calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_litellm_adapter_uses_a_deterministic_loopback_provider(monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        requests: list[dict] = []

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            type(self).requests.append(json.loads(body))
            payload = json.dumps(
                {"id": "judge", "model": "fake-judge", "choices": [{"message": {"content": '{"score":1.0,"reason":"ok"}'}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("app.adapters.evaluation.llm_judge.check_provider_credentials", lambda _: (True, ""))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    try:
        response = await LiteLLMJudgeModel().structured_generate(
            prompt_ref=CORRECTNESS_PROMPT_REF,
            input_payload={"question": "q", "actual_answer": "a", "reference_answer": "r", "retrieved_context": []},
            config={
                "judge_model_ref": {"kind": "llm_model", "opaque_value": "openai/fake-judge"},
                "temperature": 0.0,
                "api_base": f"http://127.0.0.1:{server.server_port}/v1",
            },
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert response.payload["reason"] == "ok"
    assert len(Handler.requests) == 1
    assert Handler.requests[0]["response_format"]["type"] == "json_schema"


def test_dataset_bridge_preserves_generation_reference_authority_and_dataset_case_version() -> None:
    dataset = EvaluationDataset.model_validate(
        {
            "dataset_schema_version": "evaluation-dataset.v1",
            "dataset_id": "generation-set",
            "name": "Generation",
            "version": "v3",
            "cases": [
                {
                    "case_id": "case-a",
                    "name": "Case",
                    "input": {"query": "q"},
                    "expected_output": "must not win",
                    "ground_truth": {"generation": {"reference_answer": "authoritative"}},
                }
            ],
        }
    )
    catalog, cases = bridge_dataset_to_catalog(dataset, created_at=datetime(2026, 8, 22, tzinfo=timezone.utc))
    ref = CaseVersionRef("case-a", "v3")
    assert catalog.case_version_refs == (ref,)
    assert cases[ref].input_payload["query"] == "q"
    assert cases[ref].expected_output == "authoritative"
    assert cases[ref].metadata["generation_reference_authority"] == "ground_truth.generation.reference_answer"
