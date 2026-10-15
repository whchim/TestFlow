"""N1 `parse_spec` —— OpenAPI 文档解析。**纯算法，不调用 LLM**（`02` D3）。

对应决策：`03-模块详细设计/SpecParser.md`（SP1~SP5）、`04-数据模型.md` §2.1/§2.2、
ADR-0012（指纹）。

## 为什么不用 LLM

解析是确定性的。让 LLM 参与会引入三种问题，而它们都无法用测试兜住：

1. **幻觉**——"补全"出文档里根本没有的字段，读起来完全合理
2. **不可复现**——同一份文档两次解析出的结果不同，指纹就失去意义（S2 失效判定失效）
3. **达不到指标**——PRD M3 要求解析成功率 ≥99%，而这不是 LLM 能承诺的东西

## N1 是全系统唯一"真幂等"的节点

同输入必得同输出，因此**可以安全重放**。对比 N2（`TestCaseGen`）：它调 LLM，
输出非确定，幂等只能是"已完成则跳过"（DM4）。

**两个节点的幂等语义不同**，这是本项目最容易搞混的一点。

## 本模块是纯函数，不碰数据库

`parse_spec()` 吃一段文本、吐一个 `ParsedSpec`。落库由调用方（图节点）负责。
分开的理由是可测性：整个解析流程能在毫秒内跑几百个用例，不需要数据库参与。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import yaml
from openapi_spec_validator import validate as validate_openapi_spec

from testflow.domain.targets import DeclaredTarget
from testflow.guardrail.policy import DESTRUCTIVE_METHODS

__all__ = [
    "ParsedOperation",
    "ParsedSpec",
    "SpecParseError",
    "SpecWarning",
    "parse_spec",
    "strip_expanded_components",
]

#: SP1：`$ref` 展开的深度上限。
#:
#: 递归结构在 OpenAPI 里合法且常见（树、评论、嵌套分类）。深度超限时
#: **保留字面量并标记**，而不是放弃整份解析——深层字段对测试设计的边际价值低，
#: 但下游必须知道"这里的信息不完整"。
_MAX_REF_DEPTH: Final = 10

#: `$ref` **解引用**次数上限。
#:
#: ⚠️ **深度限制单独是不够的。** 一个 schema 如果含 *n* 个指向自己的引用，
#: 展开 10 层会产生 n^10 个节点：n=2 是 1024（无所谓），n=5 就是约 1000 万，
#: n=10 更是天文数字。而这是**用户上传的文档**——必须当成不可信输入对待。
#:
#: 计的是"解引用次数"而不是"结果节点数"：正常文档里绝大多数节点是普通字段，
#: 把它们也计入会让额度在大文档上被无谓地耗掉。而**能导致爆炸的恰恰只有解引用**。
#:
#: 超限时停止展开、保留字面量。宁可少展开，不可被打挂。
_MAX_REF_EXPANSIONS: Final = 10_000

_ALLOWED_METHODS: Final = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: 文档里可以显式声明"这个方法在本接口上是破坏性的"的扩展键（DM3）。
#:
#: 默认由 HTTP 方法派生，但方法本身不足以判断业务语义：
#: `POST /users/{id}/close` 在业务上可能是不可逆的，而 POST 不在
#: `DESTRUCTIVE_METHODS` 里。反过来，某些 API 的 DELETE 只是软删除。
_DESTRUCTIVE_EXTENSION: Final = "x-destructive"

_JSON_POINTER_ESCAPES: Final = (("~1", "/"), ("~0", "~"))


class SpecParseError(Exception):
    """文档缺陷——**硬失败**，终止 Run（SP4）。

    区分"文档缺陷"与"信息不全"是 SP4 的核心：

    * **文档缺陷**（语法错误、悬空的 `$ref`、不符合规范）→ 抛本异常。
      用户能修，也必须让用户去修——静默降级会让他在后面收到一堆莫名其妙的
      `blocked_by_policy` 归因，而真正的原因在解析阶段就被掩盖了。
    * **信息不全**（缺 example、相对 URL、没有 `servers`）→ 降级继续，
      但产出 `SpecWarning`。
    """

    def __init__(self, code: str, detail: str, *, errors: list[str] | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.errors = errors or []


@dataclass(frozen=True, slots=True)
class SpecWarning:
    """降级情形。**必须在 API 响应里告知用户**（`05` API §4.1）。

    不说会怎样：用户看到后续的 `blocked_by_policy` 归因，会以为是平台故障，
    而不是"你上传的文档里没写主机名"。
    """

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ParsedOperation:
    """一个接口。对应 `operations` 表的一行。"""

    method: str  # 大写
    path: str
    operation_id: str | None
    summary: str | None
    parameters_schema: dict[str, Any] | None
    request_body_schema: dict[str, Any] | None
    responses_schema: dict[str, Any] | None
    has_unresolved_ref: bool
    is_destructive: bool


@dataclass(frozen=True, slots=True)
class ParsedSpec:
    """一次解析的全部产出。**尚未落库。**"""

    openapi_version: str
    fingerprint: str
    normalized: dict[str, Any]
    operations: list[ParsedOperation]
    declared_targets: list[DeclaredTarget]
    warnings: list[SpecWarning] = field(default_factory=list)

    @property
    def has_usable_targets(self) -> bool:
        """是否解析出了至少一个出站目标。

        为假时**所有**请求都会被 Guardrail 拒绝（白名单为空）。这不是错误，
        但必须在响应里说清楚——否则用户会以为平台坏了。
        """
        return bool(self.declared_targets)


# ══════════════════════════════════════════════════════════ ① 语法加载


def load_document(content: str) -> dict[str, Any]:
    """文本 → dict。JSON 与 YAML 都走 YAML 解析器（JSON 是 YAML 的子集）。

    Raises:
        SpecParseError: 语法错误。**消息里带行列号**——没有位置信息的解析错误
            等于让用户自己去找，而文档可能几千行。
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise SpecParseError(
            "syntax_error",
            f"文档语法错误：{exc}",
        ) from exc

    if not isinstance(data, dict):
        raise SpecParseError(
            "not_an_object",
            f"文档根节点必须是对象，实际是 {type(data).__name__}",
        )
    return data


