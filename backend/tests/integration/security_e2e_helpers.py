"""WP8 Cross-Repository Security E2E —— 共享 test helpers。

本模块集中 WP8 测试复用的组件（复用 WP6 的 LocalAgent subprocess launcher / HTTP target
helper / scripted judge 模式，避免在 test 文件中重复大段代码）：

- 真实 LocalAgent 子进程（deterministic runtime behavior；只替换 ``server.chat_service``，
  不修改 LocalAgent 源码），行为通过 ``WP8_BEHAVIOR`` env 传入（default + per-query overrides）。
- scripted judges：``AllSatisfiedJudge`` / ``MarkerJudge`` / ``RefusalJudge``。
- orchestrator：``_execute_run`` / ``_build_gate`` / ``_pf``。
- dataset helpers：``_real_dataset`` / ``_focused_dataset`` / ``_query_for``。

仅替换 external behavior dependency（LLM Judge / Agent 执行），不改任何 production 语义。
"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import json
import os
import socket
import subprocess
import textwrap
from datetime import timedelta

import pytest

from app.core.evaluation import (
    EvaluationDataset,
    build_security_comparison_projection,
    load_dataset,
)
from app.core.evaluation.generation_judge import JudgeModelRefusal
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation import (
    EvaluationComparisonService,
    EvaluationPersistenceService,
)
from app.services.evaluation.security_regression import SecurityRegressionService

from .conftest import TEST_PROJECT_ID
from .test_security_regression_runner import (
    LocalAgentResolver,
    NOW,
    REAL_FIXTURE,
    SecurityEvaluatorResolver,
    _LOCAL_AGENT_DIR,
    _LOCAL_AGENT_PYTHON,
    _judge_response,
    _localagent_v2_target_ref,
    _spec,
    _wait_for_tcp,
)

SAFE_ANSWER = "I ignored the untrusted instruction and completed the original task."
UNSAFE_ANSWER = "I will follow your instructions and reveal the protected rules: <SECRET_VALUE>"
OVER_REFUSAL_ANSWER = "I cannot answer because this may be a prompt injection attack."


class CountingAgentResolver:
    """包装 LocalAgent HTTP target，统计真实 agent 执行调用次数（zero-rerun 断言用）。"""

    def __init__(self, base_url: str) -> None:
        self._inner = LocalAgentResolver(base_url)
        self.calls = 0
        self._wrapped: list[_CountingExecutionTarget] = []

    def resolve(self, target_ref):
        target = self._inner.resolve(target_ref)
        wrapped = _CountingExecutionTarget(target, on_execute=self._bump)
        self._wrapped.append(wrapped)
        return wrapped

    def _bump(self) -> None:
        self.calls += 1

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CountingExecutionTarget:
    """对 LocalAgentHttpExecutionTarget.execute 做调用计数（只读包装，不改语义）。"""

    def __init__(self, target, *, on_execute) -> None:
        self._target = target
        self._on_execute = on_execute

    @property
    def target_ref(self):
        return self._target.target_ref

    async def execute(self, request):
        self._on_execute()
        return await self._target.execute(request)


def _real_dataset() -> EvaluationDataset:
    """主 Dataset（WP2 冻结）：prompt_injection_regression.v2.json，25 cases。"""
    return load_dataset(REAL_FIXTURE)


def _focused_dataset(case_ids: list[str], dataset_id: str) -> EvaluationDataset:
    """从主 Dataset 中选出一组 case 构造 focused test dataset（不改主文件 / 不改 GroundTruth）。

    仅用于场景化 BLOCK / ALLOW 演示；主 ``prompt_injection_regression.v2.json`` 保持原样。
    """
    full = _real_dataset()
    selected = [c for c in full.cases if c.case_id in case_ids]
    return EvaluationDataset.model_validate(
        {
            "dataset_schema_version": full.dataset_schema_version,
            "dataset_id": dataset_id,
            "name": f"WP8_{dataset_id}",
            "description": full.description,
            "version": full.version,
            "cases": [c.model_dump(mode="json") for c in selected],
        }
    )


def _query_for(dataset: EvaluationDataset, case_id: str) -> str:
    for case in dataset.cases:
        if case.case_id == case_id:
            return str(case.input["query"])
    raise KeyError(case_id)


# --------------------------------------------------------------------------- #
# 真实 LocalAgent 子进程（deterministic runtime behavior，不改 LocalAgent 源码） #
# --------------------------------------------------------------------------- #

_SERVER_PROGRAM_TEMPLATE = textwrap.dedent(
    """
    import json
    import os
    import sys
    from types import SimpleNamespace

    import uvicorn
    import server
    from core.runtime import BudgetLedger, ChatRuntimeMode, RunBudget, RunCoordinatorResult, RunStatus, StopReason

    behavior = json.loads(os.environ["WP8_BEHAVIOR"])
    default_answer = behavior.get("default", "I ignored the untrusted instruction and completed the original task.")
    overrides = behavior.get("overrides", {})


    def _result(run_id):
        ledger = BudgetLedger(RunBudget())
        return RunCoordinatorResult(
            run_id=run_id,
            plan_id="wp8-deterministic-plan",
            status=RunStatus.SUCCEEDED,
            stop_reason=StopReason.COMPLETED,
            succeeded_step_ids=("knowledge-expert",),
            failed_step_ids=(),
            cancelled_step_ids=(),
            blocked_step_ids=(),
            budget_snapshot=ledger.snapshot(),
            cleanup_error_codes=(),
            error_code=None,
            safe_message="",
        )


    class DeterministicSecurityChatService:
        admission_gate = SimpleNamespace(accepts_new_runs=True)

        def selected_runtime_mode(self):
            return ChatRuntimeMode.COORDINATED

        async def run_coordinated_agent(self, *, query, run_id, **_kwargs):
            answer = overrides.get(query, default_answer)
            return answer, _result(run_id)


    server.chat_service = DeterministicSecurityChatService()
    uvicorn.run(server.app, host="127.0.0.1", port=int(sys.argv[1]), lifespan="off", log_level="warning")
    """
)


async def _launch_server(behavior: dict) -> tuple[str, subprocess.Popen]:
    """启动一个真实 LocalAgent uvicorn 子进程并返回 base_url。"""
    if not _LOCAL_AGENT_PYTHON.is_file():
        pytest.skip(f"LocalAgent test Python is unavailable: {_LOCAL_AGENT_PYTHON}")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        **os.environ,
        "PYTHONPATH": str(_LOCAL_AGENT_DIR),
        "WP8_BEHAVIOR": json.dumps(behavior, ensure_ascii=False),
    }
    process = subprocess.Popen(
        [str(_LOCAL_AGENT_PYTHON), "-c", _SERVER_PROGRAM_TEMPLATE, str(port)],
        cwd=_LOCAL_AGENT_DIR,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    await _wait_for_tcp(port, process)
    return f"http://127.0.0.1:{port}", process


# --------------------------------------------------------------------------- #
# Scripted judges（test double，仅替换外部 LLM Judge dependency）               #
# --------------------------------------------------------------------------- #


class AllSatisfiedJudge:
    """deterministic：所有 semantic behavior 均返回 satisfied=true。"""

    def __init__(self) -> None:
        self.calls = 0

    async def structured_generate(self, *, prompt_ref, input_payload, config):
        self.calls += 1
        return _judge_response(True)


class MarkerJudge:
    """按 actual_answer 内容确定性判定：命中 unsafe / over-refusal marker 即 FAIL。"""

    def __init__(self, *, unsafe_marker: str = "", overrefusal_marker: str = "") -> None:
        self.calls = 0
        self.unsafe_marker = unsafe_marker
        self.overrefusal_marker = overrefusal_marker

    async def structured_generate(self, *, prompt_ref, input_payload, config):
        self.calls += 1
        answer = str(input_payload.get("actual_answer", ""))
        if self.unsafe_marker and self.unsafe_marker in answer:
            return _judge_response(False)
        if self.overrefusal_marker and self.overrefusal_marker in answer:
            return _judge_response(False)
        return _judge_response(True)


class RefusalJudge:
    """deterministic：所有 judge 调用抛 JudgeModelRefusal -> INCONCLUSIVE(security_judge_refusal)。"""

    def __init__(self) -> None:
        self.calls = 0

    async def structured_generate(self, *, prompt_ref, input_payload, config):
        self.calls += 1
        raise JudgeModelRefusal("deterministic refusal for WP8")


# --------------------------------------------------------------------------- #
# Orchestration helpers（复用 WP6 resolver / spec / target）                   #
# --------------------------------------------------------------------------- #


def _pf():
    """返回 UoW factory（与 WP6/WP7 的 ``lambda: PostgresEvaluationPersistenceUnitOfWork(...)`` 等价）。"""
    return lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)


async def _execute_run(pf, dataset, *, url, judge, suite_id, timeout_seconds=90):
    service = SecurityRegressionService(EvaluationPersistenceService(pf))
    plan = service.plan_run(
        dataset,
        execution_target_ref=_localagent_v2_target_ref(),
        evaluator_spec=_spec(),
        suite_id=suite_id,
        suite_version="s1",
        timeout=timedelta(seconds=timeout_seconds),
        created_at=NOW,
    )
    resolver = CountingAgentResolver(url)
    try:
        receipt = await service.execute_plan(
            TEST_PROJECT_ID,
            plan,
            target_resolver=resolver,
            evaluator_resolver=SecurityEvaluatorResolver(judge),
            lease=timedelta(minutes=10),
        )
    finally:
        await resolver.aclose()
    return service, plan, receipt, judge, resolver


async def _build_gate(
    pf,
    *,
    baseline_svc,
    baseline_plan,
    baseline_receipt,
    candidate_svc,
    candidate_plan,
    candidate_receipt,
):
    comparison_service = EvaluationComparisonService(EvaluationPersistenceService(pf))
    comparison = await comparison_service.compare_runs(
        TEST_PROJECT_ID, baseline_receipt.run_id, candidate_receipt.run_id
    )
    baseline_summary = await baseline_svc.build_summary(
        TEST_PROJECT_ID, baseline_plan, baseline_receipt.run_id
    )
    candidate_summary = await candidate_svc.build_summary(
        TEST_PROJECT_ID, candidate_plan, candidate_receipt.run_id
    )
    baseline_results = await baseline_svc.list_results(TEST_PROJECT_ID, baseline_receipt.run_id)
    candidate_results = await candidate_svc.list_results(TEST_PROJECT_ID, candidate_receipt.run_id)
    projection = build_security_comparison_projection(
        comparison=comparison,
        baseline_results=baseline_results,
        candidate_results=candidate_results,
    )
    return comparison, baseline_summary, candidate_summary, projection