"""Stateful Memory Dataset V2 契约测试：dispatch / canonical_text / identity policy / V1 隔离。"""

# ruff: noqa: D101, D105, D415

import json
import re
import unicodedata
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.evaluation.dataset import (
    EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION,
    EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2,
    EvaluationDatasetLoadError,
)
from app.core.evaluation.stateful_memory_dataset import (
    MemoryRecordExpectation,
    StatefulMemoryDataset,
    content_digest,
    load_stateful_memory_dataset,
    validate_stateful_dataset,
)
from app.core.evaluation.stateful_memory_dataset_v2 import (
    FORMATION_MAX_CANONICAL_TEXT_CHARS,
    IdentityEvidenceRequirement,
    SeededMemoryRecord,
    StatefulMemoryDatasetV2,
    load_stateful_dataset,
    load_stateful_memory_dataset_v2,
    validate_stateful_dataset_v2,
)

V1_DATASET_PATH = Path("evaluation_assets/stateful_memory_v1/stateful_memory_dataset.v1.json")
V2_DATASET_PATH = Path("evaluation_assets/stateful_memory_v2/stateful_memory_dataset.v2.json")

V1_FROZEN_DIGEST = "sha256:b9fdd0dc40b3cd1febf4fdcaa0441bb6cb8dbecc5855fa4d65adab503110da1f"

# A2 authority：六个 fixture canonical_text 的唯一 authority。
SEEDED_CANONICAL_TEXTS = {
    "retrieval_active_hit": {"db": "项目数据库使用 SQLite"},
    "retrieval_status_exclusion": {
        "db_old": "项目数据库使用 SQLite",
        "db": "项目数据库为 PostgreSQL",
    },
    "retrieval_open_hit": {"codename": "项目发布代号是 Nebula"},
    "retrieval_unrelated_rejection": {
        "db": "项目数据库使用 SQLite",
        "unrelated": "北京的天气是晴天",
    },
    "scope_isolation": {
        "db": "项目数据库使用 SQLite",
        "other": "项目部署在裸金属",
    },
    "safety_forgotten_not_injected": {
        "db": "项目数据库为 PostgreSQL",
    },
}

# 六个新 canonical fixture 中必须与 frozen query 产生 lexical overlap 的 relevant seed。
# unrelated / foreign-scope 记录是负面 fixture，不在此列。
RELEVANT_CANONICAL_FIXTURES = {
    "retrieval_active_hit": ["db"],
    "retrieval_status_exclusion": ["db"],
    "retrieval_open_hit": ["codename"],
    "retrieval_unrelated_rejection": ["db"],
    "scope_isolation": ["db"],
    "safety_forgotten_not_injected": ["db"],
}

EVIDENCE_POLICY = {"layer_1": "REQUIRED", "layer_2": "EXPECTED_LIMITATION"}

POLICY_SCENARIOS = {
    "retrieval_active_hit": {"r1"},
    "retrieval_status_exclusion": {"r1"},
    "retrieval_open_hit": {"r1"},
    "retrieval_unrelated_rejection": {"r1"},
    "scope_isolation": {"r1"},
    "safety_forgotten_not_injected": {"r1"},
    "database_correction": {"r3"},
}


