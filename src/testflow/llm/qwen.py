"""通义千问（阿里云百炼）适配器。

对应决策：`02-架构设计.md` §5（LLM 集成）、ADR-0005、`08-工程规范.md` §2.3、D2。

## ⚠️ 两条容易被忽略的硬约束

### ① `http_client` **必须注入**，不能让它自己建

`openai` SDK 在构造时不传 `http_client` 就会**自己建一个 httpx 客户端**——
那个客户端不经过 Guardrail，于是 LLM 流量绕开了 ADR-0002 的"唯一出站通道"。

**这条约束静态检查不到**：`llm/` 本来就允许 import LLM SDK，
`import openai` 和 `OpenAI(http_client=..., ...)` 在 AST 层面都是合法语句。
所以 `08` §2.3 要求一条**行为测试**：构造适配器 → 发一次真实调用 →
断言请求出现在 Guardrail 的审计里。那条测试属于 M2（见路线图）。

**但注入这件事在 M1 就做了**——因为不做的代价是"从第一天起就有一条
不受校验的出站路径"，而那种洞一旦存在，后面很难发现。

### ② `http_client` 的类型是 `Any`，**不是** `httpx.AsyncClient`

`openai` 3.x 依赖的是 **`httpx2`**（一个独立于 `httpx` 的包，
内含自己的 `httpcore2`）。写成 `httpx.AsyncClient` 会类型不符；
而写成 `httpx2.AsyncClient` 就得 import `httpx2`——
架构规则禁止 `llm/` 里出现任何 HTTP 客户端。

`Any` 在这里不是偷懒：**这个模块确实不需要知道那个对象的类型**，
它只负责原样传递。类型信息在 `guardrail/async_client.py` 那边是完整的。
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from testflow.config import Settings
from testflow.llm.client import LLMError, ModelT
from testflow.observability.logging import get_logger

__all__ = ["QwenProvider"]

logger = get_logger(__name__)


class QwenProvider:
    """百炼的 OpenAI 兼容模式。

    ## 为什么走"兼容模式"而不是百炼原生 SDK

    OpenAI 兼容模式让我们能用生态里成熟的工具（`.parse()` 的结构化输出、
    Pydantic 集成），而百炼原生 SDK 的那些便利是有对应物的。
    代价是百炼特有参数（`enable_thinking` 等）要通过 `extra_body` 传——
    而那正好被隔离在这个文件里。
    """

    def __init__(self, settings: Settings, *, http_client: Any, model: str | None = None) -> None:
        self.model_name = model or settings.llm_model
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            # ★ 注入而不是让它自己建——见模块文档字符串的第 ① 条。
            http_client=http_client,
            timeout=settings.llm_timeout_s,
            # SDK 内建重试设为 0：重试策略由我们控制（`llm_max_retries`），
            # 两层重试叠加会让最坏情况下的耗时变成乘积，
            # 而那个数会超过编排层的锁 TTL——于是锁在推进中途过期。
            max_retries=0,
        )
        self._settings = settings

    async def generate(self, *, prompt: str, schema: type[ModelT]) -> ModelT:
        """按 Schema 生成结构化输出。

        Raises:
            LLMError: 调用失败，或输出无法解析成给定的模型。
        """
        try:
            completion = await self._client.chat.completions.parse(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format=schema,
                # 百炼特有参数走 `extra_body`——**它们的名字只出现在这一行**。
                extra_body={
                    # 思考模式与流式必须同时开启（`02` §5.2 的限制表）。
                    "enable_thinking": True,
                    "result_format": "message",
                },
            )
        except Exception as exc:  # noqa: BLE001 —— SDK 的异常面很宽，统一归成 LLMError
            # **不把 `exc` 的字符串直接往上带**：它可能含请求 URL（含 api_key
            # 的 query）或响应体的片段。归成一个类型 + 类名。
            raise LLMError(f"LLM 调用失败（{type(exc).__name__}）") from exc

        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise LLMError(
                "模型返回了无法解析为结构化对象的内容。"
                "常见原因：提示词里出现了与 Schema 冲突的字段描述。"
            )
        if not isinstance(parsed, schema):
            # `parse()` 理论上保证类型，但**这个假设值得显式检查一次**：
            # SDK 升级、兼容模式的实现差异都可能让它不成立，
            # 而后果是在很远的地方才炸（某个字段访问不到）。
            raise LLMError(f"模型返回的类型不是 {schema.__name__}，而是 {type(parsed).__name__}")
        return parsed


def parse_or_raise(raw: str, schema: type[ModelT]) -> ModelT:
    """把一段 JSON 文本解析成模型。**供假 Provider 与离线测试使用。**

    单独抽出来是因为"离线跑通整条链路"需要它：假 Provider 产出的是
    文本，而真正的 Provider 直接产出对象——两者在**校验**这一步上应当一致，
    否则离线测试通过的代码可能在真实调用下失败。
    """
    try:
        return schema.model_validate_json(raw)
    except ValidationError as exc:
        raise LLMError(f"输出不符合 {schema.__name__}：{exc}") from exc
