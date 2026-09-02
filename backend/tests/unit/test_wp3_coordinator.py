# ruff: noqa: D101, D102, D103, D105, D415
"""WP3 coordinator 接入真实 EvaluationRun/Attempt/Result 与隔离合同测试。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.adapters.evaluation.http_localagent import (
    LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
    LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
)
from app.core.evaluation.dataset import EvaluationDataset
from app.core.evaluation.execution import ExecutionOutcome, ExecutionTargetRef, OutcomeKind
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1, build_rag_artifact_evidence
from app.core.evaluation.references import ArtifactRef
from app.core.evaluation.results import EvaluationResult
from app.core.evaluation.run_attempts import (
    AttemptStatus,
    EvaluationRun,
    ExecutionAttempt,
    ResultAlreadyFinalized,
    RunStatus,
)
from app.core.evaluation.wp3_candidate_gate import WP3GateStatus, WP3RunIdentity
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.services.evaluation.wp3_coordinator import (
    WP3ExperimentDescriptor,
    WP3FormalStrategyRunner,
    WP3IsolationReceipt,
    WP3PairedCoordinator,
    WP3RuntimeIsolationPlan,
    WP3SubprocessProcessController,
    identity_from_run,
    isolation_receipts_are_serial,
    run_metadata_for,
)
from app.services.evaluation.stateful_environment import ScenarioEnvironmentEvidence
from tests.unit.test_rag_artifact import artifact_payload
from tests.unit.test_wp3_candidate_gate import _identity

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
PROJECT_ID = UUID("20000000-0000-4000-a000-000000000002")


class MemoryRuns:
    def __init__(self, store: "_Store") -> None:
        self._store = store

    async def add_run_with_attempts(self, run: EvaluationRun, attempts: tuple[ExecutionAttempt, ...]) -> None:
        # Postgres run projection 不保存 config_ref；Attempt 才保存。
        self._store.runs[run.run_id] = replace(
            run, execution_target_ref=replace(run.execution_target_ref, config_ref=None)
        )
        for attempt in attempts:
            self._store.attempts[attempt.attempt_id] = attempt

    async def get_run(self, project_id: UUID, run_id: UUID) -> EvaluationRun | None:
        run = self._store.runs.get(run_id)
        return run if run is not None and run.project_id == project_id else None

    async def lock_run(self, project_id: UUID, run_id: UUID) -> EvaluationRun | None:
        return await self.get_run(project_id, run_id)

    async def set_running_if_pending(self, project_id: UUID, run_id: UUID) -> bool:
        run = await self.get_run(project_id, run_id)
        if run is None or run.status is not RunStatus.PENDING:
            return False
        self._store.runs[run_id] = run.mark_running(NOW)
        return True

    async def finish_run(self, project_id: UUID, run_id: UUID, status: RunStatus, reason: str | None) -> bool:
        run = await self.get_run(project_id, run_id)
        if run is None or run.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
            return False
        self._store.runs[run_id] = run.finish(status, NOW, reason)
        return True


class MemoryAttempts:
    def __init__(self, store: "_Store") -> None:
        self._store = store

    async def get_attempt(self, project_id: UUID, attempt_id: UUID) -> ExecutionAttempt | None:
        attempt = self._store.attempts.get(attempt_id)
        return attempt if attempt is not None and attempt.project_id == project_id else None

    async def list_attempts(self, project_id: UUID, run_id: UUID) -> tuple[ExecutionAttempt, ...]:
        return tuple(
            item for item in self._store.attempts.values() if item.project_id == project_id and item.run_id == run_id
        )

    async def list_latest_attempts(self, project_id: UUID, run_id: UUID) -> tuple[ExecutionAttempt, ...]:
        grouped: dict[tuple[str, str], ExecutionAttempt] = {}
        for item in await self.list_attempts(project_id, run_id):
            key = (item.case_ref.case_id, item.case_ref.version)
            current = grouped.get(key)
            if current is None or item.attempt_no > current.attempt_no:
                grouped[key] = item
        return tuple(grouped.values())

    async def claim_attempt(self, project_id, attempt_id, token, lease, worker_ref, task_ref):
        attempt = await self.get_attempt(project_id, attempt_id)
        if attempt is None or attempt.status is not AttemptStatus.PENDING:
            return None
        claimed = replace(
            attempt,
            status=AttemptStatus.CLAIMED,
            claim_token=token,
            claimed_at=NOW,
            lease_expires_at=NOW + lease,
            worker_ref=worker_ref,
            task_ref=task_ref,
        )
        self._store.attempts[attempt_id] = claimed
        return claimed

    async def mark_running(self, project_id, attempt_id, token):
        attempt = await self.get_attempt(project_id, attempt_id)
        if attempt is None or attempt.status is not AttemptStatus.CLAIMED or attempt.claim_token != token:
            return None
        running = replace(attempt, status=AttemptStatus.RUNNING, started_at=NOW)
        self._store.attempts[attempt_id] = running
        return running

    async def record_outcome(self, project_id, attempt_id, token, outcome: ExecutionOutcome):
        attempt = await self.get_attempt(project_id, attempt_id)
        if attempt is None or attempt.status is not AttemptStatus.RUNNING or attempt.claim_token != token:
            return None
        terminal = replace(
            attempt,
            status=AttemptStatus.TERMINAL,
            execution_outcome_kind=outcome.kind,
            output_artifact_ref=outcome.output_artifact_ref,
            outcome_evidence_refs=outcome.evidence_refs,
            outcome_metadata=outcome.metadata,
            error_category=outcome.error_category,
            reason=outcome.reason,
            finished_at=NOW,
        )
        self._store.attempts[attempt_id] = terminal
        return terminal

    async def create_retry(self, attempt: ExecutionAttempt) -> None:
        self._store.attempts[attempt.attempt_id] = attempt

    async def list_stale_candidates(self, project_id, run_id=None):
        return ()

    async def reconcile_stale(self, project_id, attempt_id, reason):
        return None


class MemoryResults:
    def __init__(self, store: "_Store") -> None:
        self._store = store

    async def get_result(self, project_id: UUID, result_id: UUID) -> EvaluationResult | None:
        return self._store.results.get(str(result_id))

    async def list_results(self, project_id, run_id, attempt_id=None):
        items = [
            item
            for item in self._store.results.values()
            if item.run_id == str(run_id) and (attempt_id is None or item.attempt_id == str(attempt_id))
        ]
        return tuple(items)

    async def list_finalized_slots(self, project_id, run_id, attempt_id):
        return frozenset(
            (item.case_id, item.case_version, item.evaluator_id, item.evaluator_version)
            for item in await self.list_results(project_id, run_id, attempt_id)
        )

    async def insert_final_result(self, project_id, result: EvaluationResult, claim_token):
        slot = (result.case_id, result.case_version, result.evaluator_id, result.evaluator_version)
        existing = await self.list_finalized_slots(project_id, UUID(result.run_id), UUID(result.attempt_id))
        if slot in existing:
            raise ResultAlreadyFinalized("slot already finalized")
        self._store.results[result.result_id] = result


@dataclass
class _Store:
    runs: dict
    attempts: dict
    results: dict


class MemoryUow:
    def __init__(self, store: _Store) -> None:
        self.runs = MemoryRuns(store)
        self.attempts = MemoryAttempts(store)
        self.results = MemoryResults(store)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.rollback()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


def memory_persistence() -> EvaluationPersistenceService:
    store = _Store({}, {}, {})
    return EvaluationPersistenceService(lambda: MemoryUow(store))


def _dataset() -> EvaluationDataset:
    cases = []
    for index in range(20):
        cases.append(
            {
                "case_id": f"ret-{index}",
                "name": f"retrieval {index}",
                "input": {"agent_id": "knowledge_expert", "query": f"query-{index}"},
                "ground_truth": {
                    "retrieval": {"relevant_chunks": [{"document_id": "source-stable", "chunk_id": "c0"}]},
                    "ranking": {"graded_relevance": [{"document_id": "source-stable", "chunk_id": "c0", "relevance": 3}]},
                },
            }
        )
    for index in range(4):
        cases.append(
            {
                "case_id": f"na-{index}",
                "name": f"no-answer {index}",
                "input": {"agent_id": "knowledge_expert", "query": f"no-answer-{index}"},
                "ground_truth": {"generation": {"reference_answer": "unknown"}},
            }
        )
    return EvaluationDataset.model_validate(
        {
            "dataset_schema_version": "evaluation-dataset.v1",
            "dataset_id": "rag-evaluation-dataset",
            "name": "WP3 fixture",
            "version": "v1",
            "cases": cases,
        }
    )


class FakeTarget:
    def __init__(self, identity: WP3RunIdentity) -> None:
        self.target_ref = ExecutionTargetRef(
            LOCALAGENT_HTTP_TARGET_ID,
            LOCALAGENT_HTTP_TARGET_KIND,
            LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
            config_ref=LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
        )
        self._identity = identity

    async def execute(self, request):
        attempt_id = request.attempt_id
        payload = artifact_payload(
            artifact_id=f"rag-eval://{attempt_id}/r1",
            run_id=attempt_id,
            attempt_id=attempt_id,
            generation_id=self._identity.generation_id,
            identity_sha256=self._identity.identity_sha256,
            rewrite_fixture_id=self._identity.rewrite_fixture_id,
            query_digest="q" * 64,
            rewritten_query_digest="r" * 64,
        )
        artifact = RagEvaluationArtifactV1.model_validate(payload)
        return ExecutionOutcome(
            request_id=request.request_id,
            kind=OutcomeKind.SUCCESS,
            started_at=NOW,
            finished_at=NOW,
            output_artifact_ref=ArtifactRef(artifact.artifact_id, digest="a" * 64, media_type="application/json"),
            evidence_refs=(build_rag_artifact_evidence(artifact, "COMPLETE"),),
        )

    async def aclose(self) -> None:
        return None


class FakeProcessController:
    def __init__(self, tmp_path: Path, *, port_released: bool = True, fail_start_after_baseline: bool = False) -> None:
        self.tmp_path = tmp_path
        self.port_released = port_released
        self.fail_start_after_baseline = fail_start_after_baseline
        self.started: list[str] = []
        self.stopped: list[str] = []
        self._targets: dict[str, FakeTarget] = {}
        self._evidence: dict[str, ScenarioEnvironmentEvidence] = {}

    async def start(self, role: str, identity: WP3RunIdentity) -> ScenarioEnvironmentEvidence:
        if self.fail_start_after_baseline and role == "CANDIDATE":
            raise AssertionError("candidate must not start")
        self.started.append(role)
        work_dir = self.tmp_path / role.lower()
        work_dir.mkdir(parents=True, exist_ok=True)
        port = 18080 if role == "BASELINE" else 18081
        evidence = ScenarioEnvironmentEvidence(
            scenario_id=f"wp3-{role.lower()}",
            scenario_environment_id=f"env-{role.lower()}",
            scenario_token=f"tok-{role.lower()}",
            work_dir=work_dir,
            memory_db_path=work_dir / "memory.db",
            journal_db_path=work_dir / "event_journal.db",
            target_instance_ref=f"localagent-process-{port}",
            localagent_base_url=f"http://127.0.0.1:{port}",
            fixture_seeded=False,
            provisioned_at=NOW,
        )
        self._evidence[role] = evidence
        self._targets[role] = FakeTarget(identity)
        return evidence

    def isolation_for(self, evidence: ScenarioEnvironmentEvidence) -> WP3IsolationReceipt:
        port = 18080 if evidence.scenario_token.endswith("baseline") else 18081
        return WP3IsolationReceipt(
            memory_db_path=str(evidence.memory_db_path),
            journal_db_path=str(evidence.journal_db_path),
            snapshot_db_path=str(evidence.work_dir / "runtime_snapshots.db"),
            observability_db_path=str(evidence.work_dir / "observability.db"),
            log_dir=str(evidence.work_dir),
            chroma_dir=str(self.tmp_path / "shared-chroma"),
            port=port,
        )

    def build_target(self, evidence: ScenarioEnvironmentEvidence):
        role = "BASELINE" if evidence.scenario_token.endswith("baseline") else "CANDIDATE"
        return self._targets[role]

    async def stop(self, evidence: ScenarioEnvironmentEvidence) -> tuple[bool, bool]:
        role = "BASELINE" if evidence.scenario_token.endswith("baseline") else "CANDIDATE"
        self.stopped.append(role)
        return True, self.port_released


def _descriptor() -> WP3ExperimentDescriptor:
    return WP3ExperimentDescriptor(
        "exp-1", "pair-1", "rag-evaluation-dataset", "v1", "d" * 64, "d" * 64,
        "rag-baseline-suite", "v1", LOCALAGENT_HTTP_TARGET_ID, LOCALAGENT_HTTP_TARGET_KIND, "evaluation-v2",
        "p" * 64, "f" * 64,
    )


@pytest.mark.asyncio
async def test_formal_runner_persists_run_attempts_results_and_identity(tmp_path: Path) -> None:
    persistence = memory_persistence()
    controller = FakeProcessController(tmp_path)
    runner = WP3FormalStrategyRunner(
        persistence=persistence,
        process_controller=controller,
        dataset=_dataset(),
        project_id=PROJECT_ID,
        generation_pin_sha256="g" * 64,
    )
    identity = _identity("BASELINE")
    receipt = await runner.run_strategy("BASELINE", identity)
    assert receipt.run.status is RunStatus.COMPLETED
    assert receipt.identity_persisted
    assert len(receipt.cases) == 24
    attempts = await persistence.list_attempts(PROJECT_ID, receipt.run.run_id)
    results = await persistence.list_results(PROJECT_ID, receipt.run.run_id)
    assert len(attempts) == 24
    assert len(results) == 24 * 6
    persisted = identity_from_run(receipt.run)
    assert persisted.evaluated_settings_profile_sha256 == identity.evaluated_settings_profile_sha256
    assert persisted.invariant_dict() == identity.invariant_dict()
    case_meta = results[0].metadata["case"]["wp3_identity_ref"]
    assert case_meta["experiment_id"] == "exp-1"
    assert case_meta["identity_sha256"] == identity.identity_sha256
    assert controller.started == ["BASELINE"]
    assert controller.stopped == ["BASELINE"]


@pytest.mark.asyncio
async def test_coordinator_runs_real_pair_and_keeps_writable_state_isolated(tmp_path: Path) -> None:
    persistence = memory_persistence()
    controller = FakeProcessController(tmp_path)
    runner = WP3FormalStrategyRunner(
        persistence=persistence,
        process_controller=controller,
        dataset=_dataset(),
        project_id=PROJECT_ID,
        generation_pin_sha256="g" * 64,
    )
    result = await WP3PairedCoordinator(_descriptor(), dataset=_dataset()).run(
        baseline_identity=_identity("BASELINE"),
        candidate_identity=_identity("CANDIDATE", "HYBRID_RRF"),
        run_strategy=runner.run_strategy,
    )
    assert controller.started == ["BASELINE", "CANDIDATE"]
    assert result.baseline_run is not None and result.candidate_run is not None
    assert result.baseline_run.run_id != result.candidate_run.run_id
    assert isolation_receipts_are_serial(
        controller.isolation_for(controller._evidence["BASELINE"]),
        controller.isolation_for(controller._evidence["CANDIDATE"]),
    )
    assert result.gates["HYBRID_CANDIDATE_GATE"] in {WP3GateStatus.PASS, WP3GateStatus.FAIL}


class IdentitylessTarget(FakeTarget):
    async def execute(self, request):
        attempt_id = request.attempt_id
        artifact = RagEvaluationArtifactV1.model_validate(
            artifact_payload(artifact_id=f"rag-eval://{attempt_id}/r1", run_id=attempt_id, attempt_id=attempt_id)
        )
        return ExecutionOutcome(
            request_id=request.request_id,
            kind=OutcomeKind.SUCCESS,
            started_at=NOW,
            finished_at=NOW,
            output_artifact_ref=ArtifactRef(artifact.artifact_id, digest="a" * 64, media_type="application/json"),
            evidence_refs=(build_rag_artifact_evidence(artifact, "COMPLETE"),),
        )


@pytest.mark.asyncio
async def test_missing_artifact_identity_does_not_validate_baseline(tmp_path: Path) -> None:
    persistence = memory_persistence()
    controller = FakeProcessController(tmp_path, fail_start_after_baseline=True)
    original_start = controller.start

    async def start_with_identityless(role: str, identity: WP3RunIdentity):
        evidence = await original_start(role, identity)
        controller._targets[role] = IdentitylessTarget(identity)
        return evidence

    controller.start = start_with_identityless  # type: ignore[method-assign]
    runner = WP3FormalStrategyRunner(
        persistence=persistence,
        process_controller=controller,
        dataset=_dataset(),
        project_id=PROJECT_ID,
        generation_pin_sha256="g" * 64,
    )
    with pytest.raises(Exception, match="candidate must not start"):
        await WP3PairedCoordinator(_descriptor()).run(
            baseline_identity=_identity("BASELINE"),
            candidate_identity=_identity("CANDIDATE", "HYBRID_RRF"),
            run_strategy=runner.run_strategy,
        )
    assert controller.started == ["BASELINE"]


@pytest.mark.asyncio
async def test_failed_port_release_does_not_start_candidate(tmp_path: Path) -> None:
    persistence = memory_persistence()
    controller = FakeProcessController(tmp_path, port_released=False, fail_start_after_baseline=True)
    runner = WP3FormalStrategyRunner(
        persistence=persistence,
        process_controller=controller,
        dataset=_dataset(),
        project_id=PROJECT_ID,
        generation_pin_sha256="g" * 64,
    )
    with pytest.raises(Exception, match="candidate must not start"):
        await WP3PairedCoordinator(_descriptor()).run(
            baseline_identity=_identity("BASELINE"),
            candidate_identity=_identity("CANDIDATE", "HYBRID_RRF"),
            run_strategy=runner.run_strategy,
        )
    assert controller.started == ["BASELINE"]


def test_settings_profile_allowed_strategy_difference_and_isolation_contract(tmp_path: Path) -> None:
    baseline = _identity("BASELINE")
    candidate = _identity("CANDIDATE", "HYBRID_RRF")
    assert baseline.evaluated_settings_profile_sha256 == candidate.evaluated_settings_profile_sha256
    left = WP3IsolationReceipt("m1", "j1", "s1", "o1", "l1", "chroma", 1)
    right = WP3IsolationReceipt("m2", "j2", "s2", "o2", "l2", "chroma", 2)
    assert isolation_receipts_are_serial(left, right)
    overlapping = WP3IsolationReceipt("m1", "j2", "s2", "o2", "l2", "chroma", 2)
    assert not isolation_receipts_are_serial(left, overlapping)


def test_run_metadata_round_trip_includes_settings_profile() -> None:
    identity = _identity("BASELINE")
    isolation = WP3IsolationReceipt("m", "j", "s", "o", "l", "chroma", 9)
    payload = run_metadata_for(identity, isolation, generation_pin_sha256="g" * 64)
    assert payload["wp3"]["evaluated_settings_profile_sha256"] == identity.evaluated_settings_profile_sha256
    assert payload["wp3"]["identity"]["dataset_digest"] == "d" * 64


@pytest.mark.asyncio
async def test_subprocess_controller_env_uses_existing_settings_keys(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class StubProvisioner:
        def __init__(self) -> None:
            self._processes = {}

        async def provision(self, scenario, extra_runtime_env_factory=None):
            env = extra_runtime_env_factory(tmp_path / "work", 19001)
            captured["env"] = env
            captured["scenario"] = scenario.scenario_id
            work = tmp_path / "work"
            work.mkdir(parents=True, exist_ok=True)
            return ScenarioEnvironmentEvidence(
                scenario_id=scenario.scenario_id,
                scenario_environment_id="env",
                scenario_token="tok",
                work_dir=work,
                memory_db_path=work / "memory.db",
                journal_db_path=work / "event_journal.db",
                target_instance_ref="localagent-process-19001",
                localagent_base_url="http://127.0.0.1:19001",
                fixture_seeded=False,
                provisioned_at=NOW,
            )

        def build_target(self, evidence):
            return FakeTarget(_identity("BASELINE"))

        async def cleanup(self, evidence, preserve):
            return None

    controller = WP3SubprocessProcessController(
        StubProvisioner(),
        WP3RuntimeIsolationPlan(str(tmp_path / "chroma"), str(tmp_path / "pin.json"), str(tmp_path / "fix.json")),
    )
    evidence = await controller.start("CANDIDATE", _identity("CANDIDATE", "HYBRID_RRF"))
    env = captured["env"]
    assert env["LOCAL_AGENT_EVALUATION_MODE"] == "1"
    assert env["LOCAL_AGENT_RETRIEVAL_STRATEGY"] == "HYBRID_RRF"
    assert env["LOCAL_AGENT_CHROMA_DIR"] == str(tmp_path / "chroma")
    assert "LOCAL_AGENT_SNAPSHOT_DB_PATH" in env
    assert evidence.localagent_base_url.endswith(":19001")