def _v2_payload() -> dict:
    return json.loads(V2_DATASET_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ V2 dataset identity


def test_v2_dataset_loads_with_identity_and_digest():
    dataset = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
    assert dataset.dataset_schema_version == EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2
    assert dataset.dataset_id == "stateful_memory_v2"
    assert dataset.version == "v2"
    assert len(dataset) == 20
    assert dataset.content_digest == content_digest(V2_DATASET_PATH.read_bytes())
    raw = V2_DATASET_PATH.read_bytes()
    assert content_digest(raw) == dataset.content_digest


def test_v2_dataset_must_declare_v2_identity():
    payload = _v2_payload()
    payload["dataset_id"] = "stateful_memory_v1"
    with pytest.raises(ValidationError):
        validate_stateful_dataset_v2(payload)
    payload = _v2_payload()
    payload["version"] = "v1"
    with pytest.raises(ValidationError):
        validate_stateful_dataset_v2(payload)


def test_v2_loader_rejects_v1_schema_version():
    payload = _v2_payload()
    payload["dataset_schema_version"] = EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION
    with pytest.raises(ValidationError):
        validate_stateful_dataset_v2(payload)


def test_v2_identity_evidence_policy_is_typed_and_present():
    dataset = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
    for scenario_id, step_ids in POLICY_SCENARIOS.items():
        scenario = next(s for s in dataset.scenarios if s.scenario_id == scenario_id)
        for step in scenario.steps:
            if step.step_id not in step_ids:
                continue
            assert step.expected_retrieval is not None
            policy = step.expected_retrieval.identity_evidence_by_layer
            assert policy is not None, (scenario_id, step.step_id)
            assert policy.layer_1 is IdentityEvidenceRequirement.REQUIRED
            assert policy.layer_2 is IdentityEvidenceRequirement.EXPECTED_LIMITATION


def test_v2_database_correction_has_no_seed_amendment():
    dataset = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
    scenario = next(s for s in dataset.scenarios if s.scenario_id == "database_correction")
    assert scenario.initial_state.kind.value == "EMPTY"
    assert scenario.initial_state.records == []


def test_v2_preserves_all_non_r3_scenario_fields():
    v1 = load_stateful_memory_dataset(V1_DATASET_PATH)
    v2 = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
    v1_by_id = {s.scenario_id: s for s in v1.scenarios}
    for scenario in v2.scenarios:
        v1_scenario = v1_by_id[scenario.scenario_id]
        assert v1_scenario.description == scenario.description
        assert v1_scenario.truthfulness_origin == scenario.truthfulness_origin
        assert v1_scenario.tags == scenario.tags
        assert v1_scenario.required == scenario.required
        assert v1_scenario.deterministic_denominator == scenario.deterministic_denominator
        assert len(v1_scenario.initial_state.records) == len(scenario.initial_state.records)
        for v1_record, v2_record in zip(
            v1_scenario.initial_state.records, scenario.initial_state.records, strict=True
        ):
            assert v1_record.alias == v2_record.alias
            assert v1_record.agent_id == v2_record.agent_id
            assert v1_record.memory_scope == v2_record.memory_scope
            assert v1_record.memory_type == v2_record.memory_type
            assert v1_record.logical_key == v2_record.logical_key
            assert v1_record.status == v2_record.status
            assert v1_record.value == v2_record.value
            assert v1_record.superseded_by_alias == v2_record.superseded_by_alias
        for v1_step, v2_step in zip(v1_scenario.steps, scenario.steps, strict=True):
            assert v1_step.step_id == v2_step.step_id
            assert v1_step.agent_id == v2_step.agent_id
            assert v1_step.memory_scope == v2_step.memory_scope
            assert v1_step.query == v2_step.query
            assert v1_step.required == v2_step.required
            if v1_step.expected_retrieval is not None:
                assert v2_step.expected_retrieval is not None
                assert v1_step.expected_retrieval.expected_selected == v2_step.expected_retrieval.expected_selected
                assert v1_step.expected_retrieval.expected_excluded == v2_step.expected_retrieval.expected_excluded
                assert v1_step.expected_retrieval.k == v2_step.expected_retrieval.k
        for v1_expected, v2_expected in zip(v1_scenario.expected_state, scenario.expected_state, strict=True):
            assert v1_expected.alias == v2_expected.alias
            assert v1_expected.agent_id == v2_expected.agent_id
            assert v1_expected.memory_scope == v2_expected.memory_scope
            assert v1_expected.logical_key == v2_expected.logical_key
            assert v1_expected.status == v2_expected.status
            assert v1_expected.value == v2_expected.value
            assert v1_expected.superseded_by_alias == v2_expected.superseded_by_alias


# ------------------------------------------------------------------ seeded canonical_text


def test_v2_six_fixture_canonical_texts_match_frozen_authority():
    dataset = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
    for scenario in dataset.scenarios:
        expected = SEEDED_CANONICAL_TEXTS.get(scenario.scenario_id)
        if expected is None:
            continue
        records = {record.alias: record for record in scenario.initial_state.records}
        for alias, text in expected.items():
            assert records[alias].canonical_text == text, (scenario.scenario_id, alias)


def test_v2_forgotten_seed_has_strict_tombstone_shape():
    dataset = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
    for scenario in dataset.scenarios:
        for record in scenario.initial_state.records:
            if record.status.value == "FORGOTTEN":
                assert record.canonical_text is None, scenario.scenario_id
                assert record.superseded_by_alias is None, scenario.scenario_id


def test_v2_seeded_non_forgotten_requires_canonical_text():
    with pytest.raises(ValidationError, match="requires canonical_text"):
        SeededMemoryRecord.model_validate(
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
            }
        )


