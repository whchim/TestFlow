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
from typing import Protocol

from sqlalchemy.orm import Session

from testflow.agents.dimensions import derive_dimensions, is_pure_rule_operation
from testflow.domain.assertions import StatusCodeAssertion
from testflow.domain.cases import CaseCategory, DimensionSpec, GeneratedCase, GenerationResult
from testflow.observability.logging import get_logger
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
        self, operation: Operation, dimensions: Sequence[DimensionSpec]
    ) -> GenerationResult:
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
    statuses = ro_repo.status_of(session, run_id)
    generated: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []

    for operation in _load_operations(session, operation_ids):
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
            result = await active.generate(operation, dimensions)
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


def _load_operations(session: Session, operation_ids: Sequence[uuid.UUID]) -> list[Operation]:
    from testflow.storage.repositories import operations as operations_repo

    return operations_repo.list_by_ids(session, operation_ids)


def _persist(
    session: Session,
    *,
    run_id: uuid.UUID,
    operation: Operation,
    result: GenerationResult,
    active: CaseGenerator,
) -> list[uuid.UUID]:
    """落库，并把 `Operation` 上的属性**继承**到每条用例。

    继承的两项各有理由：

    * `is_destructive` —— 来自 `operations` 而不是 `cases` 的生成逻辑，
      因为它是 Spec 的属性（可能由 `x-destructive` 显式声明），
      不是生成方的判断。生成方"决定"它是否是破坏性的，是错的。
    * `has_unresolved_ref` → `confidence = "low"` —— `$ref` 展开超限意味着
      schema 信息不完整（`SpecParser` SP1），据此生成的用例期望值不可靠。
    """
    generated_by = "rule" if isinstance(active, RuleBasedGenerator) else "llm"
    confidence = "low" if operation.has_unresolved_ref else None
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
                    confidence=confidence,
                )
            ),
        )
        for case in result.cases
    ]
    cases_repo.insert_many(session, rows)
    return [row.id for row in rows]
