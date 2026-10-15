# TestCaseGen 模块详细设计

| 项目 | 内容 |
| :--- | :--- |
| 文档版本 | v0.1 |
| 文档状态 | 评审中 |
| 最后更新 | 2026-10-13 |
| 上游文档 | [01-TestFlow_PRD.md](../01-TestFlow_PRD.md)、[02-架构设计.md](../02-架构设计.md)、[04-数据模型.md](../04-数据模型.md)、[06-Agent编排.md](../06-Agent编排.md) |

> **本文档的边界**：描述 TestCaseGen（图节点 N2）的生成策略、断言模型、结构化输出契约、Prompt 结构、校验与降级。
> **不做**：执行与断言求值（见 `TestExecutor.md`）、归因（见 `LogAnalyzer.md`）、报告（见 `ReportAgent.md`）。

**编号约定**：`TG` = TestCaseGen 决策，与 `02` 的 `D`、`04` 的 `DM`、`06` 的 `G` 区分。`SR`/`M`/`F`/`R`/`Q` 沿用上游编号。

---

## 1. 模块定位

### 1.1 输入与输出

| | 内容 |
| :--- | :--- |
| **输入** | 裁剪后的 Operation 结构（`operations.parameters_schema` / `request_body_schema` / `responses_schema`）+ 该 Spec 的全局上下文 |
| **输出** | 结构化用例集，落库至 `cases`（`04-数据模型.md` §2.5）；同时更新 `run_operations.gen_status` |
| **LLM** | ✅ 使用（`qwen3.5-plus`，见 `02` §5.1） |

### 1.2 职责边界

**做**：把 Operation 的 schema 转化为可执行的测试用例——包括测试维度的确定、取值的生成、断言的编写。

**不做**：

- 不决定测哪些 Operation（由用户在上游选择）
- 不执行用例、不求值断言（`TestExecutor`）
- 不判定用例质量是否达标（由 `09-测试策略.md` 的 Eval 机制独立评估）
- **不重新解析 Spec**——输入已是 `SpecParser` 产出的结构化数据（`02` D3）

---

## 2. 核心设计主张：维度确定，取值生成

### 2.1 主张

**"测什么"由 schema 确定性派生，"取什么值"才交给 LLM。**

朴素做法是让 LLM 读 schema 后自由输出用例。它的失败模式很明显：模型会漏掉显式约束（比如 `maxLength: 20` 却没生成超长用例），也会编造 schema 里不存在的校验规则。更糟的是——**你无法判断它漏了什么**，因为没有可对照的清单。

本模块把生成拆成两个阶段：

```
阶段 A（纯算法，无 LLM）
  schema ──→ 测试维度清单
              「required_missing: email」
              「boundary_max: age」
              「constraint_violation: role」

阶段 B（LLM）
  单个维度 + 上下文 ──→ 具体取值 + 断言 + 用例名
```

这样做的三个收益：

1. **可校验**：L2 覆盖率层有了唯一的对照基准——维度清单是确定的，缺了哪个维度一目了然（§5.2）
2. **抗幻觉**：模型不需要"发现"约束，只需为给定维度填值。约束来自 schema 而非模型记忆
3. **可评估**：Golden Set 比对的是**维度覆盖**而非"用例条数"，指标不会被模型多写几条凑数的用例污染

### 2.2 维度清单（阶段 A）

维度由 Operation 的 schema 确定性派生：

| 维度 | 派生自 | 期望结果 | 生成条数 |
| :--- | :--- | :--- | :--- |
| `value_valid` | 所有必填字段给合法值 | 2xx | 1 |
| `required_missing:<field>` | `required` 中的每个字段 | 4xx | 每个必填字段 1 条 |
| `type_mismatch:<field>` | 字段的 `type` | 4xx | 每个必填字段 1 条 |
| `constraint_violation:<field>` | `enum` / `pattern` / `format` / `multipleOf` | 4xx | 每个约束 1 条 |
| `boundary:<field>` | `minimum` / `maximum` / `minLength` / `maxLength` / `minItems` / `maxItems` | 见下 | 每个边界 2 条 |
| `not_found:<path_param>` | 路径参数（资源型） | 404 | 每个路径参数 1 条 |
| `auth_missing` | `security` 声明存在时 | 401 / 403 | 1 |

