"""WP6-E-D-G2-R1 driver: deterministic subprocess smoke + 12-scenario Layer1 evidence run.

Layer1 使用 EPISODIC_EVALUATION_LAYER1（scripted backend，zero network）。
SETUP_MD_READ_BEFORE_BACKEND_START = YES（已读 D:/PythonProject/Local_Agent/.ai/setup.md）。
"""
# ruff: noqa

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from app.core.evaluation.episodic_dataset import load_episodic_dataset  # noqa: E402
from app.core.evaluation.episodic_impl_ref import episodic_evaluation_implementation_ref  # noqa: E402
from app.core.evaluation.execution import ExecutionTargetRef  # noqa: E402
from app.services.evaluation.episodic_environment import (  # noqa: E402
    EPISODIC_V2_FROZEN_DATASET_DIGEST,
    EPISODIC_FROZEN_TARGET_REF,
    EpisodicLocalAgentProvisioner,
    EpisodicTargetCertification,
    certify_episodic_dataset,
    certify_episodic_target,
    compute_target_evaluation_implementation_ref,
)
from app.services.evaluation.episodic_runner import (  # noqa: E402
    EpisodicExperimentRunner,
    SCENARIO_EXECUTION_POLICY,
)
from tests.unit.episodic_fixtures import scenario_by_case  # noqa: E402

LOCAL_AGENT_REPO = Path(r"D:\PythonProject\Local_Agent")
LOCAL_AGENT_PY = r"D:\PythonProject\Local_Agent\.venv\Scripts\python.exe"
BASE_WORK_DIR = Path(r"C:\Users\GemHr\AppData\Local\Temp\opencode\episodic_evidence_run_v2_current_f784_r3")
SETUP_MD_READ = "YES"


def _target_ref() -> ExecutionTargetRef:
    return ExecutionTargetRef(
        target_id="localagent-coordinated-http",
        target_kind="LOCALAGENT_HTTP",
        target_version_ref=None,
        config_ref=None,
    )


async def smoke(provisioner) -> EpisodicTargetCertification:
    dataset = load_episodic_dataset("evaluation_assets/stateful_episodic_v2/stateful_episodic_dataset.v2.json")
    certify_episodic_dataset(dataset)
    scenario = scenario_by_case(dataset, "E01")
    evidence = await provisioner.provision(scenario)
    try:
        bound = await provisioner.verify_bound(evidence)
        cert = await certify_episodic_target(evidence.localagent_base_url, LOCAL_AGENT_REPO)
        return cert, bound
    finally:
        await provisioner.cleanup(evidence, preserve=False)


