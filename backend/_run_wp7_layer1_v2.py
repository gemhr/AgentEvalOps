"""Execute the immutable WP7-E V2 governance dataset against LocalAgent v4."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.core.evaluation.multi_agent_memory_governance import Observation, Verdict, evaluate_scenario, load_dataset
from app.services.evaluation.episodic_environment import EpisodicLocalAgentProvisioner

ROOT = Path(r"D:\PythonProject\AgentEvalOps")
TARGET = Path(r"D:\PythonProject\Local_Agent")
DATA = ROOT / "backend/evaluation_assets/multi_agent_memory_governance_v2/dataset.json"
OUT = ROOT / "backend/evaluation_artifacts/multi_agent_memory_governance"
PYTHON = TARGET / ".venv/Scripts/python.exe"
TARGET_REF = "sha256:107ff45eace28849162ddfda1bdfda2bb5e064eee50f36cd0fd0d8b6434b46d0"


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def receipt(root: Path, files: list[Path]) -> dict[str, object]:
    rows = [
        {
            "relative_file_path": str(p.relative_to(root)).replace("\\", "/"),
            "sha256": digest(p),
            "byte_length": p.stat().st_size,
        }
        for p in files
    ]
    aggregate = hashlib.sha256(
        "".join(f"{x['relative_file_path']}\0{x['sha256']}\0{x['byte_length']}\0" for x in rows).encode()
    ).hexdigest()
    return {
        "aggregate_algorithm": "sha256(path\\0file_sha256\\0byte_length\\0)",
        "aggregate_ref": "sha256:" + aggregate,
        "files": rows,
    }


def control(run) -> dict[str, object]:
    operation = run.operation.model_dump(exclude_none=True) if run.operation else None
    if operation and operation.get("source_memory_id") == "wp7-fixture-":
        operation["source_memory_id"] = "wp7-fixture-" + uuid.uuid5(uuid.NAMESPACE_URL, "private-a").hex
    payload: dict[str, object] = {
        "requester_agent_id": run.requester_agent_id,
        "project_grants": [x.model_dump() for x in run.grants],
        "private_fixtures": [x.model_dump() for x in run.private_fixtures],
        "deterministic_multi_agent": run.deterministic_multi_agent,
    }
    if run.project_id:
        payload["project_identity"] = {"project_id": run.project_id}
    if operation:
        payload["operation"] = operation
    return payload


def metric(name: str, verdicts: list[Verdict], *, failure_rate: bool = False) -> dict[str, object]:
    passed, failed, blocked = (
        sum(v is kind for v in verdicts) for kind in (Verdict.PASS, Verdict.FAIL, Verdict.BLOCKED)
    )
    denominator = passed + failed
    return {
        "metric_name": name,
        "numerator": failed if failure_rate else passed,
        "evaluable_denominator": denominator,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "value": (failed if failure_rate else passed) / denominator if denominator else None,
    }


async def main() -> None:
    dataset = load_dataset(DATA)
    provisioner = EpisodicLocalAgentProvisioner(
        localagent_repo=TARGET,
        base_work_dir=OUT / "runs_v2",
        localagent_python_executable=PYTHON,
        health_timeout_seconds=90,
    )
    results = []
    for scenario in dataset.scenarios:
        environment = await provisioner.provision(type("Scenario", (), {"scenario_id": scenario.scenario_id})())
        run_rows, observations = [], []
        try:
            async with httpx.AsyncClient(timeout=75, trust_env=False) as client:
                for run in scenario.runs:
                    response = await client.post(
                        environment.localagent_base_url + "/api/runtime/evaluation-execute/v4",
                        json={
                            "agent_id": "core_router",
                            "query": "database deployment",
                            "run_id": str(uuid.uuid4()),
                            "timeout_seconds": 60,
                            "evaluation_control": control(run),
                        },
                    )
                    body = response.json()
                    observation = Observation.from_response(body) if response.status_code == 200 else None
                    if observation is not None:
                        observations.append(observation)
                    run_rows.append({"run_id": run.run_id, "http_status": response.status_code, "response": body})
            verdict = (
                evaluate_scenario(scenario.scenario_id, observations)
                if len(observations) == len(scenario.runs)
                else Verdict.BLOCKED
            )
            results.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "required_surface": scenario.required_surface,
                    "verdict": verdict.value,
                    "runs": run_rows,
                }
            )
        finally:
            await provisioner.cleanup(environment, preserve=True)
    by_id = {row["scenario_id"]: Verdict(row["verdict"]) for row in results}
    mappings = {
        "PRIVATE_MEMORY_LEAKAGE_RATE": ([by_id["G02"]], True),
        "PROJECT_SCOPE_LEAKAGE_RATE": ([by_id["G05"]], True),
        "AUTHORIZED_PRIVATE_READ_SUCCESS_RATE": ([by_id["G01"]], False),
        "UNAUTHORIZED_PRIVATE_MUTATION_REJECTION_RATE": ([by_id["G03"]], False),
        "PROJECT_SHARED_RECALL_RATE": ([by_id["G04"]], False),
        "PROJECT_INJECTION_SUCCESS_RATE": ([by_id["G04"]], False),
        "UNAUTHORIZED_PROJECT_ACCESS_REJECTION_RATE": ([by_id["G06"]], False),
        "UNAUTHORIZED_PROJECT_MUTATION_REJECTION_RATE": ([by_id["G07"]], False),
        "PROMOTION_SUCCESS_RATE": ([by_id["G08"]], False),
        "PROMOTION_PROVENANCE_ACCURACY": ([by_id["G08"]], False),
        "SPECIALIST_OWNER_CORRECTNESS": ([by_id["G10"]], False),
        "DELEGATION_PRIVATE_PROPAGATION_RATE": ([by_id["G11"]], True),
        "INSTRUCTION_ELEVATION_VIOLATION_RATE": ([by_id["G12"]], True),
        "GOVERNANCE_SCENARIO_SUCCESS_RATE": (list(by_id.values()), False),
    }
    metrics = {name: metric(name, values, failure_rate=failure) for name, (values, failure) in mappings.items()}
    target_files = [
        TARGET / "server.py",
        TARGET / "core/runtime/memory_authorization.py",
        TARGET / "core/runtime/project_memory.py",
        TARGET / "core/runtime/run_coordinator.py",
        TARGET / "core/runtime/context.py",
        TARGET / "core/runtime/runtime_factory.py",
        TARGET / "core/chat_service.py",
    ]
    local_files = [
        DATA,
        ROOT / "backend/app/core/evaluation/multi_agent_memory_governance.py",
        ROOT / "backend/_run_wp7_layer1_v2.py",
    ]
    counts = {kind.value: sum(row["verdict"] == kind.value for row in results) for kind in Verdict}
    p0 = sum(row["verdict"] == "FAIL" for row in results if row["scenario_id"] in {"G02", "G05", "G03", "G07", "G12"})
    gate = "PASS" if counts["FAIL"] == 0 and counts["BLOCKED"] == 0 and p0 == 0 else "FAIL"
    artifact = {
        "schema_version": "multi-agent-memory-governance-experiment.v2",
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "id": dataset.dataset_id,
            "version": dataset.version,
            "digest": digest(DATA),
            "lineage": {
                "parent_dataset_id": dataset.parent_dataset_id,
                "parent_version": dataset.parent_version,
                "parent_digest": dataset.parent_digest,
                "remediation_reason": dataset.remediation_reason,
            },
        },
        "target_implementation_ref": TARGET_REF,
        "agentevalops_implementation_ref": receipt(ROOT, local_files)["aggregate_ref"],
        "target_source_receipt": receipt(TARGET, target_files),
        "agentevalops_source_receipt": receipt(ROOT, local_files),
        "execution_policy": "GLOBAL_SEQUENTIAL",
        "real_model_experiment_executed": False,
        "target_gate_field_used_as_authority": False,
        "scenarios": results,
        "metrics": metrics,
        "assertion_summary": {"pass": counts["PASS"], "fail": counts["FAIL"], "blocked": counts["BLOCKED"]},
        "p0_memory_governance_violations": p0,
        "layer1_gate": gate,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "experiment_artifact.v2.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)
    print(json.dumps({"pass": counts["PASS"], "fail": counts["FAIL"], "blocked": counts["BLOCKED"], "gate": gate}))


if __name__ == "__main__":
    asyncio.run(main())