`boundary` 维度展开为两条：**边界值本身**（应被接受）与**越界值**（应被拒绝）。例如 `maximum: 150` 生成 `age=150`（期望 2xx）与 `age=151`（期望 4xx）。

### 2.3 期望结果的来源决定置信度

**关键**：`期望结果` 不总是能从 schema 得到。

- 若 `responses` 中声明了对应的状态码 → 期望值来自 spec，`derived_from: "spec"`
- 若只声明了 `200`，而 `required_missing` 维度需要一个 4xx → 状态码是**推断**的，`derived_from: "inferred"`

这个区分不是为了学术严谨，而是为了让人工确认（IP-1）**有的放矢**——见 §7。

### 2.4 组合爆炸的处理

MVP 采用 **one-at-a-time**：每次只违反一个约束，其余字段保持合法。

不生成"两个字段同时缺失"这类组合。理由：N 个必填字段的组合空间是 `2^N`，在 `02` §5 的规模假设（单 Spec ≤ 200 Operations）下会直接失控，而多数缺陷能被单点违反捕获。组合测试（pairwise）留待 V1.0 并需先用 Eval 数据证明其收益。

### 2.5 LLM 可以补充维度，但必须标记

阶段 A 只能派生**语法层**的维度。有些缺陷只能从业务语义识别——例如"这个 `status` 字段只有 `active`/`inactive`，但传入两者之外的合法字符串会怎样"。

因此允许 LLM 在阶段 B **额外提出维度**，但：

- 额外维度必须显式标注 `derived_from: "inferred"`
- 额外维度**不计入** L2 覆盖率的分母（否则模型的发挥空间会稀释指标）
- 额外维度数量设上限（建议 ≤ 阶段 A 维度数的 50%），防止模型用它替换掉确定性维度

---

## 3. 断言模型

本节定义 `cases.assertions`（JSONB 列）的**权威结构**，解答 `04-数据模型.md` §7 的 Q1。

### 3.1 结构

断言是**判别联合**（discriminated union），`type` 为判别键：

```python
class StatusCodeAssertion(BaseModel):
    type: Literal["status_code"]
    expected: int | list[int]  # 100..599
    derived_from: Literal["spec", "inferred"]


class JsonPathAssertion(BaseModel):
    type: Literal["json_path"]
    path: str  # JSONPath，如 $.data.id
    operator: Literal[
        "exists", "not_exists", "eq", "ne", "contains", "matches", "gt", "lt", "in_range"
    ]
    expected: Any | None = None  # operator 需要时必填
    derived_from: Literal["spec", "inferred"]


class HeaderAssertion(BaseModel):
    type: Literal["header"]
    name: str  # 如 content-type
    operator: Literal["exists", "eq", "contains", "matches"]
    expected: str | None = None
    derived_from: Literal["spec", "inferred"]


class SchemaValidAssertion(BaseModel):
    type: Literal["schema_valid"]
    schema_ref: str  # 指向 operations.responses_schema 的某状态码
    derived_from: Literal["spec"]


class BodyContainsAssertion(BaseModel):
    type: Literal["body_contains"]
    text: str  # 用于非 JSON 响应
    derived_from: Literal["inferred"]


class ResponseTimeAssertion(BaseModel):
    type: Literal["response_time"]
    operator: Literal["lt", "gt"]
    expected_ms: int
    derived_from: Literal["inferred"]


Assertion = Annotated[Union[...], Field(discriminator="type")]
```

### 3.2 约束

| 编号 | 约束 | 理由 |
| :--- | :--- | :--- |
| A1 | **断言必须可确定性求值** | 见 TG2 |
| A2 | `operator` 需要期望值时，`expected` 必填 | 结构校验的一部分（L1） |
| A3 | `json_path` 的 `path` 必须语法合法 | L1 校验；非法 JSONPath 会让执行期整条用例崩掉 |
| A4 | `status_code.expected` 必须在 100–599 | 拦掉"生成 `999`"这类语义荒谬但结构合法的输出 |
| A5 | 每条断言带 `derived_from` | §2.3 / §7 |

