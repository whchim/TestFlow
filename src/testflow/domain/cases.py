"""测试用例的领域类型——维度与生成产物。

对应决策：`03-模块详细设计/TestCaseGen.md` §2.3（阶段 A 维度表）、§2.5（结构化输出契约）、TG1。

**这套类型的核心主张（TG1）：维度确定，取值生成。**

```
阶段 A（纯算法，无 LLM）   schema ──→ 测试维度清单
阶段 B（LLM）              单个维度 + 上下文 ──→ 具体取值 + 断言
```

分成两半是为了让"**测什么**"有确定性依据。如果维度也交给 LLM 决定，
覆盖率就没法度量（分母也成了模型输出），而 Golden Set 也就无从比对——
"生成质量"会退化成"看起来差不多"。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from testflow.domain.assertions import Assertion, DerivedFrom

__all__ = ["DimensionSpec", "GeneratedCase", "GenerationResult"]


CaseCategory = Literal["normal", "abnormal", "boundary"]


class DimensionSpec(BaseModel):
    """阶段 A 派生出的一个**测试维度**——"要测什么"。

    不含任何具体取值：`boundary:age` 说的是"要测 age 的边界"，
    `age=151` 才是阶段 B 才决定的事。
    """

    model_config = {"extra": "forbid"}

    dimension: str
    """维度标识，形如 `required_missing:email` / `boundary:age` / `auth_missing`。"""

    category: CaseCategory
    field: str | None = None
    """涉及的字段名（`value_valid` / `auth_missing` 等整体维度为 None）。"""

    expected_status: list[int] = Field(default_factory=list)
    """该维度**期望**的状态码。空列表表示 Spec 未声明、需由阶段 B 推断。"""

    rationale: str = ""
    """为什么派生这个维度——进可读用例名，退可解释覆盖率分母。"""


class GeneratedCase(BaseModel):
    """一条待落库的测试用例。`TestCaseGen.md` §2.5 的权威结构。"""

    model_config = {"extra": "forbid"}

    dimension: str
    name: str
    category: CaseCategory

    request_query: dict[str, Any] | None = Field(
        default=None,
        description='查询参数的扁平 JSON 对象，如 {"page": 1}；不是 OpenAPI 的 parameters 数组。',
    )
    request_query_json: str | None = Field(
        default=None,
        description=(
            "**优先使用这个字段**：把查询参数序列化成 JSON 字符串，"
            '如 "{\\"page\\": 1}"。平台会解析成对象。没有查询参数时为 null。'
        ),
    )
    #: **不含凭证**（SR-6）。这不是靠"记得别放"保证的——凭证根本不在会被
    #: 序列化给 LLM 的对象里，执行时才由 Worker 按 host 注入（TE4）。
    request_headers: dict[str, str] | None = Field(
        default=None,
        description="只放必要的非凭证请求头（如 Content-Type）；不要放 Authorization。",
    )
    request_headers_json: str | None = Field(
        default=None,
        description=(
            "**优先使用这个字段**：把请求头序列化成 JSON 字符串，"
            '如 "{\\"Content-Type\\": \\"application/json\\"}"。'
            "没有额外请求头时为 null。"
        ),
    )
    path_params: dict[str, str] | None = Field(
        default=None,
        description='路径模板变量的取值，如 {invoice_id: "inv_123"}。',
    )
    path_params_json: str | None = Field(
        default=None,
        description=(
            "**优先使用这个字段**：把路径参数序列化成 JSON 字符串。"
            "路径里每个 {变量} 都必须有值；没有路径参数时为 null。"
        ),
    )
    request_body: dict[str, Any] | None = Field(
        default=None,
        description=(
            '要发送的 JSON 对象本身，如 {"name": "Alice", "age": 30}。'
            "必须是合法 JSON（键和字符串都用双引号），不要输出 YAML；"
            '不要包成 {"content": ...}、{"type": ..., "value": ...} 或 {"body": ...}；'
            "没有请求体时为 null。"
        ),
    )
    request_body_json: str | None = Field(
        default=None,
        description=(
            "**优先使用这个字段**：把请求体序列化成一个 JSON 字符串，"
            '例如 "{\\"name\\": \\"Alice\\"}"。平台会解析成对象。'
            "没有请求体时为 null。不要和 request_body 同时填。"
        ),
    )

    assertions: list[Assertion] = Field(min_length=1)

    #: `auth_missing` 维度的用例置 false——否则执行器注入凭证后它会收到 200
    #: 而非预期的 401，测试结果完全错误。
    requires_auth: bool = True

    derived_from: DerivedFrom

    confidence: Literal["high", "low"] | None = None
    """生成方自报的置信度。`None` 表示交由确定性分类器 `_classify_confidence`
    判定（`TestCaseGen.md` §7——低置信度由具体原因构成，不应由模型自由打分）。"""

    notes: str | None = None
    """生成理由，供人工复核。不参与任何判定。"""


class GenerationResult(BaseModel):
    """一次 LLM 调用的输出。对应一个 Operation。"""

    model_config = {"extra": "forbid"}

    operation_notes: str | None = None
    cases: list[GeneratedCase]
