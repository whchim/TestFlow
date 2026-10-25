"""把 LLM Provider 适配成 `CaseGenerator`——阶段 B 的真正实现。

对应决策：`03-模块详细设计/TestCaseGen.md` §2.5（结构化输出契约）、§6（提示词）、
TG1（维度确定，取值生成）、TG4（逐 Operation）、`06-Agent编排.md` §4。

## 为什么必须有这一层：两个 `generate` 同名不同契约

| | 输入 | 输出 |
| :-- | :-- | :-- |
| `llm.client.LLMClient` | 一段完整的提示词 | 按给定 Schema 的结构化对象 |
| `agents.test_case_gen.CaseGenerator` | 一个 `Operation` + 维度清单 | 该 Operation 的用例清单 |

**方法名一样，签名完全不同**（前者关键字参数、后者位置参数）。

第一版直接把 `QwenProvider` 传给了 `case_generator`——于是每次调用都抛
`TypeError: QwenProvider.generate() takes 1 positional argument but 3 were given`，
而它被 `generate_for_operations` 的"部分失败不阻塞全局"接住了：

    Run 照样跑到 awaiting_review，报告照样生成，只是**那个接口一条用例都没有**。

这类"被兜底逻辑吞掉的接线错误"有三个特点，合起来让它极难发现：
它不报错、它不影响流程、而报告里的"未覆盖的接口"只是一处不起眼的空白。
单测抓不到（单测自己拼 generator），集成测试也抓不到（测试注入的是规则生成器）。
**只有把服务真的跑起来、然后去看数据**才发现得了。

所以这一层不是"多一层抽象"，它是**两个契约之间的显式翻译**——
而翻译放在哪里、翻成什么，是唯一能被测试和审查的地方。

## 只依赖窄接口，不认识任何厂商

构造参数是 `LLMClient`（那个只有一个方法的 Protocol），而不是 `QwenProvider`。
所以这个模块**一行 SDK 都不 import**，换 Provider 时它不用动——
`02` D7 要的正是这件事。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from testflow.agents.prompts import build_generation_prompt
from testflow.agents.spec_context import SpecContext
from testflow.agents.validation import feedback_text, validate_generation
from testflow.domain.cases import DimensionSpec, GenerationResult
from testflow.llm.client import LLMClient, LLMContentError, LLMError
from testflow.observability.logging import get_logger
from testflow.storage.models import Operation

__all__ = ["LlmCaseGenerator", "materialize_request_bodies", "repair_free_form_keys"]

logger = get_logger(__name__)

#: 提示词里需要看到的 Operation 字段。
#: 与 `agents.prompts._trim` 的 `keep` 一致——那边决定"发给模型什么"，
#: 这里决定"取出什么给它"，两处不一致会导致提示词里少一整块内容
#: （模型仍然会答，只是答得不全——一种不会报错的退化）。
_OPERATION_FIELDS = (
    "method",
    "path",
    "parameters_schema",
    "request_body_schema",
    "responses_schema",
)

#: L1/L2 不合格时的重试次数（`TestCaseGen.md` §5.3：上限 2 次）。
#:
#: **只重试"内容不合格"**。传输失败已经在 Provider 里重试过；
#: 超时更是刻意不再重试（重试会让耗时可乘，见 `qwen.py` 的说明）。
_MAX_CONTENT_RETRIES = 2
_RETRY_BACKOFF_S = 0.5

#: 单独抽出来是为了让测试能替换它而不碰全局的 `asyncio.sleep`
#: （替换全局会污染 anyio/uvicorn 的定时器，见 `test_llm_retry` 的教训）。
_sleep = asyncio.sleep


class LlmCaseGenerator:
    """阶段 B：逐 Operation 调一次模型，产出该 Operation 的全部用例。"""

    def __init__(
        self,
        llm: LLMClient,
        *,
        max_content_retries: int = _MAX_CONTENT_RETRIES,
        backoff_s: float = _RETRY_BACKOFF_S,
    ) -> None:
        self._llm = llm
        # 重试参数可注入：基准脚本要测"**首次**通过率"，那必须把重试关掉
        # （否则看到的永远是重试后的结果，模型第一次答得好不好被掩盖了）。
        self._max_retries = max(max_content_retries, 0)
        self._backoff_s = backoff_s

    @property
    def model_name(self) -> str | None:
        """落库用的模型标识（`cases.llm_model`，供 L3 Eval 比对档位）。

        从底层 Provider 取，而不是自己存一份配置：**同一件事有两个来源，
        迟早会不一致**——而"这次生成用的是哪个模型"答错的影响是
        L3 的档位对比结论整个失真。
        """
        name = getattr(self._llm, "model_name", None)
        return name if isinstance(name, str) else None

    async def generate(
        self,
        operation: Operation,
        dimensions: Sequence[DimensionSpec],
        *,
        spec_context: SpecContext | None = None,
    ) -> GenerationResult:
        """为**一个** Operation 生成全部维度的用例。

        ## TG4：逐 Operation 调用

        不把多个 Operation 合并成一次调用：Qwen3.5-Plus 按单次输入长度
        阶梯计费，超过 128K 单价跳 5 倍（`02` §5.1）。逐 Operation 天然把
        单次输入限制在几 KB，代价是更多次调用——而那正好是便宜的那一档。

        Raises:
            LLMError: 调用失败，或重试 2 次后仍未通过 L1/L2。
        """
        feedback: str | None = None
        last_issues = None
        last_content_error: LLMContentError | None = None
        last_case_count = 0

        for attempt in range(self._max_retries + 1):
            if attempt:
                # 指数退避：0.5s → 1.0s。给提供商一点喘息，也避免连续三次
                # 都在同一个瞬时问题（限流边缘）上撞墙。
                await _sleep(self._backoff_s * (2 ** (attempt - 1)))

            prompt = build_generation_prompt(
                operation=_operation_payload(operation),
                dimensions=list(dimensions),
                spec_context=spec_context,
                feedback=feedback,
            )
            try:
                result = await self._llm.generate(prompt=prompt, schema=GenerationResult)
                # 请求体走"JSON 字符串"通道再解析：直接让模型输出自由格式
                # 对象时，Qwen 3.7 Flash 会退化成带前导冒号的键（实测）。
                result = materialize_request_bodies(result)
            except LLMContentError as exc:
                last_content_error = exc
                if attempt == self._max_retries:
                    raise
                feedback = (
                    "上一次输出无法解析，或 request_body_json 不是合法 JSON。"
                    "请严格按给定的 JSON Schema 输出；request_body_json 必须是"
                    "一个合法的 JSON 对象字符串（键和字符串值都用双引号）。"
                )
                continue

            # 兼容旧路径 / 假 Provider：它们仍然直接填 request_body。
            result = repair_free_form_keys(result)

            last_case_count = len(result.cases)
            last_issues = validate_generation(
                responses_schema=operation.responses_schema,
                requested=dimensions,
                result=result,
            )
            if last_issues.ok:
                return result
            feedback = feedback_text(last_issues)

        detail = (
            feedback_text(last_issues)
            if last_issues is not None
            else (str(last_content_error) if last_content_error else "未知原因")
        )
        raise LLMError(
            f"生成结果未通过 L1/L2 校验（{operation.method} {operation.path}），"
            f"已重试 {self._max_retries} 次：\n{detail}\n"
            f"请求了 {len(dimensions)} 个维度，返回了 {last_case_count} 条用例。"
            "维度清单由阶段 A 从 Schema 派生，是**确定性的**——模型只负责填取值（TG1）。"
        )


def materialize_request_bodies(result: GenerationResult) -> GenerationResult:
    """把模型写的 `request_body_json` 解析成 `request_body`。

    ## 为什么请求体要走"字符串"通道

    `request_body` 是 `dict[str, Any]`，JSON Schema 只能声明"对象"，
    无法声明键名。实测 Qwen 3.7 Flash 在这种自由格式对象上会退化成
    **不合法的 JSON 片段**（键名前多出冒号、甚至出现空键），
    SDK 的宽松解析会把它变成 `{': email': ...}` 或 `{}`。

    改成让模型输出一个 **JSON 字符串**后，结构化输出只需要处理字符串，
    不再碰自由格式对象；平台再用 `json.loads` 解析并校验。
    解析失败会抛 `LLMContentError`，走既有的"带反馈重试"路径——
    **不会把一条坏请求静默落库**。

    Raises:
        LLMContentError: 字符串不是合法 JSON，或解析结果不是对象。
    """
    changed = False
    cases = []
    for case in result.cases:
        raw = case.request_body_json
        if raw is None or not str(raw).strip():
            cases.append(case)
            continue

        try:
            body = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LLMContentError(
                f"request_body_json 不是合法 JSON（{case.dimension}）：{exc}"
            ) from exc
        if not isinstance(body, dict):
            raise LLMContentError(
                f"request_body_json 解析后必须是 JSON 对象，收到 {type(body).__name__}"
                f"（{case.dimension}）"
            )

        cases.append(case.model_copy(update={"request_body": body, "request_body_json": None}))
        changed = True

    return result.model_copy(update={"cases": cases}) if changed else result


def repair_free_form_keys(result: GenerationResult) -> GenerationResult:
    """修复自由格式对象里**键名前导冒号**这一种退化。

    ## 为什么需要它

    `request_body` / `request_query` / `request_headers` 的类型是
    `dict[str, Any]`——JSON Schema 只能声明"这是一个对象"，无法声明每个键。
    Qwen 3.7 Flash 在长提示词 + 大量 `key:value` 形态的维度名（
    `required_missing:email`）之后，偶尔会把对象键写成 `": email"`，
    甚至产生空键 `": "`。**原始响应本身就是不合法的 JSON 片段**，
    而 SDK 的宽松解析把它变成了带冒号的键。

    这是**序列化层的退化，不是测试意图错误**：把键名清洗回 `email` 之后，
    用例的取值与断言通常仍然是对的。所以在这里做一次确定性修复，
    而不是让它把整条用例判成"生成失败"。

    ## 边界

    * 只清洗**键名**，不改值
    * 递归处理嵌套对象（`billing` / `shipping` 这类）
    * 清洗后为空的键直接丢弃——它承载不了任何信息
    * 记录修复条数，便于观察这种退化的频率；**不静默**
    """

    repaired = 0
    cases = []
    for case in result.cases:
        body = _clean_mapping(case.request_body)
        query = _clean_mapping(case.request_query)
        headers = _clean_mapping(case.request_headers)
        if (body, query, headers) != (
            case.request_body,
            case.request_query,
            case.request_headers,
        ):
            repaired += 1
            case = case.model_copy(
                update={"request_body": body, "request_query": query, "request_headers": headers}
            )
        cases.append(case)

    if repaired:
        logger.warning(
            "修复了 %d 条用例的自由格式键名（模型输出带前导冒号）；"
            "这是序列化层退化，不是测试意图错误",
            repaired,
        )
        return result.model_copy(update={"cases": cases})
    return result


def _clean_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            while key.startswith(":"):
                key = key[1:].strip()
            if not key:
                continue
            cleaned[key] = _clean_mapping(raw_value)
        return cleaned
    if isinstance(value, list):
        return [_clean_mapping(item) for item in value]
    return value


def _operation_payload(operation: Operation) -> dict[str, Any]:
    """ORM 行 → 提示词要的那个 dict。

    **只带该带的字段**：`id`、`spec_id`、`created_at` 这些对"构造一个合法
    请求"毫无帮助，却会白占 token——而 token 数直接决定落在哪个计费档。
    另外把主键发给外部模型也毫无必要。
    """
    return {name: getattr(operation, name) for name in _OPERATION_FIELDS}