**TG2：断言禁止由 LLM 判定。**

不引入 `{"type": "llm_judge", ...}` 这类断言。原因是它把"测试是否通过"这个必须确定的事实交给了概率模型——同一个响应可能今天判过、明天判不过，报告与断言失去可信度，且无法排查争议。

需要语义级判断的场景（如"错误消息是否友好"）属于**归因**范畴（`LogAnalyzer`），不是断言。

### 3.3 断言的组装规则

单条用例的断言由两部分组成：

1. **状态码断言**（必有一条，来自维度定义）
2. **内容断言**（仅在 `responses_schema` 声明了该状态码的响应结构时生成）

若 `responses_schema` 缺失或该状态码无 schema，则**只生成状态码断言**，并将用例标记为低置信度（§7）。这是 `PRD` R2"缺 example 时显式降级"的落点。

---

## 4. 结构化输出契约

### 4.1 输出模型

```python
class GeneratedCase(BaseModel):
    dimension: str  # 维度标识，如 "boundary:age"
    name: str  # 人类可读用例名
    category: Literal["normal", "abnormal", "boundary"]
    request_query: dict[str, Any] | None = None
    request_headers: dict[str, str] | None = None  # 不含凭证，见 04 §2.5
    request_body: dict[str, Any] | None = None
    assertions: list[Assertion]  # 至少 1 条
    requires_auth: bool = True  # 置 False 时执行器不注入凭证（`TestExecutor.md` §5.2）
    derived_from: Literal["spec", "inferred"]
    notes: str | None = None  # 生成理由，供人工复核


class GenerationResult(BaseModel):
    operation_notes: str | None = None  # 对该 Operation 的整体说明
    cases: list[GeneratedCase]
```

### 4.2 Schema 生成方式

**JSON Schema 由 Pydantic 模型导出，不手写。**

- 走百炼的 OpenAI 兼容模式，`response_format: {"type": "json_schema", ...}` + `strict: true` + `additionalProperties: false`（`02` §5.2）
- 借助 Pydantic 的 `.parse()` 直接得到校验过的 `GenerationResult` 实例
- **禁止**在 Prompt 里手写字段说明来"告诉模型格式"——那会立刻产生两个事实来源，且必然漂移

### 4.3 调用形态

| 参数 | 取值 | 依据 |
| :--- | :--- | :--- |
| 调用粒度 | **逐 Operation** 一次调用 | TG4，见 §8 |
| `enable_thinking` | 开启 | 边界场景需要推理 |
| 流式 | 开启 | 思考模式强制要求（`02` §5.3） |
| `result_format` | `message` | `02` §5.3 |
| 采样参数 | 由 Eval 实测确定 | 不在此拍定 |

---

## 5. 校验与重试

对应 PRD §4.3 的 L1 / L2 两层，落在本模块：

### 5.1 L1 结构层

由 `strict` Schema 在服务端保证基础结构，客户端**仍然**执行：

| 检查 | 失败处理 |
| :--- | :--- |
| Pydantic 严格校验 | 重试 |
| A2–A4 约束（§3.2） | 重试 |
| `cases` 非空 | 重试 |
| `dimension` 必须在阶段 A 的清单中，或标记为 `inferred` | 重试 |

> **为什么服务端已有 Schema 约束还要客户端再校验**：Schema 保证的是**结构**合法，不保证**业务语义**正确（`02` §5.2）。`status_code: 999` 结构完全合法，但毫无意义。这一层不能省。

### 5.2 L2 覆盖率层

| 检查 | 失败处理 |
| :--- | :--- |
| 阶段 A 的每个维度都有对应用例 | 重试 |
| `responses_schema` 中声明的每个 4xx 状态码，至少有一条用例期望它 | 重试 |
| 每条用例至少有 1 条断言 | 重试 |

覆盖率的分母**只含阶段 A 的确定性维度**（§2.5）。

