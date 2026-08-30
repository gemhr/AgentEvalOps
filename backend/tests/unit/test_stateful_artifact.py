"""StatefulScenarioAggregateV1 JSON serialization（R2-B）与 round-trip 契约。"""

# ruff: noqa: D101, D105, D415

import json

from app.core.evaluation.immutable import FrozenDict, freeze_json
from app.core.evaluation.stateful_artifact import (
    StatefulScenarioAggregateV1,
    StatefulStepAttemptRecord,
)
from app.core.evaluation.stateful_assertion import (
    AssertionDimension,
    AssertionStatus,
    BlockReason,
    EvidenceGapClassification,
    MemoryAssertion,
)
from app.core.evaluation.stateful_journal import JournalSettleEvidence
from app.core.evaluation.stateful_metrics import RatioMetric


def _nested_evidence_metadata():
    """复刻 E0-v2 真实形状：EvidenceRef metadata 被 freeze_json 递归冻结。"""
    meta = freeze_json(
        {
            "payload": {"retrieved_items": [{"document_id": "d1", "chunk_id": "c1"}], "flag": True},
            "nested": {"a": [1, 2, 3], "b": {"c": None}},
        }
    )
    assert isinstance(meta, FrozenDict)
    assert isinstance(meta["payload"], FrozenDict)
    return meta


def make_artifact() -> StatefulScenarioAggregateV1:
    meta = _nested_evidence_metadata()
    return StatefulScenarioAggregateV1(
        evaluation_run_id="run-1",
        dataset_id="stateful_memory_v1",
        dataset_version="v1",
        dataset_digest="sha256:abc",
        target_id="localagent",
        target_kind="LOCALAGENT_HTTP",
        target_version_ref={"kind": "t", "opaque_value": "v2"},
        config_ref={"kind": "c", "opaque_value": "cfg"},
        scenario_id="scn",
        truthfulness_origin="DETERMINISTIC_GROUND_TRUTH",
        regression_tags=[],
        required=True,
        deterministic_denominator=True,
        initial_state={"kind": "EMPTY"},
        step_attempts=[
            StatefulStepAttemptRecord(
                step_id="r1",
                case_id="scn.r1",
                case_version="v1",
                attempt_id="att-1",
                outcome_kind="FAILURE",
                attempt_evidence_refs=[
                    {
                        "kind": "stateful_journal_evidence",
                        "identifier": "scenario://env/r1/journal",
                        "schema_version": "v1",
                        "metadata": dict(meta),
                    }
                ],
            )
        ],
        runtime_evidence_refs=[
            {
                "kind": "stateful_journal_evidence",
                "identifier": "scenario://env/r1/journal",
                "schema_version": "v1",
                "metadata": dict(meta),
            }
        ],
        assertion_results=[
            MemoryAssertion(
                "scn.r1.formation",
                AssertionDimension.FORMATION,
                AssertionStatus.BLOCKED,
                blocked_by=BlockReason.RUNTIME,
                reason="planning failed before formation",
            ).to_metadata()
        ],
        metric_aggregates={
            "runtime_block_rate": {
                "metric_name": "runtime_block_rate",
                "passed": 0,
                "failed": 1,
                "blocked": 0,
                "not_applicable": 0,
                "evaluable_denominator": 1,
                "value": 1.0,
            }
        },
        failure_taxonomies=[],
        scenario_outcome="BLOCKED",
        scenario_outcome_assertion={"status": "BLOCKED", "assertion_id": "outcome"},
        private_evaluation_artifact=True,
        metadata={"alias_binding": {}, "localagent_python_executable_ref": r"D:\py\python.exe"},
    )


def test_model_dump_json_compat_handles_nested_frozendict():
    artifact = make_artifact()
    data = artifact.model_dump_json_compat()
    assert isinstance(data, dict)
    metadata = data["runtime_evidence_refs"][0]["metadata"]
    assert isinstance(metadata, dict)
    assert not isinstance(metadata, FrozenDict)
    assert metadata["payload"]["flag"] is True
    assert isinstance(metadata["payload"]["retrieved_items"], list)


def test_model_dump_json_compat_serializes_assertion_frozendicts():
    artifact = make_artifact()
    data = artifact.model_dump_json_compat()
    assert isinstance(data["assertion_results"], list)
    assert isinstance(data["assertion_results"][0], dict)
    assert data["assertion_results"][0]["status"] == "BLOCKED"
    assert data["assertion_results"][0]["blocked_by"] == "runtime"


def test_artifact_json_dumps_and_round_trips():
    artifact = make_artifact()
    data = artifact.model_dump_json_compat()
    raw = json.dumps(data, ensure_ascii=False)
    assert len(raw) > 0
    reloaded = StatefulScenarioAggregateV1.model_validate(json.loads(raw))
    assert reloaded.scenario_outcome == "BLOCKED"
    assert reloaded.runtime_evidence_refs[0]["metadata"]["payload"]["flag"] is True
    assert reloaded.step_attempts[0].attempt_id == "att-1"
    assert reloaded.metric_aggregates["runtime_block_rate"]["value"] == 1.0
    assert reloaded.assertion_results[0]["assertion_id"] == "scn.r1.formation"