def test_v2_canonical_text_blank_after_strip_rejected():
    with pytest.raises(ValidationError, match="must not be blank"):
        SeededMemoryRecord.model_validate(
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
                "canonical_text": "   ",
            }
        )


def test_v2_canonical_text_length_mirrors_localagent_contract():
    assert FORMATION_MAX_CANONICAL_TEXT_CHARS == 400
    over = "a" * (FORMATION_MAX_CANONICAL_TEXT_CHARS + 1)
    with pytest.raises(ValidationError, match="exceeds"):
        SeededMemoryRecord.model_validate(
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
                "canonical_text": over,
            }
        )
    ok = "a" * FORMATION_MAX_CANONICAL_TEXT_CHARS
    record = SeededMemoryRecord.model_validate(
        {
            "alias": "db",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "ACTIVE",
            "value": "SQLite",
            "canonical_text": ok,
        }
    )
    assert record.canonical_text == ok


def test_canonical_text_constant_matches_localagent_source_when_available():
    """显式 mirror 并核对 LocalAgent 当前 formation contract（源码可发现时）。"""
    import os
    import re as _re

    candidates = [
        Path(os.environ.get("LOCAL_AGENT_REPO", "")) / "core" / "runtime" / "semantic_memory_formation.py",
        Path("D:/PythonProject/Local_Agent/core/runtime/semantic_memory_formation.py"),
    ]
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        pytest.skip("LocalAgent semantic_memory_formation.py not discoverable")
    text = source.read_text(encoding="utf-8")
    match = _re.search(r"FORMATION_MAX_CANONICAL_TEXT_CHARS\s*=\s*(\d+)", text)
    assert match is not None, "LocalAgent constant not found in source"
    assert int(match.group(1)) == FORMATION_MAX_CANONICAL_TEXT_CHARS == 400


def test_v2_canonical_text_is_trimmed_before_store():
    record = SeededMemoryRecord.model_validate(
        {
            "alias": "db",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "ACTIVE",
            "value": "SQLite",
            "canonical_text": "  项目数据库使用 SQLite  ",
        }
    )
    assert record.canonical_text == "项目数据库使用 SQLite"


def test_v2_forgotten_seed_rejects_canonical_text():
    with pytest.raises(ValidationError, match="must not declare canonical_text"):
        SeededMemoryRecord.model_validate(
            {
                "alias": "forgone",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.legacy_database",
                "status": "FORGOTTEN",
                "canonical_text": "项目数据库使用 SQLite",
            }
        )


def test_v2_identity_evidence_policy_enum_strict():
    payload = _v2_payload()
    scenario = next(s for s in payload["scenarios"] if s["scenario_id"] == "retrieval_active_hit")
    policy = scenario["steps"][0]["expected_retrieval"]["identity_evidence_by_layer"]
    policy["layer_1"] = "OPTIONAL"
    with pytest.raises(ValidationError):
        validate_stateful_dataset_v2(payload)
    payload = _v2_payload()
    scenario = next(s for s in payload["scenarios"] if s["scenario_id"] == "retrieval_active_hit")
    scenario["steps"][0]["expected_retrieval"]["identity_evidence_by_layer"]["layer_3"] = "REQUIRED"
    with pytest.raises(ValidationError):
        validate_stateful_dataset_v2(payload)


# ------------------------------------------------------------------ V1/V2 dispatch & isolation


def test_v1_loads_through_dispatch_and_digest_frozen():
    dataset = load_stateful_dataset(V1_DATASET_PATH)
    assert isinstance(dataset, StatefulMemoryDataset)
    assert dataset.content_digest == V1_FROZEN_DIGEST
    assert content_digest(V1_DATASET_PATH.read_bytes()) == V1_FROZEN_DIGEST


def test_v2_loads_through_dispatch():
    dataset = load_stateful_dataset(V2_DATASET_PATH)
    assert isinstance(dataset, StatefulMemoryDatasetV2)
    assert dataset.dataset_schema_version == EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2