> **⚠️ 已知局限：这个指标是自我参照的。** 分母由阶段 A 自己算出——若派生算法漏了 `maxLength` 这类约束，分母里就没有它，生成结果里也没有它，**覆盖率仍显示 100%**。任何自动检查都发现不了这件事，因为错的是分母。
>
> 因此 L2 覆盖率只能证明"派生出的维度都被覆盖了"，**不能证明"该派生的维度都派生了"**。后者的验证需要一个**人工标注的独立基准**（Golden Set），见 `09-测试策略.md` §5.2。这是 Golden Set 最重要的用途——它测的不是 LLM，而是**我们的派生算法有没有系统性盲区**。

### 5.3 重试策略

- 上限 **2 次**（PRD §4.3 L1），指数退避
- 重试时**带上失败原因**（哪个维度缺失、哪条断言不合法），让模型能针对性修正——比原样重发有效得多
- **TG7：重试时整体重发，不做增量补生成。** 虽然维度清单是确定的、理论上可以只补缺失项，但那需要合并两次生成的结果、处理维度冲突，复杂度不低。M8 有 5.4 倍余量（`02` §5.4），复杂度不值得。
- 2 次后仍失败 → 该 Operation 置 `run_operations.gen_status = 'failed'`，**不阻塞其他 Operation**（`02` 路由表）

---

## 6. Prompt 结构

### 6.1 三层结构（服务于上下文缓存）

```
┌─ 稳定前缀 ────────────────────────────────┐  ← 缓存断点
│  系统提示：角色 · 生成规则 · 维度分类法 ·   │     整个 Run 内逐字节不变
│            断言类型表 · 输出 Schema        │
├─ 半稳定 ──────────────────────────────────┤
│  该 Spec 的全局信息：                      │     同一 Spec 内不变
│  通用错误响应结构 · 鉴权方式说明            │
├─ 易变 ────────────────────────────────────┤
│  当前 Operation 的裁剪后 schema            │     每个 Operation 变
│  + 阶段 A 派生的维度清单                   │
└───────────────────────────────────────────┘
```

**TG6：稳定前缀必须逐字节不变。**

百炼的上下文缓存按前缀匹配，命中仅收输入价的 10%（`02` §5.4）。以下内容**禁止**出现在稳定前缀中：时间戳、Run ID、随机数、未排序的 dict 序列化、变化的字段枚举顺序。这些是缓存静默失效最常见的原因，且失效时**没有任何报错**——只能靠观测缓存命中 token 数发现（§8.3）。

**裁剪发生在组装提示时，而非存储时。** `operations` 的 schema 字段整块以 JSONB 存储（`04-数据模型.md` §2.10），N2 在拼装本节的三层结构时才按当前 Operation 的需要抽取片段。这样既保住 schema 的整体性（`$ref`、嵌套、条件约束），又满足 §8.1 的单次输入长度约束。

### 6.2 系统提示的内容要点

| 部分 | 内容 |
| :--- | :--- |
| 角色 | API 测试用例设计者 |
| 维度分类法 | §2.2 的维度表，以及每个维度的语义 |
| 生成规则 | one-at-a-time（§2.4）；合法值须符合 schema 约束；不得发明 schema 中不存在的校验 |
| 断言规则 | §3 的断言类型表；**只输出可确定性求值的断言**；期望状态码优先取 `responses` 中声明的 |
| 维度补充 | 允许提出额外维度，但必须标 `inferred`，且不得替换给定维度 |
| 低置信度 | 当 schema 信息不足时，**如实标注**而非猜测 |

最后一条值得强调：必须给模型一条"承认不知道"的路径。否则在 schema 信息不足时，模型会为了完成任务而编造，而这恰恰是 PRD §1.2"易幻觉"痛点的来源。

---

## 7. 低置信度标注

PRD R2 要求"缺 example 时标注低置信度用例，供人工重点复核"。落到本模块，低置信度由**具体原因**构成，而非一个模糊的分数：

