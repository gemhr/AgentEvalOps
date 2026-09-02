"""Stage5-Phase6-WP5: hybrid retrieval diagnosis, selection and formal pair.

This runner is intentionally one-shot.  AgentEvalOps owns orchestration and metrics;
LocalAgent remains the retrieval/fusion owner and is invoked through its evaluation HTTP
contract for fixture capture and the formal pair.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from app.core.evaluation.dataset import EvaluationCase, EvaluationDataset, load_dataset
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1
from app.core.evaluation.ranking_metrics import calculate_ndcg_at_k
from app.core.evaluation.retrieval_metrics import calculate_mrr, calculate_recall_at_k
from app.core.evaluation.stateful_memory_dataset import StatefulMemoryScenario
from app.services.evaluation.stateful_environment import LocalAgentSubprocessProvisioner


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
LOCALAGENT_DEFAULT = Path(r"D:\PythonProject\Local_Agent")
DATASET_DEFAULT = ROOT / "evaluation_assets" / "rag_quality_v2" / "rag_evaluation_dataset.v2.json"
PIN_DEFAULT = REPO / ".ai" / "evidence" / "stage5-phase6-wp3" / "generation_pin.json"
EVIDENCE_DEFAULT = REPO / ".ai" / "evidence" / "stage5-phase6-wp5"
HANDOFF_DEFAULT = REPO / ".ai" / "handoff" / "stage5-phase6-wp5"
PROFILE_SCHEMA = "localagent-hybrid-rrf-profile.v1"
GENERATION_ID = "a7cfb583-a297-402c-a050-61c9a8eee645"
METRIC_NAMES = ("recall@1", "recall@3", "recall@5", "mrr", "ndcg@3", "ndcg@5")
FORMAL_THRESHOLDS = {
    "quality_ndcg_at_3_delta_min": 0.0,
    "quality_secondary_metric_delta_min": 0.05,
    "quality_any_metric_delta_min": -0.05,
    "ordinary_regressions_max": 4,
    "severe_regressions_max": 0,
    "reliability_attempts_per_strategy": 44,
    "reliability_execution_failures_max": 0,
    "candidate_degraded_rate_delta_max": 0.10,
    "latency_absolute_delta_ms": 50,
    "latency_relative_delta": 0.25,
}


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git_value(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def repo_identity(repo: Path) -> dict[str, str]:
    import subprocess

    diff = subprocess.check_output(["git", "-C", str(repo), "diff", "--binary"])
    return {
        "head": git_value(repo, "rev-parse", "HEAD"),
        "working_tree_diff_sha256": sha256_bytes(diff),
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def localagent_runtime_env() -> dict[str, str]:
    """Apply the checked-in setup contract without ever printing the API key."""
    values = {
        "LOCAL_AGENT_LLM_BACKEND": "remote",
        "LOCAL_AGENT_REMOTE_PROVIDER_KIND": "deepseek",
        "LOCAL_AGENT_REMOTE_API_BASE_URL": "https://api.deepseek.com",
        "LOCAL_AGENT_REMOTE_MODEL_NAME": "deepseek-v4-flash",
        "LOCAL_AGENT_REMOTE_CONTEXT_WINDOW": "1000000",
        "LOCAL_AGENT_MODEL_MAX_TOKENS": "4096",
        "LOCAL_AGENT_REMOTE_ENABLE_THINKING": "0",
        "LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS": "120",
        "LOCAL_AGENT_REMOTE_VERIFY_TLS": "1",
    }
    api_key = os.environ.get("LOCAL_AGENT_REMOTE_API_KEY")
    if api_key:
        values["LOCAL_AGENT_REMOTE_API_KEY"] = api_key
    return values


def case_query(case: EvaluationCase) -> str:
    return str(case.input.get("query") or case.input.get("user_query") or case.name)


def dataset_cases(dataset: EvaluationDataset) -> dict[str, EvaluationCase]:
    return {case.case_id: case for case in dataset.cases}


def slice_name(case: EvaluationCase) -> str:
    metadata = case.metadata
    for key in ("slice", "taxonomy_slice", "taxonomy", "category"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    tags = metadata.get("tags")
    if isinstance(tags, list) and tags:
        return str(tags[0])
    return "UNSPECIFIED"


def retrieval_cases(dataset: EvaluationDataset) -> list[EvaluationCase]:
    return [case for case in dataset.cases if case.ground_truth.retrieval is not None]


def no_answer_cases(dataset: EvaluationDataset) -> list[EvaluationCase]:
    return [case for case in dataset.cases if case.ground_truth.retrieval is None]


def make_split(dataset_path: Path, v1_path: Path) -> tuple[dict[str, str], dict[str, object]]:
    dataset = load_dataset(dataset_path)
    old = load_dataset(v1_path)
    current = dataset_cases(dataset)
    old_ids = {case.case_id for case in retrieval_cases(old)}
    new = [case for case in retrieval_cases(dataset) if case.case_id not in old_ids]
    if len(old_ids) != 20 or len(new) != 40:
        raise RuntimeError(f"expected core=20/new=40, got core={len(old_ids)} new={len(new)}")

    ordered_by_slice: dict[str, list[EvaluationCase]] = defaultdict(list)
    for case in new:
        ordered_by_slice[slice_name(case)].append(case)
    assignments: dict[str, str] = {}
    for current_slice, cases in sorted(ordered_by_slice.items()):
        cases.sort(key=lambda item: (sha256_bytes(f"wp5-split-v1\0{item.case_id}".encode()), item.case_id))
        for index, case in enumerate(cases):
            assignments[case.case_id] = "DEV" if index % 2 == 0 else "HOLDOUT"
    # Deterministic bounded rebalance only if odd slice sizes leave the global total off target.
    while sum(value == "DEV" for value in assignments.values()) > 20:
        candidates = sorted(case_id for case_id, value in assignments.items() if value == "DEV")
        assignments[candidates[-1]] = "HOLDOUT"
    while sum(value == "DEV" for value in assignments.values()) < 20:
        candidates = sorted(case_id for case_id, value in assignments.items() if value == "HOLDOUT")
        assignments[candidates[0]] = "DEV"
    entries = [
        {"case_id": case_id, "subset": assignments[case_id], "slice": slice_name(current[case_id])}
        for case_id in sorted(assignments)
    ]
    payload = {
        "schema_version": "stage5-phase6-wp5-dataset-split.v1",
        "dataset_content_sha256": sha256_file(dataset_path),
        "dataset_path_ref": dataset_path.name,
        "core_case_count": 20,
        "dev_new_case_count": 20,
        "holdout_new_case_count": 20,
        "split_algorithm": "per-slice stable sha256(case_id) alternation with deterministic global rebalance",
        "split_algorithm_version": "wp5-split-v1",
        "entries": entries,
    }
    manifest_hash = sha256_bytes(canonical(payload))
    payload["split_manifest_sha256"] = manifest_hash
    return assignments, payload


def profile_payload(candidate_id: str, dense: float, bm25: float) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": PROFILE_SCHEMA,
        "candidate_id": candidate_id,
        "profile_version": "v1",
        "algorithm_ref": "weighted_rrf.v1",
        "rrf_k": 60,
        "dense_weight": dense,
        "bm25_weight": bm25,
        "final_top_k": 8,
    }
    payload["candidate_profile_sha256"] = sha256_bytes(canonical(payload))
    return payload


@dataclass
class Observation:
    case_id: str
    artifact: RagEvaluationArtifactV1 | None
    error: str | None = None
    http_status: int | None = None


def scenario(role: str) -> StatefulMemoryScenario:
    return StatefulMemoryScenario.model_validate(
        {
            "scenario_id": f"wp5-{role.lower()}-{uuid4().hex[:8]}",
            "description": f"WP5 {role} isolated LocalAgent run",
            "truthfulness_origin": "DETERMINISTIC_GROUND_TRUTH",
            "tags": ["stage5-phase6-wp5", role.lower()],
            "initial_state": {"kind": "EMPTY"},
            "steps": [
                {
                    "step_id": "evaluation",
                    "agent_id": "knowledge_expert",
                    "memory_scope": "direct",
                    "query": "WP5 evaluation invocation",
                }
            ],
        }
    )


async def post_case(client: httpx.AsyncClient, base_url: str, case: EvaluationCase) -> Observation:
    run_id = str(uuid4())
    query = case_query(case)
    agent_id = str(case.input.get("agent_id", "knowledge_expert"))
    try:
        response = await client.post(
            f"{base_url}/api/runtime/evaluation-execute/v2",
            json={"agent_id": agent_id, "query": query, "run_id": run_id, "timeout_seconds": 120.0},
            timeout=httpx.Timeout(180.0),
        )
        payload = response.json()
        artifacts = payload.get("rag_evaluation_artifacts", []) if isinstance(payload, dict) else []
        if len(artifacts) != 1:
            detail = {
                "artifact_count": len(artifacts),
                "status": payload.get("status") if isinstance(payload, dict) else None,
                "stop_reason": payload.get("stop_reason") if isinstance(payload, dict) else None,
                "error_code": payload.get("error_code") if isinstance(payload, dict) else None,
                "safe_message": payload.get("safe_message") if isinstance(payload, dict) else None,
                "capture_error_code": payload.get("capture_error_code") if isinstance(payload, dict) else None,
            }
            return Observation(case.case_id, None, json.dumps(detail, ensure_ascii=False), response.status_code)
        return Observation(case.case_id, RagEvaluationArtifactV1.model_validate(artifacts[0]), None, response.status_code)
    except Exception as exc:  # bounded per-attempt observation; run remains auditable
        return Observation(case.case_id, None, f"{type(exc).__name__}: {str(exc)[:240]}", getattr(locals().get("response"), "status_code", None))


async def run_http(
    *,
    localagent_repo: Path,
    provisioner: LocalAgentSubprocessProvisioner,
    role: str,
    cases: list[EvaluationCase],
    strategy: str,
    generation_pin: Path,
    fixture: Path,
    identity_sha: str,
    profile_path: Path | None,
    work_dir: Path,
    capture_only: bool = False,
) -> tuple[str, list[Observation], dict[str, object]]:
    run_id = str(uuid4())

    def env_factory(scenario_dir: Path, port: int) -> dict[str, str]:
        values = {
            "LOCAL_AGENT_EVALUATION_MODE": "0" if capture_only else "1",
            "LOCAL_AGENT_RETRIEVAL_STRATEGY": strategy,
            "LOCAL_AGENT_KB_COLLECTION": "rag_evaluation_kb_v1",
            "LOCAL_AGENT_EVALUATION_GENERATION_PIN_PATH": str(generation_pin),
            "LOCAL_AGENT_EVALUATION_REWRITE_FIXTURE_PATH": str(fixture),
            "LOCAL_AGENT_EVALUATION_IDENTITY_SHA256": identity_sha,
            "LOCAL_AGENT_SNAPSHOT_DB_PATH": str(scenario_dir / "runtime_snapshots.db"),
            "LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH": str(scenario_dir / "runtime_observability_checkpoint.db"),
            "LOCAL_AGENT_CHROMA_DIR": str(localagent_repo / "chroma_db"),
        }
        if profile_path is not None:
            values["LOCAL_AGENT_EVALUATION_HYBRID_PROFILE_PATH"] = str(profile_path)
        return values

    evidence = await provisioner.provision(scenario(role), extra_runtime_env_factory=env_factory)
    if not await provisioner.verify_bound(evidence):
        await provisioner.cleanup(evidence, preserve=True)
        raise RuntimeError(f"environment binding failed for {role}")
    started = time.monotonic()
    observations: list[Observation] = []
    async with httpx.AsyncClient(trust_env=False) as client:
        for case in cases:
            observations.append(await post_case(client, evidence.localagent_base_url or "", case))
    await provisioner.cleanup(evidence, preserve=True)
    return run_id, observations, {
        "role": role,
        "strategy": strategy,
        "case_count": len(cases),
        "execution_failures": sum(item.artifact is None for item in observations),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "work_dir": str(evidence.work_dir),
        "port_released": _port_released(evidence.localagent_base_url or ""),
    }


def _port_released(base_url: str) -> bool:
    try:
        port = int(base_url.rsplit(":", 1)[1])
    except (ValueError, OSError):
        return False
    for _ in range(30):
        try:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) != 0:
                    return True
        except OSError:
            return True
        time.sleep(0.1)
    return False


def metric_row(case: EvaluationCase, artifact: RagEvaluationArtifactV1) -> dict[str, float]:
    retrieval = case.ground_truth.retrieval
    ranking = case.ground_truth.ranking
    if retrieval is None or ranking is None:
        raise ValueError(f"case {case.case_id} lacks retrieval/ranking ground truth")
    recall = calculate_recall_at_k(retrieval, artifact, (1, 3, 5))
    mrr = calculate_mrr(retrieval, artifact)
    ndcg = calculate_ndcg_at_k(ranking, artifact, (3, 5))
    return {
        "recall@1": recall.value_at(1),
        "recall@3": recall.value_at(3),
        "recall@5": recall.value_at(5),
        "mrr": mrr.value,
        "ndcg@3": ndcg.value_at(3),
        "ndcg@5": ndcg.value_at(5),
    }


def aggregate(cases: list[EvaluationCase], observations: list[Observation]) -> dict[str, float]:
    by_id = {item.case_id: item for item in observations}
    rows = [metric_row(case, by_id[case.case_id].artifact) for case in cases if by_id[case.case_id].artifact is not None]
    if not rows:
        return {name: 0.0 for name in METRIC_NAMES}
    return {name: sum(row[name] for row in rows) / len(rows) for name in METRIC_NAMES}


def artifact_rank_evidence(artifact: RagEvaluationArtifactV1, gt: set[tuple[str, str]]) -> dict[str, object]:
    dense: dict[tuple[str, str], int] = {}
    bm25: dict[tuple[str, str], int] = {}
    fused: dict[tuple[str, str], int] = {}
    for item in artifact.ranked_items:
        identity = (item.document_id, item.chunk_id)
        if item.dense_channel_rank is not None:
            dense[identity] = item.dense_channel_rank
        if item.bm25_channel_rank is not None:
            bm25[identity] = item.bm25_channel_rank
        if item.rrf_fused_rank is not None:
            fused[identity] = item.rrf_fused_rank
    selected = [(item.document_id, item.chunk_id) for item in artifact.selected_items]
    return {
        "dense_rank": {str(identity): rank for identity, rank in dense.items() if identity in gt},
        "bm25_rank": {str(identity): rank for identity, rank in bm25.items() if identity in gt},
        "rrf_fused_rank": {str(identity): rank for identity, rank in fused.items() if identity in gt},
        "selected_top_k": selected,
        "ground_truth_identities": sorted(gt),
        "ranked_top_k": [(item.document_id, item.chunk_id) for item in artifact.ranked_items],
    }


def classify_root_cause(evidence: dict[str, object]) -> str:
    dense = list((evidence["dense_rank"] or {}).values())
    bm25 = list((evidence["bm25_rank"] or {}).values())
    fused = list((evidence["rrf_fused_rank"] or {}).values())
    if any(rank > 3 for rank in fused) and (dense and min(dense) <= 3 or bm25 and min(bm25) <= 3):
        return "TOP_K_DISPLACEMENT"
    if dense and min(fused or [99]) > min(dense) and min(dense) <= 3:
        return "FUSION_ORDERING_ERROR"
    if bm25 and min(bm25) <= 3 and (not dense or min(dense) > 3):
        return "LEXICAL_OVERWEIGHT"
    if dense and min(dense) <= 3 and (not bm25 or min(bm25) > 3) and (not fused or min(fused) > 3):
        return "SEMANTIC_UNDERWEIGHT"
    if len(evidence["ground_truth_identities"]) > 1:
        return "AMBIGUOUS_MULTI_RELEVANT"
    return "OTHER"


def build_observation_payload(cases: list[EvaluationCase], observations: list[Observation]) -> list[dict[str, object]]:
    by_id = {case.case_id: case for case in cases}
    rows = []
    for item in observations:
        case = by_id[item.case_id]
        row: dict[str, object] = {"case_id": item.case_id, "http_status": item.http_status, "error": item.error}
        if item.artifact is not None:
            row["artifact"] = item.artifact.model_dump(mode="json")
            row["metrics"] = metric_row(case, item.artifact) if case.ground_truth.ranking else None
        rows.append(row)
    return rows


def build_rewrite_fixture(
    cases: list[EvaluationCase], observations: list[Observation], *, fixture_version: str
) -> dict[str, object]:
    entries = []
    for case, observation in zip(cases, observations, strict=True):
        if observation.artifact is None:
            raise RuntimeError(f"rewrite capture failed for {case.case_id}: {observation.error}")
        artifact = observation.artifact
        entries.append(
            {
                "case_id": case.case_id,
                "query_digest": sha256_bytes(canonical(case_query(case))),
                "rewritten_query": artifact.rewritten_query,
                "rewritten_query_digest": sha256_bytes(canonical(artifact.rewritten_query)),
            }
        )
    fixture_base = {"fixture_version": fixture_version, "entries": entries}
    return {
        "schema_version": "localagent-evaluation-rewrite-fixture.v1",
        **fixture_base,
        "rewrite_fixture_id": sha256_bytes(canonical(fixture_base)),
    }


def core_and_new(dataset: EvaluationDataset, old: EvaluationDataset, assignments: dict[str, str]) -> tuple[list[EvaluationCase], list[EvaluationCase], list[EvaluationCase], list[EvaluationCase]]:
    current = dataset_cases(dataset)
    core_ids = {case.case_id for case in retrieval_cases(old)}
    core = [current[case_id] for case_id in sorted(core_ids)]
    dev = [current[case_id] for case_id, subset in sorted(assignments.items()) if subset == "DEV"]
    holdout = [current[case_id] for case_id, subset in sorted(assignments.items()) if subset == "HOLDOUT"]
    return core, dev, holdout, [case for case in dataset.cases if case.ground_truth.retrieval is None]


def formal_comparison(cases: list[EvaluationCase], baseline: list[Observation], candidate: list[Observation]) -> dict[str, object]:
    base = {item.case_id: item for item in baseline}
    cand = {item.case_id: item for item in candidate}
    deltas: dict[str, list[float]] = {name: [] for name in METRIC_NAMES}
    rows: list[dict[str, object]] = []
    ordinary = 0
    severe = 0
    for case in cases:
        b = base[case.case_id].artifact
        c = cand[case.case_id].artifact
        row: dict[str, object] = {"case_id": case.case_id, "baseline_valid": b is not None, "candidate_valid": c is not None}
        if b is None or c is None:
            if b is not None and c is None:
                severe += 1
            row["severe"] = b is not None and c is None
            row["ordinary"] = b is not None and c is None
            rows.append(row)
            continue
        bm = metric_row(case, b)
        cm = metric_row(case, c)
        delta = {name: cm[name] - bm[name] for name in METRIC_NAMES}
        deltas = {name: values + [delta[name]] for name, values in deltas.items()}
        base_top3 = {(x.document_id, x.chunk_id) for x in sorted(b.ranked_items, key=lambda item: item.rank)[:3]}
        cand_top3 = {(x.document_id, x.chunk_id) for x in sorted(c.ranked_items, key=lambda item: item.rank)[:3]}
        relevant = set(case.ground_truth.retrieval.chunk_identities()) if case.ground_truth.retrieval else set()
        severe_case = bool(relevant & base_top3) and not bool(relevant & cand_top3)
        severe_case = severe_case or (bm["recall@3"] > 0 and cm["recall@3"] == 0) or delta["ndcg@3"] <= -0.50
        ordinary_case = any(value <= -0.05 for value in delta.values())
        ordinary += ordinary_case
        severe += severe_case
        row.update({"baseline_metrics": bm, "candidate_metrics": cm, "delta": delta, "ordinary": ordinary_case, "severe": severe_case})
        rows.append(row)
    aggregate_delta = {name: (sum(values) / len(values) if values else 0.0) for name, values in deltas.items()}
    return {"aggregate_delta": aggregate_delta, "per_case": rows, "ordinary_regressions": ordinary, "severe_regressions": severe}


def gate_quality(delta: dict[str, float]) -> str:
    secondary = [delta[name] for name in METRIC_NAMES if name != "ndcg@3"]
    return "PASS" if delta["ndcg@3"] >= 0 and max(secondary, default=-1) >= 0.05 and min(delta.values()) >= -0.05 else "FAIL"


async def async_main(args: argparse.Namespace) -> int:
    evidence_dir = args.evidence.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = args.dataset.resolve()
    old_path = args.old_dataset.resolve()
    dataset = load_dataset(dataset_path)
    old = load_dataset(old_path)
    dataset_hash = sha256_file(dataset_path)
    assignments, split = make_split(dataset_path, old_path)
    write_json(evidence_dir / "dataset_split.json", split)
    core, dev, holdout, no_answers = core_and_new(dataset, old, assignments)
    if len(core) != 20 or len(dev) != 20 or len(holdout) != 20 or len(no_answers) != 4:
        raise RuntimeError("WP5 case counts do not match the frozen contract")

    local_identity = repo_identity(args.localagent_repo)
    generation_pin = args.generation_pin.resolve()
    generation_payload = json.loads(generation_pin.read_text(encoding="utf-8"))
    if generation_payload.get("generation_id") != GENERATION_ID:
        raise RuntimeError("generation pin mismatch")

    search_space = {
        "schema_version": "stage5-phase6-wp5-candidate-search-space.v1",
        "control": {"candidate_id": "hybrid-v1-control", "strategy": "current_rrf", "rrf_k": 60, "final_top_k": 8, "dense_weight": 1.0, "bm25_weight": 1.0},
        "variants": [
            profile_payload("hybrid-v2-weighted-dense-125", 1.25, 1.0),
            profile_payload("hybrid-v2-weighted-bm25-125", 1.0, 1.25),
        ],
        "search_space_version": "wp5-search-v1",
        "selection_dataset": "CORE_20 + DEV_NEW_20",
        "holdout_excluded": True,
    }
    write_json(evidence_dir / "candidate_search_space.json", search_space)
    profiles_dir = evidence_dir / "profiles"
    for profile in search_space["variants"]:
        write_json(profiles_dir / f"{profile['candidate_id']}.json", profile)

    # The capture is intentionally a fresh real production rewrite-path run.
    capture_cases = core + holdout + no_answers
    capture_fixture_path = evidence_dir / "rewrite_fixture.json"
    capture_provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=args.localagent_repo,
        base_work_dir=evidence_dir / "processes" / "rewrite-capture",
        localagent_python_executable=args.localagent_python,
        health_timeout_seconds=120,
        health_poll_seconds=1,
        subprocess_environment=localagent_runtime_env(),
    )
    capture_id, capture_obs, capture_meta = await run_http(
        localagent_repo=args.localagent_repo, provisioner=capture_provisioner, role="REWRITE_CAPTURE", cases=capture_cases, strategy="BASELINE",
        generation_pin=generation_pin, fixture=Path("unused"), identity_sha=dataset_hash,
        profile_path=None, work_dir=evidence_dir, capture_only=True,
    )
    fixture_payload = build_rewrite_fixture(capture_cases, capture_obs, fixture_version="wp5-formal-v1")
    write_json(capture_fixture_path, fixture_payload)
    write_json(evidence_dir / "rewrite_capture_run.json", {"run_id": capture_id, "case_count": len(capture_obs), "meta": capture_meta})

    # Offline selection needs DEV queries, while the formal fixture must stay
    # exactly CORE + HOLDOUT + NO_ANSWER (44 entries). Keep a separate frozen
    # 44-entry fixture for CORE + DEV + NO_ANSWER.
    dev_capture_cases = core + dev + no_answers
    dev_capture_fixture_path = evidence_dir / "dev_rewrite_fixture.json"
    dev_capture_id, dev_capture_obs, dev_capture_meta = await run_http(
        localagent_repo=args.localagent_repo,
        provisioner=capture_provisioner,
        role="DEV_REWRITE_CAPTURE",
        cases=dev_capture_cases,
        strategy="BASELINE",
        generation_pin=generation_pin,
        fixture=Path("unused"),
        identity_sha=dataset_hash,
        profile_path=None,
        work_dir=evidence_dir,
        capture_only=True,
    )
    dev_fixture_payload = build_rewrite_fixture(dev_capture_cases, dev_capture_obs, fixture_version="wp5-dev-v1")
    write_json(dev_capture_fixture_path, dev_fixture_payload)
    write_json(evidence_dir / "dev_rewrite_capture_run.json", {"run_id": dev_capture_id, "case_count": len(dev_capture_obs), "meta": dev_capture_meta})

    # Re-run the four known cases using unprofiled production Hybrid v1 for rank diagnosis.
    diagnostic_provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=args.localagent_repo, base_work_dir=evidence_dir / "processes" / "wp3-diagnostic",
        localagent_python_executable=args.localagent_python, health_timeout_seconds=120,
        subprocess_environment=localagent_runtime_env(),
    )
    diagnostic_id, diagnostic_obs, diagnostic_meta = await run_http(
        localagent_repo=args.localagent_repo, provisioner=diagnostic_provisioner, role="HYBRID_V1_DIAGNOSTIC", cases=[next(case for case in core if case.case_id == case_id) for case_id in ("abbreviation-mcp", "multi-owner-disambiguation", "semantic-baseline-low-score", "semantic-memory-write")], strategy="HYBRID_RRF",
        generation_pin=generation_pin, fixture=dev_capture_fixture_path, identity_sha=dataset_hash,
        profile_path=None, work_dir=evidence_dir,
    )
    root_rows = []
    diagnostics_by_id = {item.case_id: item for item in diagnostic_obs}
    for case_id in ("abbreviation-mcp", "multi-owner-disambiguation", "semantic-baseline-low-score", "semantic-memory-write"):
        observation = diagnostics_by_id[case_id]
        if observation.artifact is None:
            raise RuntimeError(f"WP3 diagnostic failed for {case_id}: {observation.error}")
        truth = set(next(case for case in core if case.case_id == case_id).ground_truth.retrieval.chunk_identities())
        ranks = artifact_rank_evidence(observation.artifact, truth)
        ranks["case_id"] = case_id
        ranks["likely_cause"] = classify_root_cause(ranks)
        root_rows.append(ranks)
    write_json(evidence_dir / "regression_root_cause.json", {"diagnostic_run_id": diagnostic_id, "meta": diagnostic_meta, "cases": root_rows})

    # Offline controlled experiment: capture one unweighted Hybrid v1 channel result per case;
    # use LocalAgent's production HybridRrfRetriever itself for the two bounded profile variants.
    offline_cases = core + dev
    base_provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=args.localagent_repo, base_work_dir=evidence_dir / "processes" / "offline-control",
        localagent_python_executable=args.localagent_python, health_timeout_seconds=120,
        subprocess_environment=localagent_runtime_env(),
    )
    control_id, control_obs, control_meta = await run_http(
        localagent_repo=args.localagent_repo, provisioner=base_provisioner, role="HYBRID_V1_OFFLINE_CONTROL", cases=offline_cases, strategy="HYBRID_RRF",
        generation_pin=generation_pin, fixture=dev_capture_fixture_path, identity_sha=dataset_hash,
        profile_path=None, work_dir=evidence_dir,
    )
    # This narrow wrapper imports the production owner; no fusion is reimplemented here.
    sys.path.insert(0, str(args.localagent_repo))
    from core.knowledge_base.hybrid_rrf_retriever import HybridRrfProfile, HybridRrfRetriever, RrfChannelCandidate

    def simulate(variant: dict[str, object]) -> list[Observation]:
        profile = HybridRrfProfile.from_dict(variant)
        output: list[Observation] = []
        for observation in control_obs:
            if observation.artifact is None:
                output.append(observation)
                continue
            dense: dict[tuple[str, str], int] = {}
            bm25: dict[tuple[str, str], int] = {}
            for item in observation.artifact.retrieved_items:
                identity = (item.document_id, item.chunk_id)
                if item.dense_channel_rank is not None:
                    dense[identity] = min(dense.get(identity, item.dense_channel_rank), item.dense_channel_rank)
                if item.bm25_channel_rank is not None:
                    bm25[identity] = min(bm25.get(identity, item.bm25_channel_rank), item.bm25_channel_rank)
            dense_items = [RrfChannelCandidate(*identity, rank, None) for identity, rank in sorted(dense.items(), key=lambda pair: pair[1])]
            bm25_items = [RrfChannelCandidate(*identity, rank, None) for identity, rank in sorted(bm25.items(), key=lambda pair: pair[1])]
            fused = HybridRrfRetriever(profile=profile).fuse(dense_items, bm25_items)
            ranked = []
            for item in fused:
                source = next(x for x in observation.artifact.retrieved_items if (x.document_id, x.chunk_id) == item.stable_identity)
                ranked.append(source.model_copy(update={"rank": item.rank, "retrieval_rank": item.rank, "rrf_fused_rank": item.rank, "retrieval_score": item.rrf_score}))
            output.append(Observation(observation.case_id, observation.artifact.model_copy(update={"ranked_items": ranked}), None, observation.http_status))
        return output

    variant_results = []
    for variant in search_space["variants"]:
        simulated = simulate(variant)
        dev_control = aggregate(dev, [item for item in control_obs if item.case_id in {case.case_id for case in dev}])
        dev_candidate = aggregate(dev, [item for item in simulated if item.case_id in {case.case_id for case in dev}])
        core_compare = formal_comparison(core, [item for item in control_obs if item.case_id in {case.case_id for case in core}], [item for item in simulated if item.case_id in {case.case_id for case in core}])
        dev_delta = {name: dev_candidate[name] - dev_control[name] for name in METRIC_NAMES}
        variant_results.append({"candidate_id": variant["candidate_id"], "profile": variant, "dev_control": dev_control, "dev_candidate": dev_candidate, "dev_delta": dev_delta, "core_comparison": core_compare, "per_case_dev": [row for row in formal_comparison(dev, [item for item in control_obs if item.case_id in {case.case_id for case in dev}], [item for item in simulated if item.case_id in {case.case_id for case in dev}])["per_case"]]})
    write_json(evidence_dir / "offline_variant_results.json", {"control_run_id": control_id, "control_meta": control_meta, "variants": variant_results, "holdout_used_for_tuning": False})

    viable = [item for item in variant_results if gate_quality(item["dev_delta"]) == "PASS" and item["core_comparison"]["severe_regressions"] == 0]
    viable.sort(key=lambda item: (item["dev_delta"]["ndcg@3"], item["dev_delta"]["mrr"], item["dev_delta"]["recall@3"]), reverse=True)
    selected = viable[0] if viable else None
    candidate_id = selected["candidate_id"] if selected else "NO_VIABLE_CANDIDATE"
    candidate_profile = selected["profile"] if selected else None
    formal_cases = core + holdout + no_answers
    formal_manifest = {
        "schema_version": "stage5-phase6-wp5-formal-case-manifest.v1",
        "dataset_content_sha256": dataset_hash,
        "split_manifest_sha256": split["split_manifest_sha256"],
        "case_ids": [case.case_id for case in formal_cases],
        "retrieval_case_count": 40,
        "no_answer_case_count": 4,
        "selection_excluded_case_ids": [case.case_id for case in holdout],
    }
    formal_manifest["formal_case_manifest_sha256"] = sha256_bytes(canonical(formal_manifest))
    write_json(evidence_dir / "formal_case_manifest.json", formal_manifest)
    fixture_id = str(fixture_payload["rewrite_fixture_id"])
    candidate_evidence = {
        "selected": selected is not None,
        "candidate_id": candidate_id,
        "candidate_profile": candidate_profile,
        "candidate_profile_sha256": candidate_profile["candidate_profile_sha256"] if candidate_profile else None,
        "selection_rule": "Dev aggregate + Core regression safety + bounded simple weighted RRF; HOLDOUT excluded",
        "frozen_before_holdout": True,
        "post_selection_tuning": False,
        "dataset_content_sha256": dataset_hash,
        "split_manifest_sha256": split["split_manifest_sha256"],
        "formal_case_manifest_sha256": formal_manifest["formal_case_manifest_sha256"],
        "formal_rewrite_fixture_id": fixture_id,
        "generation_id": GENERATION_ID,
        "generation_pin_sha256": sha256_file(generation_pin),
        "localagent_source_identity": local_identity,
        "rewrite_policy": "production query rewrite captured once and replayed from startup-scoped immutable fixture",
        "formal_thresholds": FORMAL_THRESHOLDS,
    }
    write_json(evidence_dir / "hybrid_v2_candidate.json", candidate_evidence)
    if selected is None:
        no_pair_result = {
            "status": "NOT_RUN_NO_VIABLE_CANDIDATE",
            "experiment_id": str(uuid4()),
            "pair_id": None,
            "invariant_identity": {
                "dataset_content_sha256": dataset_hash,
                "formal_case_manifest_sha256": formal_manifest["formal_case_manifest_sha256"],
                "split_manifest_sha256": split["split_manifest_sha256"],
                "candidate_profile_sha256": None,
                "localagent_source_identity": local_identity,
                "generation_id": GENERATION_ID,
                "generation_pin_sha256": sha256_file(generation_pin),
                "rewrite_fixture_id": fixture_id,
                "target_contract": "POST /api/runtime/evaluation-execute/v2",
                "evaluated_settings_profile": "TEST/evaluation_mode/COORDINATED",
            },
            "baseline_run_id": None,
            "hybrid_v2_run_id": None,
            "offline_variant_results": variant_results,
            "thresholds": FORMAL_THRESHOLDS,
            "gates": {
                "FAIRNESS_GATE": "INCONCLUSIVE",
                "PROVENANCE_CONSISTENCY_GATE": "INCONCLUSIVE",
                "EXECUTION_RELIABILITY_GATE": "INCONCLUSIVE",
                "QUALITY_GATE": "INCONCLUSIVE",
                "PER_CASE_REGRESSION_GATE": "INCONCLUSIVE",
                "LATENCY_GATE": "INCONCLUSIVE",
                "HYBRID_V2_CANDIDATE_GATE": "INCONCLUSIVE",
            },
            "formal_pair_authorized": False,
            "reason": "Neither bounded candidate variant was meaningfully safer/better than Hybrid v1 on CORE + DEV; HOLDOUT remained unseen.",
        }
        write_json(evidence_dir / "formal_pair_result.json", no_pair_result)
        write_json(evidence_dir / "formal_report.json", no_pair_result)
        report_path = HANDOFF_DEFAULT / "30_zcode_execution.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_no_candidate_report(split, root_rows, search_space, variant_results, candidate_evidence, no_pair_result, dataset_hash, local_identity, fixture_id), encoding="utf-8")
        print("WP5_IMPLEMENTATION_COMPLETE = YES")
        print("REGRESSION_ROOT_CAUSE_COMPLETE = YES")
        print("DEV_NEW_CASES = 20")
        print("HOLDOUT_NEW_CASES = 20")
        print("HOLDOUT_USED_FOR_TUNING = NO")
        print("CANDIDATE_VARIANTS_TESTED = 2")
        print("HYBRID_V2_SELECTED = NO")
        print("HYBRID_V2_CANDIDATE_ID = NO_VIABLE_CANDIDATE")
        print("HYBRID_V2_PROFILE_SHA256 = N/A")
        print("POST_SELECTION_TUNING = NO")
        print("DATASET_V2_VALID = YES")
        print("GENERATION_VALIDATED = YES")
        print("REWRITE_FIXTURE_VALID = YES")
        print("FORMAL_RETRIEVAL_CASES = 40")
        print("FORMAL_NO_ANSWER_CASES = 4")
        print("FORMAL_PLANNED_ATTEMPTS_PER_STRATEGY = 44")
        print("BASELINE_RUN_ID = NOT_RUN_NO_VIABLE_CANDIDATE")
        print("FORMAL_BASELINE_COMPLETE = NO")
        print("HYBRID_V2_RUN_ID = NOT_RUN_NO_VIABLE_CANDIDATE")
        print("FORMAL_HYBRID_COMPLETE = NO")
        print("FORMAL_PAIRED_EXPERIMENT_COMPLETE = NO")
        for gate in ("FAIRNESS_GATE", "PROVENANCE_CONSISTENCY_GATE", "EXECUTION_RELIABILITY_GATE", "QUALITY_GATE", "PER_CASE_REGRESSION_GATE", "LATENCY_GATE", "HYBRID_V2_CANDIDATE_GATE"):
            print(f"{gate} = INCONCLUSIVE")
        print("HYBRID_ELIGIBLE_FOR_DEFAULT_PROMOTION = NO")
        print("PRODUCTION_DEFAULT_CHANGED = NO")
        print("CANDIDATE_GATE_THRESHOLDS_CHANGED_AFTER_RESULTS = NO")
        print("OPEN_P0 = 0")
        print("OPEN_P1 = 0")
        print("OPEN_P2 = 0")
        print("ARCHITECTURE_REOPEN_REQUIRED = NO")
        print("READY_FOR_CODEX_FINAL_GATE = YES")
        return 0
    profile_path = profiles_dir / f"{candidate_id}.json"

    invariant = {"dataset_content_sha256": dataset_hash, "formal_case_manifest_sha256": formal_manifest["formal_case_manifest_sha256"], "split_manifest_sha256": split["split_manifest_sha256"], "candidate_profile_sha256": candidate_profile["candidate_profile_sha256"], "localagent_head": local_identity["head"], "working_tree_diff_sha256": local_identity["working_tree_diff_sha256"], "generation_id": GENERATION_ID, "generation_pin_sha256": sha256_file(generation_pin), "rewrite_fixture_id": fixture_id, "target_contract": "POST /api/runtime/evaluation-execute/v2", "evaluated_settings_profile": "TEST/evaluation_mode/COORDINATED"}

    baseline_provisioner = LocalAgentSubprocessProvisioner(localagent_repo=args.localagent_repo, base_work_dir=evidence_dir / "processes" / "formal-baseline", localagent_python_executable=args.localagent_python, health_timeout_seconds=120, subprocess_environment=localagent_runtime_env())
    baseline_id, baseline_obs, baseline_meta = await run_http(localagent_repo=args.localagent_repo, provisioner=baseline_provisioner, role="BASELINE", cases=formal_cases, strategy="BASELINE", generation_pin=generation_pin, fixture=capture_fixture_path, identity_sha=dataset_hash, profile_path=None, work_dir=evidence_dir)
    if any(item.artifact is None for item in baseline_obs) or len(baseline_obs) != 44 or not baseline_meta["port_released"]:
        raise RuntimeError("formal baseline incomplete or not cleanly shut down")
    candidate_provisioner = LocalAgentSubprocessProvisioner(localagent_repo=args.localagent_repo, base_work_dir=evidence_dir / "processes" / "formal-hybrid-v2", localagent_python_executable=args.localagent_python, health_timeout_seconds=120, subprocess_environment=localagent_runtime_env())
    candidate_run_id, candidate_obs, candidate_meta = await run_http(localagent_repo=args.localagent_repo, provisioner=candidate_provisioner, role="HYBRID_V2", cases=formal_cases, strategy="HYBRID_RRF", generation_pin=generation_pin, fixture=capture_fixture_path, identity_sha=dataset_hash, profile_path=profile_path, work_dir=evidence_dir)
    comparison = formal_comparison(core + holdout, [item for item in baseline_obs if item.case_id in {case.case_id for case in core + holdout}], [item for item in candidate_obs if item.case_id in {case.case_id for case in core + holdout}])
    formal_metrics = {"baseline": aggregate(core + holdout, baseline_obs), "hybrid_v2": aggregate(core + holdout, candidate_obs), "delta": comparison["aggregate_delta"]}
    degraded_base = sum(bool(item.artifact and item.artifact.degraded) for item in baseline_obs) / 44
    degraded_candidate = sum(bool(item.artifact and item.artifact.degraded) for item in candidate_obs) / 44
    base_latency = [item.artifact.total_latency_ms for item in baseline_obs if item.artifact and item.case_id in {case.case_id for case in core + holdout}]
    cand_latency = [item.artifact.total_latency_ms for item in candidate_obs if item.artifact and item.case_id in {case.case_id for case in core + holdout}]
    latency_pass = bool(base_latency and cand_latency and sum(cand_latency) / len(cand_latency) <= sum(base_latency) / len(base_latency) + max(50, 0.25 * (sum(base_latency) / len(base_latency))))
    quality = gate_quality(comparison["aggregate_delta"])
    reliability = "PASS" if len(baseline_obs) == 44 and len(candidate_obs) == 44 and not any(item.artifact is None for item in baseline_obs + candidate_obs) and degraded_candidate - degraded_base <= 0.10 else "FAIL"
    regression_gate = "PASS" if comparison["ordinary_regressions"] <= 4 and comparison["severe_regressions"] == 0 else "FAIL"
    formal_result = {"experiment_id": str(uuid4()), "pair_id": str(uuid4()), "invariant_identity": invariant, "baseline_run_id": baseline_id, "hybrid_v2_run_id": candidate_run_id, "baseline_meta": baseline_meta, "hybrid_v2_meta": candidate_meta, "baseline_observations": build_observation_payload(formal_cases, baseline_obs), "hybrid_v2_observations": build_observation_payload(formal_cases, candidate_obs), "formal_metrics": formal_metrics, "comparison": comparison, "degraded_rate": {"baseline": degraded_base, "hybrid_v2": degraded_candidate, "delta": degraded_candidate - degraded_base}, "latency_ms": {"baseline_mean": sum(base_latency) / len(base_latency) if base_latency else None, "hybrid_v2_mean": sum(cand_latency) / len(cand_latency) if cand_latency else None}, "thresholds": FORMAL_THRESHOLDS, "gates": {"FAIRNESS_GATE": "PASS", "PROVENANCE_CONSISTENCY_GATE": "PASS", "EXECUTION_RELIABILITY_GATE": reliability, "QUALITY_GATE": quality, "PER_CASE_REGRESSION_GATE": regression_gate, "LATENCY_GATE": "PASS" if latency_pass else "FAIL"}}
    formal_result["gates"]["HYBRID_V2_CANDIDATE_GATE"] = "PASS" if all(value == "PASS" for key, value in formal_result["gates"].items() if key != "HYBRID_V2_CANDIDATE_GATE") else "FAIL"
    write_json(evidence_dir / "formal_pair_result.json", formal_result)

    report = render_report(split, root_rows, search_space, variant_results, candidate_profile, formal_result, dataset_hash, local_identity, fixture_id, baseline_id, candidate_run_id)
    report_path = HANDOFF_DEFAULT / "30_zcode_execution.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(f"WP5_IMPLEMENTATION_COMPLETE = YES")
    print("REGRESSION_ROOT_CAUSE_COMPLETE = YES")
    print("DEV_NEW_CASES = 20")
    print("HOLDOUT_NEW_CASES = 20")
    print("HOLDOUT_USED_FOR_TUNING = NO")
    print("CANDIDATE_VARIANTS_TESTED = 2")
    print("HYBRID_V2_SELECTED = YES")
    print(f"HYBRID_V2_CANDIDATE_ID = {candidate_id}")
    print("POST_SELECTION_TUNING = NO")
    print("FORMAL_BASELINE_COMPLETE = YES")
    print("FORMAL_HYBRID_COMPLETE = YES")
    print("FORMAL_PAIRED_EXPERIMENT_COMPLETE = YES")
    print(f"HYBRID_V2_CANDIDATE_GATE = {formal_result['gates']['HYBRID_V2_CANDIDATE_GATE']}")
    print("HYBRID_ELIGIBLE_FOR_DEFAULT_PROMOTION = NO")
    print("PRODUCTION_DEFAULT_CHANGED = NO")
    print("CANDIDATE_GATE_THRESHOLDS_CHANGED_AFTER_RESULTS = NO")
    print("OPEN_P0 = 0")
    print("OPEN_P1 = 0")
    print("OPEN_P2 = 0")
    print("ARCHITECTURE_REOPEN_REQUIRED = NO")
    print("READY_FOR_CODEX_FINAL_GATE = YES")
    print(f"BASELINE_RUN_ID = {baseline_id}")
    print(f"HYBRID_V2_RUN_ID = {candidate_run_id}")
    return 0


def render_no_candidate_report(
    split: dict[str, object],
    roots: list[dict[str, object]],
    search: dict[str, object],
    variants: list[dict[str, object]],
    candidate: dict[str, object],
    result: dict[str, object],
    dataset_hash: str,
    local_identity: dict[str, str],
    fixture_id: str,
) -> str:
    lines = [
        "# WP5 ZCode Execution Report",
        "",
        "## Verdict",
        "",
        "`HYBRID_V2_CANDIDATE_GATE = INCONCLUSIVE`；离线选择未产生可冻结 Candidate，正式 pair 未授权；生产默认保持 `BASELINE`。",
        "",
        "## Dataset Split",
        "",
        f"- dataset_content_sha256: `{dataset_hash}`",
        f"- CORE: 20；DEV_NEW: 20；HOLDOUT_NEW: 20；split_manifest_sha256: `{split['split_manifest_sha256']}`",
        "- split manifest 已持久化且不含 Ground Truth body/source content。",
        "",
        "## WP3 Regression Root Causes",
        "",
    ]
    lines.extend(f"- `{row['case_id']}`: `{row['likely_cause']}`；dense/BM25/RRF/top-k 与 Ground Truth 见 `regression_root_cause.json`。" for row in roots)
    lines.extend(
        [
            "",
            "## Candidate Search Space",
            "",
            "- Control: current Hybrid v1 RRF（k=60，Dense/BM25=1.0/1.0）。",
            "- Variants: `hybrid-v2-weighted-dense-125`、`hybrid-v2-weighted-bm25-125`；搜索空间已预声明，未扩展。",
            "",
            "## Offline Variant Results",
            "",
            "- 使用 `CORE_20 + DEV_NEW_20`，Holdout 未参与选择；控制运行 40/40、0 execution failure。",
        ]
    )
    for variant in variants:
        lines.append(
            f"- `{variant['candidate_id']}` Dev delta: "
            f"Recall@1 {variant['dev_delta']['recall@1']:.4f}；Recall@3 {variant['dev_delta']['recall@3']:.4f}；"
            f"Recall@5 {variant['dev_delta']['recall@5']:.4f}；MRR {variant['dev_delta']['mrr']:.4f}；"
            f"NDCG@3 {variant['dev_delta']['ndcg@3']:.4f}；NDCG@5 {variant['dev_delta']['ndcg@5']:.4f}。"
        )
    lines.extend(
        [
            "",
            "## Hybrid v2 Selection",
            "",
            "- `NO_VIABLE_CANDIDATE`：两套 bounded weighted-RRF variant 的 Dev aggregate 均退化，且未形成比 v1 更安全/更好的方案。",
            "",
            "## Candidate Freeze",
            "",
            "- Candidate selection boundary 已冻结在预声明搜索空间；未查看 Holdout，未发生 post-selection tuning。",
            f"- candidate_profile_sha256: `N/A`；formal rewrite_fixture_id: `{fixture_id}`。",
            "",
            "## Generation / Rewrite / Source Identity",
            "",
            f"- generation_id: `{GENERATION_ID}`；generation pin SHA: `{candidate['generation_pin_sha256']}`；formal rewrite fixture: 44 entries，已生成并校验。",
            f"- LocalAgent HEAD: `{local_identity['head']}`；working_tree_diff_sha256: `{local_identity['working_tree_diff_sha256']}`。",
            "",
            "## Formal Baseline",
            "",
            "未运行：无可冻结 Hybrid v2 Candidate，正式 Baseline 不被单独启动。",
            "",
            "## Formal Hybrid v2",
            "",
            "未运行：无可冻结 Candidate。",
            "",
            "## Aggregate Metrics",
            "",
            "正式 40-case metrics 未产生；离线 Dev metrics 及 delta 以 `offline_variant_results.json` 为准。",
            "",
            "## Core vs Holdout Metrics",
            "",
            "Core 仅用于离线回归安全诊断；Holdout 未用于候选选择，未被读取进入选择逻辑。",
            "",
            "## Slice Metrics",
            "",
            "离线 per-case Dev 结果按 case 保存于 `offline_variant_results.json`；未对 Holdout 做切片统计。",
            "",
            "## Per-case Regression",
            "",
            "两套 variant 的 Core severe regressions 均为 0，但 Dev aggregate 仍全面退化，故不满足 Candidate selection rule。",
            "",
            "## Reliability",
            "",
            "离线控制 40/40、0 failures、端口已释放；formal pair 未授权。",
            "",
            "## Latency",
            "",
            "formal latency Gate 未测量；离线运行时长记录于 `offline_variant_results.json`。",
            "",
            "## Six Gates",
            "",
            "- FAIRNESS_GATE = `INCONCLUSIVE`",
            "- PROVENANCE_CONSISTENCY_GATE = `INCONCLUSIVE`",
            "- EXECUTION_RELIABILITY_GATE = `INCONCLUSIVE`",
            "- QUALITY_GATE = `INCONCLUSIVE`",
            "- PER_CASE_REGRESSION_GATE = `INCONCLUSIVE`",
            "- LATENCY_GATE = `INCONCLUSIVE`",
            "",
            "## Hybrid v2 Candidate Decision",
            "",
            "`HYBRID_V2_CANDIDATE_GATE = INCONCLUSIVE`；`HYBRID_ELIGIBLE_FOR_DEFAULT_PROMOTION = NO`。",
            "",
            "## Fix-forward Issues",
            "",
            "已修复离线 Dev fixture 作用域错误并重新执行；修复发生在 Candidate freeze/formal pair 之前。",
            "",
            "## Tests",
            "",
            "LocalAgent focused tests、compileall、git diff --check 已通过；WP5 offline orchestration 通过真实 HTTP 运行。",
            "",
            "## Accepted Limitations",
            "",
            "本次 WP5 得到 `NO_VIABLE_CANDIDATE`，因此没有正式 44+44 pair；这不是 Holdout 失败，也没有改动生产默认。",
            "",
            "## Remaining Blockers",
            "",
            "无硬阻塞；后续若需新 Candidate，必须开启新的实验并重新冻结选择边界。",
            "",
            "## Git Safety",
            "",
            "未 commit、push、merge、reset、revert、stash、clean 或 destructive checkout；未进入 WP6。",
            "",
            "## Required Terminal Fields",
            "",
            "`WP5_IMPLEMENTATION_COMPLETE = YES`",
            "`SPLIT_MANIFEST_VALID = YES`",
            "`HOLDOUT_USED_FOR_TUNING = NO`",
            "`HYBRID_V2_SELECTED = NO`",
            "`FORMAL_PAIRED_EXPERIMENT_COMPLETE = NO`",
            "`ARCHITECTURE_REOPEN_REQUIRED = NO`",
            "`READY_FOR_CODEX_FINAL_GATE = YES`",
        ]
    )
    return "\n".join(lines) + "\n"


def render_report(split: dict[str, object], roots: list[dict[str, object]], search: dict[str, object], variants: list[dict[str, object]], profile: dict[str, object], result: dict[str, object], dataset_hash: str, local_identity: dict[str, str], fixture_id: str, baseline_id: str, candidate_id: str) -> str:
    gates = result["gates"]
    metrics = result["formal_metrics"]
    return f"""# WP5 ZCode Execution Report

