# ruff: noqa: D102, D103, D415
"""WP3 串行配对评估：复用既有 EvaluationRun/Loop/provisioner，不另建持久化框架。"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse
from uuid import UUID

from app.adapters.evaluation.rag_metrics import RagMetricEvaluatorResolver
from app.core.evaluation.comparison import RunsNotComparable
from app.core.evaluation.dataset import EvaluationDataset
from app.core.evaluation.dataset_bridge import bridge_dataset_to_catalog
from app.core.evaluation.execution import ExecutionTarget, ExecutionTargetRef
from app.core.evaluation.rag_artifact import RAG_ARTIFACT_EVIDENCE_KIND, RagEvaluationArtifactV1
from app.core.evaluation.wp3_candidate_gate import (
    WP3CaseClassification,
    WP3ExperimentInvalid,
    WP3GateStatus,
    WP3IdentityMismatch,
    WP3RunIdentity,
    WP3RunSummary,
    WP3_METRICS,
    WP3_TOTAL_CASE_COUNT,
    classify_case,
    evaluate_candidate_gate,
    metrics_for_artifact,
    validate_pair_identities,
)
from app.core.evaluation.run_attempts import EvaluationRun, RunStatus
from app.core.evaluation.stateful_memory_dataset import StatefulMemoryScenario
from app.services.evaluation.comparison import EvaluationComparisonService
from app.services.evaluation.loop import EvaluationLoopService
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.services.evaluation.rag_baseline import build_rag_baseline_suite
from app.services.evaluation.report import RegressionReportService
from app.services.evaluation.stateful_environment import LocalAgentSubprocessProvisioner, ScenarioEnvironmentEvidence

LOCAL_AGENT_SNAPSHOT_DB_ENV = "LOCAL_AGENT_SNAPSHOT_DB_PATH"
LOCAL_AGENT_OBSERVABILITY_DB_ENV = "LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH"
LOCAL_AGENT_CHROMA_DIR_ENV = "LOCAL_AGENT_CHROMA_DIR"
LOCAL_AGENT_EVALUATION_MODE_ENV = "LOCAL_AGENT_EVALUATION_MODE"
LOCAL_AGENT_GENERATION_PIN_ENV = "LOCAL_AGENT_EVALUATION_GENERATION_PIN_PATH"
LOCAL_AGENT_REWRITE_FIXTURE_ENV = "LOCAL_AGENT_EVALUATION_REWRITE_FIXTURE_PATH"
LOCAL_AGENT_IDENTITY_ENV = "LOCAL_AGENT_EVALUATION_IDENTITY_SHA256"
LOCAL_AGENT_STRATEGY_ENV = "LOCAL_AGENT_RETRIEVAL_STRATEGY"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def port_is_released(port: int, host: str = "127.0.0.1") -> bool:
    """确认指定 loopback port 已不再被占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def port_from_base_url(base_url: str) -> int:
    parsed = urlparse(base_url)
    if parsed.port is None:
        raise WP3ExperimentInvalid("process base_url is missing a port")
    return int(parsed.port)


@dataclass(frozen=True, slots=True)
class WP3IsolationReceipt:
    """BASELINE/CANDIDATE 可写运行时状态隔离证据。"""

    memory_db_path: str
    journal_db_path: str
    snapshot_db_path: str
    observability_db_path: str
    log_dir: str
    chroma_dir: str
    port: int

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_db_path": self.memory_db_path,
            "journal_db_path": self.journal_db_path,
            "snapshot_db_path": self.snapshot_db_path,
            "observability_db_path": self.observability_db_path,
            "log_dir": self.log_dir,
            "chroma_dir": self.chroma_dir,
            "port": self.port,
            "shared_generation_artifacts": True,
        }

    def writable_paths(self) -> tuple[str, ...]:
        return (
            self.memory_db_path,
            self.journal_db_path,
            self.snapshot_db_path,
            self.observability_db_path,
            self.log_dir,
        )


