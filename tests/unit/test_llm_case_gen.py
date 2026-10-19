"""`LlmCaseGenerator` —— 两个 `generate` 契约之间的翻译层。

对应决策：`03/模块详细设计/TestCaseGen.md` TG1（维度确定）、TG4（逐 Operation）、
§2.5（结构化输出契约）、`04-数据模型.md` DM4（失败可重试）。

## 这批测试存在的理由

M2 的第一版把 `QwenProvider` **直接**当 `case_generator` 传进图——而它的
`generate(*, prompt, schema)` 与图要的 `generate(operation, dimensions)`
同名不同契约。后果是每次调用抛 `TypeError`，然后被"部分失败不阻塞全局"
吞掉：Run 跑完、报告出来、那个接口一条用例都没有。

**这个 bug 在单测与集成测试里都是隐形的**，所以下面每一条都在钉死
"这一层真的把两边接上了"这个具体的形状，而不只是"类存在"。
"""

from __future__ import annotations

import json

import pytest

from testflow.agents.llm_case_gen import LlmCaseGenerator
from testflow.agents.test_case_gen import RuleBasedGenerator
from testflow.domain.cases import DimensionSpec, GeneratedCase, GenerationResult
from testflow.llm.client import LLMError, ModelT


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """重试退避不真的睡——否则每个失败用例都要多花 1.5 秒。

    只替换 `llm_case_gen` 看到的那个 `_sleep`，不碰全局 `asyncio.sleep`
    （后者会污染 anyio/uvicorn 的定时器，见 `test_llm_retry` 的教训）。
    """

    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr("testflow.agents.llm_case_gen._sleep", _noop)


def _dimension(name: str = "value_valid", **extra: object) -> DimensionSpec:
    return DimensionSpec(
        dimension=name,
        category="normal",
        expected_status=[200],
        **extra,  # type: ignore[arg-type]
    )


class _StubProvider:
    """一个**只实现 `LLMClient` 协议**的桩。记下收到的 prompt 与 schema。"""

    model_name = "stub-model"

    def __init__(self, *, cases: list[GeneratedCase] | None = None, error: Exception | None = None):
        self.prompts: list[str] = []
        self.schemas: list[type] = []
        self._cases = cases
        self._error = error

    async def generate(self, *, prompt: str, schema: type[ModelT]) -> ModelT:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self._error is not None:
            raise self._error
        assert self._cases is not None
        return schema.model_validate(GenerationResult(cases=self._cases))  # type: ignore[return-value]


def _case(dimension: str) -> GeneratedCase:
    return GeneratedCase(
        dimension=dimension,
        name=f"{dimension} 的用例",
        category="normal",
        assertions=[{"type": "status_code", "expected": 200, "derived_from": "spec"}],  # type: ignore[list-item]
        derived_from="spec",
    )


class _Operation:
    """`Operation` 行的最小替身。**只带提示词需要的字段。**

    刻意不用真 ORM 对象：那样这个测试就得起数据库，而它测的
    （提示词怎么组装、契约怎么校验）与数据库毫无关系。
    """

    def __init__(self, **overrides: object) -> None:
        self.method = "POST"
        self.path = "/echo"
        self.parameters_schema = None
        self.request_body_schema = {"content": {"application/json": {"schema": {"type": "object"}}}}
        self.responses_schema = {"200": {"description": "ok"}}
        for key, value in overrides.items():
            setattr(self, key, value)
        # 这些字段**不该**出现在提示词里（浪费 token + 主键没有外发的理由），
        # 放进替身是为了能断言它们没被带出去。
        self.id = "should-not-leak-id"
        self.spec_id = "should-not-leak-spec"
        self.is_destructive = False


class TestThePromptIsBuiltCorrectly:
    async def test_it_sends_the_markers_the_template_defines(self) -> None:
        """★ `OPERATION:` / `DIMENSIONS:` 两行是**假 Provider 与模板之间的契约**。

        真实模型靠读提示词理解，而假 Provider 靠这两行解析。行没了或格式变了，
        假 Provider 会抛 `LLMError`——那还算好的。真正危险的是它**静默解析出
        空维度**，于是离线环境下"生成成功但零用例"。
        """
        provider = _StubProvider(cases=[_case("value_valid")])
        generator = LlmCaseGenerator(provider)

        await generator.generate(_Operation(), [_dimension()])

        prompt = provider.prompts[0]
        assert "OPERATION: POST /echo" in prompt
        assert "DIMENSIONS: " in prompt
        line = next(line for line in prompt.splitlines() if line.startswith("DIMENSIONS: "))
        parsed = json.loads(line[len("DIMENSIONS: ") :])
        assert [item["dimension"] for item in parsed] == ["value_valid"]

    async def test_the_operation_travels_with_its_schemas(self) -> None:
        """接口结构必须整块进去——少了它模型只能凭路径名猜。"""
        provider = _StubProvider(cases=[_case("value_valid")])
        await LlmCaseGenerator(provider).generate(_Operation(), [_dimension()])

        prompt = provider.prompts[0]
        assert "request_body_schema" in prompt
        assert "responses_schema" in prompt

    async def test_internal_identifiers_do_not_leak_into_the_prompt(self) -> None:
        """主键与 `spec_id` 对"构造一个合法请求"毫无帮助。

        它们白占 token（而 token 数决定计费档），并且是一次没有必要的
        内部标识外发。
        """
        provider = _StubProvider(cases=[_case("value_valid")])
        await LlmCaseGenerator(provider).generate(_Operation(), [_dimension()])

        assert "should-not-leak-id" not in provider.prompts[0]
        assert "should-not-leak-spec" not in provider.prompts[0]

    async def test_the_schema_is_generation_result(self) -> None:
        """输出契约是 `GenerationResult`——**不是** `GeneratedCase`。

        一次调用要产出该 Operation 的**全部**维度（TG4：逐 Operation）。
        传错 Schema 的话模型只会回来一条用例，而"少了几条"会被维度契约
        校验抓住——那是第二道防线，第一道是这个断言。
        """
        provider = _StubProvider(cases=[_case("value_valid")])
        await LlmCaseGenerator(provider).generate(_Operation(), [_dimension()])

        assert provider.schemas == [GenerationResult]


