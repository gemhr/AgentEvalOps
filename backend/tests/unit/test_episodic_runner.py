"""WP6-E global-sequential scenario runner 契约测试（fake provisioner / fake v3 target）。"""

# ruff: noqa: D101, D102, D105, D415

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapters.evaluation.episodic_http_target import (
    EpisodicV3ExecutionResult,
    EpisodicV3Response,
)
from app.core.evaluation.episodic_dataset import (
    EPISODIC_DATASET_ID,
    EpisodicDataset,
    EpisodicEvaluationControl,
    EpisodicScenario,
    load_episodic_dataset,
)
from app.core.evaluation.execution import ExecutionRequest, ExecutionTargetRef
from app.core.evaluation.stateful_assertion import AssertionStatus
from app.services.evaluation.episodic_runner import (
    SCENARIO_EXECUTION_POLICY,
    EpisodicExperimentExecutionReceipt,
    EpisodicExperimentRunner,
    EpisodicScenarioRunPlan,
    EpisodicScenarioRunner,
    build_episodic_fixture_wire,
)
from app.services.evaluation.episodic_environment import EpisodicTargetCertification
from app.services.evaluation.stateful_environment import ScenarioEnvironmentEvidence
from tests.unit.episodic_fixtures import (
    load_dataset,
    scenario_by_case,
    v3_response_wire,
)

DATASET: EpisodicDataset = load_dataset()


class FakeProvisioner:
    """fake provisioner：fresh env per scenario；verify/cleanup 可追踪。"""

    def __init__(self) -> None:
        import tempfile

        self.provisioned: list[str] = []
        self.cleaned: list[str] = []
        self._index = 0
        self._root = Path(tempfile.mkdtemp(prefix="episodic-fake-"))

    async def provision(self, scenario: EpisodicScenario) -> ScenarioEnvironmentEvidence:
        import sqlite3

        self._index += 1
        self.provisioned.append(scenario.scenario_id)
        work = self._root / f"{scenario.scenario_id}-{self._index}"
        work.mkdir(parents=True, exist_ok=True)
        memory_db = work / "memory.db"
        connection = sqlite3.connect(memory_db)
        try:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS long_term_memory ("
                "memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, status TEXT NOT NULL, "
                "agent_id TEXT NOT NULL, memory_scope TEXT NOT NULL, canonical_text TEXT NOT NULL, "
                "payload TEXT NOT NULL, logical_key TEXT, origin_run_id TEXT NOT NULL, "
                "created_at TEXT NOT NULL, formation_method TEXT)"
            )
            connection.commit()
        finally:
            connection.close()
        journal_db = work / "journal.db"
        return ScenarioEnvironmentEvidence(
            scenario_id=scenario.scenario_id,
            scenario_environment_id=f"env-{scenario.case_code}",
            scenario_token=f"scn-{scenario.case_code}",
            work_dir=work,
            memory_db_path=memory_db,
            journal_db_path=journal_db,
            target_instance_ref=f"fake-process-{self._index}",
            localagent_base_url="http://127.0.0.1:9",
            fixture_seeded=False,
            provisioned_at=datetime.now(UTC),
            evaluation_only_harness=True,
        )

    async def verify_bound(self, evidence: ScenarioEnvironmentEvidence) -> bool:
        return True

    def build_target(self, evidence: ScenarioEnvironmentEvidence) -> "FakeV3Target":
        self.last_target = FakeV3Target(evidence)
        return self.last_target

    async def cleanup(self, evidence: ScenarioEnvironmentEvidence, *, preserve: bool) -> None:
        self.cleaned.append(f"{evidence.scenario_id}:preserve={preserve}")
        if not preserve:
            import shutil

            work = Path(evidence.work_dir)
            if self._root in work.parents:
                shutil.rmtree(work, ignore_errors=True)