# ══════════════════════════════════════════════════════════ ② 规范校验


def validate_spec(document: dict[str, Any]) -> str:
    """用 `openapi-spec-validator` 校验，返回 `openapi` 版本号。

    在 `$ref` 展开**之前**校验：校验器看得懂 `$ref`，而在展开后的结构上跑它
    会把"深度超限时保留的字面量"误报成非法。

    Raises:
        SpecParseError: 文档不合规。`code` 区分"悬空 `$ref`"与"其他不合规"——
            前者能给出更可操作的提示。

    Note:
        **这里刻意捕获宽泛的 `Exception`，并且是必要的。**

        校验库的异常面并不统一：不合规文档抛 `OpenAPIValidationError`，
        未知版本号抛 `ValidatorDetectError`（继承自 `OpenAPISpecValidatorError`，
        **不是** `OpenAPIValidationError`），而悬空的 `$ref` 由底层的
        `referencing` 库抛 `PointerToNowhere`——三个不同的继承树。

        这里是**库的边界**：任何从这里抛出的异常都意味着同一件事——
        "这份文档不能用"。将它们统一转换成本模块的结构化错误，
        好过让三种第三方异常泄漏到调用方（调用方无从穷举，只能也写 `except Exception`）。
    """
    try:
        validate_openapi_spec(document)
    except Exception as exc:
        if _is_unresolvable_ref(exc):
            raise SpecParseError(
                "dangling_ref",
                f"文档中的 `$ref` 指向不存在的位置：{exc}",
            ) from exc
        message = getattr(exc, "message", None) or str(exc)
        raise SpecParseError(
            "invalid_openapi",
            f"不符合 OpenAPI 规范：{message}",
            errors=[str(exc)],
        ) from exc

    version = document.get("openapi")
    if not isinstance(version, str):
        raise SpecParseError("missing_openapi_version", "缺少 `openapi` 版本字段")
    return version


def _is_unresolvable_ref(exc: BaseException) -> bool:
    """判断异常是否表示"`$ref` 指向了不存在的位置"。

    按**类名与模块名**判断而非 `isinstance`：`PointerToNowhere` 来自 `referencing`
    库——它是 `openapi-spec-validator` 的传递依赖，直接 import 它等于把
    "直接使用的必须直接声明"这条规则绕过去。

    判断不出来时会落到"其他不合规"分支——那仍然是一个正确的硬失败，
    只是提示不如专项分支精确。**降级路径是安全的**，这是按名字判断可以接受的前提。
    """
    return type(exc).__name__ in {"PointerToNowhere", "Unresolvable"} or (
        type(exc).__module__ or ""
    ).startswith("referencing")


