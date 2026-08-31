"""WP6-E 测试辅助：构造 typed evidence / v3 response / run evidence。"""

# ruff: noqa: D105, D415

from __future__ import annotations

import json
from pathlib import Path

from app.core.evaluation.episodic_dataset import load_episodic_dataset

DATASET_PATH = Path("evaluation_assets/stateful_episodic_v1/stateful_episodic_dataset.v1.json")


def load_dataset():
    return load_episodic_dataset(DATASET_PATH)


def scenario_by_case(dataset, case_code: str):
    return next(s for s in dataset.scenarios if s.case_code == case_code)


def formation_receipt_wire(run_id: str, outcome: str, memory_id: str | None) -> dict:
    return {
        "run_id": run_id,
        "outcome": outcome,
        "memory_id": memory_id,
        "lesson_status": "ABSENT",
        "safe_reason": None,
    }


def fixture_receipt_wire(fixture_ref: str, memory_id: str, origin_run_id: str) -> dict:
    return {
        "fixture_ref": fixture_ref,
        "memory_id": memory_id,
        "origin_run_id": origin_run_id,
        "origin_kind": "DATASET_CONTROLLED_INITIAL_FIXTURE",
        "memory_scope": "orchestration",
    }


def replay_receipt_wire(run_id: str, outcome: str, memory_id: str) -> dict:
    return {
        "run_id": run_id,
        "outcome": outcome,
        "memory_id": memory_id,
        "lesson_status": "ABSENT",
        "safe_reason": None,
    }


def runtime_receipt_wire(
    run_id: str,
    *,
    terminal_status: str = "SUCCEEDED",
    delivery_status: str = "DELIVERED",
    formed_memory_id: str | None = None,
    formation_outcome: str | None = "CREATED",
    plan_goal: str | None = None,
    step_names: tuple[str, ...] = ("release_list",),
    step_statuses: tuple[str, ...] = ("SUCCEEDED",),
) -> dict:
    return {
        "run_id": run_id,
        "plan_goal": plan_goal,
        "step_names": list(step_names),
        "step_statuses": list(step_statuses),
        "terminal_status": terminal_status,
        "stop_reason": "COMPLETED" if terminal_status == "SUCCEEDED" else "UNHANDLED_ERROR",
        "delivery_status": delivery_status,
        "formed_memory_id": formed_memory_id,
        "formation_outcome": formation_outcome,
        "canonical_text_sha256": None,
    }


def selection_wire(
    *,
    candidate_count: int,
    items: list[dict],
) -> dict:
    return {
        "candidate_count": candidate_count,
        "selected": items,
    }


def selection_item_wire(memory_id: str, rank: int, score: int, selected: bool, drop_reason=None) -> dict:
    return {
        "memory_id": memory_id,
        "rank": rank,
        "lexical_match_score": score,
        "selected": selected,
        "drop_reason": drop_reason,
    }


def supplied_wire(memory_ids: list[str]) -> dict:
    return {
        "episodic_memory_ids": memory_ids,
        "record_count": len(memory_ids),
    }


def injected_wire(target: str, memory_ids: list[str], *, source_type: str = "EPISODIC_MEMORY_RETRIEVAL") -> dict:
    return {
        "target": target,
        "episodic_memory_ids": memory_ids,
        "context_record_count": len(memory_ids),
        "source_type": source_type,
        "trust_level": "USER_CONTENT",
    }


def capture_wire(
    *,
    run_id: str,
    selection: dict | None,
    supplied: dict | None,
    injected: list[dict],
    capture_outcome: str = "COMPLETE",
) -> dict:
    return {
        "schema_version": "episodic-evaluation-capture.v1",
        "run_id": run_id,
        "capture_outcome": capture_outcome,
        "selection": selection,
        "supplied": supplied,
        "injected": injected,
    }


def v3_response_wire(
    *,
    run_id: str,
    status: str = "SUCCEEDED",
    formation_receipts: list[dict] | None = None,
    fixture_receipts: list[dict] | None = None,
    replay_receipts: list[dict] | None = None,
    capture: dict | None = None,
    runtime_receipt: dict | None = None,
) -> dict:
    return {
        "protocol_version": "localagent-episodic-evaluation-execute.v1",
        "run_id": run_id,
        "status": status,
        "stop_reason": "COMPLETED" if status == "SUCCEEDED" else "UNHANDLED_ERROR",
        "error_code": None if status == "SUCCEEDED" else "DETERMINISTIC_FAILURE",
        "safe_message": None if status == "SUCCEEDED" else "deterministic failed run",
        "evaluation_control_status": "EXECUTED" if (formation_receipts or fixture_receipts or capture) else "NONE",
        "evaluation_error_code": None,
        "capture_status": "COMPLETE" if capture else "NOT_REQUESTED",
        "capture_error_code": None,
        "episodic_capture": capture,
        "runtime_receipt": runtime_receipt,
        "formation_receipts": formation_receipts or [],
        "fixture_receipts": fixture_receipts or [],
        "replay_receipts": replay_receipts or [],
    }


def dump_episodic_payload(record: dict) -> str:
    """把 typed payload dict 序列化为 SQLite payload 列内容。"""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def episodic_payload(
    *,
    situation: str = "项目生产环境的发布清单",
    goal: str = "整理发布清单",
    goal_authority: str = "RUNTIME_OBSERVED_PLAN_GOAL",
    observations: list[dict] | None = None,
    terminal_status: str = "SUCCEEDED",
    stop_reason: str = "COMPLETED",
    delivery_status: str = "DELIVERED",
    lesson: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "situation": {"text": situation},
        "goal": {"text": goal, "authority": goal_authority},
        "observations": observations or [{"observation_type": "STEP", "name": "release_list", "status": "SUCCEEDED"}],
        "result": {
            "terminal_status": terminal_status,
            "stop_reason": stop_reason,
            "delivery_status": delivery_status,
            "delivered_result_digest": None,
        },
        "lesson": lesson,
    }
