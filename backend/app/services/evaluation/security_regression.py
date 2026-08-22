"""Prompt Injection Security Regression Runner（WP6）。

复用既有 ``EvaluationRun`` / ``EvaluationLoopService`` / ``EvaluationResult`` /
Comparison / Report 能力的薄批量编排层。Runner 只负责：

resolve cases（deterministic Execution Support Matrix）→ execute existing loop →
collect result ids → produce projection。

不创建第二套 Security Runner / Comparison / Report Domain；Verdict Authority 完全属于
``PromptInjectionSecurityEvaluator``，本模块不重新判定任何安全结论、不重试 Judge、
不 override prompt / threshold / judge model。
"""

# ruff: noqa: D105, D415

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from uuid import UUID

from app.core.evaluation.catalog import (
    DatasetVersion,
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluatorSpec,
    TestCaseVersion,
)
from app.core.evaluation.dataset import AttackSource, EvaluationDataset
from app.core.evaluation.dataset_bridge import bridge_dataset_to_catalog
from app.core.evaluation.execution import FIXTURE_TARGET_KIND, ExecutionTargetRef
from app.core.evaluation.references import CaseVersionRef
from app.core.evaluation.security_evaluator import SECURITY_EVALUATOR_ID
from app.core.evaluation.security_projection import (
    SecurityCaseFacts,
    SecurityRunSummary,
    UnmappedSecurityCaseRef,
    build_security_run_summary,
)
from app.core.evaluation.results import EvaluationResult
from app.services.evaluation.loop import (
    EvaluationLoopResult,
    EvaluationLoopService,
    EvaluatorResolver,
    ExecutionTargetResolver,
)
from app.services.evaluation.persistence import EvaluationPersistenceService

# 必须与 app.adapters.evaluation.http_localagent.LOCALAGENT_HTTP_TARGET_KIND 一致；
# services 层不反向依赖 adapters，此处显式声明该耦合。
LOCALAGENT_HTTP_TARGET_KIND = "LOCALAGENT_HTTP"

GAP_TARGET_KIND_UNSUPPORTED = "unsupported_execution_target"
GAP_TOOL_OUTPUT_BOUNDARY = "tool_output_boundary_unsupported"
GAP_AGENT_MESSAGE_BOUNDARY = "agent_message_boundary_unsupported"
GAP_RETRIEVED_CONTEXT_INJECTION = "retrieved_context_kb_injection_unsupported"
GAP_REFERENCE_DATA_DELIVERY = "reference_data_not_runtime_deliverable"
GAP_NON_QUERY_STIMULUS = "non_query_stimulus_keys_unsupported"
GAP_WIRE_PAYLOAD_INVALID = "localagent_wire_payload_invalid"

_SOURCE_GAP_REASONS = {
    AttackSource.TOOL_OUTPUT.value: GAP_TOOL_OUTPUT_BOUNDARY,
    AttackSource.AGENT_MESSAGE.value: GAP_AGENT_MESSAGE_BOUNDARY,
    AttackSource.RETRIEVED_CONTEXT.value: GAP_RETRIEVED_CONTEXT_INJECTION,
    AttackSource.REFERENCE_DATA.value: GAP_REFERENCE_DATA_DELIVERY,
}

RUNNER_INFRASTRUCTURE_FAILURE = "RUNNER_INFRASTRUCTURE_FAILURE"

_SUCCESS_LOOP_OUTCOMES = frozenset(
    {EvaluationLoopResult.PROGRESSED.value, EvaluationLoopResult.ALREADY_COMPLETE.value}
)


class SecurityRegressionPlanError(RuntimeError):
    """Runner plan 输入违反 deterministic 执行映射 contract。"""


def map_security_case_input(
    *,
    target_kind: str,
    case_input: Mapping[str, object],
    attack_source: str | None,
    localagent_agent_id: str,
) -> tuple[dict[str, object] | None, str | None]:
    """按 Execution Support Matrix 把 case input 投影为 target wire payload。

    返回 ``(payload, gap_reason)``：恰好一个为 ``None``。映射必须保证 Evaluator 可见的
    stimulus 与 dataset 声明逐字一致；无法无损映射时显式拒绝，不伪造执行边界。
    """
    if target_kind == FIXTURE_TARGET_KIND:
        return dict(case_input), None
    if target_kind != LOCALAGENT_HTTP_TARGET_KIND:
        return None, GAP_TARGET_KIND_UNSUPPORTED
    keys = frozenset(case_input)
    query = case_input.get("query") if "query" in keys else None
    if keys == {"query"} and isinstance(query, str):
        return {"agent_id": localagent_agent_id, "query": query}, None
    if attack_source is not None:
        reason = _SOURCE_GAP_REASONS.get(attack_source)
        if reason is not None:
            return None, reason
    if keys != {"query"}:
        return None, GAP_NON_QUERY_STIMULUS
    return None, GAP_WIRE_PAYLOAD_INVALID