def reject_external_refs(node: Any) -> None:
    """在交给校验器**之前**，先把外部文件引用挡下来。

    **为什么要抢先。** 校验器遇到 `./common.yaml#/User` 也会失败，但它只会说
    "指向不存在的位置"——而用户看了一头雾水：那个文件明明存在，就在他本地的
    同一个目录里。

    真正的原因是：**用户上传的是文档内容，不是一份可访问的 URL 集合。**
    我们没有基准地址，也就永远不可能解析外部引用。说清楚这一点，
    用户才知道该怎么做（合并成一个文件）；只说"指向不存在"，
    他大概会去反复检查那个文件路径。

    **这是一条"把系统约束翻译成用户可执行的下一步"的提示**，
    而这类提示的价值往往高于错误本身的准确性。
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            raise SpecParseError(
                "external_ref_unsupported",
                f"不支持外部文件引用：{ref!r}。"
                "TestFlow 接收的是文档**内容**而非可访问的 URL，无法解析外部文件；"
                "请把被引用的内容合并进同一份文档",
            )
        for value in node.values():
            reject_external_refs(value)
    elif isinstance(node, list):
        for item in node:
            reject_external_refs(item)


# ══════════════════════════════════════════════════════════ ③ $ref 展开


def _resolve_pointer(document: dict[str, Any], ref: str) -> Any:
    """解析一个**文档内**的 JSON Pointer（`#/components/schemas/Foo`）。

    Raises:
        SpecParseError: 指针指向不存在的位置。这是**文档缺陷**（SP4）——
            悬空的 `$ref` 说明文档本身就是坏的，不能降级。
    """
    if not ref.startswith("#/"):
        # 外部文件引用（`./common.yaml#/...`）。用户上传的是**文档内容**而不是
        # 可访问的 URL，没有基准地址可解析——所以无法处理，而"无法处理"在这里
        # 等于文档缺陷，因为它永远不可能成功。
        raise SpecParseError(
            "external_ref_unsupported",
            f"不支持外部文件引用：{ref!r}。请把被引用的内容合并进同一份文档",
        )

    node: Any = document
    for raw_token in ref[2:].split("/"):
        token = raw_token
        for escaped, literal in _JSON_POINTER_ESCAPES:
            token = token.replace(escaped, literal)
        if not isinstance(node, dict) or token not in node:
            raise SpecParseError("dangling_ref", f"`$ref` 指向不存在的位置：{ref!r}")
        node = node[token]
    return node


@dataclass
class _ExpandBudget:
    """展开过程的预算。

    对象而非简单计数器，是为了让"超限"能被内层置位、被外层看到。
    `expansions` 只在真正**解引用**时递增——见 `_MAX_REF_EXPANSIONS` 的说明。
    """

    expansions: int = 0
    exceeded: bool = False


def _expand(
    node: Any,
    document: dict[str, Any],
    depth: int,
    budget: _ExpandBudget,
) -> tuple[Any, bool]:
    """递归展开 `$ref`。

    Returns:
        `(展开后的结构, 是否有未展开的 $ref)`。第二个值一路上浮，
        最终落到 `operations.has_unresolved_ref`。

    Note:
        预算耗尽后**立刻停止递归**，但外层循环仍要继续（否则返回的结构会缺键）。
        所以循环里在**进入下一次递归前**检查 `budget.exceeded`，把剩下的兄弟节点
        原样拷过去。少了这一步，一个 10^10 节点的结构仍会被完整走一遍——
        只是每次都立刻返回。**那依然是 100 亿次函数调用。**
    """
    if budget.exceeded:
        return node, True

    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if depth >= _MAX_REF_DEPTH:
                # **SP1：超限保留字面量。** 保留而不是删掉——下游至少能看到
                # "这里本来有个 $ref"，而删除会让缺失变得不可见。
                return node, True
            budget.expansions += 1
            if budget.expansions > _MAX_REF_EXPANSIONS:
                budget.exceeded = True
                return node, True

            target = _resolve_pointer(document, ref)
            expanded, unresolved = _expand(target, document, depth + 1, budget)
            # 兄弟键在 OpenAPI 3.1 里合法（用于附加描述）；3.0 忽略它们。
            # 这里保留 `$ref` 之外的兄弟键，避免丢信息。
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if siblings and isinstance(expanded, dict):
                merged, more = _expand(siblings, document, depth, budget)
                return {**expanded, **merged}, unresolved or more
            return expanded, unresolved

        resolved: dict[str, Any] = {}
        unresolved_any = False
        for key, value in node.items():
            if budget.exceeded:
                resolved[key] = value  # 预算已耗尽，原样保留，不再深入
                continue
            new_value, unresolved = _expand(value, document, depth, budget)
            resolved[key] = new_value
            unresolved_any = unresolved_any or unresolved
        return resolved, unresolved_any

    if isinstance(node, list):
        items = []
        unresolved_any = False
        for item in node:
            if budget.exceeded:
                items.append(item)
                continue
            new_item, unresolved = _expand(item, document, depth, budget)
            items.append(new_item)
            unresolved_any = unresolved_any or unresolved
        return items, unresolved_any

    return node, False


def expand_refs(document: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """展开全部 `$ref`，并报告是否留有未展开的。

    **展开发生在解析期而非消费期**：`TestCaseGen` 需要自包含的 schema 片段。
    若留着 `$ref`，要么每个消费者各自实现一遍解析器（必然漂移），
    要么把整份文档塞进提示词（触发 Qwen 阶梯计费跳档，成本 ×5）。
    """
    budget = _ExpandBudget()
    expanded, unresolved = _expand(document, document, 0, budget)
    if not isinstance(expanded, dict):  # pragma: no cover —— 根节点已在 ① 校验
        raise SpecParseError("not_an_object", "展开后的根节点不是对象")
    return expanded, unresolved


# ══════════════════════════════════════════════════════ ④⑤ 规范化与指纹


def _normalize_number(value: float) -> int | float:
    """把整数值的浮点数收成整数：`1.0` 与 `1` 必须产生同一个指纹。

    `json.dumps(1.0)` 得到 `1.0`，`json.dumps(1)` 得到 `1`——两个字节不同的
    字符串，两个不同的 sha256，于是一次"只改了写法"的编辑会被 S2 判成"Spec 变了"。
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


