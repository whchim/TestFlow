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

import asyncio
import time
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from testflow.config import Settings
from testflow.guardrail.errors import GuardrailTimeout, GuardrailTransportError
from testflow.llm.client import LLMContentError, LLMError, ModelT, UsageRecord, UsageSink
from testflow.observability.logging import get_logger
from testflow.observability.tracing import (
    current_llm_purpose,
    current_run_id,
    current_trace_id,
)

__all__ = ["QwenProvider"]

logger = get_logger(__name__)

#: 首次重试的等待秒数。第 n 次重试等 `_RETRY_BACKOFF_BASE_S * 2**(n-1)`。
#:
#: 不用配置项：它只影响等待时长，不影响任何安全性质
#: （`config.py` 的判断标准是"改了这个值会不会让安全性变差"）。
#: 一个能调这个值的旋钮，收益是"省几秒"，代价是多一个没人会去盘点的配置。
_RETRY_BACKOFF_BASE_S = 0.5


class QwenProvider:
    """百炼的 OpenAI 兼容模式。

    ## 为什么走"兼容模式"而不是百炼原生 SDK

    OpenAI 兼容模式让我们能用生态里成熟的工具（`.parse()` 的结构化输出、
    Pydantic 集成），而百炼原生 SDK 的那些便利是有对应物的。
    代价是百炼特有参数（`enable_thinking` 等）要通过 `extra_body` 传——
    而那正好被隔离在这个文件里。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Any,
        model: str | None = None,
        on_usage: UsageSink | None = None,
    ) -> None:
        self.model_name = model or settings.llm_model
        self._on_usage = on_usage
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
        """按 Schema 生成结构化输出，**传输类失败按 `llm_max_retries` 重试**。

        ## 重试什么、不重试什么

        只重试**传输层**的失败（连接被拒、DNS 失败、TLS 失败）——
        它们是"这一次没成功"，换一次往往就好了。

        三类失败**刻意不重试**：

        * **超时**（`GuardrailTimeout`）。超时是唯一"重试会让代价变乘积"的失败：
          三次 120 秒超时 = 360 秒，而这**超过编排层的锁 TTL**——
          锁在推进中途过期，另一个推进者就会同时跑同一个节点。
          这正是 SDK 内建重试被设成 0 的原因（见 `__init__`），
          把它加回来等于把那个洞重新打开。
        * **输出不能解析**。模型给出的内容不合 Schema 是**内容**问题，
          重试只是在同一件不确定的事上再赌一次。
        * **其他 SDK 异常**（鉴权、参数错误、限流）。这些重试没用或不该由我们重试
          ——限流该等多久是服务端说了算，猜一个退避时长只会把配额耗得更快。

        Raises:
            LLMError: 重试耗尽，或输出无法解析成给定的模型。
        """
        attempts = max(self._settings.llm_max_retries, 0) + 1
        last_transport_error: GuardrailTransportError | None = None

        for attempt in range(attempts):
            if attempt:
                delay = _RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "LLM 调用第 %d/%d 次重试（等待 %.1fs）：%s",
                    attempt,
                    attempts - 1,
                    delay,
                    last_transport_error,
                )
                await asyncio.sleep(delay)
            try:
                return await self._generate_once(prompt=prompt, schema=schema, attempt=attempt + 1)
            except GuardrailTimeout as exc:
                raise LLMError(f"LLM 调用超时（{type(exc).__name__}）") from exc
            except GuardrailTransportError as exc:
                last_transport_error = exc

        raise LLMError(
            f"LLM 调用连续 {attempts} 次传输失败（{type(last_transport_error).__name__}）"
        ) from last_transport_error

    async def _generate_once(
        self, *, prompt: str, schema: type[ModelT], attempt: int = 1
    ) -> ModelT:
        """一次调用。**不含重试**——重试在 `generate` 里，那个循环要能看到全部尝试。"""
        started = time.perf_counter()
        params: dict[str, Any] = {}
        # 采样参数只在**显式配置**时传。传一个猜测值会让 M5 的 Golden Set
        # 失去"提供商默认值"这个基线，而且不同模型的合法区间不一样。
        for name in ("llm_temperature", "llm_top_p", "llm_seed", "llm_max_tokens"):
            value = getattr(self._settings, name)
            if value is not None:
                params[name.removeprefix("llm_")] = value

        try:
            completion = await self._client.chat.completions.parse(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format=schema,
                # 百炼特有参数走 `extra_body`——**它们的名字只出现在这一行**。
                extra_body={
                    # 思考模式按用途开关（`02` §5.3）：生成开、归因关。
                    "enable_thinking": self._thinking_enabled(),
                    "result_format": "message",
                },
                **params,
            )
        except (GuardrailTimeout, GuardrailTransportError):
            # ★ **不能在这里被下面那个 `except Exception` 吞掉。**
            # Guardrail 的异常必须原样交给 `generate` 的重试循环：
            # 一旦被归成笼统的 `LLMError`，重试策略就再也看不到它们了
            # ——而"从不重试"是看不出来的，只会表现为偶发失败率偏高。
            #
            # 这也解释了为什么这条 except 必须写在前面：`GuardrailError`
            # 是 `Exception` 的子类，顺序反了就被吞了。
            raise
        except Exception as exc:  # noqa: BLE001 —— SDK 的异常面很宽，统一归成 LLMError
            # 有些 SDK 异常**带着已经拿到的 completion**（例如
            # `LengthFinishReasonError`——输出被 max_tokens 截断）。那种情况下
            # 调用已经计费，必须把用量记下来；否则成本会系统性偏低。
            billed = getattr(exc, "completion", None)
            if billed is not None:
                self._emit_usage(
                    billed,
                    schema=schema,
                    attempt=attempt,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            # **不把 `exc` 的字符串直接往上带**：它可能含请求 URL（含 api_key
            # 的 query）或响应体的片段。归成一个类型 + 类名。
            raise LLMError(f"LLM 调用失败（{type(exc).__name__}）") from exc

        # ★ **在解析校验之前记录**：能走到这里说明提供商已经返回 200
        # （已经计费）。哪怕内容不可用，成本也已经发生。
        self._emit_usage(
            completion,
            schema=schema,
            attempt=attempt,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise LLMContentError(
                "模型返回了无法解析为结构化对象的内容。"
                "常见原因：提示词里出现了与 Schema 冲突的字段描述。"
            )
        if not isinstance(parsed, schema):
            # `parse()` 理论上保证类型，但**这个假设值得显式检查一次**：
            # SDK 升级、兼容模式的实现差异都可能让它不成立，
            # 而后果是在很远的地方才炸（某个字段访问不到）。
            raise LLMContentError(
                f"模型返回的类型不是 {schema.__name__}，而是 {type(parsed).__name__}"
            )
        return parsed

    def _thinking_enabled(self) -> bool:
        """按调用用途决定思考模式（`02` §5.3：生成开、归因关）。

        归因只是从 11 个封闭类别里选一个，思考只会增加延迟与 token；
        生成则确实需要理解 schema 与维度，思考有质量收益。
        """
        if current_llm_purpose() == "attribution":
            return self._settings.llm_enable_thinking_attribution
        return self._settings.llm_enable_thinking_generation

    def _emit_usage(
        self,
        completion: Any,
        *,
        schema: type[ModelT],
        attempt: int,
        elapsed_ms: int,
    ) -> None:
        """把一次调用的用量交给 `on_usage`。

        **在解析/类型校验之前调用**——模型返回 200 就已经计费，
        哪怕内容不能解析。只在成功路径记录会让成本系统性偏低。
        """
        if self._on_usage is None:
            return

        usage = getattr(completion, "usage", None)
        if usage is None:
            # 兼容模式/中转层可能不回 usage。**不猜**：记零并告警，
            # 报告里的 `usage_missing_calls` 会让这件事可见。
            logger.warning("LLM 响应没有 usage 字段，本次调用按 0 token 记录")
        details = getattr(usage, "prompt_tokens_details", None) if usage else None

        record = UsageRecord(
            run_id=current_run_id(),
            trace_id=current_trace_id(),
            purpose=current_llm_purpose(),
            # 用响应里的 `model`：中转/别名可能把请求的模型名换掉，
            # 而账单按**实际返回的**那个算。
            model=getattr(completion, "model", None) or self.model_name,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cached_tokens=int(getattr(details, "cached_tokens", 0) or 0),
            cache_write_tokens=int(getattr(details, "cache_write_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
            attempt=attempt,
            elapsed_ms=elapsed_ms,
        )
        try:
            self._on_usage(record)
        except Exception:  # noqa: BLE001 —— 指标写入不能影响一次已付费的调用
            logger.exception("用量写入失败 model=%s purpose=%s", record.model, record.purpose)


def parse_or_raise(raw: str, schema: type[ModelT]) -> ModelT:
    """把一段 JSON 文本解析成模型。**供假 Provider 与离线测试使用。**

    单独抽出来是因为"离线跑通整条链路"需要它：假 Provider 产出的是
    文本，而真正的 Provider 直接产出对象——两者在**校验**这一步上应当一致，
    否则离线测试通过的代码可能在真实调用下失败。
    """
    try:
        return schema.model_validate_json(raw)
    except ValidationError as exc:
        raise LLMContentError(f"输出不符合 {schema.__name__}：{exc}") from exc
