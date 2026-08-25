"""WP2 model adapter focused tests: DeepSeek json_text compatibility + native json_schema regression.

Uses mocked LiteLLM responses (no real model call). Verifies:
- DeepSeek family does NOT send native json_schema response_format, but sends JSON-only instruction.
- Native providers keep json_schema response_format.
- Plain JSON still strict-validated to a typed result.
- Invalid JSON / schema violation fail with FeatureRiskReviewModelOutputError.
- Exactly one provider request per agent invocation (no hidden fallback retry).
"""

# ruff: noqa: D415

from __future__ import annotations

import litellm
import pytest
from pydantic import BaseModel

from app.adapters.feature_risk_review.model import LiteLLMFeatureRiskReviewModelPort
from app.core.feature_risk_review.errors import FeatureRiskReviewModelOutputError


class _SampleSchema(BaseModel):
    name: str
    score: int


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(_FakeMessage(content))]


async def test_deepseek_path_sends_no_json_schema_and_includes_json_instruction(monkeypatch) -> None:
    captured: dict = {}
    calls: list = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        captured.update(kwargs)
        return _FakeResponse('{"name": "plugin", "score": 3}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    port = LiteLLMFeatureRiskReviewModelPort(model="deepseek/deepseek-chat")
    result = await port.generate(prompt="analyze", response_schema=_SampleSchema)

    assert "response_format" not in captured
    user_content = captured["messages"][1]["content"]
    assert "STRUCTURED OUTPUT REQUIREMENT" in user_content
    assert "Return ONLY valid JSON" in user_content
    assert "name" in user_content  # schema field constraint embedded from Pydantic schema
    assert result.name == "plugin"
    assert result.score == 3
    assert len(calls) == 1


async def test_native_provider_keeps_json_schema_response_format(monkeypatch) -> None:
    captured: dict = {}
    calls: list = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        captured.update(kwargs)
        return _FakeResponse('{"name": "plugin", "score": 3}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(
        "app.adapters.feature_risk_review.model.check_provider_credentials", lambda _k: (True, "ok")
    )
    port = LiteLLMFeatureRiskReviewModelPort(model="openai/gpt-4o-mini")
    result = await port.generate(prompt="analyze", response_schema=_SampleSchema)

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert "STRUCTURED OUTPUT REQUIREMENT" not in captured["messages"][1]["content"]
    assert result.name == "plugin"
    assert len(calls) == 1


async def test_deepseek_valid_json_returns_typed_result(monkeypatch) -> None:
    async def fake_acompletion(**kwargs):
        return _FakeResponse('{"name": "plugin", "score": 5}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    port = LiteLLMFeatureRiskReviewModelPort(model="deepseek/deepseek-chat")
    result = await port.generate(prompt="analyze", response_schema=_SampleSchema)
    assert isinstance(result, _SampleSchema)
    assert result.score == 5


async def test_deepseek_invalid_json_fails(monkeypatch) -> None:
    async def fake_acompletion(**kwargs):
        return _FakeResponse("this is not json")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    port = LiteLLMFeatureRiskReviewModelPort(model="deepseek/deepseek-chat")
    with pytest.raises(FeatureRiskReviewModelOutputError):
        await port.generate(prompt="analyze", response_schema=_SampleSchema)


async def test_deepseek_schema_violation_fails(monkeypatch) -> None:
    async def fake_acompletion(**kwargs):
        return _FakeResponse('{"name": "plugin"}')  # missing required 'score'

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    port = LiteLLMFeatureRiskReviewModelPort(model="deepseek/deepseek-chat")
    with pytest.raises(FeatureRiskReviewModelOutputError):
        await port.generate(prompt="analyze", response_schema=_SampleSchema)


async def test_deepseek_wrong_type_fails(monkeypatch) -> None:
    async def fake_acompletion(**kwargs):
        return _FakeResponse('{"name": "plugin", "score": "not-an-int"}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    port = LiteLLMFeatureRiskReviewModelPort(model="deepseek/deepseek-chat")
    with pytest.raises(FeatureRiskReviewModelOutputError):
        await port.generate(prompt="analyze", response_schema=_SampleSchema)


async def test_no_hidden_second_request_for_native_provider(monkeypatch) -> None:
    calls: list = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _FakeResponse('{"name": "plugin", "score": 3}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(
        "app.adapters.feature_risk_review.model.check_provider_credentials", lambda _k: (True, "ok")
    )
    port = LiteLLMFeatureRiskReviewModelPort(model="openai/gpt-4o-mini")
    await port.generate(prompt="analyze", response_schema=_SampleSchema)
    assert len(calls) == 1