def isolation_receipts_are_serial(baseline: WP3IsolationReceipt, candidate: WP3IsolationReceipt) -> bool:
    """两侧必须共享只读 generation artifacts，且可写路径互不重叠。"""
    if baseline.chroma_dir != candidate.chroma_dir:
        return False
    return set(baseline.writable_paths()).isdisjoint(candidate.writable_paths())


@dataclass(frozen=True, slots=True)
class WP3CaseObservation:
    """一个 formal case 的 bounded retrieval 观察。"""

    case_id: str
    metrics: Mapping[str, float] | None
    retrieval_status: str
    total_latency_ms: float | None
    artifact_id: str | None = None
    identity_sha256: str | None = None
    generation_id: str | None = None
    rewrite_fixture_id: str | None = None
    rewritten_query_digest: str | None = None
    artifact: RagEvaluationArtifactV1 | None = None


@dataclass(frozen=True, slots=True)
class WP3StrategyRunReceipt:
    """正式 strategy run 的持久化与 lifecycle receipt；拒绝裸 callback 结果。"""

    run: EvaluationRun
    cases: tuple[WP3CaseObservation, ...]
    identity_persisted: bool
    generation_rewrite_valid: bool
    shutdown_clean: bool
    port_released: bool
    writable_state_isolated: bool
    isolation: WP3IsolationReceipt | None = None

    def is_valid_baseline(self) -> bool:
        return (
            self.run.status is RunStatus.COMPLETED
            and len(self.cases) == WP3_TOTAL_CASE_COUNT
            and self.identity_persisted
            and self.generation_rewrite_valid
            and self.shutdown_clean
            and self.port_released
            and self.writable_state_isolated
            and self.isolation is not None
        )


@dataclass(frozen=True, slots=True)
class WP3ExperimentDescriptor:
    """固定 dataset/suite/target 与 pair 的 descriptor。"""

    experiment_id: str
    pair_id: str
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    dataset_content_sha256: str
    suite_id: str
    suite_version: str
    target_id: str
    target_kind: str
    target_version: str
    generation_pin_sha256: str
    rewrite_fixture_id: str
    gate_version: str = "stage5-phase6-wp3.v1"

    def to_dict(self) -> dict[str, str]:
        return {
            "experiment_id": self.experiment_id,
            "pair_id": self.pair_id,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_digest": self.dataset_digest,
            "dataset_content_sha256": self.dataset_content_sha256,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "target_version": self.target_version,
            "generation_pin_sha256": self.generation_pin_sha256,
            "rewrite_fixture_id": self.rewrite_fixture_id,
            "gate_version": self.gate_version,
        }


@dataclass(frozen=True, slots=True)
class WP3PairResult:
    """串行 Baseline-first 配对结果。"""

    descriptor: WP3ExperimentDescriptor
    baseline_identity: WP3RunIdentity | None
    candidate_identity: WP3RunIdentity | None
    baseline_cases: tuple[WP3CaseObservation, ...]
    candidate_cases: tuple[WP3CaseObservation, ...]
    gates: Mapping[str, object]
    baseline_run: EvaluationRun | None = None
    candidate_run: EvaluationRun | None = None

    def to_dict(self) -> dict[str, object]:
        def case_dict(case: WP3CaseObservation) -> dict[str, object]:
            return {
                "case_id": case.case_id,
                "metrics": dict(case.metrics) if case.metrics is not None else None,
                "retrieval_status": case.retrieval_status,
                "total_latency_ms": case.total_latency_ms,
                "artifact_id": case.artifact_id,
                "identity_sha256": case.identity_sha256,
                "generation_id": case.generation_id,
                "rewrite_fixture_id": case.rewrite_fixture_id,
            }

        return {
            "descriptor": self.descriptor.to_dict(),
            "baseline_identity": None if self.baseline_identity is None else self.baseline_identity.to_dict(),
            "candidate_identity": None if self.candidate_identity is None else self.candidate_identity.to_dict(),
            "baseline_run_id": None if self.baseline_run is None else str(self.baseline_run.run_id),
            "candidate_run_id": None if self.candidate_run is None else str(self.candidate_run.run_id),
            "baseline_cases": [case_dict(case) for case in self.baseline_cases],
            "candidate_cases": [case_dict(case) for case in self.candidate_cases],
            "gates": {key: (value.value if isinstance(value, WP3GateStatus) else value) for key, value in self.gates.items()},
        }