def test_dispatch_rejects_unknown_schema_version(tmp_path):
    path = tmp_path / "unknown.json"
    path.write_text(
        json.dumps(
            {
                "dataset_schema_version": "stateful-memory-scenario.v9",
                "dataset_id": "x",
                "version": "v9",
                "name": "x",
                "scenarios": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvaluationDatasetLoadError, match="unsupported"):
        load_stateful_dataset(path)


def test_v1_rejects_v2_seed_canonical_text_field():
    payload = json.loads(V1_DATASET_PATH.read_text(encoding="utf-8"))
    scenario = next(s for s in payload["scenarios"] if s["scenario_id"] == "retrieval_active_hit")
    scenario["initial_state"]["records"][0]["canonical_text"] = "项目数据库使用 SQLite"
    with pytest.raises(ValidationError):
        validate_stateful_dataset(payload)


def test_v1_rejects_v2_identity_evidence_policy_field():
    payload = json.loads(V1_DATASET_PATH.read_text(encoding="utf-8"))
    scenario = next(s for s in payload["scenarios"] if s["scenario_id"] == "retrieval_active_hit")
    scenario["steps"][0]["expected_retrieval"]["identity_evidence_by_layer"] = dict(EVIDENCE_POLICY)
    with pytest.raises(ValidationError):
        validate_stateful_dataset(payload)


def test_v1_seed_legacy_behavior_preserved():
    legacy = MemoryRecordExpectation.model_validate(
        {
            "alias": "db",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "ACTIVE",
            "value": "SQLite",
        }
    )
    assert getattr(legacy, "canonical_text", None) is None
    # V1 seed helper fallback（<logical_key>: <value>）仍是 V1 compatibility path 的一部分
    assert "project.database: SQLite" == "project.database: SQLite"


# ------------------------------------------------------------------ lexical tokenizer fixtures


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _lexical_tokens(text: str) -> tuple[str, ...]:
    """LocalAgent current lexical tokenizer 的等价 mirror（memory_retrieval._tokenize）。"""
    token_run = re.compile(r"[0-9a-z_]+|[\u4e00-\u9fff]+")
    cjk_run = re.compile(r"[\u4e00-\u9fff]+")
    normalized = _normalize_text(text)
    tokens: list[str] = []
    for run in token_run.findall(normalized):
        if cjk_run.fullmatch(run):
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        else:
            tokens.append(run)
    return tuple(tokens)


def _lexical_overlap(canonical_text: str, query: str) -> int:
    query_tokens = set(_lexical_tokens(query))
    candidate_tokens = set(_lexical_tokens(canonical_text))
    return sum(1 for token in query_tokens if token in candidate_tokens)


def test_six_canonical_fixtures_have_lexical_overlap_with_frozen_query():
    dataset = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
    by_id = {s.scenario_id: s for s in dataset.scenarios}
    for scenario_id, aliases in RELEVANT_CANONICAL_FIXTURES.items():
        scenario = by_id[scenario_id]
        query = scenario.steps[0].query
        records = {record.alias: record for record in scenario.initial_state.records}
        for alias in aliases:
            overlap = _lexical_overlap(records[alias].canonical_text, query)
            assert overlap > 0, (scenario_id, alias)


def test_unrelated_and_foreign_canonical_have_no_relevance_for_db_query():
    dataset = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
    unrelated = next(s for s in dataset.scenarios if s.scenario_id == "retrieval_unrelated_rejection")
    unrelated_record = next(r for r in unrelated.initial_state.records if r.alias == "unrelated")
    db_query = next(s for s in dataset.scenarios if s.scenario_id == "retrieval_active_hit").steps[0].query
    assert _lexical_overlap(unrelated_record.canonical_text, db_query) == 0

    scope = next(s for s in dataset.scenarios if s.scenario_id == "scope_isolation")
    other = next(r for r in scope.initial_state.records if r.alias == "other")
    assert other.memory_scope == "team"
    assert scope.steps[0].memory_scope == "direct"
    # foreign-scope record 不在 direct retrieval 的 candidate set（eligibility 排除），
    # 与 lexical overlap 无关；这里断言它与 query 不共享 relevance token 也不是必要前提。
    assert other.canonical_text == "项目部署在裸金属"
