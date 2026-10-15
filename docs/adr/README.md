# 架构决策记录（ADR）

本目录收录 TestFlow 的架构决策记录。

## 什么值得写成 ADR

项目在设计阶段积累了约 114 条编号决策，分布在 `docs/` 各文档的决策表中。**不是每一条都值得单开一篇。**

一条决策进入本目录，需满足：

1. **有明确的备选方案被否决过**——否则它只是"做法"，不是"决策"
2. **改变它会导致显著返工**
3. **未来读代码的人会问"为什么这么做"**

按此标准入选 **27 条**（约 24%）。其余决策保留在原文档的决策表中——它们依然有效，只是理由直白，不需要单独论证。

## 格式

每篇包含四部分：

| 部分 | 回答的问题 |
| :--- | :--- |
| **背景** | 为什么会有这个问题 |
| **决策** | 我们选了什么 |
| **被否决的备选** | 还考虑过什么，以及为什么没选 |
| **后果** | 正面与代价 |

**「被否决的备选」是最重要的部分。** 一个决策的价值不在于"我们这么做了"，而在于"我们知道另一条路错在哪"。

## 索引

### 架构与编排

| 编号 | 标题 | 来源 |
| :--- | :--- | :--- |
| [0001](0001-async-orchestration-sync-execution.md) | 编排层异步、执行层同步 | `02` D1 |
| [0002](0002-guardrail-single-egress-path.md) | Guardrail 作为唯一出站通道 | `02` D2、`03/Guardrail` GR7、`08` EN2 |
| [0003](0003-no-llm-for-deterministic-tasks.md) | 确定性任务不引入 LLM | `02` D3、D5 |
| [0004](0004-single-queue-for-mvp.md) | MVP 采用单队列，不做长短任务分流 | `02` D6 |
| [0005](0005-llm-provider-abstraction.md) | LLM 调用收敛到 Provider 接口 | `02` D7 |
| [0006](0006-graph-state-holds-references.md) | GraphState 只存引用不存大对象 | `06` G3 |
| [0007](0007-unified-interrupt-mechanism.md) | 两个中断点统一使用 interrupt 机制 | `06` G6 |
| [0008](0008-callback-wakeup-via-internal-http.md) | 回调唤醒：内部 HTTP 端点 + 查库判定完成 | `06` G7、G8 |
| [0009](0009-postgres-as-source-of-truth.md) | PostgreSQL 是唯一事实来源 | `02` D4、`04` DM5 |

### 数据与幂等

| 编号 | 标题 | 来源 |
| :--- | :--- | :--- |
| [0010](0010-idempotency-via-unique-constraint.md) | 幂等由数据库唯一约束保证 | `04` DM1 |
| [0011](0011-llm-node-idempotency-is-skip.md) | LLM 生成节点的幂等是「跳过」而非「重放」 | `04` DM4 |
| [0012](0012-spec-fingerprint-after-ref-expansion.md) | Spec 指纹在 `$ref` 展开后规范化计算 | `04` DM2、`03/SpecParser` SP5 |

### AI 生成与归因

| 编号 | 标题 | 来源 |
| :--- | :--- | :--- |
| [0013](0013-schema-derived-dimensions.md) | 用 schema 派生测试维度，LLM 只负责取值 | `03/TestCaseGen` TG1 |
| [0014](0014-deterministic-assertions-only.md) | 断言必须可确定性求值 | `03/TestCaseGen` TG2 |
| [0015](0015-three-state-assertion-outcome.md) | 断言级区分「不符」与「无法求值」 | `03/TestExecutor` TE2 |
| [0016](0016-rules-first-attribution.md) | 归因规则优先、LLM 兜底 | `03/LogAnalyzer` LA1 |
| [0017](0017-no-retrieval-attribution-in-mvp.md) | 不引入检索式归因 | `03/LogAnalyzer` LA5 |

### 安全

| 编号 | 标题 | 来源 |
| :--- | :--- | :--- |
| [0018](0018-private-target-exceptions-not-from-spec.md) | 内网例外只能来自配置，永不从 Spec 派生 | `03/Guardrail` §4.6 |
| [0019](0019-validate-resolved-ip-and-pin.md) | 校验解析后的 IP 并钉死连接目标 | `03/Guardrail` GR1、GR2 |
| [0020](0020-no-redirect-following.md) | 不跟随重定向 | `03/Guardrail` GR5 |
| [0021](0021-no-destructive-authorization-in-mvp.md) | MVP 不提供破坏性用例的执行与授权入口 | `03/TestExecutor` TE3 |
| [0022](0022-safety-baselines-are-not-configurable.md) | 安全底线不做成配置项 | `10` OP1、`05` API3 |

### 契约与工程

| 编号 | 标题 | 来源 |
| :--- | :--- | :--- |
| [0023](0023-code-as-contract-source-of-truth.md) | 契约以代码为唯一事实来源 | `05` API1、`08` EN6 |
| [0024](0024-sse-signals-only.md) | SSE 只发信号，不发数据 | `05` API7 |
| [0025](0025-separate-platform-tests-from-ai-eval.md) | 平台测试与 AI 评测严格分开 | `09` TS1 |
| [0026](0026-external-baseline-for-coverage-metric.md) | 覆盖率指标必须有人工独立基准 | `09` TS5 |
| [0027](0027-end-to-end-at-every-milestone.md) | 每个里程碑都必须端到端可跑 | `11` RM1 |

## 状态约定

| 状态 | 含义 |
| :--- | :--- |
| 已接受 | 当前遵循 |
| 已取代 | 被后续决策取代，保留记录 |
| 已废弃 | 不再适用，保留记录 |

本目录当前全部为**已接受**。

## 与其他文档的关系

- **决策表的权威定义在各正式文档中**，本目录是它们的论证展开
- 两者冲突时，以正式文档为准并修正本目录
- `docs/学习笔记/` 是这 27 条的大白话版本，**不对外发布**（已在 `.gitignore` 中排除）