class TestTheDimensionContract:
    """TG1：维度由阶段 A 确定，模型只填取值。"""

    async def test_a_missing_dimension_is_a_hard_failure(self) -> None:
        """★ **少了维度绝不能当成成功。**

        放行的话，那个维度没有被测到，而报告里的覆盖率**看起来是满的**
        （分母是清单，分子是"清单里出现过几条"）——一条漏测伪装成
        "生成成功"，且没有任何东西提示。
        """
        provider = _StubProvider(cases=[_case("value_valid")])
        generator = LlmCaseGenerator(provider)

        with pytest.raises(LLMError, match="缺少维度"):
            await generator.generate(_Operation(), [_dimension(), _dimension("boundary:name")])

    async def test_an_invented_dimension_is_a_hard_failure(self) -> None:
        """★ 模型自己发明维度同样不行。

        放行的话它会被执行、被写进报告，而它不在派生的清单里——
        **覆盖率的分子分母从此不再对应同一套定义**，比值也就失去了意义。
        """
        provider = _StubProvider(cases=[_case("value_valid"), _case("model_invented")])
        generator = LlmCaseGenerator(provider)

        with pytest.raises(LLMError, match="多出未请求的维度"):
            await generator.generate(_Operation(), [_dimension()])

    async def test_the_error_says_what_was_requested_and_what_came_back(self) -> None:
        """错误信息要能直接回答"差在哪"，而不是只说"不一致"。

        排查这个问题的人手上只有日志——他需要知道请求了几个、回来了几个、
        具体差哪些。
        """
        provider = _StubProvider(cases=[_case("value_valid")])
        with pytest.raises(LLMError) as caught:
            await LlmCaseGenerator(provider).generate(
                _Operation(), [_dimension(), _dimension("boundary:name")]
            )

        message = str(caught.value)
        assert "POST /echo" in message
        assert "请求了 2 个维度，返回了 1 条用例" in message
        assert "boundary:name" in message

    async def test_an_exact_match_passes(self) -> None:
        """正向的一侧：维度完全对上就正常返回（否则上面几条可能"永远报错"）。"""
        cases = [_case("value_valid"), _case("boundary:name")]
        provider = _StubProvider(cases=cases)
        result = await LlmCaseGenerator(provider).generate(
            _Operation(), [_dimension(), _dimension("boundary:name")]
        )

        assert [c.dimension for c in result.cases] == ["value_valid", "boundary:name"]


class TestFailurePropagates:
    async def test_a_provider_error_is_not_swallowed(self) -> None:
        """Provider 的失败必须原样抛出去。

        吞掉的话 Operation 会被标成 `succeeded` 而没有任何用例——
        比"失败"糟得多：失败可以重试（DM4），而"成功但空"不会。
        """
        provider = _StubProvider(error=LLMError("模型挂了"))
        with pytest.raises(LLMError, match="模型挂了"):
            await LlmCaseGenerator(provider).generate(_Operation(), [_dimension()])


class TestModelName:
    def test_it_delegates_to_the_provider(self) -> None:
        """`cases.llm_model` 供 L3 Eval 比对档位——答错会让结论整个失真。"""
        assert LlmCaseGenerator(_StubProvider()).model_name == "stub-model"

    def test_a_provider_without_one_is_none_not_a_crash(self) -> None:
        class _Anonymous:
            async def generate(
                self, *, prompt: str, schema: type[ModelT]
            ) -> ModelT:  # pragma: no cover
                raise NotImplementedError

        assert LlmCaseGenerator(_Anonymous()).model_name is None


class TestItIsNotARuleGenerator:
    def test_isinstance_check_drives_generated_by(self) -> None:
        """★ `_persist` 用 `isinstance(active, RuleBasedGenerator)` 决定
        `generated_by`。

        LLM 生成的用例被记成 `rule` 的话，`09` §5.3 的 L3 Eval
        （"生成质量是否随模型档位下降"）就失去了区分能力——
        而它只会表现为"统计出来的 LLM 用例数是 0"，没有任何报错。
        """
        assert not isinstance(LlmCaseGenerator(_StubProvider()), RuleBasedGenerator)