| 触发条件 | 影响的维度 |
| :--- | :--- |
| `responses_schema` 未声明该错误状态码 | 所有 4xx 维度（期望值靠推断） |
| 字段无 `example` 也无强约束 | `value_valid`（合法值靠猜） |
| 字段无 `minimum`/`maximum`/长度约束 | 该字段的 `boundary` 无法派生，或派生值靠推断 |
| 参数使用自定义 `format`（非标准格式） | 该参数的 `constraint_violation` |
| 响应体无 schema | 所有内容断言缺失，仅剩状态码断言 |
| 维度标记为 `inferred` | 该用例本身 |

**聚合规则**：一条用例只要含任一 `inferred` 断言或命中任一触发条件，则 `cases.confidence = 'low'`，否则 `'high'`。

**落库**：`cases.confidence`（`04` §2.5）。前端在 IP-1 默认**按置信度排序**，让"低置信度"排在最前——人工确认的注意力应花在模型不确定的地方，而不是逐条看一遍。

---

## 8. 成本控制落点

`02` §5.4 的成本预算（≈ ¥0.37 / Run）依赖本模块的三个行为，实现时必须保持：

### 8.1 逐 Operation 调用（TG4）

**不把多个 Operation 合并进一次调用。** Qwen3.5-Plus 按单次请求输入长度分档，超过 128K 单价跳 5 倍（`02` §5.1）。合并调用看似省次数，实则可能把整个 Spec 推过阈值。

逐 Operation 天然把单次输入限制在"一个 interface 的 schema + 维度清单"，量级在几 KB。

### 8.2 稳定前缀命中缓存（TG6）

见 §6.1。验证方式见 §8.3。

### 8.3 缓存命中的可观测性

必须记录每次调用的缓存命中 token 数与总用量（PRD §5 可观测性要求）。**若缓存命中长期为 0，视为缺陷而非正常波动**——它意味着输入成本涨了 10 倍，而这个问题不会以任何报错的形式出现。

### 8.4 无信息 Operation 走纯规则（TG5）

若一个 Operation 同时满足：**无请求参数、无请求体、`responses_schema` 中无 4xx 声明**——那么阶段 A 的维度清单只含 `value_valid` 一条，没有需要 LLM 发挥的余地。

此时**不调用 LLM**，直接按规则生成一条用例。这是 `02` D3（"AI 只用在该用的地方"）在模块内部的延伸：不是所有 Operation 都值得一次 LLM 调用。

---

## 9. Eval 集成

对应 PRD §4.3 的 L3，本模块需产出的评测能力：

| 指标 | 计算方式 | 自动化程度 |
| :--- | :--- | :--- |
| 维度覆盖率 | 已生成维度 / 阶段 A 维度清单 | 全自动 |
| 结构合法率 | 一次通过 L1 的 Operation 数 / 总数 | 全自动 |
| 人工采纳率（M1） | `cases.review_status = 'approved'` 的比例 | 全自动（来自 IP-1） |
| 断言准确率 | 人工抽检断言是否正确 | 需人工标注 |

**Golden Set**（PRD §4.3）：人工准备 10 个接口的参考用例集，存放于 `tests/eval/golden/`，与代码同目录维护。用途有二：

1. 比对维度覆盖的完整性（模型漏了哪些维度）
2. **档位对比**——`02` §5.1 提到 V1.0 可能降级到 Flash 档位，届时须在此数据集上测量质量损失。`cases.llm_model` 字段（`04` §2.5）使不同档位的生成结果可追溯到同一条用例上

跑 L3 时使用 **Batch 调用**（5 折，`02` §5.4）。

---

## 10. 错误与降级

| 情况 | 处理 |
| :--- | :--- |
| LLM 调用失败（网络 / 限流） | 指数退避重试 2 次；仍失败 → 该 Operation 置 `failed` |
| L1 / L2 校验失败 | 带失败原因重试 2 次；仍失败 → 该 Operation 置 `failed` |
| `parameters_schema` 部分无法解析 | **降级**：仅生成 `value_valid` 维度，该 Operation 的用例全部标 `low` 置信度 |
| 整个 Operation 无可用 schema | 走 TG5 的纯规则路径；若连状态码都无声明，则该 Operation 置 `failed` 并记入 `node_failures` |