def test_artifact_serializes_r2_ratio_settle_and_implementation_ref():
    artifact = make_artifact().model_copy(
        update={
            "evaluation_implementation_ref": "HEAD:sha256:abc",
            "metric_aggregates": {
                "runtime_block_rate": RatioMetric(
                    metric_name="runtime_block_rate",
                    numerator=3,
                    denominator=138,
                    not_applicable=0,
                    value=3 / 138,
                ).as_dict()
            },
            "step_attempts": [
                StatefulStepAttemptRecord(
                    step_id="r1",
                    case_id="scn.r1",
                    case_version="v1",
                    attempt_id="attempt-1",
                    outcome_kind="SUCCESS",
                    metadata={
                        "journal_settle": JournalSettleEvidence(
                            initial_sequence_watermark=1,
                            final_sequence_watermark=3,
                            poll_attempts=2,
                            stop_reason="EXPECTED_EVIDENCE_OBSERVED",
                        ).to_dict()
                    },
                )
            ],
        }
    )

    data = artifact.model_dump_json_compat()
    reloaded = StatefulScenarioAggregateV1.model_validate(json.loads(json.dumps(data)))

    assert reloaded.evaluation_implementation_ref == "HEAD:sha256:abc"
    assert reloaded.metric_aggregates["runtime_block_rate"]["numerator"] == 3
    assert reloaded.step_attempts[0].metadata["journal_settle"]["poll_attempts"] == 2


def test_private_evaluation_artifact_preserved():
    artifact = make_artifact()
    data = artifact.model_dump_json_compat()
    assert data["private_evaluation_artifact"] is True
    reloaded = StatefulScenarioAggregateV1.model_validate(data)
    assert reloaded.private_evaluation_artifact is True


def test_evidence_metadata_preserved_and_no_secret_added():
    artifact = make_artifact()
    data = artifact.model_dump_json_compat()
    raw = json.dumps(data, ensure_ascii=False)
    assert "api_key" not in raw.lower()
    assert "authorization" not in raw.lower()
    assert "sk-" not in raw.lower()
    assert data["runtime_evidence_refs"][0]["metadata"]["payload"]["retrieved_items"][0]["document_id"] == "d1"


def test_domain_immutability_remains_frozendict_in_dto():
    artifact = make_artifact()
    # DTO 内部：metadata 顶层是 plain dict（`dict(FrozenDict)`），但嵌套 payload 仍是
    # immutable FrozenDict；只有 JSON projection 才转成纯 dict。
    metadata = artifact.runtime_evidence_refs[0]["metadata"]
    assert isinstance(metadata, dict)
    assert not isinstance(metadata, FrozenDict)
    assert isinstance(metadata["payload"], FrozenDict)
    projected = artifact.model_dump_json_compat()
    projected_metadata = projected["runtime_evidence_refs"][0]["metadata"]
    assert isinstance(projected_metadata["payload"], dict)
    assert not isinstance(projected_metadata["payload"], FrozenDict)


def test_artifact_serializes_evidence_gap_classification_and_expected_limitation_count():
    expected_limitation = MemoryAssertion(
        "scn.r1.retrieval.recall_at_k",
        AssertionDimension.RETRIEVAL,
        AssertionStatus.BLOCKED,
        expected=["db"],
        blocked_by=BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE,
        evidence_gap_classification=EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION,
        reason="identity-level selection evidence not supported",
    )
    artifact = make_artifact().model_copy(
        update={
            "assertion_results": [dict(expected_limitation.to_metadata())],
            "metric_aggregates": {
                "expected_evidence_limitation_blocked_count": {
                    "metric_name": "expected_evidence_limitation_blocked_count",
                    "passed": 0,
                    "failed": 0,
                    "blocked": 2,
                    "not_applicable": 0,
                    "evaluable_denominator": 0,
                    "value": None,
                }
            },
        }
    )
    data = artifact.model_dump_json_compat()
    raw = json.dumps(data, ensure_ascii=False)
    reloaded = StatefulScenarioAggregateV1.model_validate(json.loads(raw))
    assertion_meta = reloaded.assertion_results[0]
    assert assertion_meta["evidence_gap_classification"] == "EXPECTED_EVIDENCE_LIMITATION"
    assert assertion_meta["blocked_by"] == "not_supported_by_current_evidence"
    count = reloaded.metric_aggregates["expected_evidence_limitation_blocked_count"]
    assert count["blocked"] == 2
    assert count["value"] is None
    # privacy：artifact 只记录 enum/count，不新增 raw canonical/query/credential
    assert "canonical_text" not in raw or "项目数据库" not in raw
    assert "api_key" not in raw.lower()
    assert "authorization" not in raw.lower()