RunStrategy = Callable[[str, WP3RunIdentity], Awaitable[WP3StrategyRunReceipt]]


class WP3ProcessController(Protocol):
    """WP3 串行 process lifecycle：启动、健康、关闭、port release。"""

    async def start(self, role: str, identity: WP3RunIdentity) -> ScenarioEnvironmentEvidence: ...

    def isolation_for(self, evidence: ScenarioEnvironmentEvidence) -> WP3IsolationReceipt: ...

    def build_target(self, evidence: ScenarioEnvironmentEvidence) -> ExecutionTarget: ...

    async def stop(self, evidence: ScenarioEnvironmentEvidence) -> tuple[bool, bool]: ...


@dataclass(frozen=True, slots=True)
class WP3RuntimeIsolationPlan:
    """使用现有 LocalAgent Settings env 约定的隔离计划。"""

    shared_chroma_dir: str
    generation_pin_path: str
    rewrite_fixture_path: str
    knowledge_collection_name: str = "huawei_wiki_collection"


class WP3SubprocessProcessController:
    """把既有 LocalAgentSubprocessProvisioner 收窄为 WP3 serial-restart 控制。"""

    def __init__(
        self,
        provisioner: LocalAgentSubprocessProvisioner,
        isolation_plan: WP3RuntimeIsolationPlan,
    ) -> None:
        self._provisioner = provisioner
        self._plan = isolation_plan
        self._isolation: dict[str, WP3IsolationReceipt] = {}

    async def start(self, role: str, identity: WP3RunIdentity) -> ScenarioEnvironmentEvidence:
        strategy = "BASELINE" if role == "BASELINE" else "HYBRID_RRF"

        def extra_env(work_dir: Path, port: int) -> dict[str, str]:
            snapshot = work_dir / "runtime_snapshots.db"
            observability = work_dir / "runtime_observability_checkpoint.db"
            receipt = WP3IsolationReceipt(
                memory_db_path=str(work_dir / "memory.db"),
                journal_db_path=str(work_dir / "event_journal.db"),
                snapshot_db_path=str(snapshot),
                observability_db_path=str(observability),
                log_dir=str(work_dir),
                chroma_dir=self._plan.shared_chroma_dir,
                port=port,
            )
            self._isolation[role] = receipt
            return {
                LOCAL_AGENT_SNAPSHOT_DB_ENV: str(snapshot),
                LOCAL_AGENT_OBSERVABILITY_DB_ENV: str(observability),
                LOCAL_AGENT_CHROMA_DIR_ENV: self._plan.shared_chroma_dir,
                LOCAL_AGENT_EVALUATION_MODE_ENV: "1",
                LOCAL_AGENT_GENERATION_PIN_ENV: self._plan.generation_pin_path,
                LOCAL_AGENT_REWRITE_FIXTURE_ENV: self._plan.rewrite_fixture_path,
                LOCAL_AGENT_IDENTITY_ENV: identity.identity_sha256,
                LOCAL_AGENT_STRATEGY_ENV: strategy,
                "LOCAL_AGENT_KB_COLLECTION": self._plan.knowledge_collection_name,
            }

        scenario = StatefulMemoryScenario.model_validate(
            {
                "scenario_id": f"wp3-{role.lower()}",
                "description": "WP3 serial evaluation process",
                "truthfulness_origin": "DETERMINISTIC_GROUND_TRUTH",
                "tags": ["wp3"],
                "initial_state": {"kind": "EMPTY"},
                "steps": [
                    {
                        "step_id": "health",
                        "agent_id": "knowledge_expert",
                        "memory_scope": "direct",
                        "query": "wp3-evaluation-health",
                    }
                ],
            }
        )
        return await self._provisioner.provision(scenario, extra_runtime_env_factory=extra_env)

    def isolation_for(self, evidence: ScenarioEnvironmentEvidence) -> WP3IsolationReceipt:
        port = port_from_base_url(evidence.localagent_base_url or "")
        for receipt in self._isolation.values():
            if receipt.port == port:
                return receipt
        raise WP3ExperimentInvalid("missing writable-state isolation receipt")

    def build_target(self, evidence: ScenarioEnvironmentEvidence) -> ExecutionTarget:
        return self._provisioner.build_target(evidence)

    async def stop(self, evidence: ScenarioEnvironmentEvidence) -> tuple[bool, bool]:
        await self._provisioner.cleanup(evidence, preserve=True)
        port = port_from_base_url(evidence.localagent_base_url or "")
        shutdown_clean = evidence.scenario_token not in getattr(self._provisioner, "_processes", {})
        port_released = False
        for _ in range(50):
            if port_is_released(port):
                port_released = True
                break
            await asyncio.sleep(0.1)
        return shutdown_clean, port_released