**降级的原则**：能生成一部分优于完全不生成，但**降级必须留下痕迹**——要么在 `confidence` 上体现，要么在 `node_failures` 中记录。静默降级会让报告的可信度无法评估。

---

## 11. 决策汇总

| 编号 | 决策 | 要点 |
| :--- | :--- | :--- |
| **TG1** | 维度确定，取值生成 | 测试维度由 schema 确定性派生，LLM 只负责填值与补维度。这使覆盖率可校验、幻觉有边界 |
| **TG2** | 断言必须可确定性求值 | 禁止 LLM 判定型断言——它会让"通过与否"这个事实变成概率 |
| **TG3** | 断言带 `derived_from` | 区分来自 spec 与推断的断言，聚合为用例置信度 |
| **TG4** | 逐 Operation 调用 | 避免合并调用触发 Qwen 阶梯计费跳档（5 倍） |
| **TG5** | 无信息 Operation 走纯规则 | 无参数、无 4xx 声明的接口不必调用 LLM |
| **TG6** | 稳定前缀逐字节不变 | 缓存命中与否无报错，只能靠观测发现 |
| **TG7** | 重试整体重发，不做增量补生成 | M8 有 5.4 倍余量，复杂度不值得 |

---

## 12. 开放问题

| 编号 | 问题 | 影响 | 待决 |
| :--- | :--- | :--- | :--- |
| Q1 | 阶段 A 的维度分类法是否够全？业务语义维度（如"分页参数越界"）目前靠 LLM 补充 | 影响覆盖率的上限 | Golden Set 建立后评估 |
| Q2 | 参数组合测试（pairwise）的收益是否值得引入 | 影响 §2.4 的 one-at-a-time 策略 | V1.0，需数据支撑 |
| Q3 | 自定义 `format` 的语义映射表由谁维护（如 `format: uuid-v7`） | 影响 `constraint_violation` 维度的生成质量 | V1.0 |
| Q4 | 额外维度数量上限（暂定 ≤ 确定性维度的 50%）是否合理 | 影响模型发挥空间 | Golden Set 实测 |

---

## 13. 与上游文档的追溯

| 上游条目 | 本文档落点 |
| :--- | :--- |
| PRD F2 生成正常/异常/边界用例 | §2.2 维度分类法 |
| PRD §4.3 L1 结构校验 + 重试 2 次 | §5.1、§5.3 |
| PRD §4.3 L2 覆盖率校验 | §5.2 |
| PRD §4.3 L3 Golden Set | §9 |
| PRD R2 Spec 质量参差 → 低置信度标注 | §7 |
| PRD M1 用例采纳率 | §9（数据来自 IP-1 的 `review_status`） |
| PRD M8 成本 | §8 |
| PRD §1.1 "AI 只用在该用的地方" | TG5、§2.1 |
| 02 D3 SpecParser 不调 LLM | §1.2（输入已是结构化数据） |
| 02 §5.1 阶梯计费 | TG4、§8.1 |
| 02 §5.2 结构化输出限制 | §4.2 |
| 02 §5.4 上下文缓存 | TG6、§6.1、§8.3 |
| 04 §2.5 `cases` 表 | §4.1 输出模型映射 |
| 04 §2.10 `assertions` 列结构 | **§3（已解答）** |
| 04 §2.5 `cases` 表 | §4.1 输出模型映射；本文档为其新增 `dimension` 列（`04` v0.2）、`requires_auth` 列（`04` v0.4，由 `TestExecutor.md` §5.2 驱动） |
| 06 §3 节点 N2 | §1.1 |
| 06 §5.3 N2 幂等语义 | 本文档不含幂等逻辑（由 `run_operations` 承载，见 04 DM4） |

---

## 14. 变更记录

| 版本 | 日期 | 变更内容 |
| :--- | :--- | :--- |
| v0.1 | 2026-10-13 | 初稿：维度确定/取值生成的核心主张、7 类维度、断言判别联合模型（解答 `04` Q1）、结构化输出契约、L1/L2 校验与重试、三层 Prompt 结构、低置信度标注、成本控制落点、Eval 集成、降级策略，沉淀 TG1~TG7 |