#: 展开之后**从指纹里剔除**的 `components` 子节。
#:
#: **这是对 SP5 的细化，设计文档没有明说。**
#:
#: SP5 的目标是"内联写法的文档与 `$ref` 写法的文档得到相同指纹"。但仅仅
#: "在展开之后算指纹"达不到这个目标：展开**不会删除** `components` 里的定义，
#: 于是 `$ref` 版比内联版多出一整块内容，两者指纹必然不同。
#:
#: 所以还要把 `components` 里**那些只是 `$ref` 目标的子节**去掉——展开之后，
#: 它们的内容已经内联到 `paths` 里了，留在原地是纯粹的重复。剔除它们，
#: 指纹才真正只反映"这份文档描述了哪些接口、长什么样"。
#:
#: `securitySchemes` **刻意保留**：它不被 `$ref` 内联，而是由 `security`
#: 需求按**名字**引用，剔掉会让"改了认证方案"检测不到。
_REF_TARGET_COMPONENT_SECTIONS: Final = frozenset(
    {
        "schemas",
        "responses",
        "parameters",
        "examples",
        "requestBodies",
        "headers",
        "links",
        "callbacks",
        "pathItems",  # OpenAPI 3.1
    }
)


def strip_expanded_components(document: dict[str, Any]) -> dict[str, Any]:
    """剔除 `components` 中已内联的 `$ref` 目标（见上方常量说明）。

    保守起见**只删已知的那几个子节**，保留其余一切——包括 `securitySchemes`
    和任何自定义的 `x-` 扩展。删多了会让指纹漏掉真实变更，
    而漏检测比误报更难发现：误报会立刻表现为"用例失效了"，
    漏检测则表现为"该失效的没失效"，没有任何提示。
    """
    components = document.get("components")
    if not isinstance(components, dict):
        return document

    kept = {
        key: value for key, value in components.items() if key not in _REF_TARGET_COMPONENT_SECTIONS
    }
    stripped = dict(document)
    if kept:
        stripped["components"] = kept
    else:
        stripped.pop("components", None)
    return stripped