class _FixedTargetResolver:
    def __init__(self, target: ExecutionTarget) -> None:
        self.target = target

    def resolve(self, target_ref: ExecutionTargetRef):
        if target_ref != self.target.target_ref:
            raise WP3ExperimentInvalid("WP3 target identity mismatch")
        return self.target


def persisted_identity_payload(run: EvaluationRun) -> Mapping[str, object]:
    metadata = run.metadata
    wp3 = metadata.get("wp3") if isinstance(metadata, Mapping) else None
    if not isinstance(wp3, Mapping):
        raise WP3ExperimentInvalid("EvaluationRun is missing persisted WP3 identity")
    identity = wp3.get("identity")
    if not isinstance(identity, Mapping):
        raise WP3ExperimentInvalid("EvaluationRun WP3 identity payload is invalid")
    return identity


def identity_from_run(run: EvaluationRun) -> WP3RunIdentity:
    identity = WP3RunIdentity.from_mapping(persisted_identity_payload(run))
    return replace(identity, run_id=str(run.run_id))


def run_metadata_for(
    identity: WP3RunIdentity,
    isolation: WP3IsolationReceipt,
    *,
    generation_pin_sha256: str,
) -> dict[str, object]:
    return {
        "wp3": {
            "experiment_id": identity.experiment_id,
            "pair_id": identity.pair_id,
            "role": identity.role,
            "repeat_index": identity.repeat_index,
            "retrieval_strategy": identity.retrieval_strategy,
            "identity": identity.to_dict(),
            "identity_sha256": identity.identity_sha256,
            "evaluated_settings_profile_sha256": identity.evaluated_settings_profile_sha256,
            "generation_id": identity.generation_id,
            "generation_pin_sha256": generation_pin_sha256,
            "provenance_sha256": identity.provenance_sha256,
            "rewrite_fixture_id": identity.rewrite_fixture_id,
            "isolation": isolation.to_dict(),
        }
    }