@dataclass(frozen=True, slots=True)
class SecurityRunPlan:
    """一次 Security batch Run 的 deterministic plan（dataset 权威 + 映射结果）。"""

    dataset: DatasetVersion
    suite: EvaluationSuiteVersion
    cases: Mapping[CaseVersionRef, TestCaseVersion]
    facts: tuple[SecurityCaseFacts, ...]
    mapped_refs: tuple[CaseVersionRef, ...]
    unmapped: tuple[UnmappedSecurityCaseRef, ...]
    target_ref: ExecutionTargetRef
    timeout: timedelta


@dataclass(frozen=True, slots=True)
class SecurityAttemptRecord:
    """单个 Attempt 的 runner-level 编排结果（非安全 verdict）。"""

    case_ref: CaseVersionRef
    attempt_id: UUID
    outcome: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class SecurityRunExecutionReceipt:
    """一次批量执行的编排凭据：run identity + per-attempt 控制流记录。"""

    run_id: UUID
    records: tuple[SecurityAttemptRecord, ...]


def collect_not_evaluated_reasons(
    receipt: SecurityRunExecutionReceipt,
) -> dict[tuple[str, str], str]:
    """从编排凭据推导 NOT_EVALUATED 状态的稳定 status_reason。"""
    reasons: dict[tuple[str, str], str] = {}
    for record in receipt.records:
        if record.outcome in _SUCCESS_LOOP_OUTCOMES:
            continue
        reasons[(record.case_ref.case_id, record.case_ref.version)] = record.outcome.lower()
    return reasons


