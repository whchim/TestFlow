"""N2 `generate_cases` —— 用例生成。

对应决策：`03-模块详细设计/TestCaseGen.md` §2（两阶段）、TG4/TG5、`04` DM4。

## 幂等语义：**跳过，不是重放**

这是本项目最容易搞混的一点。两个生成类节点的语义**完全不同**：

| 节点 | 语义 | 为什么 |
| :--- | :--- | :--- |
| N1 `parse_spec` | **真幂等** | 纯算法，同输入必得同输出，重跑无害 |
| N2 `generate_cases` | **已完成则跳过** | 调 LLM，输出非确定 |

对 N2 来说，"重放"意味着重跑一遍 LLM、拿到一批**不同的**用例，
然后把用户已经确认过的那些**静默替换掉**。那不是幂等，那是数据丢失。

所以"是否已完成"必须持久化——那就是 `run_operations.gen_status` 存在的
唯一理由（DM4）。

## M1 的范围

路线图明确：**阶段 A + 阶段 B 的简化版（不做重试与覆盖率校验）**。

* L1（结构校验）由 Pydantic 天然承担——`GenerationResult` 会拒绝结构不对的输出
* L1 的重试、L2 覆盖率校验与重试 → **M4**
* TG5（无信息接口走纯规则）在 M1 **实现**，因为它决定了 demo 能不能离线跑

## TG4：逐 Operation 调用

不能把多个 Operation 合并成一次调用：Qwen3.5-Plus 按**单次请求的输入长度**
阶梯计费，超过 128K 单价跳 5 倍（`02` §5.1）。逐 Operation 天然把单次输入
限制在几 KB，代价是更多次调用——而那正好是便宜的那一档。
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from testflow.agents.dimensions import derive_dimensions, is_pure_rule_operation, json_body_schema
from testflow.agents.spec_context import SpecContext, build_spec_context
from testflow.domain.assertions import StatusCodeAssertion
from testflow.domain.cases import CaseCategory, DimensionSpec, GeneratedCase, GenerationResult
from testflow.observability.logging import get_logger
from testflow.observability.tracing import bind_llm_purpose, reset_llm_purpose
from testflow.storage.models import Case, Operation
from testflow.storage.repositories import cases as cases_repo
from testflow.storage.repositories import run_operations as ro_repo

__all__ = ["CaseGenerator", "GenerateOutcome", "generate_for_operations"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GeneratedCasePayload:
    """一条待落库的用例（**不含 ID**——ID 由数据库生成）。"""

    name: str
    category: CaseCategory
    dimension: str
    request_method: str
    request_path: str
    request_query: dict | None
    request_headers: dict | None
    request_body: dict | None
    assertions: list[dict]
    requires_auth: bool
    generated_by: str
    confidence: str | None
    llm_model: str | None


class CaseGenerator(Protocol):
    """阶段 B 的接口。**M1 只需要一个实现：规则生成器。**

    LLM 适配器（`llm/qwen.py`）会是另一个实现。把接口留在这里而不是
    让 N2 直接依赖 LLM 客户端，是为了让**整条链路能离线跑通**——
    这正是 CI 与 demo 用不同 Provider 的落点。
    """

    async def generate(
        self,
        operation: Operation,
        dimensions: Sequence[DimensionSpec],
        *,
        spec_context: SpecContext | None = None,
    ) -> GenerationResult: ...


@dataclass
class GenerateOutcome:
    """一次生成的结果摘要。供图节点与测试使用。"""

    generated_case_ids: list[uuid.UUID]
    failed_operations: list[uuid.UUID]
    skipped_operations: list[uuid.UUID]


class RuleBasedGenerator:
    """**纯规则**的生成器。处理 TG5 的"无信息接口"，也是 CI 与离线 demo 的实现。

    它只能派出 `value_valid` 一条用例——因为那是"无信息接口"唯一有意义的维度。
    对更复杂的接口它会**明确拒绝**，而不是产出一个看起来像样、实际不完整的结果。
    """

    def __init__(self, *, base_path: str = "") -> None:
        self._base_path = base_path

    async def generate(
        self,
        operation: Operation,
        dimensions: Sequence[DimensionSpec],
        *,
        spec_context: SpecContext | None = None,
    ) -> GenerationResult:
        # 规则生成器不用 Spec 全局信息：它只处理"无信息接口"，
        # 那些接口没有鉴权也没有 4xx。参数保留是为了协议一致。
        del spec_context
        cases: list[GeneratedCase] = []
        for spec in dimensions:
            if spec.dimension != "value_valid":
                raise ValueError(
                    f"规则生成器只支持 `value_valid` 维度，收到 {spec.dimension!r}。"
                    "复杂维度需要 LLM（阶段 B）——让它静默产出一条不完整的用例"
                    "比报错更糟：报告会显示一个不存在的覆盖。"
                )
            cases.append(self._value_valid_case(operation, spec))
        return GenerationResult(cases=cases, operation_notes=None)

    def _value_valid_case(self, operation: Operation, spec: DimensionSpec) -> GeneratedCase:
        expected = spec.expected_status[0] if spec.expected_status else 200
        return GeneratedCase(
            dimension=spec.dimension,
            name=f"{operation.method} {operation.path} 正常请求",
            category="normal",
            request_body=None,
            assertions=[
                # 用模型构造而不是裸 dict：`GeneratedCase.assertions` 是判别联合，
                # 传 dict 能靠 Pydantic 的强制转换通过，但**类型检查看不见**——
                # 而"拼错的字段名被静默忽略"正是 A2~A4 那几条约束要防的事。
                StatusCodeAssertion(type="status_code", expected=expected, derived_from="spec")
            ],
            requires_auth=True,
            derived_from="spec",
            notes="无请求参数与请求体，合法请求即是不带任何输入的请求",
        )


def _to_payload(
    case: GeneratedCase,
    operation: Operation,
    *,
    generated_by: str,
    llm_model: str | None,
    confidence: str | None,
) -> GeneratedCasePayload:
    return GeneratedCasePayload(
        name=case.name,
        category=case.category,
        dimension=case.dimension,
        request_method=operation.method,
        request_path=operation.path,
        request_query=case.request_query,
        request_headers={k.lower(): v for k, v in (case.request_headers or {}).items()} or None,
        request_body=case.request_body,
        assertions=[a.model_dump(mode="json") for a in case.assertions],
        requires_auth=case.requires_auth,
        generated_by=generated_by,
        confidence=confidence,
        llm_model=llm_model,
    )


async def generate_for_operations(
    session: Session,
    *,
    run_id: uuid.UUID,
    operation_ids: Sequence[uuid.UUID],
    generator: CaseGenerator,
    fallback_rule_generator: CaseGenerator | None = None,
) -> GenerateOutcome:
    """为一批 Operation 生成用例。**幂等：已成功的跳过**（DM4）。

    Args:
        generator: 复杂接口用的生成器（阶段 B）。
        fallback_rule_generator: TG5 的纯规则生成器。`None` 表示不区分——
            此时 `generator` 会被用于全部接口。

    Returns:
        本次实际产出与失败的清单。**部分失败不阻塞全局**——
        一个接口的 schema 特别难处理，不该让整份 Spec 的生成全部作废。
    """
    # 绑定调用用途：Provider 据此决定是否开思考模式（`02` §5.3）。
    # 放在这里而不是每个调用点，是因为**所有**生成路径都必须带同一个用途。
    token = bind_llm_purpose("case_generation")
    try:
        return await _generate_for_operations(
            session,
            run_id=run_id,
            operation_ids=operation_ids,
            generator=generator,
            fallback_rule_generator=fallback_rule_generator,
        )
    finally:
        reset_llm_purpose(token)


async def _generate_for_operations(
    session: Session,
    *,
    run_id: uuid.UUID,
    operation_ids: Sequence[uuid.UUID],
    generator: CaseGenerator,
    fallback_rule_generator: CaseGenerator | None,
) -> GenerateOutcome:
    statuses = ro_repo.status_of(session, run_id)
    generated: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []

    operations = _load_operations(session, operation_ids)
    # 同一份 Spec 的全局信息**只提取一次**：它逐字节相同，重复提取没有意义，
    # 而"两次提取结果不同"会让缓存前缀在同一个 Run 内抖动。
    spec_context = _load_spec_context(session, operations)

    for operation in operations:
        # ★ DM4：**已成功的跳过。** 不是"重放"——LLM 输出非确定，
        # 重跑会得到不同的用例，并静默替换用户已确认过的那些。
        if statuses.get(operation.id) == "succeeded":
            skipped.append(operation.id)
            continue

        dimensions = derive_dimensions(
            operation_path=operation.path,
            parameters_schema=operation.parameters_schema,
            request_body_schema=operation.request_body_schema,
            responses_schema=operation.responses_schema,
        )

        # TG5：无信息接口**不调 LLM**。它的取值是平凡的，让模型来做
        # 只是把一个确定的答案换成一次网络请求和一个不确定的答案。
        if fallback_rule_generator is not None and is_pure_rule_operation(
            operation.parameters_schema, operation.request_body_schema, operation.responses_schema
        ):
            active = fallback_rule_generator
        else:
            active = generator

        try:
            result = await active.generate(operation, dimensions, spec_context=spec_context)
        except Exception as exc:  # noqa: BLE001 —— 单个接口失败不该带崩整批
            logger.warning("用例生成失败 operation=%s：%s", operation.id, exc)
            ro_repo.mark_failed(session, run_id, operation.id, error=str(exc)[:500])
            failed.append(operation.id)
            continue

        created = _persist(
            session, run_id=run_id, operation=operation, result=result, active=active
        )
        ro_repo.mark_succeeded(session, run_id, operation.id, case_count=len(created))
        generated.extend(created)

    session.flush()
    return GenerateOutcome(
        generated_case_ids=generated, failed_operations=failed, skipped_operations=skipped
    )


def _load_spec_context(session: Session, operations: Sequence[Operation]) -> SpecContext | None:
    """取这批接口所属 Spec 的半稳定上下文。

    一批里混了多个 `spec_id` 时返回 `None`：那说明调用方把不该放一起的接口
    放一起了。此时**宁可不给上下文**，也不要把 A 的鉴权方式用在 B 的接口上。
    """
    from testflow.storage.repositories import specs as specs_repo

    spec_ids = {operation.spec_id for operation in operations}
    if len(spec_ids) != 1:
        return None
    spec = specs_repo.get(session, next(iter(spec_ids)))
    if spec is None:  # pragma: no cover —— 外键保证不会发生
        return None
    return build_spec_context(spec.raw_content)


def _load_operations(session: Session, operation_ids: Sequence[uuid.UUID]) -> list[Operation]:
    from testflow.storage.repositories import operations as operations_repo

    return operations_repo.list_by_ids(session, operation_ids)


#: JSON Schema / OpenAPI 里**语义有标准定义**的 `format`。
#:
#: 不在这份集合里的（例如 `imei`、`uuid-v7`、`CN-PHONE`）是业务自定义格式：
#: 阶段 A 仍然派 `constraint_violation` 维度（"应当拒绝不符合格式的值"是
#: 确定的），但**具体什么值算不符合**只能由模型猜——所以该用例标低置信度
#: （`TestCaseGen.md` §7 触发 ④）。这是 Golden Set 第 10 号接口检验的降级行为。
_STANDARD_FORMATS: frozenset[str] = frozenset(
    {
        "date-time",
        "date",
        "time",
        "duration",
        "email",
        "idn-email",
        "hostname",
        "idn-hostname",
        "ipv4",
        "ipv6",
        "uri",
        "uri-reference",
        "iri",
        "iri-reference",
        "uuid",
        "uri-template",
        "json-pointer",
        "relative-json-pointer",
        "regex",
        "password",
        "byte",
        "binary",
        "int32",
        "int64",
        "float",
        "double",
        "decimal",
    }
)


def _classify_confidence(operation: Operation, case: GeneratedCase) -> Literal["high", "low"]:
    """§7 低置信度：由具体原因构成，任一触发 → `low`，否则 `high`。

    §7 的六触发里，当前实现可观测五条：

    * ① `responses_schema` 未声明该状态码 → 断言 `derived_from == "inferred"`（期望靠推断）
    * ④ 字段使用自定义 `format` → 该字段的 `constraint_violation` 用例
    * ⑤ 响应体无 schema → 内容断言缺失、仅剩状态码
    * ⑥ 维度标记 `inferred` → 该用例本身
    * `has_unresolved_ref`（比 §7 更强：`$ref` 展开超限，schema 信息整体不完整）

    ②③（字段无 example / 无 min-max-长度）暂不触发：`value_valid` 本身
    不携带字段级信息，要判断"合法值靠猜"需要阶段 B 的产物逐字段分析，
    当前没有可靠的落点。**不在这里留一个永远走不到的分支。**
    """
    if operation.has_unresolved_ref:
        return "low"
    if case.derived_from == "inferred":
        return "low"
    if any(a.derived_from == "inferred" for a in case.assertions):
        return "low"
    if _uses_custom_format(operation, case.dimension):
        return "low"
    if _has_no_content_assertion(case) and not _has_response_body_schema(
        operation.responses_schema
    ):
        return "low"
    return "high"


def _uses_custom_format(operation: Operation, dimension: str) -> bool:
    """`constraint_violation:<field>` 的字段是否用了非标准 `format`。

    只认请求体顶层字段：阶段 A 目前只从顶层 `properties` 派生约束维度，
    去嵌套结构里找一个没被派生过的字段名只会制造误判。
    """
    prefix = "constraint_violation:"
    if not dimension.startswith(prefix):
        return False
    field = dimension[len(prefix) :]
    schema = json_body_schema(operation.request_body_schema) or {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    field_schema = properties.get(field)
    if not isinstance(field_schema, dict):
        return False
    fmt = field_schema.get("format")
    return isinstance(fmt, str) and fmt not in _STANDARD_FORMATS


def _has_no_content_assertion(case: GeneratedCase) -> bool:
    """用例是否**没有任何内容断言**（只剩状态码）——§7 触发 ⑤ 的前半。"""
    return all(a.type == "status_code" for a in case.assertions)


def _has_response_body_schema(responses_schema: dict | None) -> bool:
    """`responses_schema` 里是否**任何**响应声明了 body schema。

    没有 body schema 时，内容断言无从派生——这正是 §7 触发 ⑤ 的原因。
    """
    if not isinstance(responses_schema, dict):
        return False
    for response in responses_schema.values():
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        if isinstance(content, dict) and any(
            isinstance(media, dict) and "schema" in media for media in content.values()
        ):
            return True
    return False


def _resolve_confidence(operation: Operation, case: GeneratedCase) -> str:
    """最终落库的置信度。保守合并：任一来源判 `low` 则 `low`（§7 聚合规则）。

    生成方（LLM）可在 `case.confidence` 显式标 `low`；`_classify_confidence`
    是确定性兜底与下限——两者任一判 `low` 就 `low`，绝不因生成方自报 `high`
    而放行一条 schema 信息不完整的用例。
    """
    if case.confidence == "low" or _classify_confidence(operation, case) == "low":
        return "low"
    return "high"


def _persist(
    session: Session,
    *,
    run_id: uuid.UUID,
    operation: Operation,
    result: GenerationResult,
    active: CaseGenerator,
) -> list[uuid.UUID]:
    """落库，并把 `Operation` 上的属性**继承**到每条用例。

    `is_destructive` 来自 `operations` 而不是生成逻辑，因为它是 Spec 的属性
    （可能由 `x-destructive` 显式声明），不是生成方的判断。生成方"决定"它
    是不是破坏性的，是错的。置信度则按 §7 逐条计算——它取决于每条用例自己的
    断言与 `derived_from`，不再是"整个 Operation 一个值"。
    """
    generated_by = "rule" if isinstance(active, RuleBasedGenerator) else "llm"
    llm_model = getattr(active, "model_name", None) if generated_by == "llm" else None

    rows = [
        Case(
            run_id=run_id,
            operation_id=operation.id,
            is_destructive=operation.is_destructive,
            # 用 `asdict` 而不是 `__dict__`：`GeneratedCasePayload` 是
            # `frozen + slots` 的 dataclass，**没有 `__dict__`**。
            # （同一个坑在 `guardrail/audit.py` 上踩过一次了。）
            **dataclasses.asdict(
                _to_payload(
                    case,
                    operation,
                    generated_by=generated_by,
                    llm_model=llm_model,
                    confidence=_resolve_confidence(operation, case),
                )
            ),
        )
        for case in result.cases
    ]
    cases_repo.insert_many(session, rows)
    return [row.id for row in rows]