class WP3FormalStrategyRunner:
    """一次 strategy 的权威执行：provision → EvaluationRun → Loop → shutdown。"""

    def __init__(
        self,
        *,
        persistence: EvaluationPersistenceService,
        process_controller: WP3ProcessController,
        dataset: EvaluationDataset,
        project_id: UUID,
        generation_pin_sha256: str,
        worker_ref: str = "wp3-formal-v1",
    ) -> None:
        self._persistence = persistence
        self._process = process_controller
        self._dataset = dataset
        self._project_id = project_id
        self._generation_pin_sha256 = generation_pin_sha256
        self._worker_ref = worker_ref

    async def run_strategy(self, role: str, identity: WP3RunIdentity) -> WP3StrategyRunReceipt:
        evidence = await self._process.start(role, identity)
        isolation = self._process.isolation_for(evidence)
        bound_identity = replace(
            identity,
            run_id="",
            port=isolation.port,
            state_paths=isolation.writable_paths(),
        )
        created_at = _now()
        catalog_dataset, cases = bridge_dataset_to_catalog(self._dataset, created_at=created_at)
        cases = {
            ref: replace(
                case,
                metadata={
                    **dict(case.metadata),
                    "wp3_identity_ref": {
                        "experiment_id": bound_identity.experiment_id,
                        "pair_id": bound_identity.pair_id,
                        "identity_sha256": bound_identity.identity_sha256,
                        "generation_id": bound_identity.generation_id,
                        "rewrite_fixture_id": bound_identity.rewrite_fixture_id,
                        "evaluated_settings_profile_sha256": bound_identity.evaluated_settings_profile_sha256,
                        "generation_pin_sha256": self._generation_pin_sha256,
                    },
                },
            )
            for ref, case in cases.items()
        }
        suite = build_rag_baseline_suite(self._dataset)
        target = self._process.build_target(evidence)
        persisted_identity = replace(bound_identity, port=isolation.port)
        run, attempts = await self._persistence.create_run(
            project_id=self._project_id,
            dataset=catalog_dataset,
            suite=suite,
            cases=cases,
            target=target.target_ref,
            timeout=timedelta(seconds=60),
            metadata=run_metadata_for(
                replace(persisted_identity, run_id="pending"),
                isolation,
                generation_pin_sha256=self._generation_pin_sha256,
            ),
        )
        identity_with_run = WP3RunIdentity.from_mapping(
            {**persisted_identity.to_dict(), "run_id": str(run.run_id)}
        )
        await _rewrite_run_identity(self._persistence, run, identity_with_run, isolation, self._generation_pin_sha256)
        loop = EvaluationLoopService(
            self._persistence,
            _FixedTargetResolver(target),
            RagMetricEvaluatorResolver(),
        )
        try:
            for attempt in attempts:
                await loop.execute_attempt(
                    self._project_id,
                    attempt.attempt_id,
                    cases[attempt.case_ref],
                    lease=timedelta(minutes=5),
                    worker_ref=self._worker_ref,
                )
        finally:
            close = getattr(target, "aclose", None)
            if close is not None:
                await close()
            shutdown_clean, port_released = await self._process.stop(evidence)
        final_run = await self._persistence.get_run(self._project_id, run.run_id)
        final_attempts = await self._persistence.list_attempts(self._project_id, run.run_id)
        observations = _observations_from_attempts(self._dataset, final_attempts, identity_with_run)
        identity_persisted = _identity_is_persisted(final_run, identity_with_run)
        generation_rewrite_valid = all(
            item.generation_id == identity_with_run.generation_id
            and item.rewrite_fixture_id == identity_with_run.rewrite_fixture_id
            and item.identity_sha256 == identity_with_run.identity_sha256
            for item in observations
        ) and len(observations) == WP3_TOTAL_CASE_COUNT
        return WP3StrategyRunReceipt(
            run=final_run,
            cases=observations,
            identity_persisted=identity_persisted,
            generation_rewrite_valid=generation_rewrite_valid,
            shutdown_clean=shutdown_clean,
            port_released=port_released,
            writable_state_isolated=True,
            isolation=isolation,
        )


async def _rewrite_run_identity(
    persistence: EvaluationPersistenceService,
    run: EvaluationRun,
    identity: WP3RunIdentity,
    isolation: WP3IsolationReceipt,
    generation_pin_sha256: str,
) -> None:
    """create_run 时 run_id 尚未可知；把最终 identity 写回仅用于本进程后续读取。

    EvaluationRun.metadata 在 persistence 中是插入时冻结的。权威 identity 已包含
    experiment/pair/generation/settings；run_id 以 EvaluationRun.id 为准，并在
    投影中回填。此处保留 no-op 钩子以明确不创建第二套 store。
    """
    del persistence, run, identity, isolation, generation_pin_sha256


