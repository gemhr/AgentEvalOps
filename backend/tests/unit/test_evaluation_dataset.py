"""Evaluation Dataset v1 - Case / Ground Truth / Dataset schema and loader tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.core.evaluation.dataset import (
    EVALUATION_DATASET_ANSWERABILITY_SCHEMA_VERSION,
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationDatasetLoadError,
    GroundTruth,
    iter_cases,
    load_dataset,
    validate_case,
    validate_dataset,
)


def _ground_truth_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "retrieval": {
            "relevant_chunks": [
                {"document_id": "doc1", "chunk_id": "chunk10"},
                {"document_id": "doc1", "chunk_id": "chunk11"},
            ]
        },
        "ranking": {
            "graded_relevance": [
                {"chunk_id": "chunk10", "relevance": 3},
                {"chunk_id": "chunk11", "relevance": 1},
            ]
        },
        "generation": {"reference_answer": "CDT 字段映射的参考解释。"},
    }
    payload.update(changes)
    return payload


def _case_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "case_id": "case-001",
        "name": "CDT 字段映射解释",
        "input": {"query": "解释CDT字段映射"},
        "expected_output": "应说明 CDT 字段映射规则。",
        "ground_truth": _ground_truth_payload(),
        "metadata": {"topic": "cdt"},
    }
    payload.update(changes)
    return payload


def _dataset_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "dataset_schema_version": EVALUATION_DATASET_SCHEMA_VERSION,
        "dataset_id": "rag-regression",
        "name": "RAG_REGRESSION_V1",
        "description": "RAG 回归集",
        "version": "v1",
        "cases": [_case_payload()],
    }
    payload.update(changes)
    return payload


def test_valid_case_with_all_ground_truth_sections() -> None:
    case = validate_case(_case_payload())
    assert case.case_id == "case-001"
    assert case.input == {"query": "解释CDT字段映射"}
    assert case.ground_truth.retrieval is not None
    assert case.ground_truth.ranking is not None
    assert case.ground_truth.generation is not None


def test_valid_case_without_optional_fields() -> None:
    payload = _case_payload()
    del payload["expected_output"]
    del payload["metadata"]
    case = validate_case(payload)
    assert case.expected_output is None
    assert case.metadata == {}


def test_case_missing_case_id_rejected() -> None:
    payload = _case_payload()
    del payload["case_id"]
    with pytest.raises(ValidationError):
        validate_case(payload)


def test_case_invalid_case_id_format_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid case_id format"):
        validate_case(_case_payload(case_id="case 001"))


def test_case_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_case(_case_payload(run_id="must-not-exist"))


def test_case_non_object_ground_truth_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_case(_case_payload(ground_truth={"answer": "xxx"}))

    with pytest.raises(ValidationError):
        validate_case(_case_payload(ground_truth="xxx"))


def test_case_empty_input_rejected() -> None:
    with pytest.raises(ValidationError, match="input must not be empty"):
        validate_case(_case_payload(input={}))


def test_case_non_json_input_rejected() -> None:
    with pytest.raises(ValidationError, match="JSON-compatible"):
        validate_case(_case_payload(input={"query": {"bad": {1, 2}}}))


def test_case_does_not_bind_runtime_identity() -> None:
    payload = _case_payload()
    assert "run_id" not in payload
    assert "artifact" not in payload


def test_retrieval_ground_truth_chunks() -> None:
    case = validate_case(_case_payload(ground_truth=_ground_truth_payload(ranking=None, generation=None)))
    assert case.ground_truth.retrieval is not None
    assert case.ground_truth.retrieval.chunk_identities() == {
        ("doc1", "chunk10"),
        ("doc1", "chunk11"),
    }


def test_retrieval_ground_truth_rejects_empty_chunks() -> None:
    payload = _ground_truth_payload(ranking=None, generation=None)
    payload["retrieval"] = {"relevant_chunks": []}
    with pytest.raises(ValidationError):
        validate_case(_case_payload(ground_truth=payload))


def test_retrieval_ground_truth_rejects_duplicate_chunks() -> None:
    payload = _ground_truth_payload(ranking=None, generation=None)
    payload["retrieval"] = {
        "relevant_chunks": [
            {"document_id": "doc1", "chunk_id": "chunk10"},
            {"document_id": "doc1", "chunk_id": "chunk10"},
        ]
    }
    with pytest.raises(ValidationError, match="duplicate relevant chunk"):
        validate_case(_case_payload(ground_truth=payload))


def test_retrieval_ground_truth_rejects_missing_chunk_id() -> None:
    payload = _ground_truth_payload(ranking=None, generation=None)
    payload["retrieval"] = {"relevant_chunks": [{"document_id": "doc1"}]}
    with pytest.raises(ValidationError):
        validate_case(_case_payload(ground_truth=payload))


def test_ranking_ground_truth_graded_relevance() -> None:
    case = validate_case(_case_payload(ground_truth=_ground_truth_payload(retrieval=None, generation=None)))
    assert case.ground_truth.ranking is not None
    grades = {item.identity(): item.relevance for item in case.ground_truth.ranking.graded_relevance}
    assert grades == {(None, "chunk10"): 3, (None, "chunk11"): 1}


def test_ranking_ground_truth_rejects_negative_relevance() -> None:
    payload = _ground_truth_payload(retrieval=None, generation=None)
    payload["ranking"] = {"graded_relevance": [{"chunk_id": "chunk10", "relevance": -1}]}
    with pytest.raises(ValidationError):
        validate_case(_case_payload(ground_truth=payload))


def test_ranking_ground_truth_rejects_duplicate_chunk() -> None:
    payload = _ground_truth_payload(retrieval=None, generation=None)
    payload["ranking"] = {
        "graded_relevance": [
            {"chunk_id": "chunk10", "relevance": 3},
            {"chunk_id": "chunk10", "relevance": 2},
        ]
    }
    with pytest.raises(ValidationError, match="duplicate graded relevance"):
        validate_case(_case_payload(ground_truth=payload))


def test_generation_ground_truth_reference_answer() -> None:
    case = validate_case(_case_payload(ground_truth=_ground_truth_payload(retrieval=None, ranking=None)))
    assert case.ground_truth.generation is not None
    assert case.ground_truth.generation.reference_answer == "CDT 字段映射的参考解释。"


def test_generation_ground_truth_rejects_empty_reference_answer() -> None:
    payload = _ground_truth_payload(retrieval=None, ranking=None)
    payload["generation"] = {"reference_answer": ""}
    with pytest.raises(ValidationError):
        validate_case(_case_payload(ground_truth=payload))


def test_ground_truth_requires_at_least_one_section() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        GroundTruth.model_validate({})


def test_ground_truth_rejects_unknown_section() -> None:
    payload = _ground_truth_payload(retrieval=None, ranking=None, generation=None)
    payload["answer"] = "xxx"
    with pytest.raises(ValidationError):
        validate_case(_case_payload(ground_truth=payload))


def test_dataset_load_from_json_file(tmp_path) -> None:
    file_path = tmp_path / "dataset.json"
    file_path.write_text(json.dumps(_dataset_payload(), ensure_ascii=False), encoding="utf-8")
    dataset = load_dataset(file_path)
    assert dataset.dataset_id == "rag-regression"
    assert dataset.version == "v1"
    assert len(dataset) == 1


def test_dataset_iterate_cases() -> None:
    dataset = validate_dataset(
        _dataset_payload(
            cases=[
                _case_payload(),
                _case_payload(case_id="case-002", name="second"),
            ]
        )
    )
    assert [case.case_id for case in iter_cases(dataset)] == ["case-001", "case-002"]


def test_dataset_version_validation_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError, match="unsupported dataset_schema_version"):
        validate_dataset(_dataset_payload(dataset_schema_version="evaluation-dataset.v0"))


def test_dataset_rejects_duplicate_case_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate case_id"):
        validate_dataset(_dataset_payload(cases=[_case_payload(), _case_payload()]))


def test_dataset_rejects_empty_cases() -> None:
    with pytest.raises(ValidationError):
        validate_dataset(_dataset_payload(cases=[]))


def test_dataset_rejects_unknown_top_level_field() -> None:
    with pytest.raises(ValidationError):
        validate_dataset(_dataset_payload(owner="must-not-exist"))


def test_dataset_rejects_invalid_case_payload() -> None:
    invalid_case = _case_payload()
    del invalid_case["ground_truth"]
    with pytest.raises(ValidationError):
        validate_dataset(_dataset_payload(cases=[invalid_case]))


def test_load_dataset_rejects_invalid_json(tmp_path) -> None:
    file_path = tmp_path / "broken.json"
    file_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(EvaluationDatasetLoadError, match="not valid JSON"):
        load_dataset(file_path)


def test_load_dataset_rejects_non_object_json(tmp_path) -> None:
    file_path = tmp_path / "list.json"
    file_path.write_text("[]", encoding="utf-8")
    with pytest.raises(EvaluationDatasetLoadError, match="JSON object"):
        load_dataset(file_path)


def test_load_dataset_missing_file(tmp_path) -> None:
    with pytest.raises(EvaluationDatasetLoadError, match="cannot read dataset file"):
        load_dataset(tmp_path / "missing.json")


def test_loaded_dataset_supports_all_evaluator_input_families() -> None:
    dataset = validate_dataset(_dataset_payload())
    case = next(iter_cases(dataset))
    assert case.ground_truth.retrieval is not None
    assert case.ground_truth.ranking is not None
    assert case.ground_truth.generation is not None


def _v4_case(case_id: str = "v4-answer", **truth_changes: object) -> dict[str, object]:
    truth: dict[str, object] = {
        "answerable": True,
        "case_type": "ANSWERABLE",
        "expected_decision": "ANSWER",
        "split": "CALIBRATION",
        "corpus_ref": "rag-evaluation-corpus.v1",
        "expected_support_fact_ids": ["fact-1"],
        "annotation_reason_code": "EXPLICIT_CORPUS_SUPPORT",
    }
    truth.update(truth_changes)
    return {
        "case_id": case_id,
        "name": case_id,
        "input": {"query": "controlled query"},
        "ground_truth": {"answerability": truth},
        "metadata": {"tags": ["wp4"], "leakage_group": f"group-{case_id}"},
    }


def _v4_dataset(cases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "dataset_schema_version": EVALUATION_DATASET_ANSWERABILITY_SCHEMA_VERSION,
        "dataset_id": "no-answer-threshold-dataset",
        "name": "No Answer",
        "version": "v1",
        "cases": cases,
    }


def test_v4_accepts_typed_answerability_ground_truth() -> None:
    dataset = validate_dataset(_v4_dataset([_v4_case()]))
    assert dataset.cases[0].ground_truth.answerability.answerable is True


def test_v4_keeps_prior_ground_truth_sections_optional_and_composable() -> None:
    prior = _case_payload()
    retrieval_only = _case_payload(
        case_id="v4-retrieval",
        ground_truth=_ground_truth_payload(ranking=None, generation=None),
    )
    generation_only = _case_payload(
        case_id="v4-generation",
        ground_truth=_ground_truth_payload(retrieval=None, ranking=None),
    )
    answerability_with_prior = _v4_case("v4-composed")
    answerability_with_prior["ground_truth"] = {
        **_ground_truth_payload(),
        **answerability_with_prior["ground_truth"],
    }
    dataset = validate_dataset(
        _v4_dataset([prior, retrieval_only, generation_only, answerability_with_prior])
    )
    assert dataset.cases[0].ground_truth.answerability is None
    assert dataset.cases[1].ground_truth.retrieval is not None
    assert dataset.cases[2].ground_truth.generation is not None
    assert dataset.cases[3].ground_truth.answerability is not None
    assert dataset.cases[3].ground_truth.retrieval is not None


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"answerable": False}, "ANSWERABLE"),
        ({"expected_decision": "ABSTAIN"}, "ANSWERABLE"),
        ({"expected_support_fact_ids": []}, "support"),
        (
            {
                "answerable": False,
                "case_type": "CONFLICT",
                "expected_decision": "DIAGNOSTIC_ONLY",
                "split": "EVALUATION",
                "expected_support_fact_ids": [],
                "annotation_reason_code": "CONFLICTING_REFERENCE_FACTS",
            },
            "diagnostic",
        ),
        ({"expected_decision": "DIAGNOSTIC_ONLY"}, "ANSWERABLE"),
    ],
)
def test_v4_rejects_inconsistent_answerability(changes: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        validate_dataset(_v4_dataset([_v4_case(**changes)]))


def test_v4_rejects_missing_leakage_group() -> None:
    case = _v4_case()
    case["metadata"] = {"tags": ["wp4"]}
    with pytest.raises(ValidationError, match="leakage_group"):
        validate_dataset(_v4_dataset([case]))


def test_v4_rejects_cross_split_leakage_and_duplicate_case_id() -> None:
    first = _v4_case("one")
    second = _v4_case("two", split="EVALUATION")
    second["metadata"] = {"tags": ["wp4"], "leakage_group": "group-one"}
    with pytest.raises(ValidationError, match="cross dataset splits"):
        validate_dataset(_v4_dataset([first, second]))
    with pytest.raises(ValidationError, match="duplicate case_id"):
        validate_dataset(_v4_dataset([first, first]))


def test_v4_rejects_negative_case_with_support_facts() -> None:
    with pytest.raises(ValidationError, match="must not declare"):
        validate_dataset(
            _v4_dataset(
                [
                    _v4_case(
                        answerable=False,
                        case_type="WEAK",
                        expected_decision="ABSTAIN",
                        expected_support_fact_ids=["fact-1"],
                        annotation_reason_code="INSUFFICIENT_SUPPORT",
                    )
                ]
            )
        )
