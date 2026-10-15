"""确定性的假 Provider。**CI 与离线 demo 用。**

对应决策：`llm/client.py`（窄接口）、`11-开发路线图.md` M1（验收）。

## 为什么它和真 Provider 是**并列的实现**，而不是 mock

mocking 真实实现（`unittest.mock` 打桩 `AsyncOpenAI`）的问题有两个：

1. **它测的是我有没有打对桩**——桩的形状错了，测试全绿而真实调用失败
2. **它把"Provider 是一个可替换的实现"这件事藏起来了**，于是接口设计得不好
   也不会被发现

而写一个真的实现了 `LLMClient` 协议的东西，**同时在给接口做压力测试**：
如果假实现写起来很别扭，说明接口设计有问题。这是"窄接口"这个设计的第一次收益。

## 它产出什么

按维度**确定性地**取值——同样输入永远得到同样输出。这不是"质量低"，
而是**这类测试要的正是确定性**：CI 需要可复现，而"生成质量"由 M5 的
Golden Set 单独评估（那才需要真实模型）。

**它绝不假装能处理它处理不了的维度。** 遇到阶段 A 派生的复杂维度
（`boundary` / `constraint_violation`）时它会明确报错——一个"总是产出
一条 `value_valid`"的假 Provider 会让覆盖率相关的断言全部失去意义。
"""

from __future__ import annotations

import json
from typing import Any

from testflow.domain.cases import DimensionSpec, GeneratedCase, GenerationResult
from testflow.llm.client import LLMError, ModelT
from testflow.observability.logging import get_logger

__all__ = ["FakeLLMProvider"]

logger = get_logger(__name__)


class FakeLLMProvider:
    """确定性的 `LLMClient` 实现。"""

    model_name = "fake-llm-0"

    def __init__(self, *, fail_for: frozenset[str] = frozenset()) -> None:
        """
        Args:
            fail_for: 对这些 `operation.path` 抛出 `LLMError`——
                用来验证"部分失败不阻塞全局"（一个接口的生成失败，
                不该让整份 Spec 的生成全部作废）。
        """
        self._fail_for = fail_for
        self.calls: list[str] = []

    async def generate(self, *, prompt: str, schema: type[ModelT]) -> ModelT:
        """按 Schema 生成确定性的输出。

        **只支持 `GenerationResult`。** 其他 Schema 直接报错——
        一个"什么都返回点东西"的假 Provider 会让"Schema 用错了"这类 bug
        在离线测试里消失，而它只在真实调用时才暴露。
        """
        if schema is not GenerationResult:
            raise LLMError(
                f"假 Provider 只实现了 GenerationResult，收到 {schema.__name__}。"
                "若新增了别的结构化输出场景，请显式扩展这里——"
                "静默返回一个空对象会让那个场景在离线测试里永远通过。"
            )

        operation_path = _extract_operation_path(prompt)
        self.calls.append(operation_path)
        if operation_path in self._fail_for:
            raise LLMError(f"（假 Provider 按要求让 {operation_path} 失败）")

        dimensions = _extract_dimensions(prompt)
        cases = [_case_for(op_path=operation_path, spec=spec) for spec in dimensions]
        if not cases:
            raise LLMError(
                "提示词里没有解析出任何维度。**这是提示词模板的问题**，"
                "不是模型的问题——而它必须报错：一个产出零用例的"
                "「成功」会让覆盖率静默变成 0% 而没人发现。"
            )
        return schema.model_validate(
            GenerationResult(cases=cases, operation_notes="由假 Provider 确定性生成")
        )


def _extract_operation_path(prompt: str) -> str:
    """从提示词里取出当前 Operation 的路径。

    真实 Provider 不需要做这件事（它直接读提示词理解）。假 Provider 需要，
    因为它得"按维度确定性地产出"——而维度是按 Operation 派生的。

    **用标记而不是猜**：提示词模板里写了一个明确的 `OPERATION: /path` 行。
    靠正则去猜提示词里的路径会让这个假实现和模板格式耦合得极其脆弱。
    """
    for line in prompt.splitlines():
        if line.startswith("OPERATION: "):
            return line[len("OPERATION: ") :].strip()
    raise LLMError("提示词里缺少 `OPERATION: <path>` 标记行——见 agents/prompts.py")


def _extract_dimensions(prompt: str) -> list[DimensionSpec]:
    """从提示词里取出阶段 A 派生的维度清单。

    同样用明确的标记：`DIMENSIONS:` 后面跟一行 JSON 数组。
    **这是假 Provider 与提示词模板之间的契约**——两边都写死，
    改了模板就得改这里，而测试会立刻发现。
    """
    marker = "DIMENSIONS: "
    for line in prompt.splitlines():
        if line.startswith(marker):
            raw: Any = json.loads(line[len(marker) :])
            return [DimensionSpec.model_validate(item) for item in raw]
    raise LLMError("提示词里缺少 `DIMENSIONS:` 标记行——见 agents/prompts.py")


def _case_for(*, op_path: str, spec: DimensionSpec) -> GeneratedCase:
    """按维度确定性地造一条用例。

    取值的规则是**平凡但合法**的：`value_valid` 传空、其他维度不产出
    （由上面的 `raise` 保证）。这样"假 Provider 产出的用例能跑通整条链路"
    这件事是真的——它产出的东西是合法输入，不是占位符。
    """
    if spec.dimension != "value_valid":
        raise LLMError(
            f"假 Provider 不支持维度 {spec.dimension!r}。"
            "阶段 A 派生出的复杂维度需要真实模型——"
            "让它产出一个形状类似的假用例，会让边界测试在离线环境里"
            "「通过」而实际上什么都没验。"
        )
    expected = spec.expected_status[0] if spec.expected_status else 200
    return GeneratedCase(
        dimension=spec.dimension,
        name=f"{op_path} 正常请求（假 Provider）",
        category="normal",
        assertions=[
            {"type": "status_code", "expected": expected, "derived_from": "spec"}  # type: ignore[list-item]
        ],
        requires_auth=True,
        derived_from="spec",
    )