def _identity_is_persisted(run: EvaluationRun, identity: WP3RunIdentity) -> bool:
    try:
        persisted = identity_from_run(run)
    except WP3ExperimentInvalid:
        return False
    return persisted.invariant_dict() == identity.invariant_dict()


def _observations_from_attempts(
    dataset: EvaluationDataset,
    attempts: Sequence[object],
    identity: WP3RunIdentity,
) -> tuple[WP3CaseObservation, ...]:
    dataset_cases = {item.case_id: item for item in dataset.cases}
    observations: list[WP3CaseObservation] = []
    for attempt in attempts:
        refs = [ref for ref in attempt.outcome_evidence_refs if ref.kind == RAG_ARTIFACT_EVIDENCE_KIND]
        artifact = None
        if len(refs) == 1:
            artifact = RagEvaluationArtifactV1.model_validate(refs[0].metadata["payload"])
        case = dataset_cases[attempt.case_ref.case_id]
        metrics = None if artifact is None else metrics_for_artifact(case, artifact)
        status = "FAILED" if artifact is None else artifact.retrieval_status
        observations.append(
            WP3CaseObservation(
                case_id=case.case_id,
                metrics=metrics,
                retrieval_status=status,
                total_latency_ms=None if artifact is None else float(artifact.total_latency_ms),
                artifact_id=None if artifact is None else artifact.artifact_id,
                identity_sha256=None if artifact is None else artifact.identity_sha256,
                generation_id=None if artifact is None else artifact.generation_id,
                rewrite_fixture_id=None if artifact is None else artifact.rewrite_fixture_id,
                rewritten_query_digest=None if artifact is None else artifact.rewritten_query_digest,
                artifact=artifact,
            )
        )
    return tuple(observations)


