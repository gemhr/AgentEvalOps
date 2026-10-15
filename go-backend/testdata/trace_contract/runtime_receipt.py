"""使用现有 Runtime 执行一次公开合成任务；Trace 由真实 writers 自然产生。"""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile

from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
from core.agent_router import AgentRouter
from core.llm_engine import ScriptedEvaluationLLMEngine
from core.memory_manager import MemoryManager
from core.runtime import CoordinatedRuntimeFactory, InMemorySpanRecorder
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.agent_evalops_trace_exporter import AgentEvalOpsTraceExporter
from core.runtime.model_selection import ModelCostProfile, ModelProfile, ModelProfileId
from core.runtime.trace_export_dispatcher import TraceExportDispatcher
from tests._runtime_assembly_fixtures import make_services


class RecordingScriptedModel(ScriptedEvaluationLLMEngine):
    """只记录脚本模型调用数，不替换 Router、Coordinator 或 Trace writer。"""

    def __init__(self):
        self.calls = 0

    def generate(self, messages, temperature=0.7, max_tokens=1024, enable_thinking=None):
        self.calls += 1
        yield from super().generate(
            messages, temperature=temperature, max_tokens=max_tokens,
            enable_thinking=enable_thinking,
        )


async def main():
    registry = compile_agent_catalog(
        AgentRegistrationBundle(registrations=()),
        builtin_registrations=tuple(
            DEFAULT_AGENT_REGISTRY.resolve(agent_id)
            for agent_id in DEFAULT_AGENT_REGISTRY.agent_ids
        ),
    ).agent_registry
    exporter = AgentEvalOpsTraceExporter(
        base_url=os.environ["LOCALAGENT_TRACE_API_BASE_URL"],
        api_key=os.environ["LOCALAGENT_TRACE_API_KEY"],
        project_id=os.environ["LOCALAGENT_TRACE_PROJECT_ID"],
        connect_timeout_seconds=1.0,
        total_deadline_seconds=5.0,
    )
    dispatcher = TraceExportDispatcher(exporter=exporter, queue_capacity=32)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    model = RecordingScriptedModel()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        memory.append_exchange_atomic(
            "core_router", "direct", "MARKER_MEMORY_PRIVATE", "synthetic history",
            run_id="synthetic-history",
        )
        profile = ModelProfile(
            ModelProfileId.LOCAL_FAST, 8192, 640,
            False, True, True, True, 1, 1,
            ModelCostProfile(ModelProfileId.LOCAL_FAST, False), False, "scripted",
        )
        router = AgentRouter(
            model, memory, agent_registry=registry, span_recorder=recorder,
            model_profiles=(profile,),
        )
        # 复用现有基础设施 seam；业务 Router、模型调用与执行/投影链均为真实代码。
        services = replace(
            make_services(span_recorder=recorder, snapshot_enabled=False),
            model_invocation_router=router.model_invocation_router,
            tool_execution_service=router.tool_execution_service,
            retrieval_execution_service=router.retrieval_execution_service,
            trace_export_dispatcher=dispatcher,
        )
        scope = await CoordinatedRuntimeFactory(
            router, services, agent_registry=registry, event_channel_capacity=128,
        ).create_run_scope(
            "core_router",
            "release checklist; MARKER_PROMPT_PRIVATE; "
            "synthetic tool arguments: MARKER_TOOL_ARGUMENTS_PRIVATE",
            persist=True, timeout_seconds=30,
        )

        async def consume():
            async for _event in scope.event_channel:
                pass

        consumer = asyncio.create_task(consume())
        try:
            result = await scope.execute()
            assert result.status.value == "SUCCEEDED", result
            assert result.stop_reason.value == "COMPLETED", result
            assert model.calls >= 4  # planner、两项 specialist、synthesis
            history = memory.get_chat_history("core_router", ascending=True)
            assert len(history) == 4
            assert history[0]["content"] == "MARKER_MEMORY_PRIVATE"
            assert "MARKER_PROMPT_PRIVATE" in history[-2]["content"]
        finally:
            await scope.close()
            await consumer
            recorder.close()
            assert dispatcher.flush(10.0)
            assert dispatcher.close(10.0)
        health = dispatcher.health()
        assert health.failed_total == 0 and health.sent_total > 0, health
        print(json.dumps({
            "run_id": scope.run_id,
            "trace_id": scope.run_context.trace_id,
            "model_calls": model.calls,
            "sent_count": health.sent_total,
        }))


if __name__ == "__main__":
    asyncio.run(main())