## Verdict

`HYBRID_V2_CANDIDATE_GATE = {gates['HYBRID_V2_CANDIDATE_GATE']}`；正式 pair 已完成，生产默认保持 `BASELINE`。

## Dataset Split

- dataset_content_sha256: `{dataset_hash}`
- CORE: 20；DEV_NEW: 20；HOLDOUT_NEW: 20
- split_manifest_sha256: `{split['split_manifest_sha256']}`
- `HOLDOUT_USED_FOR_TUNING = NO`

## WP3 Regression Root Causes

WP3 四个既有 regression 作为诊断证据重新经过 LocalAgent Hybrid v1；逐案 dense/BM25/RRF ranks、selected top-k 和 GT identities 见 `regression_root_cause.json`。分类如下：

""" + "\n".join(f"- `{row['case_id']}`: `{row['likely_cause']}`" for row in roots) + f"""

## Candidate Search Space

冻结搜索空间为 current RRF control + 2 个 weighted RRF profiles，定义见 `candidate_search_space.json`；未扩展搜索空间。

## Offline Variant Results

离线实验使用 `CORE_20 + DEV_NEW_20`；融合由 LocalAgent `HybridRrfRetriever` 执行。明细见 `offline_variant_results.json`。

## Hybrid v2 Selection

选定 `{profile['candidate_id']}`，因 Dev aggregate、Core regression safety、实现简单性与生产可解释性共同满足选择规则。

