"""WP6-E global-sequential Episodic Scenario Runner（复用 WP5 subprocess provisioner）。

执行模型：

- ``SCENARIO_EXECUTION_POLICY = GLOBAL_SEQUENTIAL``：E01 -> E02 -> ... -> E12，禁止并行。
- Scenario 内 Runs 严格按 Dataset 顺序 await；Run N 的证据收集完成后才开始 Run N+1。
- 每个 Scenario：fresh LocalAgent subprocess / port / Memory DB / Journal DB /
  environment token / work directory；Scenario 内 Runs 复用同一 Memory/Journal DB 与
  同一 target subprocess，但每 Run 使用新的 runtime run identity；Run B 不得重新
  provision Memory DB。
- Control expansion：Dataset symbolic declaration -> LocalAgent v3 真实 request。
  - ``DETERMINISTIC_FAILED_RUN``（E02/E10 Run A）、``DETERMINISTIC_EPISODIC_SUCCESS_RUN``
    （E08 Run A）、``REPLAY_EPISODIC_FORMATION_OBSERVER``（E04）、
    ``INSTALL_EPISODIC_FIXTURE``（E09）、``CAPTURE_EPISODIC_PIPELINE``。
  - ``replay_run_id`` 必须从 symbolic run_id 映射到 actual runtime UUID，不能发送
    ``"run_a"`` 给 Target。
- Evidence：capture / formation / fixture / replay / runtime receipts 严格 parse；
  journal count-level 证据（可选）；final SQLite Episode projection 只做 persistence。
- FAIL/BLOCKED scenario 保留 artifacts 后才 cleanup；cleanup 后 artifact 仍可读取。

本 runner 的 run artifacts 直接持久化在 scenario aggregate artifact（typed
``EpisodicRunAttemptRecord``），不依赖 Postgres persistence；与 WP5 的
``EvaluationPersistenceService`` 解耦（artifact 即 append-only 持久化）。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.adapters.evaluation.episodic_http_target import (
    EpisodicHttpEvaluationV3Target,
    EpisodicV3TargetError,
    validate_run_uuid,
)
from app.core.evaluation.episodic_artifact import (
    ExperimentBlockReason,
    ExperimentExecutionStatus,
    EpisodicExperimentArtifact,
    EpisodicRunAttemptRecord,
    EpisodicScenarioArtifact,
)
from app.core.evaluation.episodic_assertion import (
    EpisodicAssertion,
    EpisodicAssertionGroup,
    EpisodicBlockReason,
    EpisodicFailureTaxonomy,
)
from app.core.evaluation.episodic_baseline import (
    EPISODIC_BASELINE_ID,
    EPISODIC_BASELINE_V2_ID,
    EpisodicBaselineCandidate,
    EpisodicBaselineProvenance,
    EpisodicBaselineStatus,
)
from app.core.evaluation.episodic_dataset import (
    EPISODIC_DATASET_ID,
    EPISODIC_DATASET_VERSION,
    EpisodicDataset,
    EpisodicEvaluationControl,
    EpisodicInitialFixture,
    EpisodicRun,
    EpisodicScenario,
)
from app.core.evaluation.episodic_evaluators import (
    EpisodicScenarioEvaluation,
    evaluate_episodic_scenario,
)
from app.core.evaluation.episodic_evidence import (
    EpisodicRunEvidence,
    EpisodicScenarioEvaluationEvidence,
    RunExecutionStatus,
    EpisodicEvidenceError,
    validate_runtime_uuid_binding,
)
from app.core.evaluation.episodic_gate import (
    EpisodicLayer1GateResult,
    evaluate_episodic_layer1_gate,
)
from app.core.evaluation.episodic_identity import (
    EpisodicIdentityMap,
    EpisodicIdentityResolver,
)
from app.core.evaluation.episodic_impl_ref import episodic_evaluation_implementation_ref
from app.core.evaluation.episodic_metrics import (
    build_episodic_experiment_metrics,
    build_episodic_scenario_success_aggregate,
)
from app.core.evaluation.episodic_projection import (
    EpisodicProjectionError,
    EpisodicProjectionRecord,
    read_episodic_projection,
)
from app.core.evaluation.execution import ExecutionRequest, ExecutionTargetRef
from app.core.evaluation.references import CaseVersionRef
from app.core.evaluation.stateful_assertion import AssertionStatus, EvaluationLayer
from app.core.evaluation.stateful_journal import JournalEvidenceError, read_journal_step_facts
from app.services.evaluation.episodic_environment import (
    EPISODIC_FROZEN_TARGET_REF,
    EpisodicEnvironmentProvisioner,
    EpisodicTargetCertification,
    certify_episodic_target,
    compute_target_evaluation_implementation_ref,
)
from app.services.evaluation.stateful_environment import (
    ScenarioEnvironmentEvidence,
    StatefulEnvironmentError,
)

SCENARIO_EXECUTION_POLICY: str = "GLOBAL_SEQUENTIAL"


def _now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Control / fixture expansion（Dataset symbolic -> Target v3 wire）
# ---------------------------------------------------------------------------


def build_episodic_fixture_wire(fixture: EpisodicInitialFixture) -> dict[str, object]:
    """Dataset typed fixture -> LocalAgent v3 ``EpisodicFixtureSpec`` wire。

    禁止 canonical_text / raw SQL / arbitrary payload；canonical text 由 target
    renderer 生成。
    """
    return {
        "fixture_ref": fixture.fixture_ref,
        "agent_id": fixture.agent_id,
        "memory_scope": fixture.memory_scope,
        "origin_run_id": fixture.origin_run_id,
        "situation": fixture.situation,
        "goal": fixture.goal,
        "observations": [item.model_dump(exclude_none=True) for item in fixture.observations],
        "result": fixture.result.model_dump(),
        "lesson": fixture.lesson,
    }


def build_episodic_evaluation_control(
    run: EpisodicRun,
    scenario: EpisodicScenario,
    *,
    actual_runtime_run_id: str,
) -> dict[str, object]:
    """Dataset run control -> LocalAgent v3 ``evaluation_control`` wire。

    ``replay_run_id`` 映射为 actual runtime UUID（绝不发送 ``"run_a"``）；fixture
    展开为完整 typed fixture wire。
    """
    declaration = run.evaluation_control
    if declaration is None:
        return {}
    control: dict[str, object] = {
        "schema_version": "episodic-evaluation-control.v1",
        "capabilities": [capability.value for capability in declaration.capabilities],
    }
    if EpisodicEvaluationControl.INSTALL_EPISODIC_FIXTURE in declaration.capability_set:
        if scenario.initial_fixture is None:
            raise EpisodicV3TargetError("INSTALL_EPISODIC_FIXTURE requires scenario initial_fixture")
        control["fixture"] = build_episodic_fixture_wire(scenario.initial_fixture)
    if EpisodicEvaluationControl.REPLAY_EPISODIC_FORMATION_OBSERVER in declaration.capability_set:
        control["replay_run_id"] = validate_run_uuid(actual_runtime_run_id)
    return control


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodicScenarioRunPlan:
    """一次 scenario run 的 deterministic plan（dataset 权威 + 执行绑定）。"""

    dataset: EpisodicDataset
    scenario: EpisodicScenario
    target_ref: ExecutionTargetRef
    timeout: timedelta
    created_at: datetime

    @property
    def dataset_id(self) -> str:
        """Return the frozen dataset id."""
        return self.dataset.dataset_id

    @property
    def dataset_version(self) -> str:
        """Return the frozen dataset version."""
        return self.dataset.version

    @property
    def dataset_digest(self) -> str | None:
        """Return the frozen dataset raw digest."""
        return self.dataset.content_digest


@dataclass(frozen=True, slots=True)
class EpisodicScenarioExecutionReceipt:
    """一次 scenario 执行的完整凭据（evidence + evaluation + artifact）。"""

    plan: EpisodicScenarioRunPlan
    environment: ScenarioEnvironmentEvidence
    run_records: tuple[EpisodicRunEvidence, ...]
    final_projection: tuple[EpisodicProjectionRecord, ...]
    identity_map: EpisodicIdentityMap
    evaluation: EpisodicScenarioEvaluation
    artifact: EpisodicScenarioArtifact


@dataclass(frozen=True, slots=True)
class EpisodicExperimentExecutionReceipt:
    """12-scenario experiment 的完整凭据。"""

    dataset: EpisodicDataset
    scenario_receipts: tuple[EpisodicScenarioExecutionReceipt, ...]
    gate: EpisodicLayer1GateResult
    aggregate_artifact: EpisodicExperimentArtifact
    baseline_candidate: EpisodicBaselineCandidate
    target_certification: EpisodicTargetCertification | None


# ---------------------------------------------------------------------------
# request / evidence builders
# ---------------------------------------------------------------------------


def build_episodic_execution_request(
    plan: EpisodicScenarioRunPlan,
    run: EpisodicRun,
    run_id: str,
) -> ExecutionRequest:
    """构造一次 Run 的 ExecutionRequest（run_id 是 canonical UUID）。"""
    canonical_run_id = validate_run_uuid(run_id)
    case_ref = CaseVersionRef(f"{plan.scenario.scenario_id}.{run.run_id}", plan.dataset.version)
    return ExecutionRequest(
        request_id=str(uuid4()),
        run_id=canonical_run_id,
        attempt_id=canonical_run_id,
        case_ref=case_ref,
        input_payload={"agent_id": run.agent_id, "query": run.user_request},
        timeout=plan.timeout,
        idempotency_key=canonical_run_id,
    )


def build_run_evidence(
    plan: EpisodicScenarioRunPlan,
    run: EpisodicRun,
    actual_runtime_run_id: str,
    *,
    response: object,
) -> EpisodicRunEvidence:
    """把 v3 typed response 投影为 ``EpisodicRunEvidence``（receipt/capture/runtime）。"""
    from app.adapters.evaluation.episodic_http_target import EpisodicV3Response

    if not isinstance(response, EpisodicV3Response):
        raise TypeError("response must be EpisodicV3Response")
    controls_sent = (
        tuple(capability.value for capability in run.evaluation_control.capabilities)
        if run.evaluation_control is not None
        else ()
    )
    return EpisodicRunEvidence(
        scenario_id=plan.scenario.scenario_id,
        case_code=plan.scenario.case_code,
        dataset_run_id=run.run_id,
        actual_runtime_run_id=actual_runtime_run_id,
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status=response.status,
        delivery_status=(response.runtime_receipt.delivery_status if response.runtime_receipt is not None else None),
        evaluation_controls_sent=controls_sent,
        target_run_id=response.run_id,
        target_status=response.status,
        target_stop_reason=response.stop_reason,
        target_error_code=response.error_code,
        target_safe_message=response.safe_message,
        evaluation_control_status=response.evaluation_control_status,
        evaluation_error_code=response.evaluation_error_code,
        capture_status=response.capture_status,
        capture_error_code=response.capture_error_code,
        formation_receipt=(response.formation_receipts[0] if response.formation_receipts else None),
        fixture_receipt=(response.fixture_receipts[0] if response.fixture_receipts else None),
        replay_receipt=(response.replay_receipts[0] if response.replay_receipts else None),
        capture=response.episodic_capture,
        runtime_receipt=response.runtime_receipt,
    )


def infra_failure_run_evidence(
    plan: EpisodicScenarioRunPlan,
    run: EpisodicRun,
    actual_runtime_run_id: str,
    error: object,
    *,
    infra_status: str,
) -> EpisodicRunEvidence:
    """evaluation-infra 失败的 run evidence（EVALUATION_INFRA，不污染 runtime 行为）。"""
    return EpisodicRunEvidence(
        scenario_id=plan.scenario.scenario_id,
        case_code=plan.scenario.case_code,
        dataset_run_id=run.run_id,
        actual_runtime_run_id=actual_runtime_run_id,
        execution_status=RunExecutionStatus.INFRA_FAILURE,
        terminal_status=None,
        delivery_status=None,
        evaluation_controls_sent=(
            tuple(capability.value for capability in run.evaluation_control.capabilities)
            if run.evaluation_control is not None
            else ()
        ),
        infra_status=infra_status,
    )


def resolve_episodic_identity(
    scenario: EpisodicScenario,
    run_records: tuple[EpisodicRunEvidence, ...],
) -> EpisodicIdentityMap:
    """从 receipts 构建 symbolic identity map（严格 receipt mapping，无 content 推断）。"""
    formation_by_run: dict[str, object] = {}
    fixture_by_ref: dict[str, object] = {}
    for record in run_records:
        validate_runtime_uuid_binding(record)
        if record.formation_receipt is not None:
            formation_by_run[record.dataset_run_id] = record.formation_receipt
        if record.fixture_receipt is not None:
            fixture_by_ref[record.fixture_receipt.fixture_ref] = record.fixture_receipt
    return EpisodicIdentityResolver().resolve(
        scenario,
        formation_receipt_by_run_id=formation_by_run,
        fixture_receipt_by_ref=fixture_by_ref,
    )


# ---------------------------------------------------------------------------
# scenario artifact builder
# ---------------------------------------------------------------------------


def _run_attempt_record(
    record: EpisodicRunEvidence,
    environment_id: str,
    *,
    capture_ref: str | None,
    journal_ref: str | None,
    projection_ref: str | None,
) -> EpisodicRunAttemptRecord:
    return EpisodicRunAttemptRecord(
        scenario_id=record.scenario_id,
        case_code=record.case_code,
        dataset_run_id=record.dataset_run_id,
        actual_runtime_run_id=record.actual_runtime_run_id,
        execution_status=record.execution_status.value,
        terminal_status=record.terminal_status,
        delivery_status=record.delivery_status,
        evaluation_controls_sent=list(record.evaluation_controls_sent),
        target_status=record.target_status,
        target_stop_reason=record.target_stop_reason,
        target_error_code=record.target_error_code,
        evaluation_control_status=record.evaluation_control_status,
        evaluation_error_code=record.evaluation_error_code,
        capture_status=record.capture_status,
        capture_error_code=record.capture_error_code,
        formation_receipt_summary=(
            {
                "run_id": record.formation_receipt.run_id,
                "outcome": record.formation_receipt.outcome,
                "memory_id": record.formation_receipt.memory_id,
                "safe_reason": record.formation_receipt.safe_reason,
            }
            if record.formation_receipt is not None
            else None
        ),
        fixture_receipt_summary=(
            {
                "fixture_ref": record.fixture_receipt.fixture_ref,
                "memory_id": record.fixture_receipt.memory_id,
                "origin_kind": record.fixture_receipt.origin_kind,
            }
            if record.fixture_receipt is not None
            else None
        ),
        replay_receipt_summary=(
            {
                "run_id": record.replay_receipt.run_id,
                "outcome": record.replay_receipt.outcome,
                "memory_id": record.replay_receipt.memory_id,
            }
            if record.replay_receipt is not None
            else None
        ),
        capture_artifact_reference=capture_ref,
        journal_artifact_reference=journal_ref,
        sqlite_projection_reference=projection_ref,
        evaluation_infra_status=record.infra_status,
        journal_step_facts=(
            {
                "step_ids": list(record.step_facts.step_ids()),
                "facts": [
                    {
                        "event_id": fact.event_id,
                        "event_type": fact.event_type,
                        "step_id": fact.step_id,
                        "status": fact.status,
                    }
                    for fact in record.step_facts.facts
                ],
            }
            if record.step_facts is not None
            else None
        ),
        journal_error=record.journal_error,
    )


def build_episodic_scenario_artifact(
    plan: EpisodicScenarioRunPlan,
    environment: ScenarioEnvironmentEvidence,
    run_records: tuple[EpisodicRunEvidence, ...],
    final_projection: tuple[EpisodicProjectionRecord, ...],
    identity_map: EpisodicIdentityMap,
    evaluation: EpisodicScenarioEvaluation,
) -> EpisodicScenarioArtifact:
    """把 scenario execution + evaluation 聚合为 append-only scenario aggregate。"""
    environment_id = environment.scenario_environment_id
    return EpisodicScenarioArtifact(
        evaluation_run_id=f"{environment.scenario_token}",
        scenario_id=plan.scenario.scenario_id,
        case_code=plan.scenario.case_code,
        truthfulness_origin=plan.scenario.truthfulness_origin.value,
        episode_origin_kind=plan.scenario.episode_origin_kind.value,
        scenario_outcome=evaluation.scenario_outcome.value,
        scenario_outcome_assertion=evaluation.scenario_outcome_assertion.to_metadata(),
        assertion_results=[assertion.to_metadata() for assertion in evaluation.assertions],
        metric_aggregates={name: metric.as_dict() for name, metric in evaluation.metrics.items()},
        failure_taxonomies=list(evaluation.failure_taxonomies),
        run_attempts=[
            _run_attempt_record(
                record,
                environment_id,
                capture_ref=(
                    f"scenario://{environment_id}/{record.dataset_run_id}/capture"
                    if record.capture is not None
                    else None
                ),
                journal_ref=(
                    f"scenario://{environment_id}/{record.dataset_run_id}/journal"
                    if record.journal is not None
                    else None
                ),
                projection_ref=f"scenario://{environment_id}/final-sqlite",
            )
            for record in run_records
        ],
        identity_resolutions=[
            {
                "episode_ref": resolution.episode_ref,
                "origin_kind": resolution.origin_kind.value,
                "status": resolution.status.value,
                "memory_id": resolution.memory_id,
                "source": resolution.source,
            }
            for resolution in identity_map.resolutions
        ],
        episode_projection_summary=[record.to_projection_dict(include_content=False) for record in final_projection],
        evaluation_implementation_ref=episodic_evaluation_implementation_ref(),
        private_evaluation_artifact=True,
        metadata={
            "scenario_environment_id": environment_id,
            "target_instance_ref": environment.target_instance_ref,
            "localagent_python_executable_ref": environment.localagent_python_executable_ref,
        },
    )


def _infra_scenario_artifact(
    plan: EpisodicScenarioRunPlan,
    environment: ScenarioEnvironmentEvidence | None,
    error: object,
) -> tuple[EpisodicScenarioEvaluation, EpisodicScenarioArtifact]:
    from app.core.evaluation.episodic_metrics import build_episodic_scenario_metrics

    assertion = EpisodicAssertion(
        assertion_id=f"{plan.scenario.scenario_id}.infra",
        group=EpisodicAssertionGroup.INVARIANT,
        status=AssertionStatus.BLOCKED,
        blocked_by=(
            EpisodicBlockReason.EVIDENCE_CAPTURE
            if isinstance(error, EpisodicEvidenceError)
            else EpisodicBlockReason.EVALUATION_INFRA
        ),
        evidence_source="provisioner",
        reason=str(error),
    )
    evaluation = EpisodicScenarioEvaluation(
        scenario_id=plan.scenario.scenario_id,
        assertions=(assertion,),
        metrics={},
        scenario_outcome=AssertionStatus.BLOCKED,
        scenario_outcome_assertion=assertion,
        failure_taxonomies=(),
    )
    artifact = EpisodicScenarioArtifact(
        evaluation_run_id=str(uuid4()),
        scenario_id=plan.scenario.scenario_id,
        case_code=plan.scenario.case_code,
        truthfulness_origin=plan.scenario.truthfulness_origin.value,
        episode_origin_kind=plan.scenario.episode_origin_kind.value,
        scenario_outcome=AssertionStatus.BLOCKED.value,
        scenario_outcome_assertion=assertion.to_metadata(),
        assertion_results=[assertion.to_metadata()],
        metric_aggregates={},
        failure_taxonomies=[],
        run_attempts=[],
        identity_resolutions=[],
        episode_projection_summary=[],
        evaluation_implementation_ref=episodic_evaluation_implementation_ref(),
        private_evaluation_artifact=True,
        metadata={
            "scenario_environment_id": (environment.scenario_environment_id if environment is not None else None),
            "infra_failure": str(error),
        },
    )
    return evaluation, artifact


# ---------------------------------------------------------------------------
# scenario runner
# ---------------------------------------------------------------------------


class EpisodicScenarioTargetResolver(Protocol):
    """把 persisted target ref 解析为绑定到隔离环境的 v3 target。"""

    def resolve(self, target_ref: ExecutionTargetRef) -> EpisodicHttpEvaluationV3Target:
        """Resolve the symbolic episode identities from receipts (no content inference)."""
        ...


class EpisodicScenarioRunner:
    """顺序执行一个 scenario 的全部 Runs（evidence + identity + evaluation + artifact）。"""

    def __init__(
        self,
        *,
        uuid_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._uuid_factory = uuid_factory

    async def execute_scenario(
        self,
        plan: EpisodicScenarioRunPlan,
        provisioner: EpisodicEnvironmentProvisioner,
        *,
        target_resolver: EpisodicScenarioTargetResolver | None = None,
    ) -> EpisodicScenarioExecutionReceipt:
        """Provision, run all scenario Runs in order, collect evidence and evaluate."""
        scenario = plan.scenario
        """Provision, run all scenario Runs in order, collect evidence and evaluate."""
        environment: ScenarioEnvironmentEvidence | None = None
        try:
            environment = await provisioner.provision(scenario)
            if not await provisioner.verify_bound(environment):
                raise StatefulEnvironmentError(f"scenario {scenario.scenario_id} binding could not be verified")
            target = (
                provisioner.build_target(environment)
                if target_resolver is None
                else target_resolver.resolve(plan.target_ref)
            )
            try:
                run_records: list[EpisodicRunEvidence] = []
                for run in scenario.runs:
                    actual_run_id = self._uuid_factory()
                    request = build_episodic_execution_request(plan, run, actual_run_id)
                    control = build_episodic_evaluation_control(run, scenario, actual_runtime_run_id=actual_run_id)
                    try:
                        result = await target.execute_v3(
                            request=request,
                            run_id=actual_run_id,
                            evaluation_control=control,
                        )
                        record = build_run_evidence(plan, run, actual_run_id, response=result.response)
                        # canonical Runtime step identity 来自 Journal RuntimeEvent.step_id
                        # （frozen authority）；采集失败即 evidence failure，不改写 runtime。
                        try:
                            record = dataclasses.replace(
                                record,
                                step_facts=read_journal_step_facts(environment.journal_db_path, actual_run_id),
                            )
                        except JournalEvidenceError as exc:
                            record = dataclasses.replace(
                                record,
                                infra_status="JOURNAL_STEP_FACTS_CAPTURE_FAILED",
                            )
                            record = dataclasses.replace(record, journal_error=str(exc))
                        run_records.append(record)
                    except EpisodicV3TargetError as exc:
                        run_records.append(
                            infra_failure_run_evidence(plan, run, actual_run_id, exc, infra_status="EVALUATION_INFRA")
                        )
            finally:
                await target.aclose()

            final_projection = read_episodic_projection(environment.memory_db_path)
            identity_map = resolve_episodic_identity(scenario, tuple(run_records))
            evaluation_evidence = EpisodicScenarioEvaluationEvidence(
                scenario=scenario,
                run_evidence_by_dataset_run_id={record.dataset_run_id: record for record in run_records},
                identity_map=identity_map,
                final_projection=final_projection,
                evaluation_layer=EvaluationLayer.LAYER_1_DETERMINISTIC,
            )
            evaluation = evaluate_episodic_scenario(evaluation_evidence)
            artifact = build_episodic_scenario_artifact(
                plan,
                environment,
                tuple(run_records),
                final_projection,
                identity_map,
                evaluation,
            )
            preserve = evaluation.scenario_outcome in {AssertionStatus.FAIL, AssertionStatus.BLOCKED}
            await provisioner.cleanup(environment, preserve=preserve)
            return EpisodicScenarioExecutionReceipt(
                plan=plan,
                environment=environment,
                run_records=tuple(run_records),
                final_projection=final_projection,
                identity_map=identity_map,
                evaluation=evaluation,
                artifact=artifact,
            )
        except (
            StatefulEnvironmentError,
            EpisodicProjectionError,
            EpisodicV3TargetError,
            EpisodicEvidenceError,
        ) as exc:
            evaluation, artifact = _infra_scenario_artifact(plan, environment, exc)
            if environment is not None:
                await provisioner.cleanup(environment, preserve=True)
            return EpisodicScenarioExecutionReceipt(
                plan=plan,
                environment=environment,  # type: ignore[arg-type]
                run_records=(),
                final_projection=(),
                identity_map=EpisodicIdentityMap(),
                evaluation=evaluation,
                artifact=artifact,
            )


# ---------------------------------------------------------------------------
# experiment runner（global sequential）
# ---------------------------------------------------------------------------


def _baseline_candidate(
    dataset: EpisodicDataset,
    *,
    agentevalops_ref: str,
    target_ref: str,
    interpreter_ref: str | None,
    scenario_receipts: tuple[EpisodicScenarioExecutionReceipt, ...],
    experiment_artifact_ref: str | None,
) -> EpisodicBaselineCandidate:
    return EpisodicBaselineCandidate(
        status=EpisodicBaselineStatus.CANDIDATE,
        provenance=EpisodicBaselineProvenance(
            baseline_id=(EPISODIC_BASELINE_V2_ID if dataset.version == "v2" else EPISODIC_BASELINE_ID),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            dataset_digest=dataset.content_digest,
            agentevalops_implementation_ref=agentevalops_ref,
            target_evaluation_implementation_ref=target_ref,
            interpreter_ref=interpreter_ref,
            execution_policy=SCENARIO_EXECUTION_POLICY,
        ),
        scenario_outcomes={
            receipt.plan.scenario.case_code: receipt.evaluation.scenario_outcome.value for receipt in scenario_receipts
        },
        failure_taxonomy=tuple(
            sorted({taxonomy for receipt in scenario_receipts for taxonomy in receipt.evaluation.failure_taxonomies})
        ),
        blocked_taxonomy=tuple(
            sorted(
                {
                    item.blocked_by.value
                    for receipt in scenario_receipts
                    for item in receipt.evaluation.assertions
                    if item.status is AssertionStatus.BLOCKED and item.blocked_by is not None
                }
            )
        ),
        experiment_artifact_ref=experiment_artifact_ref,
        canonical_baseline=False,
    )


def _gate_dict(gate: EpisodicLayer1GateResult) -> dict[str, object]:
    return {
        "passed": gate.passed,
        "reasons": list(gate.reasons),
        "layer": gate.layer.value,
        "deterministic_gate_required_total": gate.deterministic_gate_required_total,
        "deterministic_gate_required_passed": gate.deterministic_gate_required_passed,
        "required_blocked_total": gate.required_blocked_total,
        "p0_violations": gate.p0_violations,
        "fabricated_fact_violations": gate.fabricated_fact_violations,
        "privacy_violations": gate.privacy_violations,
        "scope_leakage_violations": gate.scope_leakage_violations,
        "instruction_elevation_violations": gate.instruction_elevation_violations,
        "evidence_failure_retained": gate.evidence_failure_retained,
    }


def build_episodic_experiment_artifact(
    dataset: EpisodicDataset,
    *,
    experiment_id: str,
    agentevalops_ref: str,
    target_ref: str,
    scenario_receipts: tuple[EpisodicScenarioExecutionReceipt, ...],
    gate: EpisodicLayer1GateResult,
    environment_provenance: dict[str, object],
    warnings: list[str],
    baseline_candidate: EpisodicBaselineCandidate | None,
    execution_status: ExperimentExecutionStatus = ExperimentExecutionStatus.COMPLETED,
    block_reason: ExperimentBlockReason | None = None,
    scenario_execution_started: bool = True,
) -> EpisodicExperimentArtifact:
    """把 12-scenario 聚合为 experiment-level aggregate artifact。"""
    scenario_artifacts = [receipt.artifact for receipt in scenario_receipts]
    all_assertions: list[EpisodicAssertion] = [
        assertion for receipt in scenario_receipts for assertion in receipt.evaluation.assertions
    ]
    counts: dict[str, int] = {
        AssertionStatus.PASS.value: 0,
        AssertionStatus.FAIL.value: 0,
        AssertionStatus.BLOCKED.value: 0,
        AssertionStatus.NOT_APPLICABLE.value: 0,
    }
    for assertion in all_assertions:
        counts[assertion.status.value] += 1
    failure_taxonomy = sorted(
        {
            item.failure_taxonomy.value
            for item in all_assertions
            if item.status is AssertionStatus.FAIL and item.failure_taxonomy is not None
        }
    )
    blocked_taxonomy = sorted(
        {
            item.blocked_by.value
            for item in all_assertions
            if item.status is AssertionStatus.BLOCKED and item.blocked_by
        }
    )
    experiment_metrics = build_episodic_experiment_metrics(
        [dict(receipt.evaluation.metrics) for receipt in scenario_receipts]
    )
    experiment_metrics["stateful_episodic_scenario_success_rate"] = build_episodic_scenario_success_aggregate(
        [receipt.evaluation.scenario_outcome for receipt in scenario_receipts]
    )
    return EpisodicExperimentArtifact(
        experiment_id=experiment_id,
        dataset={
            "schema": dataset.dataset_schema_version,
            "id": dataset.dataset_id,
            "version": dataset.version,
            "raw_digest": dataset.content_digest,
            "scenario_count": len(dataset.scenarios),
        },
        agentevalops_implementation_ref=agentevalops_ref,
        target_evaluation_implementation_ref=target_ref,
        execution_policy=SCENARIO_EXECUTION_POLICY,
        experiment_execution_status=execution_status,
        experiment_block_reason=block_reason,
        scenario_execution_started=scenario_execution_started,
        scenario_artifacts=scenario_artifacts,
        assertion_summary=counts,
        failure_taxonomy=failure_taxonomy,
        blocked_taxonomy=blocked_taxonomy,
        metrics={name: metric.as_dict() for name, metric in experiment_metrics.items()},
        layer1_gate=_gate_dict(gate),
        environment_provenance=environment_provenance,
        created_at=_utc_iso(_now()),
        tooling_runtime_warnings=warnings,
        baseline=(
            baseline_candidate.to_dict()
            if baseline_candidate is not None
            else {"status": EpisodicBaselineStatus.NOT_CREATED.value, "baseline_id": EPISODIC_BASELINE_ID}
        ),
        private_evaluation_artifact=True,
    )


class EpisodicExperimentRunner:
    """global-sequential 12-scenario experiment runner。"""

    def __init__(
        self,
        *,
        scenario_runner: EpisodicScenarioRunner | None = None,
        uuid_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._scenario_runner = scenario_runner or EpisodicScenarioRunner()
        self._uuid_factory = uuid_factory

    async def run_experiment(
        self,
        *,
        dataset: EpisodicDataset,
        provisioner: EpisodicEnvironmentProvisioner,
        target_ref: ExecutionTargetRef,
        timeout: timedelta,
        localagent_repo: Path,
        target_certification: EpisodicTargetCertification | None = None,
        interpreter_ref: str | None = None,
        warnings: list[str] | None = None,
    ) -> EpisodicExperimentExecutionReceipt:
        """Run the 12-scenario dataset global-sequential and aggregate the experiment."""
        from app.services.evaluation.episodic_environment import (
            certify_episodic_dataset,
        )

        """Run the 12-scenario dataset global-sequential and aggregate the experiment."""
        certify_episodic_dataset(dataset)

        if target_certification is None:
            actual_target_ref = compute_target_evaluation_implementation_ref(localagent_repo)
            if actual_target_ref != EPISODIC_FROZEN_TARGET_REF:
                warnings = warnings or []
                warnings.append("TARGET_REF_MISMATCH")
            target_certification = EpisodicTargetCertification(
                target_reachable=False,
                evaluation_execute_v3_available=False,
                actual_target_ref=actual_target_ref,
                expected_target_ref=EPISODIC_FROZEN_TARGET_REF,
                ref_matches=actual_target_ref == EPISODIC_FROZEN_TARGET_REF,
            )

        if not target_certification.passed:
            agentevalops_ref = episodic_evaluation_implementation_ref()
            gate = EpisodicLayer1GateResult(False, reasons=("experiment prerequisite unavailable",))
            baseline_candidate = _baseline_candidate(
                dataset,
                agentevalops_ref=agentevalops_ref,
                target_ref=target_certification.actual_target_ref,
                interpreter_ref=interpreter_ref,
                scenario_receipts=(),
                experiment_artifact_ref=None,
            )
            aggregate = build_episodic_experiment_artifact(
                dataset,
                experiment_id=self._uuid_factory(),
                agentevalops_ref=agentevalops_ref,
                target_ref=target_certification.actual_target_ref,
                scenario_receipts=(),
                gate=gate,
                environment_provenance={
                    "target_ref": target_certification.actual_target_ref,
                    "execution_policy": SCENARIO_EXECUTION_POLICY,
                    "interpreter_ref": interpreter_ref,
                },
                warnings=(warnings or []) + ["TARGET_PREFLIGHT_FAILED"],
                baseline_candidate=baseline_candidate,
                execution_status=ExperimentExecutionStatus.BLOCKED,
                block_reason=ExperimentBlockReason.PREREQUISITE,
                scenario_execution_started=False,
            )
            return EpisodicExperimentExecutionReceipt(
                dataset, (), gate, aggregate, baseline_candidate, target_certification
            )

        receipt_by_case: list[EpisodicScenarioExecutionReceipt] = []
        for scenario in dataset.scenarios:
            plan = EpisodicScenarioRunPlan(
                dataset=dataset,
                scenario=scenario,
                target_ref=target_ref,
                timeout=timeout,
                created_at=_now(),
            )
            receipt = await self._scenario_runner.execute_scenario(plan, provisioner)
            receipt_by_case.append(receipt)

        receipts = tuple(receipt_by_case)
        evaluations = [receipt.evaluation for receipt in receipts]
        gate = evaluate_episodic_layer1_gate(evaluations)
        agentevalops_ref = episodic_evaluation_implementation_ref()
        baseline_candidate = _baseline_candidate(
            dataset,
            agentevalops_ref=agentevalops_ref,
            target_ref=target_certification.actual_target_ref,
            interpreter_ref=interpreter_ref,
            scenario_receipts=receipts,
            experiment_artifact_ref=None,
        )
        aggregate = build_episodic_experiment_artifact(
            dataset,
            experiment_id=self._uuid_factory(),
            agentevalops_ref=agentevalops_ref,
            target_ref=target_certification.actual_target_ref,
            scenario_receipts=receipts,
            gate=gate,
            environment_provenance={
                "target_ref": target_certification.actual_target_ref,
                "execution_policy": SCENARIO_EXECUTION_POLICY,
                "interpreter_ref": interpreter_ref,
            },
            warnings=warnings or [],
            baseline_candidate=baseline_candidate,
        )
        return EpisodicExperimentExecutionReceipt(
            dataset=dataset,
            scenario_receipts=receipts,
            gate=gate,
            aggregate_artifact=aggregate,
            baseline_candidate=baseline_candidate,
            target_certification=target_certification,
        )


__all__ = [
    "EPISODIC_DATASET_ID",
    "EPISODIC_DATASET_VERSION",
    "SCENARIO_EXECUTION_POLICY",
    "EpisodicExperimentExecutionReceipt",
    "EpisodicExperimentRunner",
    "EpisodicScenarioExecutionReceipt",
    "EpisodicScenarioRunPlan",
    "EpisodicScenarioRunner",
    "EpisodicScenarioTargetResolver",
    "build_episodic_evaluation_control",
    "build_episodic_execution_request",
    "build_episodic_experiment_artifact",
    "build_episodic_fixture_wire",
    "build_episodic_scenario_artifact",
    "build_run_evidence",
    "infra_failure_run_evidence",
    "resolve_episodic_identity",
]