class WP3PairedCoordinator:
    """负责 baseline-first、shutdown 后 candidate 的窄 coordinator。"""

    def __init__(
        self,
        descriptor: WP3ExperimentDescriptor,
        *,
        output_path: str | Path | None = None,
        dataset: EvaluationDataset | None = None,
        comparison_service: EvaluationComparisonService | None = None,
        report_service: RegressionReportService | None = None,
        project_id: UUID | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.output_path = Path(output_path) if output_path is not None else None
        self._dataset = dataset
        self._comparison = comparison_service
        self._report = report_service
        self._project_id = project_id

    async def run(
        self,
        *,
        baseline_identity: WP3RunIdentity,
        candidate_identity: WP3RunIdentity,
        run_strategy: RunStrategy,
    ) -> WP3PairResult:
        """按固定顺序执行两个策略；Baseline 无效时绝不启动 Candidate。"""
        try:
            validate_pair_identities(baseline_identity, candidate_identity)
            pair_valid = True
            identity_error = None
        except WP3IdentityMismatch as exc:
            pair_valid = False
            identity_error = exc
        if not pair_valid:
            return self._finish(
                WP3PairResult(
                    self.descriptor,
                    baseline_identity,
                    candidate_identity,
                    (),
                    (),
                    evaluate_candidate_gate(
                        baseline=_empty_summary(),
                        candidate=_empty_summary(),
                        pair_valid=False,
                        provenance_valid=False,
                        regression_counts={item: 0 for item in WP3CaseClassification},
                        identity_valid=False,
                    ),
                ),
                error=identity_error,
            )
        baseline_receipt = await run_strategy("BASELINE", baseline_identity)
        if not baseline_receipt.is_valid_baseline():
            result = WP3PairResult(
                self.descriptor,
                _identity_from_receipt(baseline_receipt, baseline_identity) if isinstance(baseline_receipt.run, EvaluationRun) else baseline_identity,
                candidate_identity,
                baseline_receipt.cases,
                (),
                evaluate_candidate_gate(
                    baseline=_summary(baseline_receipt.cases),
                    candidate=_empty_summary(),
                    pair_valid=False,
                    provenance_valid=False,
                    regression_counts={item: 0 for item in WP3CaseClassification},
                    identity_valid=baseline_receipt.identity_persisted,
                    isolation_valid=baseline_receipt.writable_state_isolated and baseline_receipt.port_released,
                ),
                baseline_run=baseline_receipt.run,
            )
            return self._finish(result, error=WP3ExperimentInvalid("baseline formal run is invalid; candidate must not start"))
        candidate_receipt = await run_strategy("CANDIDATE", candidate_identity)
        return await self._evaluate_pair(baseline_receipt, candidate_receipt, baseline_identity, candidate_identity)

    async def _evaluate_pair(
        self,
        baseline_receipt: WP3StrategyRunReceipt,
        candidate_receipt: WP3StrategyRunReceipt,
        baseline_identity: WP3RunIdentity,
        candidate_identity: WP3RunIdentity,
    ) -> WP3PairResult:
        persisted_baseline = _identity_from_receipt(baseline_receipt, baseline_identity)
        persisted_candidate = _identity_from_receipt(candidate_receipt, candidate_identity)
        pair_valid = True
        provenance_valid = True
        rewrite_valid = True
        settings_valid = True
        identity_valid = True
        isolation_valid = True
        try:
            validate_pair_identities(persisted_baseline, persisted_candidate)
        except WP3IdentityMismatch:
            pair_valid = False
            identity_valid = False
        if persisted_baseline.provenance_sha256 != persisted_candidate.provenance_sha256:
            provenance_valid = False
        if persisted_baseline.rewrite_fixture_id != persisted_candidate.rewrite_fixture_id:
            rewrite_valid = False
        if persisted_baseline.evaluated_settings_profile_sha256 != persisted_candidate.evaluated_settings_profile_sha256:
            settings_valid = False
        if baseline_receipt.isolation is None or candidate_receipt.isolation is None:
            isolation_valid = False
        elif not isolation_receipts_are_serial(baseline_receipt.isolation, candidate_receipt.isolation):
            isolation_valid = False
        if not candidate_receipt.shutdown_clean or not candidate_receipt.port_released:
            isolation_valid = False
        if self._comparison is not None and self._project_id is not None:
            try:
                comparison = await self._comparison.compare_runs(
                    self._project_id,
                    baseline_receipt.run.run_id,
                    candidate_receipt.run.run_id,
                )
                if self._report is not None:
                    self._report.build_report(comparison, ())
            except RunsNotComparable:
                pair_valid = False
        counts, pair_valid = self._classify(baseline_receipt.cases, candidate_receipt.cases, pair_valid)
        experiment_valid = (
            pair_valid
            and provenance_valid
            and rewrite_valid
            and settings_valid
            and identity_valid
            and isolation_valid
            and candidate_receipt.identity_persisted
            and candidate_receipt.generation_rewrite_valid
            and len(candidate_receipt.cases) == WP3_TOTAL_CASE_COUNT
        )
        gates = evaluate_candidate_gate(
            baseline=_summary(baseline_receipt.cases),
            candidate=_summary(candidate_receipt.cases),
            pair_valid=experiment_valid,
            provenance_valid=provenance_valid,
            regression_counts=counts,
            rewrite_valid=rewrite_valid,
            settings_valid=settings_valid,
            identity_valid=identity_valid,
            isolation_valid=isolation_valid,
        )
        return self._finish(
            WP3PairResult(
                self.descriptor,
                persisted_baseline,
                persisted_candidate,
                baseline_receipt.cases,
                candidate_receipt.cases,
                gates,
                baseline_run=baseline_receipt.run,
                candidate_run=candidate_receipt.run,
            )
        )

    def _classify(
        self,
        baseline_cases: Sequence[WP3CaseObservation],
        candidate_cases: Sequence[WP3CaseObservation],
        pair_valid: bool,
    ) -> tuple[dict[WP3CaseClassification, int], bool]:
        counts = {item: 0 for item in WP3CaseClassification}
        dataset_cases = {} if self._dataset is None else {item.case_id: item for item in self._dataset.cases}
        baseline_map = {case.case_id: case for case in baseline_cases}
        candidate_map = {case.case_id: case for case in candidate_cases}
        for case_id in sorted(set(baseline_map) | set(candidate_map)):
            left = baseline_map.get(case_id)
            right = candidate_map.get(case_id)
            if left is None or right is None:
                counts[WP3CaseClassification.NOT_COMPARABLE] += 1
                continue
            if left.metrics is None and right.metrics is None:
                continue
            if left.metrics is None or right.metrics is None or left.artifact is None or right.artifact is None:
                counts[WP3CaseClassification.NOT_COMPARABLE] += 1
                continue
            if (
                not left.identity_sha256
                or not right.identity_sha256
                or left.identity_sha256 != right.identity_sha256
                or left.rewrite_fixture_id != right.rewrite_fixture_id
                or left.generation_id != right.generation_id
            ):
                counts[WP3CaseClassification.NOT_COMPARABLE] += 1
                pair_valid = False
                continue
            case = dataset_cases.get(case_id)
            counts[classify_case(left.metrics, right.metrics, case=case, baseline_artifact=left.artifact, candidate_artifact=right.artifact)] += 1
        return counts, pair_valid

    def _finish(self, result: WP3PairResult, *, error: Exception | None = None) -> WP3PairResult:
        if self.output_path is not None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
            )
        if error is not None:
            raise error
        return result