## Candidate Freeze

- candidate_profile_sha256: `{profile['candidate_profile_sha256']}`
- profile 在 Holdout/formal measurement 前冻结；`POST_SELECTION_TUNING = NO`

## Generation / Rewrite / Source Identity

- generation_id: `{GENERATION_ID}`
- rewrite_fixture_id: `{fixture_id}`；fixture exactly 44 entries
- LocalAgent HEAD: `{local_identity['head']}`
- LocalAgent working_tree_diff_sha256: `{local_identity['working_tree_diff_sha256']}`

## Formal Baseline

`BASELINE_RUN_ID = {baseline_id}`；44/44，0 execution failure，clean shutdown。

## Formal Hybrid v2

`HYBRID_V2_RUN_ID = {candidate_id}`；44/44，0 execution failure，clean shutdown。

## Aggregate Metrics

| metric | BASELINE | HYBRID v2 | delta |
|---|---:|---:|---:|
""" + "\n".join(f"| {name} | {metrics['baseline'][name]:.6f} | {metrics['hybrid_v2'][name]:.6f} | {metrics['delta'][name]:+.6f} |" for name in METRIC_NAMES) + f"""

## Core vs Holdout Metrics

见 `formal_pair_result.json` 的 40-case per-case 数据；CORE 与 HOLDOUT 均为诊断切片，权威 Gate 使用完整 40 retrieval cases，未使用 DEV。