class FakeV3Target:
    """fake v3 target：按 control 自动生成 deterministic v3 response（不调用真实 Store）。"""

    def __init__(self, evidence: ScenarioEnvironmentEvidence) -> None:
        self.evidence = evidence
        self.executed_run_ids: list[str] = []
        self.controls: list[dict] = []

    async def aclose(self) -> None:
        pass

    def _memory_id(self, run_id: str) -> str:
        return "episode-" + run_id.replace("-", "")[:16]

    async def execute_v3(self, *, request: ExecutionRequest, run_id: str, evaluation_control: dict):
        self.executed_run_ids.append(run_id)
        self.controls.append(evaluation_control)
        caps = set(evaluation_control.get("capabilities") or [])
        memory_id = self._memory_id(run_id)
        receipts = [
            {
                "run_id": run_id,
                "outcome": "CREATED",
                "memory_id": memory_id,
                "lesson_status": "ABSENT",
                "safe_reason": None,
            }
        ]
        replay = (
            [
                {
                    "run_id": run_id,
                    "outcome": "REUSED",
                    "memory_id": memory_id,
                    "lesson_status": "ABSENT",
                    "safe_reason": None,
                }
            ]
            if EpisodicEvaluationControl.REPLAY_EPISODIC_FORMATION_OBSERVER.value in caps
            else []
        )
        fixture = (
            [
                {
                    "fixture_ref": evaluation_control["fixture"]["fixture_ref"],
                    "memory_id": "episode-fixture-" + run_id[:8],
                    "origin_run_id": evaluation_control["fixture"]["origin_run_id"],
                    "origin_kind": "DATASET_CONTROLLED_INITIAL_FIXTURE",
                    "memory_scope": evaluation_control["fixture"]["memory_scope"],
                }
            ]
            if EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE.value in caps
            else []
        )
        capture = None
        if EpisodicEvaluationControl.CAPTURE_EPISODIC_PIPELINE.value in caps:
            capture = {
                "schema_version": "episodic-evaluation-capture.v1",
                "run_id": run_id,
                "capture_outcome": "COMPLETE",
                "selection": {"candidate_count": 0, "selected": []},
                "supplied": {"episodic_memory_ids": [], "record_count": 0},
                "injected": [],
            }
        wire = v3_response_wire(
            run_id=run_id,
            formation_receipts=receipts,
            replay_receipts=replay,
            fixture_receipts=fixture,
            capture=capture,
            runtime_receipt={
                "run_id": run_id,
                "plan_goal": None,
                "step_names": [],
                "step_statuses": [],
                "terminal_status": "SUCCEEDED",
                "stop_reason": "COMPLETED",
                "delivery_status": "DELIVERED",
                "formed_memory_id": memory_id,
                "formation_outcome": "CREATED",
                "canonical_text_sha256": None,
            },
        )
        typed = EpisodicV3Response.from_wire(wire)
        return EpisodicV3ExecutionResult(outcome=None, response=typed)  # type: ignore[arg-type]