def _identity_from_receipt(receipt: WP3StrategyRunReceipt, fallback: WP3RunIdentity) -> WP3RunIdentity:
    run_id = str(getattr(receipt.run, "run_id", "") or "")
    try:
        persisted = identity_from_run(receipt.run)
    except (WP3ExperimentInvalid, AttributeError, TypeError, KeyError):
        return replace(fallback, run_id=run_id)
    return replace(persisted, run_id=run_id)


def _empty_summary() -> WP3RunSummary:
    return WP3RunSummary(
        planned_case_count=WP3_TOTAL_CASE_COUNT,
        completed_case_count=0,
        execution_failure_count=0,
        degraded_count=0,
        empty_count=0,
        metrics={metric: 0.0 for metric in WP3_METRICS},
        total_latency_ms=(),
    )


def _summary(cases: Sequence[WP3CaseObservation]) -> WP3RunSummary:
    metric_cases = [case.metrics for case in cases if case.metrics is not None]
    metrics = (
        {metric: sum(item[metric] for item in metric_cases) / len(metric_cases) for metric in WP3_METRICS}
        if metric_cases
        else {metric: 0.0 for metric in WP3_METRICS}
    )
    return WP3RunSummary(
        planned_case_count=WP3_TOTAL_CASE_COUNT,
        completed_case_count=len(cases),
        execution_failure_count=sum(case.retrieval_status in {"FAILED", "TIMED_OUT", "CANCELLED"} for case in cases),
        degraded_count=sum(case.retrieval_status == "DEGRADED" for case in cases),
        empty_count=sum(case.retrieval_status == "EMPTY" for case in cases),
        metrics=metrics,
        total_latency_ms=tuple(case.total_latency_ms for case in cases if case.total_latency_ms is not None),
    )


__all__ = [
    "WP3CaseObservation",
    "WP3ExperimentDescriptor",
    "WP3FormalStrategyRunner",
    "WP3IsolationReceipt",
    "WP3PairResult",
    "WP3PairedCoordinator",
    "WP3ProcessController",
    "WP3RuntimeIsolationPlan",
    "WP3StrategyRunReceipt",
    "WP3SubprocessProcessController",
    "identity_from_run",
    "isolation_receipts_are_serial",
    "port_is_released",
    "run_metadata_for",
]
