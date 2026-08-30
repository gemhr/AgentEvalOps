"""WP5 Stateful Memory 的 sequential scenario runner（复用既有 run/attempt/target）。

执行模型（WP5 Architecture Option B）：

- 一个 EvaluationRun 固定一个 scenario（dataset snapshot）；
- 每个 ``scenario.step`` 创建独立 ``ExecutionAttempt``，``case_ref.case_id =
  "<scenario_id>.<step_id>"``，``case_ref.version = dataset version``；
- 同一 scenario 内按 dataset order 顺序 await：step N 的 terminal observation +
  evidence 持久化完成后才开始 step N+1；
- 调用完全经已有 ``LocalAgentHttpExecutionTarget``（或注入的 ExecutionTarget），
  不创建第二个 LocalAgent runner。

每 step 记录：create ExecutionAttempt -> claim/start -> invoke HTTP target ->
terminal outcome -> persist attempt -> capture journal events -> capture post-step
snapshot -> persist evidence refs -> next step。只读 projection 只在隔离 DB 上执行。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from app.core.evaluation.catalog import (
    DatasetVersion,
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluatorKind,
    EvaluatorSpec,
    ScoreDirection,
    TestCaseVersion,
)
from app.core.evaluation.execution import (
    ExecutionOutcome,
    ExecutionTarget,
    ExecutionTargetRef,
    OutcomeKind,
)
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef, VersionRef
from app.core.evaluation.stateful_assertion import EvaluationLayer
from app.core.evaluation.stateful_artifact import (
    StatefulScenarioAggregateV1,
    StatefulSnapshotRef,
    StatefulStepAttemptRecord,
)
from app.core.evaluation.stateful_evaluators import (
    RetrievalSelectionEvidence,
    ScenarioEvaluation,
    ScenarioEvaluationEvidence,
    build_alias_binding,
    evaluate_scenario,
)
from app.core.evaluation.stateful_journal import (
    JournalEvidenceError,
    JournalEvents,
    JournalSettleEvidence,
    has_required_memory_events,
    journal_sequence_watermark,
    read_journal_events,
)
from app.core.evaluation.stateful_metrics import (
    MetricAggregate,
    RatioMetric,
    build_evaluation_infra_failure_rate,
    build_runtime_block_rate,
)
from app.core.evaluation.stateful_memory_dataset import (
    InitialMemoryStateKind,
    StatefulMemoryScenario,
    StatefulMemoryStep,
)
from app.core.evaluation.stateful_projection import (
    MemoryStateSnapshot,
    StateProjectionError,
    read_memory_projection,
    snapshot_memory_state,
    state_diff,
)
from app.core.evaluation.run_attempts import (
    AttemptStatus,
    EvaluationRun,
    ExecutionAttempt,
    RunStatus,
)
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.services.evaluation.stateful_environment import (
    ScenarioEnvironmentEvidence,
    StatefulEnvironmentError,
    StatefulEnvironmentProvisioner,
    seed_fixture_memory,
)
from app.registry.settings import settings
from app.core.evaluation.stateful_impl_ref import evaluation_implementation_ref

SCENARIO_PLACEHOLDER_EVALUATOR_ID = "stateful_memory_scenario"
SCENARIO_PLACEHOLDER_EVALUATOR_VERSION = "v1"

StatefulStepClock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ScenarioRunPlan:
    """一次 scenario run 的 deterministic plan（dataset 权威 + 执行绑定）。"""

    dataset_id: str
    dataset_version: str
    dataset_digest: str | None
    scenario: StatefulMemoryScenario
    target_ref: ExecutionTargetRef
    timeout: timedelta
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScenarioStepExecutionRecord:
    """一个 step 的 runner-level 编排结果（含只读 evidence 凭据）。"""

    step: StatefulMemoryStep
    case_ref: CaseVersionRef
    attempt_id: UUID
    outcome_kind: OutcomeKind
    outcome_error_category: str | None
    outcome_evidence_refs: tuple[EvidenceRef, ...]
    journal: JournalEvents
    journal_settle: JournalSettleEvidence | None
    pre_snapshot: MemoryStateSnapshot
    post_snapshot: MemoryStateSnapshot
    selection_evidence: RetrievalSelectionEvidence | None


@dataclass(frozen=True, slots=True)
class ScenarioExecutionReceipt:
    """一次 scenario 执行的完整凭据：run/attempt + evidence + evaluation + artifact。"""

    run_id: UUID
    plan: ScenarioRunPlan
    environment: ScenarioEnvironmentEvidence
    step_records: tuple[ScenarioStepExecutionRecord, ...]
    final_snapshot: MemoryStateSnapshot | None
    alias_binding: dict[str, str]
    evaluation: ScenarioEvaluation
    artifact: StatefulScenarioAggregateV1


class ScenarioTargetResolver(Protocol):
    """把 persisted target ref 解析为绑定到隔离环境的 executable Target。"""

    def resolve(self, target_ref: ExecutionTargetRef) -> ExecutionTarget:
        """解析 Target；未知配置异常原样传播。"""
        ...


def build_step_case_ref(scenario_id: str, step_id: str, version: str) -> CaseVersionRef:
    """构造一个 step 的 case ref：``<scenario_id>.<step_id>``。"""
    return CaseVersionRef(f"{scenario_id}.{step_id}", version)


def _is_v2_scenario(scenario: object) -> bool:
    """是否 V2 ``StatefulMemoryScenarioV2``（V2 seed 需要 strict canonical_text）。"""
    from app.core.evaluation.stateful_memory_dataset_v2 import StatefulMemoryScenarioV2

    return isinstance(scenario, StatefulMemoryScenarioV2)


def build_step_case(
    scenario_id: str,
    step: StatefulMemoryStep,
    version: str,
    created_at: datetime,
) -> TestCaseVersion:
    """构造一个 step 的 execution TestCase snapshot（input 走 LocalAgent wire 契约）。"""
    ref = build_step_case_ref(scenario_id, step.step_id, version)
    return TestCaseVersion(
        case_id=ref.case_id,
        version=version,
        name=step.step_id,
        input_payload={"agent_id": step.agent_id, "query": step.query},
        created_at=created_at,
    )


def build_scenario_catalog(
    plan: ScenarioRunPlan,
) -> tuple[DatasetVersion, EvaluationSuiteVersion, dict[CaseVersionRef, TestCaseVersion]]:
    """把 scenario 投影为 Run/Attempt persistence 所需的 catalog snapshot。"""
    refs: list[CaseVersionRef] = []
    cases: dict[CaseVersionRef, TestCaseVersion] = {}
    for step in plan.scenario.steps:
        ref = build_step_case_ref(plan.scenario.scenario_id, step.step_id, plan.dataset_version)
        refs.append(ref)
        cases[ref] = build_step_case(plan.scenario.scenario_id, step, plan.dataset_version, plan.created_at)
    dataset = DatasetVersion(
        dataset_id=plan.dataset_id,
        version=plan.dataset_version,
        name=plan.dataset_id,
        created_at=plan.created_at,
        case_version_refs=tuple(refs),
    )
    placeholder = EvaluatorSpec(
        evaluator_id=SCENARIO_PLACEHOLDER_EVALUATOR_ID,
        evaluator_version=SCENARIO_PLACEHOLDER_EVALUATOR_VERSION,
        evaluator_kind=EvaluatorKind.DETERMINISTIC,
        config_ref=VersionRef("stateful_placeholder", "v1"),
        score_direction=ScoreDirection.NOT_APPLICABLE,
        required=False,
    )
    suite = EvaluationSuiteVersion(
        suite_id=f"stateful-scenario-{plan.scenario.scenario_id}",
        version=plan.dataset_version,
        case_selection=tuple(refs),
        evaluator_specs=(placeholder,),
        evaluation_policy=EvaluationPolicy(),
        created_at=plan.created_at,
    )
    return dataset, suite, cases


def build_selection_evidence(
    step: StatefulMemoryStep,
    journal: JournalEvents,
    run_id: str,
    *,
    selection_ids_path: Path | None = None,
    allow_evaluation_only_selection_ids: bool = False,
) -> RetrievalSelectionEvidence | None:
    """从 journal retrieval events 构建 selection evidence。

    当前 LocalAgent journal content-minimized：只提供 counts/flags。若 isolation
    harness 以 ``selection_ids_path`` 暴露 identity 级证据，则 enrich 为 SELECTION_IDS。
    production LocalAgent journal 不含该 identity；未显式标记为 evaluation-only harness
    时不得读取同名文件，以免把本地杂项文件误作 Runtime evidence。
    """
    if not journal.retrieval:
        return None
    event = journal.retrieval[-1]
    selected_ids: tuple[str, ...] | None = None
    if allow_evaluation_only_selection_ids and selection_ids_path is not None and selection_ids_path.is_file():
        try:
            payload = json.loads(selection_ids_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("selected_memory_ids"), list):
            selected_ids = tuple(str(item) for item in payload["selected_memory_ids"])
    return RetrievalSelectionEvidence(
        step_id=step.step_id,
        run_id=run_id,
        retrieval_status=event.status,
        selected_count=event.selected_count,
        context_record_count=event.context_record_count,
        planning_injected=event.planning_injected,
        direct_entry_supplied=event.direct_entry_supplied,
        registered_selected_count=event.registered_selected_count,
        open_selected_count=event.open_selected_count,
        selected_memory_ids=selected_ids,
    )


def _journal_evidence_ref(environment_id: str, step_id: str, run_id: str) -> EvidenceRef:
    return EvidenceRef(
        kind="stateful_journal_evidence",
        identifier=f"scenario://{environment_id}/{step_id}/journal",
        media_type="application/vnd.stateful.journal+json",
        schema_version="v1",
        metadata={"run_id": run_id, "environment_id": environment_id},
    )


def _snapshot_evidence_ref(environment_id: str, step_id: str, phase: str, snapshot_id: str) -> EvidenceRef:
    return EvidenceRef(
        kind="stateful_state_snapshot",
        identifier=f"scenario://{environment_id}/{step_id}/{phase}",
        media_type="application/vnd.stateful.snapshot+json",
        schema_version="v1",
        metadata={"snapshot_id": snapshot_id, "environment_id": environment_id},
    )


def _step_expects_events(step: StatefulMemoryStep) -> dict[str, bool]:
    """该 step 的 expectations 需要哪些 memory events 作为证据（用于 settle 停止）。"""
    decision = step.expected_formation.decision if step.expected_formation is not None else None
    expects_formation = decision is not None and decision.value in {"REMEMBER", "IGNORE"}
    lifecycle = step.expected_lifecycle
    expects_lifecycle = lifecycle is not None and lifecycle.value != "POLICY_IGNORED"
    return {
        "expects_formation": expects_formation,
        "expects_lifecycle": expects_lifecycle,
        "expects_retrieval": step.expected_retrieval is not None,
    }


class StatefulScenarioRunnerService:
    """顺序执行一个 scenario 的全部 steps 并持久化 attempt/evidence/artifact。"""

    def __init__(
        self,
        persistence: EvaluationPersistenceService,
        *,
        clock: StatefulStepClock = _now,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._persistence = persistence
        self._clock = clock
        self._uuid_factory = uuid_factory

    async def execute_scenario(
        self,
        project_id: UUID,
        plan: ScenarioRunPlan,
        provisioner: StatefulEnvironmentProvisioner,
        *,
        lease: timedelta,
        worker_ref: str | None = None,
        task_ref: str | None = None,
        target_resolver: ScenarioTargetResolver | None = None,
    ) -> ScenarioExecutionReceipt:
        """Provision -> verify -> run steps 顺序执行 -> evidence -> evaluate -> artifact。"""
        scenario = plan.scenario
        evidence = await provisioner.provision(scenario)
        try:
            if not await provisioner.verify_bound(evidence):
                raise StatefulEnvironmentError(f"scenario {scenario.scenario_id} binding could not be verified")
            if scenario.initial_state.kind is InitialMemoryStateKind.SEEDED:
                seed_fixture_memory(
                    evidence.memory_db_path,
                    records=scenario.initial_state.records,
                    environment_id=evidence.scenario_environment_id,
                    strict_canonical=_is_v2_scenario(scenario),
                )
                evidence = replace(evidence, fixture_seeded=True)

            target = (
                provisioner.build_target(evidence)
                if target_resolver is None
                else target_resolver.resolve(plan.target_ref)
            )
            try:
                dataset, suite, cases = build_scenario_catalog(plan)
                run, attempts = await self._persistence.create_run(
                    project_id=project_id,
                    dataset=dataset,
                    suite=suite,
                    cases=cases,
                    target=plan.target_ref,
                    timeout=plan.timeout,
                    metadata={
                        "scenario_id": scenario.scenario_id,
                        "scenario_environment_id": evidence.scenario_environment_id,
                        "dataset_digest": plan.dataset_digest,
                        "truthfulness_origin": scenario.truthfulness_origin.value,
                        "regression_tags": [tag.value for tag in scenario.regression_tags],
                    },
                )
                step_records: list[ScenarioStepExecutionRecord] = []
                binding_proven = False
                success_without_writes = False
                for attempt, step in zip(attempts, scenario.steps, strict=True):
                    record = await self._execute_step(
                        project_id,
                        plan,
                        evidence,
                        target,
                        attempt,
                        step,
                        lease=lease,
                        worker_ref=worker_ref,
                        task_ref=task_ref,
                    )
                    run_id = str(attempt.attempt_id)
                    journal_written = bool(
                        record.journal.formation or record.journal.lifecycle or record.journal.retrieval
                    )
                    memory_written = any(rec.origin_run_id == run_id for rec in record.post_snapshot.records)
                    if journal_written or memory_written:
                        binding_proven = True
                    elif record.outcome_kind is OutcomeKind.SUCCESS:
                        success_without_writes = True
                    step_records.append(record)
                if not binding_proven and success_without_writes:
                    raise StatefulEnvironmentError(
                        "LocalAgent instance did not write to the scenario-owned DB/journal"
                    )
            finally:
                if hasattr(target, "aclose"):
                    close = target.aclose()
                    if close is not None:
                        await close

            final_snapshot = snapshot_memory_state(
                evidence.memory_db_path,
                f"{evidence.scenario_environment_id}.final",
                captured_at=self._clock(),
            )
            alias_binding = build_alias_binding(
                scenario.expected_state,
                [record.post_snapshot for record in step_records if record.post_snapshot.records] + [final_snapshot],
            )
            evaluation_evidence = ScenarioEvaluationEvidence(
                scenario=scenario,
                journal_by_step={record.step.step_id: record.journal for record in step_records},
                snapshots_by_step={
                    record.step.step_id: (record.pre_snapshot, record.post_snapshot) for record in step_records
                },
                final_snapshot=final_snapshot,
                outcome_kind_by_step={record.step.step_id: record.outcome_kind.value for record in step_records},
                run_id_by_step={record.step.step_id: str(record.attempt_id) for record in step_records},
                selection_by_step={
                    record.step.step_id: record.selection_evidence
                    for record in step_records
                    if record.selection_evidence is not None
                },
                alias_binding=alias_binding,
                evaluation_layer=(
                    EvaluationLayer.LAYER_1_DETERMINISTIC
                    if evidence.evaluation_only_harness
                    else EvaluationLayer.LAYER_2_REAL_MODEL
                ),
            )
            evaluation = evaluate_scenario(evaluation_evidence)
            artifact = build_scenario_artifact(
                run_id=run.run_id,
                plan=plan,
                environment=evidence,
                step_records=tuple(step_records),
                final_snapshot=final_snapshot,
                alias_binding=alias_binding,
                evaluation=evaluation,
            )
            try:
                await self._persistence.finish_run(
                    project_id,
                    run.run_id,
                    failure_reason=(
                        f"scenario {scenario.scenario_id} execution failed"
                        if evaluation.scenario_outcome.value == "FAIL"
                        else "execution failed"
                    ),
                )
            except Exception:
                pass
            preserve = evaluation.scenario_outcome.value in {"FAIL", "BLOCKED"} or _has_blocked_or_failed_evidence(
                evaluation
            )
            await provisioner.cleanup(evidence, preserve=preserve)
            return ScenarioExecutionReceipt(
                run_id=run.run_id,
                plan=plan,
                environment=evidence,
                step_records=tuple(step_records),
                final_snapshot=final_snapshot,
                alias_binding=alias_binding,
                evaluation=evaluation,
                artifact=artifact,
            )
        except (StatefulEnvironmentError, JournalEvidenceError, StateProjectionError) as exc:
            evaluation, artifact = self._infra_failure(plan, evidence, exc)
            await provisioner.cleanup(evidence, preserve=True)
            return ScenarioExecutionReceipt(
                run_id=self._uuid_factory(),
                plan=plan,
                environment=evidence,
                step_records=(),
                final_snapshot=None,
                alias_binding={},
                evaluation=evaluation,
                artifact=artifact,
            )

    async def _execute_step(
        self,
        project_id: UUID,
        plan: ScenarioRunPlan,
        evidence: ScenarioEnvironmentEvidence,
        target: ExecutionTarget,
        attempt: ExecutionAttempt,
        step: StatefulMemoryStep,
        *,
        lease: timedelta,
        worker_ref: str | None,
        task_ref: str | None,
    ) -> ScenarioStepExecutionRecord:
        environment_id = evidence.scenario_environment_id
        pre_snapshot = snapshot_memory_state(
            evidence.memory_db_path,
            f"{environment_id}.{step.step_id}.pre",
            captured_at=self._clock(),
        )
        claim = await self._persistence.claim_attempt(
            project_id, attempt.attempt_id, lease=lease, worker_ref=worker_ref, task_ref=task_ref
        )
        if not claim.claimed or claim.claim_token is None:
            raise StatefulEnvironmentError(f"step {step.step_id} attempt could not be claimed")
        started = await self._persistence.start_attempt(project_id, attempt.attempt_id, claim.claim_token)
        outcome = await target.execute(started.execution_request)
        if not isinstance(outcome, ExecutionOutcome):
            raise StatefulEnvironmentError("target returned an invalid outcome type")

        run_id = str(attempt.attempt_id)
        journal, settle = await self._capture_journal_with_settle(evidence, step, run_id)
        post_snapshot = snapshot_memory_state(
            evidence.memory_db_path,
            f"{environment_id}.{step.step_id}.post",
            captured_at=self._clock(),
        )

        selection = build_selection_evidence(
            step,
            journal,
            run_id,
            selection_ids_path=evidence.work_dir / f"retrieval_selection_{run_id}.json",
            allow_evaluation_only_selection_ids=evidence.evaluation_only_harness,
        )
        captured_refs = (
            _journal_evidence_ref(environment_id, step.step_id, run_id),
            _snapshot_evidence_ref(environment_id, step.step_id, "pre", pre_snapshot.snapshot_id),
            _snapshot_evidence_ref(environment_id, step.step_id, "post", post_snapshot.snapshot_id),
        )
        augmented = replace(
            outcome,
            evidence_refs=tuple(dict.fromkeys((*outcome.evidence_refs, *captured_refs))),
        )
        terminal = await self._persistence.record_outcome(project_id, attempt.attempt_id, claim.claim_token, augmented)
        if terminal is None:
            raise StatefulEnvironmentError(f"step {step.step_id} outcome could not be persisted")
        return ScenarioStepExecutionRecord(
            step=step,
            case_ref=attempt.case_ref,
            attempt_id=attempt.attempt_id,
            outcome_kind=outcome.kind,
            outcome_error_category=outcome.error_category,
            outcome_evidence_refs=augmented.evidence_refs,
            journal=journal,
            journal_settle=settle,
            pre_snapshot=pre_snapshot,
            post_snapshot=post_snapshot,
            selection_evidence=selection,
        )

    async def _capture_journal_with_settle(
        self,
        evidence: ScenarioEnvironmentEvidence,
        step: StatefulMemoryStep,
        run_id: str,
    ) -> tuple[JournalEvents, JournalSettleEvidence]:
        """bounded, deadline-aware journal settle（E1-R2 / EVALUATION_EVIDENCE_CAPTURE_BUG）。

        先立即读取；若该 step 期望的 memory events 未齐备，按固定 poll interval 有界
        重读，直到 required events 齐备、watermark 稳定一个 interval、或 settle
        deadline 到达。绝不使用任意 magic sleep；deadline 后仍缺失 → 由 evaluator 判
        BLOCKED(evidence_capture)。
        """
        db_path = evidence.journal_db_path
        budget_ms = int(settings.STATEFUL_JOURNAL_SETTLE_BUDGET_MS)
        poll_ms = int(settings.STATEFUL_JOURNAL_POLL_INTERVAL_MS)
        if budget_ms < 0 or poll_ms <= 0:
            raise StatefulEnvironmentError("invalid journal settle settings")
        deadline = asyncio.get_running_loop().time() + budget_ms / 1000.0
        poll_interval = poll_ms / 1000.0

        expects = _step_expects_events(step)
        journal = read_journal_events(db_path, run_id)
        initial_wm = journal_sequence_watermark(db_path, run_id)
        current_wm = initial_wm
        attempts = 1
        stop_reason: str | None = None
        while not has_required_memory_events(journal, **expects):
            if asyncio.get_running_loop().time() >= deadline:
                stop_reason = "DEADLINE_REACHED"
                break
            await asyncio.sleep(poll_interval)
            attempts += 1
            new_journal = read_journal_events(db_path, run_id)
            new_wm = journal_sequence_watermark(db_path, run_id)
            if new_wm == current_wm:
                journal = new_journal
                stop_reason = "STABLE_WATERMARK"
                break
            journal = new_journal
            current_wm = new_wm
        if stop_reason is None:
            stop_reason = "EXPECTED_EVIDENCE_OBSERVED"
        settle = JournalSettleEvidence(
            initial_sequence_watermark=initial_wm,
            final_sequence_watermark=current_wm,
            poll_attempts=attempts,
            stop_reason=stop_reason,
        )
        return journal, settle

    def _infra_failure(
        self,
        plan: ScenarioRunPlan,
        evidence: ScenarioEnvironmentEvidence,
        error: StatefulEnvironmentError,
    ) -> tuple[ScenarioEvaluation, StatefulScenarioAggregateV1]:
        from app.core.evaluation.stateful_assertion import (
            AssertionDimension,
            AssertionStatus,
            BlockReason,
            MemoryAssertion,
        )

        assertion = MemoryAssertion(
            assertion_id=f"{plan.scenario.scenario_id}.infra",
            dimension=AssertionDimension.E2E,
            status=AssertionStatus.BLOCKED,
            blocked_by=BlockReason.EVALUATION_INFRASTRUCTURE,
            reason=str(error),
        )
        evaluation = ScenarioEvaluation(
            scenario_id=plan.scenario.scenario_id,
            assertions=(assertion,),
            metrics={},
            runtime_block_rate=build_runtime_block_rate([assertion]),
            evaluation_infra_failure_rate=build_evaluation_infra_failure_rate([assertion]),
            scenario_outcome=AssertionStatus.BLOCKED,
            scenario_outcome_assertion=assertion,
            failure_taxonomies=(),
        )
        artifact = StatefulScenarioAggregateV1(
            evaluation_run_id=str(self._uuid_factory()),
            dataset_id=plan.dataset_id,
            dataset_version=plan.dataset_version,
            dataset_digest=plan.dataset_digest,
            target_id=plan.target_ref.target_id,
            target_kind=plan.target_ref.target_kind,
            target_version_ref=_version_ref_json(plan.target_ref.target_version_ref),
            config_ref=_version_ref_json(plan.target_ref.config_ref),
            scenario_id=plan.scenario.scenario_id,
            truthfulness_origin=plan.scenario.truthfulness_origin.value,
            regression_tags=[tag.value for tag in plan.scenario.regression_tags],
            required=plan.scenario.required,
            deterministic_denominator=plan.scenario.deterministic_denominator,
            initial_state={"kind": plan.scenario.initial_state.kind.value},
            scenario_outcome=AssertionStatus.BLOCKED.value,
            scenario_outcome_assertion=assertion.to_metadata(),
            assertion_results=[assertion.to_metadata()],
            failure_taxonomies=[],
            evaluation_implementation_ref=evaluation_implementation_ref(),
            private_evaluation_artifact=True,
        )
        return evaluation, artifact


def _version_ref_json(ref: VersionRef | None) -> dict[str, object] | None:
    if ref is None:
        return None
    return {"kind": ref.kind, "opaque_value": ref.opaque_value}


def _has_blocked_or_failed_evidence(evaluation: ScenarioEvaluation) -> bool:
    return any(
        assertion.status.value in {"FAIL", "BLOCKED"}
        for assertion in evaluation.assertions
        if assertion.assertion_id.endswith(".infra")
        or assertion.assertion_id.endswith(".final_state")
        or assertion.assertion_id.endswith(".invariant")
    )


def build_scenario_artifact(
    *,
    run_id: UUID,
    plan: ScenarioRunPlan,
    environment: ScenarioEnvironmentEvidence,
    step_records: tuple[ScenarioStepExecutionRecord, ...],
    final_snapshot: MemoryStateSnapshot | None,
    alias_binding: dict[str, str],
    evaluation: ScenarioEvaluation,
    baseline_comparison: Mapping[str, object] | None = None,
    retention_ref: Mapping[str, object] | None = None,
) -> StatefulScenarioAggregateV1:
    """把 scenario execution + evaluation 聚合为 append-only aggregate artifact。"""
    expected_state = [item.model_dump(mode="json") for item in plan.scenario.expected_state]
    actual_state = (
        [record.to_projection_dict() for record in final_snapshot.records] if final_snapshot is not None else []
    )
    state_diffs = (
        [
            diff.to_dict()
            for diff in state_diff(plan.scenario.expected_state, final_snapshot.records, alias_binding=alias_binding)
        ]
        if final_snapshot is not None
        else []
    )
    return StatefulScenarioAggregateV1(
        evaluation_run_id=str(run_id),
        dataset_id=plan.dataset_id,
        dataset_version=plan.dataset_version,
        dataset_digest=plan.dataset_digest,
        target_id=plan.target_ref.target_id,
        target_kind=plan.target_ref.target_kind,
        target_version_ref=_version_ref_json(plan.target_ref.target_version_ref),
        config_ref=_version_ref_json(plan.target_ref.config_ref),
        scenario_id=plan.scenario.scenario_id,
        truthfulness_origin=plan.scenario.truthfulness_origin.value,
        regression_tags=[tag.value for tag in plan.scenario.regression_tags],
        required=plan.scenario.required,
        deterministic_denominator=plan.scenario.deterministic_denominator,
        initial_state={
            "kind": plan.scenario.initial_state.kind.value,
            "fixture_seeded": environment.fixture_seeded,
            "evaluation_only_harness": environment.evaluation_only_harness,
            "scenario_token": environment.scenario_token,
        },
        step_attempts=[
            StatefulStepAttemptRecord(
                step_id=record.step.step_id,
                case_id=record.case_ref.case_id,
                case_version=record.case_ref.version,
                attempt_id=str(record.attempt_id),
                outcome_kind=record.outcome_kind.value,
                attempt_evidence_refs=[
                    {
                        "kind": ref.kind,
                        "identifier": ref.identifier,
                        "schema_version": ref.schema_version,
                        "metadata": dict(ref.metadata),
                    }
                    for ref in record.outcome_evidence_refs
                ],
                metadata={
                    "journal_settle": (record.journal_settle.to_dict() if record.journal_settle is not None else None)
                },
            )
            for record in step_records
        ],
        runtime_evidence_refs=[
            {
                "kind": ref.kind,
                "identifier": ref.identifier,
                "schema_version": ref.schema_version,
                "metadata": dict(ref.metadata),
            }
            for record in step_records
            for ref in record.outcome_evidence_refs
        ],
        snapshot_refs=[
            StatefulSnapshotRef(
                snapshot_id=record.pre_snapshot.snapshot_id,
                phase="PRE_STEP",
                step_id=record.step.step_id,
                db_path=record.pre_snapshot.db_path,
                record_count=len(record.pre_snapshot.records),
                evidence_ref={
                    "kind": "stateful_state_snapshot",
                    "identifier": f"scenario://{environment.scenario_environment_id}/{record.step.step_id}/pre",
                },
            )
            for record in step_records
        ]
        + [
            StatefulSnapshotRef(
                snapshot_id=record.post_snapshot.snapshot_id,
                phase="POST_STEP",
                step_id=record.step.step_id,
                db_path=record.post_snapshot.db_path,
                record_count=len(record.post_snapshot.records),
                evidence_ref={
                    "kind": "stateful_state_snapshot",
                    "identifier": f"scenario://{environment.scenario_environment_id}/{record.step.step_id}/post",
                },
            )
            for record in step_records
        ]
        + (
            [
                StatefulSnapshotRef(
                    snapshot_id=final_snapshot.snapshot_id,
                    phase="FINAL",
                    step_id=None,
                    db_path=final_snapshot.db_path,
                    record_count=len(final_snapshot.records),
                    evidence_ref={
                        "kind": "stateful_state_snapshot",
                        "identifier": f"scenario://{environment.scenario_environment_id}/final",
                    },
                )
            ]
            if final_snapshot is not None
            else []
        ),
        expected_state=expected_state,
        actual_state=actual_state,
        state_diff=state_diffs,
        assertion_results=[assertion.to_metadata() for assertion in evaluation.assertions],
        metric_aggregates={name: aggregate.as_dict() for name, aggregate in evaluation.metrics.items()},
        failure_taxonomies=list(evaluation.failure_taxonomies),
        scenario_outcome=evaluation.scenario_outcome.value,
        scenario_outcome_assertion=evaluation.scenario_outcome_assertion.to_metadata(),
        retention_ref=dict(retention_ref) if retention_ref else None,
        baseline_comparison=dict(baseline_comparison) if baseline_comparison else None,
        evaluation_implementation_ref=evaluation_implementation_ref(),
        metadata={
            "alias_binding": alias_binding,
            "localagent_python_executable_ref": environment.localagent_python_executable_ref,
        },
    )


__all__ = [
    "SCENARIO_PLACEHOLDER_EVALUATOR_ID",
    "ScenarioExecutionReceipt",
    "ScenarioRunPlan",
    "ScenarioStepExecutionRecord",
    "ScenarioTargetResolver",
    "StatefulScenarioRunnerService",
    "build_scenario_artifact",
    "build_scenario_catalog",
    "build_selection_evidence",
    "build_step_case",
    "build_step_case_ref",
]
