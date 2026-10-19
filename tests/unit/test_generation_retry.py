"""L1/L2 不合格时的重试：次数、退避、反馈、不重试的失败类型。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from testflow.agents import llm_case_gen
from testflow.agents.llm_case_gen import LlmCaseGenerator
from testflow.agents.prompts import SYSTEM_PREFIX
from testflow.domain.cases import DimensionSpec, GeneratedCase, GenerationResult
from testflow.llm.client import LLMContentError, LLMError


class _ScriptedProvider:
    model_name = "stub-model"

    def __init__(self, items: list[object]) -> None:
        self._items = list(items)
        self.prompts: list[str] = []

    async def generate(self, *, prompt: str, schema):
        self.prompts.append(prompt)
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _operation(responses: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        method="POST",
        path="/echo",
        parameters_schema=None,
        request_body_schema=None,
        responses_schema=responses or {"200": {}},
    )


def _dimension(name: str = "value_valid") -> DimensionSpec:
    return DimensionSpec(dimension=name, category="normal", expected_status=[200], rationale="r")


def _case(dim: str = "value_valid") -> GeneratedCase:
    return GeneratedCase(
        dimension=dim,
        name=dim,
        category="normal",
        assertions=[{"type": "status_code", "expected": 200, "derived_from": "spec"}],
        derived_from="spec",
    )


def _result(*cases: GeneratedCase) -> GenerationResult:
    return GenerationResult(cases=list(cases))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(llm_case_gen, "_sleep", _noop)


async def test_contract_failure_is_retried_and_can_succeed() -> None:
    provider = _ScriptedProvider([_result(), _result(_case())])
    generator = LlmCaseGenerator(provider)

    result = await generator.generate(_operation(), [_dimension()])

    assert [case.dimension for case in result.cases] == ["value_valid"]
    assert len(provider.prompts) == 2


async def test_gives_up_after_two_retries() -> None:
    # 返回了一条"别的维度"而不是空列表——这样反馈里会出现"缺少维度"，
    # 而不是笼统的"cases 是空的"。
    provider = _ScriptedProvider(
        [_result(_case("other")), _result(_case("other")), _result(_case("other"))]
    )
    generator = LlmCaseGenerator(provider)

    with pytest.raises(LLMError, match="缺少维度"):
        await generator.generate(_operation(), [_dimension()])

    assert len(provider.prompts) == 3


async def test_content_error_is_retried() -> None:
    provider = _ScriptedProvider([LLMContentError("输出不合 Schema"), _result(_case())])
    generator = LlmCaseGenerator(provider)

    result = await generator.generate(_operation(), [_dimension()])

    assert result.cases
    assert len(provider.prompts) == 2
    assert "无法解析" in provider.prompts[1]


async def test_transport_error_is_not_retried_by_the_generator() -> None:
    """传输失败已经在 Provider 内部重试过；再套一层会让耗时变成乘积。"""
    provider = _ScriptedProvider([LLMError("连接被拒")])
    generator = LlmCaseGenerator(provider)

    with pytest.raises(LLMError, match="连接被拒"):
        await generator.generate(_operation(), [_dimension()])

    assert len(provider.prompts) == 1


async def test_backoff_is_exponential(monkeypatch) -> None:
    delays: list[float] = []

    async def _record(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(llm_case_gen, "_sleep", _record)
    provider = _ScriptedProvider([_result(), _result(), _result()])

    with pytest.raises(LLMError):
        await LlmCaseGenerator(provider).generate(_operation(), [_dimension()])

    assert delays == [0.5, 1.0]


async def test_feedback_is_appended_to_the_volatile_layer_only() -> None:
    provider = _ScriptedProvider([_result(_case("other")), _result(_case())])

    await LlmCaseGenerator(provider).generate(_operation(), [_dimension()])

    first, second = provider.prompts
    assert first[: len(SYSTEM_PREFIX)] == second[: len(SYSTEM_PREFIX)]
    assert "缺少维度" in second
    assert "缺少维度" not in second[: len(SYSTEM_PREFIX)]


async def test_declared_4xx_without_coverage_triggers_a_retry() -> None:
    provider = _ScriptedProvider(
        [
            _result(_case()),
            _result(
                GeneratedCase(
                    dimension="value_valid",
                    name="409",
                    category="normal",
                    assertions=[{"type": "status_code", "expected": 409, "derived_from": "spec"}],
                    derived_from="spec",
                )
            ),
        ]
    )

    result = await LlmCaseGenerator(provider).generate(
        _operation({"200": {}, "409": {}}), [_dimension()]
    )

    assert len(provider.prompts) == 2
    assert "409" in provider.prompts[1]
    assert result.cases[0].assertions[0].expected == 409