def normalize(node: Any) -> Any:
    """递归规范化（DM2 / SP5）。

    | 步骤 | 说明 |
    | :--- | :--- |
    | 递归按 key 排序 | 消除 key 顺序差异 |
    | 数组**保持顺序** | 数组顺序在 OpenAPI 里**有语义**（`enum` 取值顺序、`required` 列表） |
    | 数值规范化 | `1` 与 `1.0` 归一 |

    **在 `$ref` 展开之后进行**（SP5）：一份把 schema 内联的文档与一份用 `$ref`
    引用的文档语义等价，必须得到相同指纹。否则 S2 会把"改了写法"误报成"Spec 变了"，
    进而让一个本来有效的 Run 被判为用例失效——而用户什么都没改。

    单靠"展开后计算"还不够，还要先 `strip_expanded_components`——
    见那个函数的说明。
    """
    if isinstance(node, dict):
        return {key: normalize(node[key]) for key in sorted(node)}
    if isinstance(node, list):
        return [normalize(item) for item in node]
    if isinstance(node, float):
        return _normalize_number(node)
    return node


def fingerprint_of(normalized: dict[str, Any]) -> str:
    """规范化结构的 sha256。

    用紧凑分隔符（无空格）并且**不**排序 key——排序已经在 `normalize` 里做过，
    这里再排一次会让两处各有一份"排序规则"，将来只改一处的风险就出现了。
    """
    canonical = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════ ⑦ 提取接口


def _op_is_destructive(method: str, operation: dict[str, Any]) -> bool:
    """判断接口是否破坏性。

    默认由方法派生，但**允许文档显式覆盖**（DM3）：HTTP 方法不足以表达业务语义。
    """
    override = operation.get(_DESTRUCTIVE_EXTENSION)
    if isinstance(override, bool):
        return override
    return method.upper() in DESTRUCTIVE_METHODS


