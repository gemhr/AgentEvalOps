"""Phase4 最小 FeatureRiskReviewModelPort adapter。

复用 LiteLLM 结构化输出与现有 provider 凭据校验；只做一次调用，不实现 Model Router /
Registry / Fallback Chain。解析、验证失败统一转为 ``FeatureRiskReviewModelOutputError``，
不静默修复、不猜缺字段、不制造证据。

Provider 兼容：按 ``litellm.get_llm_provider`` 的 provider identity 在调用前决定
structured-output transport 模式，**不做任何“异常后自动重试 fallback”**：

- ``json_schema``（native）：OpenAI-style ``response_format``，当前已支持 provider 保持不变。
- ``json_text``：DeepSeek 等不支持 native ``json_schema`` 的 provider 不传 ``response_format``，
  改为在 request 中加入最小 JSON-only instruction 并嵌入由 Pydantic schema 生成的权威 JSON schema。
  最终输出仍经 ``parse_structured_model_output`` 严格 parse + validate。

降级 transport constraint，不降低 business contract：invalid JSON / schema violation 仍抛出
``FeatureRiskReviewModelOutputError``。
"""

# ruff: noqa: D415

from __future__ import annotations

import json
from typing import TypeVar

import litellm
from pydantic import BaseModel, ValidationError

from app.core.feature_risk_review.errors import FeatureRiskReviewModelOutputError
from app.infrastructure.llm.providers import (
    check_provider_credentials,
    provider_key_from_model,
    resolve_model_string,
)
from app.registry.settings import settings

T = TypeVar("T", bound=BaseModel)

_DEFAULT_SYSTEM_PROMPT = (
    "You are a Kubernetes feature risk review analyst. Return only valid JSON matching the "
    "required schema, with no extra fields and no text outside the JSON object."
)

# 已知不支持 OpenAI-style native json_schema response_format 的 provider family。
# 最小显式集合，非大规模 model-name registry。
_JSON_TEXT_PROVIDERS = frozenset({"deepseek"})


def parse_structured_model_output(raw: str, response_schema: type[T]) -> T:
    """去掉 Markdown fence、解析 JSON 并严格验证为 ``response_schema``。

    失败时抛出 ``FeatureRiskReviewModelOutputError``；不做修复或补字段。
    """
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeatureRiskReviewModelOutputError("model output is not valid JSON") from exc
    try:
        return response_schema.model_validate(payload)
    except ValidationError as exc:
        fields = ", ".join(error["loc"][0] for error in exc.errors() if error["loc"]) or "unknown"
        raise FeatureRiskReviewModelOutputError(f"model output failed schema validation: {fields}") from exc


def _structured_output_mode(model: str) -> str:
    """在调用前决定 structured-output transport 模式；不触发任何请求。

    复用 LiteLLM 的 provider identity（``get_llm_provider`` 第二项）。DeepSeek family 走
    ``json_text``；其余（native json-schema provider）走 ``json_schema``。无法识别时
    安全回退为 native ``json_schema``。
    """
    try:
        provider = litellm.get_llm_provider(model)[1]
    except Exception:  # noqa: BLE001 - 无法识别 provider 时保持 native 默认
        provider = None
    if provider in _JSON_TEXT_PROVIDERS:
        return "json_text"
    return "json_schema"


def _json_text_instruction(response_schema: type[T]) -> str:
    """为 json_text 模式生成最小 JSON-only instruction，并嵌入权威 JSON schema。

    直接复用 ``response_schema.model_json_schema()``，避免维护第二套手写 schema。
    """
    schema_json = json.dumps(
        response_schema.model_json_schema(), ensure_ascii=False, separators=(",", ":")
    )
    return (
        "\n\nSTRUCTURED OUTPUT REQUIREMENT:\n"
        "Return ONLY valid JSON. Do not use Markdown code fences. Do not include any text or "
        "explanation outside the JSON object. The JSON object MUST conform to exactly this schema:\n"
        f"{schema_json}"
    )


class LiteLLMFeatureRiskReviewModelPort:
    """通过 LiteLLM 调用模型并返回 typed/validated 结果的最小 adapter。"""

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._system_prompt = system_prompt

    async def generate(self, *, prompt: str, response_schema: type[T]) -> T:
        """调用模型并返回 typed/validated 的 structured result。

        只发一次请求：provider capability 在调用前决定，无异常后自动重试 fallback。
        """
        model = resolve_model_string(self._model or settings.EVAL_LLM_MODEL)
        provider_key = provider_key_from_model(model)
        if provider_key:
            available, message = check_provider_credentials(provider_key)
            if not available:
                raise FeatureRiskReviewModelOutputError(message)

        mode = _structured_output_mode(model)
        user_content = prompt + (_json_text_instruction(response_schema) if mode == "json_text" else "")
        request = {
            "model": model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self._temperature,
        }
        if mode == "json_schema":
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "strict": True,
                    "schema": response_schema.model_json_schema(),
                },
            }
        try:
            response = await litellm.acompletion(**request)
        except Exception as exc:  # noqa: BLE001 - adapter 边界把任意模型调用错误转为安全错误
            raise FeatureRiskReviewModelOutputError(f"model call failed: {type(exc).__name__}") from exc
        message = response.choices[0].message
        raw = getattr(message, "content", None)
        if not isinstance(raw, str):
            raise FeatureRiskReviewModelOutputError("model returned empty or non-text output")
        return parse_structured_model_output(raw, response_schema)


__all__ = ["LiteLLMFeatureRiskReviewModelPort", "parse_structured_model_output"]