class SecurityRegressionService:
    """Resolve → execute existing loop → collect → project 的最小编排 facade。"""

    def __init__(
        self,
        persistence: EvaluationPersistenceService,
        *,
        localagent_agent_id: str = "core_router",
    ) -> None:
        self._persistence = persistence
        self._localagent_agent_id = localagent_agent_id

    def plan_run(
        self,
        dataset: EvaluationDataset,
        *,
        execution_target_ref: ExecutionTargetRef,
        evaluator_spec: EvaluatorSpec,
        suite_id: str,
        suite_version: str,
        timeout: timedelta,
        created_at: datetime,
        evaluation_policy: EvaluationPolicy | None = None,
    ) -> SecurityRunPlan:
        """把版本化 security dataset 解析为 Run plan；case set 在此冻结且 deterministic。"""
        if evaluator_spec.evaluator_id != SECURITY_EVALUATOR_ID:
            raise SecurityRegressionPlanError(
                "security regression runner requires the prompt_injection_security evaluator spec"
            )
        catalog_dataset, bridged_cases = bridge_dataset_to_catalog(dataset, created_at=created_at)
        facts: list[SecurityCaseFacts] = []
        mapped_refs: list[CaseVersionRef] = []
        unmapped: list[UnmappedSecurityCaseRef] = []
        projected_cases: dict[CaseVersionRef, TestCaseVersion] = {}
        for item in dataset.cases:
            ground_truth = item.ground_truth.security
            if ground_truth is None:
                raise SecurityRegressionPlanError(f"case {item.case_id} has no security ground truth")
            fact = SecurityCaseFacts(
                case_id=item.case_id,
                case_version=catalog_dataset.version,
                case_kind=ground_truth.case_kind.value,
                attack_type=(
                    ground_truth.attack_type.value if ground_truth.attack_type is not None else None
                ),
                attack_source=(
                    ground_truth.attack_source.value if ground_truth.attack_source is not None else None
                ),
                severity=ground_truth.severity.value if ground_truth.severity is not None else None,
            )
            facts.append(fact)
            ref = CaseVersionRef(item.case_id, catalog_dataset.version)
            payload, gap_reason = map_security_case_input(
                target_kind=execution_target_ref.target_kind,
                case_input=item.input,
                attack_source=fact.attack_source,
                localagent_agent_id=self._localagent_agent_id,
            )
            if gap_reason is not None or payload is None:
                unmapped.append(
                    UnmappedSecurityCaseRef(
                        case_id=item.case_id,
                        case_version=catalog_dataset.version,
                        gap_reason=str(gap_reason),
                    )
                )
                continue
            projected_cases[ref] = replace_input_payload(bridged_cases[ref], payload)
            mapped_refs.append(ref)
        if not mapped_refs:
            raise SecurityRegressionPlanError("no selected case maps to the requested execution target")
        suite = EvaluationSuiteVersion(
            suite_id=suite_id,
            version=suite_version,
            case_selection=tuple(mapped_refs),
            evaluator_specs=(evaluator_spec,),
            evaluation_policy=evaluation_policy or EvaluationPolicy(),
            created_at=created_at,
        )
        return SecurityRunPlan(
            dataset=catalog_dataset,
            suite=suite,
            cases=MappingProxyType(projected_cases),
            facts=tuple(facts),
            mapped_refs=tuple(mapped_refs),
            unmapped=tuple(unmapped),
            target_ref=execution_target_ref,
            timeout=timeout,
        )

    async def execute_plan(
        self,
        project_id: UUID,
        plan: SecurityRunPlan,
        *,
        target_resolver: ExecutionTargetResolver,
        evaluator_resolver: EvaluatorResolver,
        lease: timedelta,
        worker_ref: str | None = None,
        task_ref: str | None = None,
    ) -> SecurityRunExecutionReceipt:
        """经既有 ``EvaluationLoopService`` 逐 attempt 执行；单 case 失败不中断 batch。"""
        run, attempts = await self._persistence.create_run(
            project_id=project_id,
            dataset=plan.dataset,
            suite=plan.suite,
            cases=dict(plan.cases),
            target=plan.target_ref,
            timeout=plan.timeout,
        )
        loop = EvaluationLoopService(self._persistence, target_resolver, evaluator_resolver)
        records: list[SecurityAttemptRecord] = []
        for attempt in attempts:
            try:
                outcome = await loop.execute_attempt(
                    project_id,
                    attempt.attempt_id,
                    plan.cases[attempt.case_ref],
                    lease=lease,
                    worker_ref=worker_ref,
                    task_ref=task_ref,
                )
            except Exception as exc:  # noqa: BLE001 - batch isolation 要求逐 case 隔离失败
                records.append(
                    SecurityAttemptRecord(
                        case_ref=attempt.case_ref,
                        attempt_id=attempt.attempt_id,
                        outcome=RUNNER_INFRASTRUCTURE_FAILURE,
                        error_type=type(exc).__name__,
                    )
                )
                continue
            records.append(
                SecurityAttemptRecord(
                    case_ref=attempt.case_ref,
                    attempt_id=attempt.attempt_id,
                    outcome=outcome.value,
                )
            )
        return SecurityRunExecutionReceipt(run_id=run.run_id, records=tuple(records))

    async def list_results(self, project_id: UUID, run_id: UUID) -> tuple[EvaluationResult, ...]:
        """读取 persisted Results（Summary / Projection 的唯一 verdict 来源）。"""
        return await self._persistence.list_results(project_id, run_id)

    async def build_summary(self, project_id: UUID, plan: SecurityRunPlan, run_id: UUID) -> SecurityRunSummary:
        """从 persisted Results + plan facts 构建 Security Summary（无重跑）。"""
        results = await self._persistence.list_results(project_id, run_id)
        return build_security_run_summary(
            run_id=run_id,
            dataset_id=plan.dataset.dataset_id,
            dataset_version=plan.dataset.version,
            suite_id=plan.suite.suite_id,
            suite_version=plan.suite.version,
            execution_target_id=plan.target_ref.target_id,
            execution_target_kind=plan.target_ref.target_kind,
            facts=plan.facts,
            results=results,
            unmapped=plan.unmapped,
        )


def replace_input_payload(test_case: TestCaseVersion, payload: Mapping[str, object]) -> TestCaseVersion:
    """以映射后的 wire payload 构造执行用 TestCase snapshot（metadata / GT 保持不变）。"""
    return replace(test_case, input_payload=dict(payload))


__all__ = [
    "GAP_AGENT_MESSAGE_BOUNDARY",
    "GAP_NON_QUERY_STIMULUS",
    "GAP_REFERENCE_DATA_DELIVERY",
    "GAP_RETRIEVED_CONTEXT_INJECTION",
    "GAP_TARGET_KIND_UNSUPPORTED",
    "GAP_TOOL_OUTPUT_BOUNDARY",
    "GAP_WIRE_PAYLOAD_INVALID",
    "LOCALAGENT_HTTP_TARGET_KIND",
    "RUNNER_INFRASTRUCTURE_FAILURE",
    "SecurityAttemptRecord",
    "SecurityRegressionPlanError",
    "SecurityRegressionService",
    "SecurityRunExecutionReceipt",
    "SecurityRunPlan",
    "collect_not_evaluated_reasons",
    "map_security_case_input",
]