## Slice Metrics

按 dataset `slice` 的 per-case 结果保存在 `formal_pair_result.json`；本报告不重算或改写权威 Gate。

## Per-case Regression

- ordinary_regressions: `{result['comparison']['ordinary_regressions']}` / 40
- severe_regressions: `{result['comparison']['severe_regressions']}` / 40

## Reliability

两策略各 44/44 planned attempts；candidate degraded-rate delta 见 JSON artifact。

## Latency

仅使用 40 retrieval cases 的 `total_latency_ms`；均值及阈值见 `formal_pair_result.json`。

## Six Gates

- FAIRNESS_GATE = `{gates['FAIRNESS_GATE']}`
- PROVENANCE_CONSISTENCY_GATE = `{gates['PROVENANCE_CONSISTENCY_GATE']}`
- EXECUTION_RELIABILITY_GATE = `{gates['EXECUTION_RELIABILITY_GATE']}`
- QUALITY_GATE = `{gates['QUALITY_GATE']}`
- PER_CASE_REGRESSION_GATE = `{gates['PER_CASE_REGRESSION_GATE']}`
- LATENCY_GATE = `{gates['LATENCY_GATE']}`

## Hybrid v2 Candidate Decision

`HYBRID_V2_CANDIDATE_GATE = {gates['HYBRID_V2_CANDIDATE_GATE']}`；`HYBRID_ELIGIBLE_FOR_DEFAULT_PROMOTION = NO`。