def _plan(scenario: EpisodicScenario) -> EpisodicScenarioRunPlan:
    return EpisodicScenarioRunPlan(
        dataset=DATASET,
        scenario=scenario,
        target_ref=ExecutionTargetRef(
            target_id="localagent-coordinated-http",
            target_kind="LOCALAGENT_HTTP",
        ),
        timeout=timedelta(seconds=30),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_scenario_runs_ordered_and_shared_target():
    scenario = scenario_by_case(DATASET, "E07")
    provisioner = FakeProvisioner()
    ids = iter(
        [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ]
    )
    runner = EpisodicScenarioRunner(uuid_factory=lambda: next(ids))
    receipt = await runner.execute_scenario(_plan(scenario), provisioner)

    assert receipt.evaluation.scenario_id == scenario.scenario_id
    assert [record.dataset_run_id for record in receipt.run_records] == ["run_a", "run_b"]
    assert provisioner.provisioned == [scenario.scenario_id]  # single provision per scenario
    assert provisioner.cleaned == [
        f"{scenario.scenario_id}:preserve=True"
    ]  # fake 无 projection -> FAIL/BLOCKED -> preserve


@pytest.mark.asyncio
async def test_scenario_evidence_persists_into_artifact():
    scenario = scenario_by_case(DATASET, "E01")
    provisioner = FakeProvisioner()
    runner = EpisodicScenarioRunner(uuid_factory=lambda: "33333333-3333-3333-3333-333333333333")
    receipt = await runner.execute_scenario(_plan(scenario), provisioner)
    artifact = receipt.artifact
    assert artifact.scenario_id == scenario.scenario_id
    assert artifact.run_attempts[0].dataset_run_id == "run_a"
    assert artifact.run_attempts[0].actual_runtime_run_id == "33333333-3333-3333-3333-333333333333"
    assert artifact.identity_resolutions[0]["episode_ref"] == "run_a_episode"
    assert artifact.identity_resolutions[0]["status"] == "RESOLVED"


@pytest.mark.asyncio
async def test_e04_replay_run_id_mapped_to_actual_uuid():
    scenario = scenario_by_case(DATASET, "E04")
    provisioner = FakeProvisioner()
    runner = EpisodicScenarioRunner(uuid_factory=lambda: "44444444-4444-4444-4444-444444444444")
    receipt = await runner.execute_scenario(_plan(scenario), provisioner)
    control = provisioner.last_target.controls[0]
    assert control is not None
    assert control["replay_run_id"] == "44444444-4444-4444-4444-444444444444"
    assert control["replay_run_id"] != "run_a"
    assert "REPLAY_EPISODIC_FORMATION_OBSERVER" in control["capabilities"]
    assert receipt.evaluation.scenario_outcome in {
        AssertionStatus.FAIL,
        AssertionStatus.BLOCKED,
    }  # fake 无 projection -> 无法 PASS


@pytest.mark.asyncio
async def test_failed_scenario_preserves_artifacts():
    scenario = scenario_by_case(DATASET, "E05")
    provisioner = FakeProvisioner()
    runner = EpisodicScenarioRunner(uuid_factory=lambda: "55555555-5555-5555-5555-555555555555")
    receipt = await runner.execute_scenario(_plan(scenario), provisioner)
    # fake 有 CREATED receipt 但无 projection -> persistence FAIL（真实行为）-> preserve=True
    assert receipt.evaluation.scenario_outcome in {AssertionStatus.FAIL, AssertionStatus.BLOCKED}
    assert provisioner.cleaned == [f"{scenario.scenario_id}:preserve=True"]


@pytest.mark.asyncio
async def test_experiment_runs_12_scenarios_global_sequential():
    provisioner = FakeProvisioner()
    experiment_runner = EpisodicExperimentRunner(uuid_factory=lambda: str(uuid4()))
    receipt = await experiment_runner.run_experiment(
        dataset=DATASET,
        provisioner=provisioner,
        target_ref=ExecutionTargetRef(target_id="localagent-coordinated-http", target_kind="LOCALAGENT_HTTP"),
        timeout=timedelta(seconds=30),
        localagent_repo=Path(r"D:\PythonProject\Local_Agent"),
        target_certification=EpisodicTargetCertification(True, True, "sha256:ok", "sha256:ok", True),
    )
    assert isinstance(receipt, EpisodicExperimentExecutionReceipt)
    assert len(receipt.scenario_receipts) == 12
    assert [r.plan.scenario.case_code for r in receipt.scenario_receipts] == [f"E{i:02d}" for i in range(1, 13)]
    # 每个 scenario 独立 provision；global sequential
    assert provisioner.provisioned == [s.scenario_id for s in DATASET.scenarios]
    assert len(provisioner.cleaned) == 12
    assert receipt.aggregate_artifact.schema_version == "stateful-episodic-evaluation-artifact.v1"
    assert receipt.aggregate_artifact.dataset["id"] == EPISODIC_DATASET_ID
    assert len(receipt.aggregate_artifact.scenario_artifacts) == 12
    assert receipt.baseline_candidate.status.value == "CANDIDATE"
    assert receipt.baseline_candidate.canonical_baseline is False


def test_scenario_execution_policy_global_sequential():
    assert SCENARIO_EXECUTION_POLICY == "GLOBAL_SEQUENTIAL"


def test_fixture_wire_no_plan_prompt():
    scenario = scenario_by_case(DATASET, "E09")
    wire = build_episodic_fixture_wire(scenario.initial_fixture)
    assert "canonical_text" not in wire
    assert "raw_sql" not in wire
    assert "plan" not in wire


def test_dataset_certification_mismatch_blocks():
    from app.services.evaluation.episodic_environment import certify_episodic_dataset

    from app.core.evaluation.episodic_dataset import load_episodic_dataset as _load

    ds = _load("evaluation_assets/stateful_episodic_v1/stateful_episodic_dataset.v1.json")
    certify_episodic_dataset(ds)  # 匹配冻结 digest -> no raise
    with pytest.raises(Exception, match="DATASET_MISMATCH"):
        certify_episodic_dataset(ds, expected_digest="sha256:deadbeef")


# ---------------------------------------------------------------------------
# G2-R1 缺失 negative tests（14-23）：target preflight + UUID binding
# ---------------------------------------------------------------------------


class _CountingScenarioRunner:
    """统计 scheduler 调用次数（preflight block 必须为 0）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute_scenario(self, plan, provisioner, *, target_resolver=None):
        self.calls += 1
        raise AssertionError("scheduler must not be invoked when preflight blocks")


@pytest.mark.asyncio
async def test_wrong_target_ref_preflight_blocks_experiment():
    """14/15/16/17：wrong target ref -> preflight BLOCKED、scheduler call=0、artifacts=[]、SCENARIOS_BLOCKED=0。"""
    from app.core.evaluation.episodic_artifact import ExperimentBlockReason, ExperimentExecutionStatus

    counter = _CountingScenarioRunner()
    experiment = EpisodicExperimentRunner(scenario_runner=counter, uuid_factory=lambda: str(uuid4()))
    cert = EpisodicTargetCertification(False, False, "sha256:wrong-ref", "sha256:91611b1b1af027707e47641d9e3f69fed81dc216858998a71520a0bf1d7c9dc3", False)
    receipt = await experiment.run_experiment(
        dataset=DATASET,
        provisioner=FakeProvisioner(),
        target_ref=ExecutionTargetRef(target_id="localagent-coordinated-http", target_kind="LOCALAGENT_HTTP"),
        timeout=timedelta(seconds=30),
        localagent_repo=Path(r"D:\PythonProject\Local_Agent"),
        target_certification=cert,
    )
    artifact = receipt.aggregate_artifact
    assert artifact.experiment_execution_status is ExperimentExecutionStatus.BLOCKED
    assert artifact.experiment_block_reason is ExperimentBlockReason.PREREQUISITE
    assert artifact.scenario_execution_started is False
    assert artifact.scenario_artifacts == []
    assert counter.calls == 0  # scheduler 一次都没被调用
    assert receipt.scenario_receipts == ()


@pytest.mark.asyncio
async def test_preflight_block_no_scenario_artifacts_and_zero_blocked():
    """Preflight block 时 aggregate 不生成 12 个 scenario BLOCKED。"""
    from app.core.evaluation.episodic_artifact import ExperimentExecutionStatus

    experiment = EpisodicExperimentRunner(uuid_factory=lambda: str(uuid4()))
    cert = EpisodicTargetCertification(False, False, "sha256:wrong", "sha256:91611b1b1af027707e47641d9e3f69fed81dc216858998a71520a0bf1d7c9dc3", False)
    receipt = await experiment.run_experiment(
        dataset=DATASET,
        provisioner=FakeProvisioner(),
        target_ref=ExecutionTargetRef(target_id="localagent-coordinated-http", target_kind="LOCALAGENT_HTTP"),
        timeout=timedelta(seconds=30),
        localagent_repo=Path(r"D:\PythonProject\Local_Agent"),
        target_certification=cert,
    )
    assert receipt.aggregate_artifact.experiment_execution_status is ExperimentExecutionStatus.BLOCKED
    assert len(receipt.aggregate_artifact.scenario_artifacts) == 0
    # SCENARIOS_BLOCKED 来自 scenario receipts；preflight block 前无 scenario -> 0
    assert receipt.scenario_receipts == ()


@pytest.mark.asyncio
async def test_scenario_provision_failure_blocks_scenario():
    """18：scenario execution 开始后 provision failure -> scenario BLOCKED（EVALUATION_INFRA）。"""
    from app.services.evaluation.stateful_environment import StatefulEnvironmentError

    class FailingProvisioner:
        async def provision(self, scenario):
            raise StatefulEnvironmentError("provision failed")

        async def verify_bound(self, evidence):
            return False

        def build_target(self, evidence):
            raise AssertionError

        async def cleanup(self, evidence, *, preserve):
            pass

    scenario = scenario_by_case(DATASET, "E01")
    runner = EpisodicScenarioRunner(uuid_factory=lambda: str(uuid4()))
    receipt = await runner.execute_scenario(_plan(scenario), FailingProvisioner())
    assert receipt.evaluation.scenario_outcome is AssertionStatus.BLOCKED
    assert receipt.artifact.scenario_outcome == "BLOCKED"


def _run_evidence_with_run_id(run_id: str, *, dataset_run_id: str = "run_a", formation_run_id: str, runtime_run_id: str, capture_run_id: str | None):
    from app.core.evaluation.episodic_evidence import (
        EpisodicCaptureEvidence,
        EpisodicFormationReceiptEvidence,
        EpisodicRuntimeReceiptEvidence,
        EpisodicRunEvidence,
        RunExecutionStatus,
    )

    runtime = EpisodicRuntimeReceiptEvidence(
        run_id=runtime_run_id, plan_goal=None, step_names=(), step_statuses=(),
        terminal_status="SUCCEEDED", stop_reason="COMPLETED", delivery_status="DELIVERED",
        formed_memory_id="episode-x", formation_outcome="CREATED", canonical_text_sha256=None,
    )
    capture = (
        EpisodicCaptureEvidence.from_wire(
            {
                "schema_version": "episodic-evaluation-capture.v1",
                "run_id": capture_run_id,
                "capture_outcome": "COMPLETE",
                "selection": {"candidate_count": 0, "selected": []},
                "supplied": {"episodic_memory_ids": [], "record_count": 0},
                "injected": [],
            }
        )
        if capture_run_id is not None
        else None
    )
    return EpisodicRunEvidence(
        scenario_id="s",
        case_code="E07",
        dataset_run_id=dataset_run_id,
        actual_runtime_run_id=run_id,
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status="SUCCEEDED",
        delivery_status="DELIVERED",
        target_run_id=run_id,
        formation_receipt=EpisodicFormationReceiptEvidence(
            run_id=formation_run_id, outcome="CREATED", memory_id="episode-x", lesson_status="ABSENT"
        ),
        runtime_receipt=runtime,
        capture=capture,
    )


def test_runtime_receipt_uuid_mismatch_blocks():
    """19：runtime receipt UUID mismatch -> BLOCKED/EVIDENCE_CAPTURE。"""
    from app.core.evaluation.episodic_evidence import EpisodicEvidenceError, validate_runtime_uuid_binding

    record = _run_evidence_with_run_id(
        "11111111-1111-1111-1111-111111111111",
        formation_run_id="11111111-1111-1111-1111-111111111111",
        runtime_run_id="22222222-2222-2222-2222-222222222222",
        capture_run_id=None,
    )
    with pytest.raises(EpisodicEvidenceError, match="runtime_receipt.run_id"):
        validate_runtime_uuid_binding(record)


def test_formation_receipt_uuid_mismatch_blocks():
    """20：formation receipt UUID mismatch -> BLOCKED/EVIDENCE_CAPTURE。"""
    from app.core.evaluation.episodic_evidence import EpisodicEvidenceError, validate_runtime_uuid_binding

    record = _run_evidence_with_run_id(
        "11111111-1111-1111-1111-111111111111",
        formation_run_id="33333333-3333-3333-3333-333333333333",
        runtime_run_id="11111111-1111-1111-1111-111111111111",
        capture_run_id=None,
    )
    with pytest.raises(EpisodicEvidenceError, match="formation_receipt.run_id"):
        validate_runtime_uuid_binding(record)


def test_capture_uuid_mismatch_blocks():
    """21：capture UUID mismatch -> BLOCKED/EVIDENCE_CAPTURE。"""
    from app.core.evaluation.episodic_evidence import EpisodicEvidenceError, validate_runtime_uuid_binding

    record = _run_evidence_with_run_id(
        "11111111-1111-1111-1111-111111111111",
        formation_run_id="11111111-1111-1111-1111-111111111111",
        runtime_run_id="11111111-1111-1111-1111-111111111111",
        capture_run_id="44444444-4444-4444-4444-444444444444",
    )
    with pytest.raises(EpisodicEvidenceError, match="capture.run_id"):
        validate_runtime_uuid_binding(record)


def test_invalid_uuid_evidence_not_accepted_by_identity_resolver():
    """22：invalid UUID evidence 不能进入 identity resolver（resolve 前 fail closed）。"""
    from app.core.evaluation.episodic_evidence import EpisodicEvidenceError
    from app.services.evaluation.episodic_runner import resolve_episodic_identity

    scenario = scenario_by_case(DATASET, "E07")
    bad_record = _run_evidence_with_run_id(
        "11111111-1111-1111-1111-111111111111",
        formation_run_id="33333333-3333-3333-3333-333333333333",
        runtime_run_id="11111111-1111-1111-1111-111111111111",
        capture_run_id=None,
    )
    with pytest.raises(EpisodicEvidenceError, match="formation_receipt.run_id"):
        resolve_episodic_identity(scenario, (bad_record,))


def test_valid_uuid_chain_identity_resolves():
    """23：valid UUID chain（dataset run -> actual UUID -> receipt）-> identity RESOLVED。"""
    from app.core.evaluation.episodic_identity import IdentityResolutionStatus
    from app.services.evaluation.episodic_runner import resolve_episodic_identity

    scenario = scenario_by_case(DATASET, "E07")
    run_a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    run_b_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    record_a = _run_evidence_with_run_id(
        run_a_id,
        formation_run_id=run_a_id,
        runtime_run_id=run_a_id,
        capture_run_id=None,
    )
    record_b = _run_evidence_with_run_id(
        run_b_id,
        dataset_run_id="run_b",
        formation_run_id=run_b_id,
        runtime_run_id=run_b_id,
        capture_run_id=None,
    )
    identity_map = resolve_episodic_identity(scenario, (record_a, record_b))
    assert identity_map.status_for("run_a_episode") is IdentityResolutionStatus.RESOLVED
    assert identity_map.memory_id_for("run_a_episode") == "episode-x"
    assert identity_map.status_for("run_b_episode") is IdentityResolutionStatus.RESOLVED