async def main() -> None:
    start = time.monotonic()
    dataset = load_episodic_dataset("evaluation_assets/stateful_episodic_v2/stateful_episodic_dataset.v2.json")
    certify_episodic_dataset(dataset)
    assert dataset.content_digest == EPISODIC_V2_FROZEN_DATASET_DIGEST

    agentevalops_ref = episodic_evaluation_implementation_ref()
    target_ref_computed = compute_target_evaluation_implementation_ref(LOCAL_AGENT_REPO)
    print("SETUP_MD_READ_BEFORE_BACKEND_START =", SETUP_MD_READ)
    print("DATASET_DIGEST", dataset.content_digest, "MATCH", dataset.content_digest == EPISODIC_V2_FROZEN_DATASET_DIGEST)
    print("TARGET_REF_COMPUTED", target_ref_computed, "MATCH", target_ref_computed == EPISODIC_FROZEN_TARGET_REF)
    print("AGENTEVALOPS_REF", agentevalops_ref)

    provisioner = EpisodicLocalAgentProvisioner(
        localagent_repo=LOCAL_AGENT_REPO,
        base_work_dir=BASE_WORK_DIR,
        localagent_python_executable=LOCAL_AGENT_PY,
        health_timeout_seconds=90.0,
    )

    print("\n=== deterministic subprocess smoke ===")
    cert, bound = await smoke(provisioner)
    print("SMOKE_REACHABLE", cert.target_reachable)
    print("SMOKE_V3_AVAILABLE", cert.evaluation_execute_v3_available)
    print("SMOKE_REF_MATCH", cert.ref_matches)
    print("SMOKE_BOUND", bound)
    if not (cert.target_reachable and cert.evaluation_execute_v3_available and cert.ref_matches and bound):
        print("DETERMINISTIC_SUBPROCESS_SMOKE = FAIL")
        sys.exit(2)
    print("DETERMINISTIC_SUBPROCESS_SMOKE = PASS")

    passed_cert = EpisodicTargetCertification(
        target_reachable=True,
        evaluation_execute_v3_available=True,
        actual_target_ref=target_ref_computed,
        expected_target_ref=EPISODIC_FROZEN_TARGET_REF,
        ref_matches=target_ref_computed == EPISODIC_FROZEN_TARGET_REF,
    )

    print("\n=== 12-scenario global sequential run ===")
    experiment = EpisodicExperimentRunner(uuid_factory=lambda: str(__import__("uuid").uuid4()))
    receipt = await experiment.run_experiment(
        dataset=dataset,
        provisioner=provisioner,
        target_ref=_target_ref(),
        timeout=__import__("datetime").timedelta(seconds=60),
        localagent_repo=LOCAL_AGENT_REPO,
        target_certification=passed_cert,
        interpreter_ref=LOCAL_AGENT_PY,
        warnings=[f"SETUP_MD_READ_BEFORE_BACKEND_START={SETUP_MD_READ}"],
    )

    print("EXPERIMENT_EXECUTION_STATUS", receipt.aggregate_artifact.experiment_execution_status.value)
    print("EXPERIMENT_BLOCK_REASON", receipt.aggregate_artifact.experiment_block_reason.value if receipt.aggregate_artifact.experiment_block_reason else None)
    print("SCENARIO_EXECUTION_STARTED", receipt.aggregate_artifact.scenario_execution_started)
    print("SCENARIO_ARTIFACT_COUNT", len(receipt.aggregate_artifact.scenario_artifacts))
    outcomes = {}
    for scenario_artifact in receipt.aggregate_artifact.scenario_artifacts:
        outcomes[scenario_artifact.case_code] = scenario_artifact.scenario_outcome
        print(f"  {scenario_artifact.case_code} {scenario_artifact.scenario_id} -> {scenario_artifact.scenario_outcome}")
        for assertion in scenario_artifact.assertion_results:
            if assertion["status"] in {"FAIL", "BLOCKED"}:
                print(f"      {assertion['assertion_id']} {assertion['status']} failure={assertion.get('failure_taxonomy')} blocked={assertion.get('blocked_by')}")
    pass_count = sum(1 for v in outcomes.values() if v == "PASS")
    fail_count = sum(1 for v in outcomes.values() if v == "FAIL")
    blocked_count = sum(1 for v in outcomes.values() if v == "BLOCKED")
    print("SCENARIOS_TOTAL", len(outcomes))
    print("SCENARIOS_PASS", pass_count)
    print("SCENARIOS_FAIL", fail_count)
    print("SCENARIOS_BLOCKED", blocked_count)
    print("LAYER1_GATE_PASSED", receipt.gate.passed)
    print("LAYER1_GATE_REASONS", list(receipt.gate.reasons))
    print("METRICS")
    for name, metric in receipt.aggregate_artifact.metrics.items():
        print(f"  {name} value={metric.get('value')} denom={metric.get('evaluable_denominator')} blocked={metric.get('blocked')}")
    print("FAILURE_TAXONOMY", receipt.aggregate_artifact.failure_taxonomy)
    print("BLOCKED_TAXONOMY", receipt.aggregate_artifact.blocked_taxonomy)
    print("ELAPSED_SECONDS", round(time.monotonic() - start, 1))

    # persist aggregate artifact for the handoff evidence
    out_dir = BASE_WORK_DIR / "experiment_artifact"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "experiment_artifact.json").write_text(
        json.dumps(receipt.aggregate_artifact.model_dump_json_compat(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("ARTIFACT_WRITTEN", out_dir / "experiment_artifact.json")


if __name__ == "__main__":
    asyncio.run(main())
