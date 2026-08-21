"""Evaluation Domain 的 ports-and-adapters 协议。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.immutable import FrozenJsonValue, freeze_json
from app.core.evaluation.references import VersionRef
from app.core.evaluation.results import EvaluationResultDraft


@dataclass(frozen=True, slots=True)
class JudgeModelResponse:
    """单次 Judge 调用的结构化 payload 与实际请求模型 provenance。"""

    payload: FrozenJsonValue
    model_ref: VersionRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json(self.payload))
        if not isinstance(self.model_ref, VersionRef):
            raise TypeError("model_ref must be VersionRef")


class JudgeModelPort(Protocol):
    """LLM Judge 所需的最小结构化生成能力。"""

    async def structured_generate(
        self,
        *,
        prompt_ref: VersionRef,
        input_payload: FrozenJsonValue,
        config: FrozenJsonValue,
    ) -> JudgeModelResponse:
        """根据版本化 prompt 和 opaque input 生成结构化 JSON。"""
        ...


class Evaluator(Protocol):
    """由 deterministic 或 LLM Judge adapter 实现的评价端口。"""

    async def evaluate(
        self,
        evaluation_input: EvaluationInput,
        context: EvaluatorContext,
    ) -> EvaluationResultDraft:
        """评价统一输入并返回尚未绑定完整 provenance 的 draft。"""
        ...