def extract_operations(
    document: dict[str, Any], *, has_unresolved_ref: bool
) -> list[ParsedOperation]:
    """逐 `path` × `method` 提取接口。

    `parameters_schema` 等三块**整块存 JSONB**，不按 parameter 拆行（`04` §2.10）：
    schema 是整体（`$ref`、嵌套、条件约束），拆行会破坏结构完整性。裁剪在组装
    提示词时进行——那是 Qwen 阶梯计费（`02` §5.1）的要求。
    """
    operations: list[ParsedOperation] = []

    for path, path_item in (document.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        # 路径级 parameters 会被操作级覆盖，这里先取出来做基线。
        path_params = path_item.get("parameters") or []

        for method, operation in path_item.items():
            if method.lower() not in _ALLOWED_METHODS or not isinstance(operation, dict):
                continue

            merged_params = [*path_params, *(operation.get("parameters") or [])]
            operations.append(
                ParsedOperation(
                    method=method.upper(),
                    path=path,
                    operation_id=operation.get("operationId"),
                    summary=operation.get("summary"),
                    parameters_schema={"parameters": merged_params} if merged_params else None,
                    request_body_schema=operation.get("requestBody"),
                    responses_schema=operation.get("responses"),
                    has_unresolved_ref=has_unresolved_ref,
                    is_destructive=_op_is_destructive(method, operation),
                )
            )

    # 稳定排序：接口顺序会影响生成顺序与报告顺序。让它在两次运行间保持一致，
    # 否则"同一个 Run 跑两次"的对比会变成噪音。
    operations.sort(key=lambda op: (op.path, op.method))
    return operations


# ═════════════════════════════════════════════════════ ⑧ 出站目标提取

_VARIABLE_PATTERN: Final = re.compile(r"\{([^{}]+)\}")


def _instantiate_server(
    server: dict[str, Any], *, location: str
) -> tuple[str | None, SpecWarning | None]:
    """把 server 的 URL 模板实例化成一个具体 URL（SP2）。

    **用变量的 `default` 值**，不取 `enum` 的全部取值——那会不必要地放大白名单，
    而白名单每宽一分，SR-1 就弱一分。
    """
    url = server.get("url")
    if not isinstance(url, str) or not url:
        return None, SpecWarning(
            code="server_missing_url", detail=f"{location} 的 server 缺少 url，已跳过"
        )

    variables = server.get("variables") or {}
    placeholders = _VARIABLE_PATTERN.findall(url)
    if not placeholders:
        return url, None

    resolved = url
    for name in placeholders:
        spec = variables.get(name)
        default = spec.get("default") if isinstance(spec, dict) else None
        if default is None:
            # 规范要求变量必须有 default，但实际文档常常不合规。
            # 无法实例化 → 跳过并记警告，而不是猜一个值——猜错会放行一个
            # 用户从未声明过的主机。
            return None, SpecWarning(
                code="server_variable_without_default",
                detail=f"{location} 的变量 {{{name}}} 没有 default，无法确定主机，已跳过",
            )
        resolved = resolved.replace(f"{{{name}}}", str(default))
    return resolved, None


def _iter_servers(document: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str]]:
    """产出三个层级声明的全部 server（**必须全部提取**）。

    | 层级 | 说明 |
    | :--- | :--- |
    | 根级 | 最常见 |
    | 路径级 | 覆盖根级 |
    | 操作级 | 优先级最高 |

    只提取根级是不够的：某个 Operation 用操作级 `servers` 指向另一台主机时，
    那个主机会被漏掉，于是它的请求全部被 Guardrail 拒绝（`Guardrail.md` §3.3）。
    而现象是"一批用例莫名 `blocked_by_policy`"，很难联想到解析阶段。
    """
    for server in document.get("servers") or []:
        if isinstance(server, dict):
            yield server, "根级 servers"

    for path, path_item in (document.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for server in path_item.get("servers") or []:
            if isinstance(server, dict):
                yield server, f"路径 {path} 的 servers"
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            for server in operation.get("servers") or []:
                if isinstance(server, dict):
                    yield server, f"操作 {method.upper()} {path} 的 servers"


def extract_declared_targets(
    document: dict[str, Any],
) -> tuple[list[DeclaredTarget], list[SpecWarning]]:
    """提取出站白名单的来源（SR-1）。

    **提取单位是 `scheme://host:port` 三元组，不是 host**（GR3）：白名单必须含
    scheme 与 port，否则"允许 `http` 等于允许明文传凭证"、"允许 host:任意端口
    等于把平台变成端口扫描器"这两条规则无法执行。
    """
    targets: set[DeclaredTarget] = set()
    warnings: list[SpecWarning] = []

    for server, location in _iter_servers(document):
        url, warning = _instantiate_server(server, location=location)
        if warning is not None:
            warnings.append(warning)
            continue
        assert url is not None  # _instantiate_server 保证：warning 为 None 时 url 非空

        try:
            targets.add(DeclaredTarget.parse(url))
        except ValueError as exc:
            # SP3：相对 URL 无法确定 host——用户上传的是**文档内容**而非可访问的
            # URL，没有可解析的基准地址。
            warnings.append(
                SpecWarning(
                    code="relative_server_url",
                    detail=(
                        f"{location} 的 url {url!r} 无法确定主机（{exc}），"
                        "相关请求将被 Guardrail 拒绝"
                    ),
                )
            )

    if not targets and not warnings:
        # 完全没有 servers。`declared_targets` 为空意味着**所有请求都会被拒绝**，
        # 这必须让用户知道。
        warnings.append(
            SpecWarning(
                code="no_servers",
                detail="文档没有声明任何 servers，无法确定出站目标，所有请求将被 Guardrail 拒绝",
            )
        )

    # 排序而非直接用集合：落库的数组顺序稳定，两份等价文档的产出可以逐字比对。
    return sorted(targets), warnings


# ══════════════════════════════════════════════════════════ 主流程


def parse_spec(content: str) -> ParsedSpec:
    """完整的八步解析流水线。

    Raises:
        SpecParseError: 文档缺陷（语法、规范、悬空 `$ref`）。**终止 Run。**
    """
    document = load_document(content)
    # 先于校验器——见 `reject_external_refs` 的说明。
    reject_external_refs(document)
    version = validate_spec(document)
    expanded, has_unresolved = expand_refs(document)
    # 指纹算在**剔除已内联的 components 之后**的结构上——否则 `$ref` 写法与
    # 内联写法会得到不同的指纹，SP5 的目标就落空了。
    normalized = normalize(strip_expanded_components(expanded))
    fingerprint = fingerprint_of(normalized)
    operations = extract_operations(expanded, has_unresolved_ref=has_unresolved)
    targets, warnings = extract_declared_targets(expanded)

    return ParsedSpec(
        openapi_version=version,
        fingerprint=fingerprint,
        normalized=normalized,
        operations=operations,
        declared_targets=targets,
        warnings=warnings,
    )
