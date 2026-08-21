# ruff: noqa: D415

"""Prompt Injection Regression Case Set —— WP2 数据集结构测试。

覆盖：回归数据集加载、case_id 唯一且 wire-safe、7 类 AttackType 全覆盖、
5 类 AttackSource 全覆盖、Benign Control 数量下限（>=5）、4 类 Expected
Security Behavior 全覆盖、Attack Case 单一 primary attack type、非机械四
behavior 膨胀、Benign 不声明攻击字段、synthetic truthfulness label、
contradiction fail-closed 与 v1 regression。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.evaluation.dataset import (
    EVALUATION_DATASET_SCHEMA_VERSION,
    EVALUATION_DATASET_SECURITY_SCHEMA_VERSION,
    AttackSource,
    AttackType,
    ExpectedSecurityBehavior,
    SecurityCaseKind,
    iter_cases,
    load_dataset,
    validate_case,
    validate_dataset,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prompt_injection_regression.v2.json"

_SYNTHETIC_LABEL = "SYNTHETIC_SECURITY_REGRESSION_CASE"

_ALL_ATTACK_TYPES = set(AttackType)
_ALL_ATTACK_SOURCES = {
    AttackSource.USER_INPUT,
    AttackSource.RETRIEVED_CONTEXT,
    AttackSource.TOOL_OUTPUT,
    AttackSource.AGENT_MESSAGE,
    AttackSource.REFERENCE_DATA,
}
_ALL_EXPECTED_BEHAVIORS = {
    ExpectedSecurityBehavior.PRESERVE_ORIGINAL_TASK,
    ExpectedSecurityBehavior.IGNORE_UNTRUSTED_INSTRUCTION,
    ExpectedSecurityBehavior.DO_NOT_DISCLOSE_PROTECTED_CONTENT,
    ExpectedSecurityBehavior.DO_NOT_PERFORM_UNAUTHORIZED_ACTION,
}
_MIN_BENIGN_COUNT = 5


def _load() -> tuple[object, list[object]]:
    dataset = load_dataset(_FIXTURE)
    return dataset, list(iter_cases(dataset))


# --- Dataset load ------------------------------------------------------------


def test_regression_dataset_loads() -> None:
    dataset, cases = _load()
    assert dataset.dataset_schema_version == EVALUATION_DATASET_SECURITY_SCHEMA_VERSION
    assert dataset.dataset_id == "prompt-injection-regression-v2"
    assert len(cases) >= 20
    for case in cases:
        assert case.ground_truth.security is not None


def test_regression_case_ids_unique_and_wire_safe() -> None:
    _, cases = _load()
    ids = [case.case_id for case in cases]
    assert len(ids) == len(set(ids)), "case_id 必须唯一"
    for case_id in ids:
        assert case_id == case_id.strip()
        assert " " not in case_id and "\t" not in case_id


def test_regression_round_trip_through_tmp_json(tmp_path: Path) -> None:
    source, _ = _load()
    out = tmp_path / "regression-copy.json"
    out.write_text(source.model_dump_json(), encoding="utf-8")
    reloaded = load_dataset(out)
    assert reloaded.model_dump() == source.model_dump()


# --- Attack coverage ---------------------------------------------------------


def test_regression_all_attack_types_covered() -> None:
    _, cases = _load()
    present = {
        case.ground_truth.security.attack_type
        for case in cases
        if case.ground_truth.security.case_kind == SecurityCaseKind.ATTACK
    }
    assert present == _ALL_ATTACK_TYPES


def test_regression_all_attack_sources_covered() -> None:
    _, cases = _load()
    present = {
        case.ground_truth.security.attack_source
        for case in cases
        if case.ground_truth.security.case_kind == SecurityCaseKind.ATTACK
    }
    assert _ALL_ATTACK_SOURCES <= present


def test_regression_focused_attack_types_have_multiple_cases() -> None:
    _, cases = _load()
    counter: dict[AttackType, int] = {}
    for case in cases:
        gt = case.ground_truth.security
        if gt.case_kind == SecurityCaseKind.ATTACK:
            counter[gt.attack_type] = counter.get(gt.attack_type, 0) + 1
    for attack_type, minimum in {
        AttackType.ROLE_CONFUSION: 2,
        AttackType.UNAUTHORIZED_TOOL_INSTRUCTION: 2,
        AttackType.CROSS_AGENT_INSTRUCTION_INJECTION: 2,
    }.items():
        assert counter.get(attack_type, 0) >= minimum


def test_regression_attack_case_has_single_primary_attack_type() -> None:
    _, cases = _load()
    for case in cases:
        gt = case.ground_truth.security
        if gt.case_kind == SecurityCaseKind.ATTACK:
            assert gt.attack_type is not None
            assert gt.attack_source is not None
            assert gt.severity is not None


# --- Benign control ----------------------------------------------------------


def test_regression_benign_control_count_meets_minimum() -> None:
    _, cases = _load()
    benign_count = sum(
        1
        for case in cases
        if case.ground_truth.security.case_kind == SecurityCaseKind.BENIGN_CONTROL
    )
    assert benign_count >= _MIN_BENIGN_COUNT


def test_regression_benign_controls_do_not_declare_attack_fields() -> None:
    _, cases = _load()
    benign_cases = [
        case
        for case in cases
        if case.ground_truth.security.case_kind == SecurityCaseKind.BENIGN_CONTROL
    ]
    assert benign_cases, "必须存在 benign control"
    for case in benign_cases:
        gt = case.ground_truth.security
        assert gt.attack_type is None
        assert gt.attack_source is None
        assert gt.severity is None


# --- Expected behavior coverage ----------------------------------------------


def test_regression_all_expected_behaviors_covered() -> None:
    _, cases = _load()
    present: set[ExpectedSecurityBehavior] = set()
    for case in cases:
        present.update(case.ground_truth.security.expected_behaviors)
    assert _ALL_EXPECTED_BEHAVIORS <= present


def test_regression_no_mechanical_full_behavior_inflation() -> None:
    _, cases = _load()
    all_attack_behaviors = [
        case.ground_truth.security.expected_behaviors
        for case in cases
        if case.ground_truth.security.case_kind == SecurityCaseKind.ATTACK
    ]
    assert len(all_attack_behaviors) >= 2
    assert any(len(behaviors) < len(_ALL_EXPECTED_BEHAVIORS) for behaviors in all_attack_behaviors)


def test_regression_expected_behavior_subset_is_authoritative() -> None:
    _, cases = _load()
    for case in cases:
        behaviors = case.ground_truth.security.expected_behaviors
        assert len(behaviors) == len(set(behaviors))
        assert set(behaviors) <= _ALL_EXPECTED_BEHAVIORS


# --- Synthetic / real boundary -----------------------------------------------


def test_regression_truthfulness_label_is_synthetic() -> None:
    _, cases = _load()
    assert cases
    for case in cases:
        assert case.metadata.get("truthfulness_label") == _SYNTHETIC_LABEL


# --- Contradiction fail-closed -----------------------------------------------


def _security_gt(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "case_kind": "ATTACK",
        "attack_type": "DIRECT_INSTRUCTION_OVERRIDE",
        "attack_source": "USER_INPUT",
        "severity": "HIGH",
        "expected_behaviors": ["IGNORE_UNTRUSTED_INSTRUCTION", "PRESERVE_ORIGINAL_TASK"],
    }
    payload.update(changes)
    return payload


def test_regression_contradictory_benign_with_attack_type_fails_closed() -> None:
    with pytest.raises(ValidationError, match="benign control must not declare attack_type"):
        validate_case(
            {
                "case_id": "sec-contradiction-test",
                "name": "contradiction",
                "input": {"query": "x"},
                "ground_truth": {
                    "security": _security_gt(
                        case_kind="BENIGN_CONTROL",
                        attack_type="SYSTEM_PROMPT_EXTRACTION",
                        severity=None,
                    )
                },
            }
        )


def test_regression_contradictory_attack_missing_severity_fails_closed() -> None:
    with pytest.raises(ValidationError, match="attack case requires severity"):
        validate_case(
            {
                "case_id": "sec-contradiction-test-2",
                "name": "contradiction",
                "input": {"query": "x"},
                "ground_truth": {"security": _security_gt(severity=None)},
            }
        )


def test_regression_unknown_attack_type_fails_closed() -> None:
    with pytest.raises(ValidationError):
        validate_case(
            {
                "case_id": "sec-unknown-type",
                "name": "unknown",
                "input": {"query": "x"},
                "ground_truth": {"security": _security_gt(attack_type="HACK")},
            }
        )


def test_regression_v1_dataset_rejects_security() -> None:
    payload = {
        "dataset_schema_version": EVALUATION_DATASET_SCHEMA_VERSION,
        "dataset_id": "v1-contract",
        "name": "V1_CONTRACT",
        "version": "v1",
        "cases": [
            {
                "case_id": "sec-v1-reject",
                "name": "v1 security",
                "input": {"query": "x"},
                "ground_truth": {"security": _security_gt()},
            }
        ],
    }
    with pytest.raises(ValidationError, match="must not declare security ground truth"):
        validate_dataset(payload)


# --- v1 regression -----------------------------------------------------------


def test_regression_v1_phase1_dataset_still_loads() -> None:
    dataset = validate_dataset(
        {
            "dataset_schema_version": EVALUATION_DATASET_SCHEMA_VERSION,
            "dataset_id": "rag-regression",
            "name": "RAG_REGRESSION_V1",
            "version": "v1",
            "cases": [
                {
                    "case_id": "case-001",
                    "name": "CDT 字段映射解释",
                    "input": {"query": "解释CDT字段映射"},
                    "ground_truth": {
                        "retrieval": {
                            "relevant_chunks": [
                                {"document_id": "doc1", "chunk_id": "chunk10"},
                            ]
                        },
                        "ranking": {
                            "graded_relevance": [{"chunk_id": "chunk10", "relevance": 3}],
                        },
                        "generation": {"reference_answer": "参考解释。"},
                    },
                }
            ],
        }
    )
    case = next(iter_cases(dataset))
    assert case.ground_truth.retrieval is not None
    assert case.ground_truth.ranking is not None
    assert case.ground_truth.generation is not None
    assert case.ground_truth.security is None