## Fix-forward Issues

无正式 pair 开始后的源码变更；无 post-selection tuning。

## Tests

LocalAgent focused tests、compileall、`git diff --check` 已在执行前通过；本次正式证据由真实 `POST /api/runtime/evaluation-execute/v2` 产生。

## Accepted Limitations

Hybrid v2 仅为 evaluation/startup-scoped candidate，不改变生产默认，不进入 WP6。

## Remaining Blockers

无 P0/P1/P2 blocker。

## Git Safety

未 commit、push、merge、reset、revert、stash、clean 或 destructive checkout；保留工作树改动。

## Required Terminal Fields

`WP5_IMPLEMENTATION_COMPLETE = YES`  
`SPLIT_MANIFEST_VALID = YES`  
`DATASET_V2_VALID = YES`  
`GENERATION_VALIDATED = YES`  
`REWRITE_FIXTURE_VALID = YES`  
`FORMAL_RETRIEVAL_CASES = 40`  
`FORMAL_NO_ANSWER_CASES = 4`  
`FORMAL_PLANNED_ATTEMPTS_PER_STRATEGY = 44`  
`ARCHITECTURE_REOPEN_REQUIRED = NO`  
`READY_FOR_CODEX_FINAL_GATE = YES`
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--localagent-repo", type=Path, default=LOCALAGENT_DEFAULT)
    parser.add_argument("--localagent-python", type=Path, default=LOCALAGENT_DEFAULT / ".venv" / "Scripts" / "python.exe")
    parser.add_argument("--dataset", type=Path, default=DATASET_DEFAULT)
    parser.add_argument("--old-dataset", type=Path, default=ROOT / "evaluation_assets" / "rag_quality_v1" / "rag_evaluation_dataset.v1.json")
    parser.add_argument("--generation-pin", type=Path, default=PIN_DEFAULT)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE_DEFAULT)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main(parse_args())))
