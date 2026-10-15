# TestFlow API 契约

| 项目 | 内容 |
| :--- | :--- |
| 文档版本 | v0.2 |
| 文档状态 | 评审中 |
| 最后更新 | 2026-10-14 |
| 上游文档 | [01-TestFlow_PRD.md](01-TestFlow_PRD.md)、[02-架构设计.md](02-架构设计.md)、[04-数据模型.md](04-数据模型.md)、[06-Agent编排.md](06-Agent编排.md)、[03-模块详细设计/](03-模块详细设计/) |

> **本文档的边界**：定义 HTTP 接口的设计意图、通用约定、端点清单与变更流程。
> **不做**：逐字段的 Schema（那由代码生成的 `openapi.json` 承担，见 §2）、表结构（`04-数据模型.md`）、业务逻辑（`03-模块详细设计/`）。

**编号约定**：`API` = 接口设计决策。`SR`/`M`/`F`/`R`/`Q` 沿用上游编号。

---

## 1. 契约的载体

### 1.1 唯一事实来源是代码

**API1：契约的唯一事实来源是 FastAPI 的路由与 Pydantic 模型；`openapi.json` 是它的产物，不是输入。**

> **⚠️ 对 `01-TestFlow_PRD.md` §7 的修订**：该处原写"`openapi.yaml`（FastAPI 据此生成，为唯一事实来源）"，**方向写反了**。FastAPI 是代码优先框架——它从代码生成 OpenAPI 文档，不能反向接收一份手写的 `openapi.yaml` 来生成服务端。见该文档 v0.4。

理由：手写 `openapi.yaml` 再让代码去迎合它，会立刻产生两份需要同步的契约——**这正是本项目从第一份文档起就在避免的"两个事实来源"**（PRD §7）。FastAPI 的自动生成是选用它的主要理由之一，不该主动放弃。

### 1.2 契约的可见性与变更

生成的 `openapi.json` **提交进仓库**，并在 CI 中校验它是最新的：

| 环节 | 做法 |
| :--- | :--- |
| 生成 | 从运行中的应用导出，落到仓库固定路径 |
| 提交 | 与代码一同提交，使契约变更**出现在 diff 里** |
| CI 校验 | 重新生成并与仓库中的快照比对，不一致则构建失败 |

最后一条是关键：若不校验，"实现改了但契约没更新"会静默发生，而契约的消费者（CLI、前端、未来的外部集成）不会得到任何提示。

### 1.3 版本策略

**API2：MVP 不加 URL 版本前缀。**

理由：目前只有两个仓库内的消费者（前端与 CLI），它们与后端**同版本发布**。此时加 `/v1` 并不提供任何保护，反而暗示了一个尚未承诺的稳定性。

引入时机：V1.0 对外开放（外部 CI 集成、脚本调用）时。届时新增带版本的前缀，旧的无前缀路径可保留一段时间。

---

## 2. 通用约定

### 2.1 认证

**API3：默认仅监听 `127.0.0.1`；绑定到非环回地址时强制要求 Token。**

| 绑定地址 | 认证要求 |
| :--- | :--- |
| `127.0.0.1`（默认） | 可选——单机自用场景 |
| 非环回地址 | **强制**，未配置 Token 则拒绝启动 |

这是"默认安全"而非"默认开放"：MVP 是单机 Docker Compose（PRD §5），绝大多数用户不会暴露端口；而不小心 `-p 0.0.0.0:3000` 的后果是整个测试平台（含凭证存储）对局域网开放。**让用户主动承担暴露的代价，而不是让他们主动发现风险。**

Token 通过 `Authorization: Bearer <token>` 传递：

| 消费者 | Token 来源 |
| :--- | :--- |
| CLI | 环境变量 `TESTFLOW_TOKEN` 或配置文件 |
| 前端 | 首次访问时输入并存入浏览器本地存储 |

### 2.2 错误格式

**API4：错误响应统一为 `application/problem+json`（RFC 9457）。**

```json
{
  "type": "https://testflow.local/errors/spec-invalid",
  "title": "Spec 解析失败",
  "status": 422,
  "detail": "3 处 schema 校验错误",
  "instance": "/api/specs",
  "errors": [
    {"pointer": "/paths/~1users/get/responses/200", "message": "缺少 content 定义"}
  ]
}
```

`errors` 数组是本项目的扩展字段——Spec 校验错误常有**多条且需要定位**，把它们塞进一个 `detail` 字符串会让用户无法定位（`SpecParser.md` SP4 要求硬失败时给出可定位错误）。

### 2.3 分页

**API5：列表接口统一 `limit` + `offset`，响应含总数。**

| 参数 | 默认 | 上限 |
| :--- | :--- | :--- |
| `limit` | 50 | 500 |
| `offset` | 0 | — |

大 Run 可能有 2000 条用例（PRD §5 规模假设），必须分页。不用游标分页：用例在生成后**不再变动**（`04` DM4：已完成则跳过），没有游标分页要解决的"数据在翻页时变化"问题。

### 2.4 幂等

**API6：`POST /api/runs` 支持可选的 `Idempotency-Key` 请求头。**

| 情况 | 行为 |
| :--- | :--- |
| 无该头 | 每次调用创建新 Run（默认） |
| 带该头，且此前用过同值 | 返回首次创建的 Run，**不新建** |
| 带该头，首次使用 | 正常创建，记录该 key 与该 Run 的绑定 |

存在的理由是一个具体场景：CLI 提交后网络超时——**客户端不知道 Run 建了没建**。没有这个头，用户只能重试，而重试会创建第二个 Run，跑一遍完全相同的测试（还可能污染数据）。有了它，重试是安全的。

`POST /api/runs/{id}/decisions` 天然幂等（`06` §3 的 `apply_decisions` 按 `(run_id, case_id)` upsert），不需要额外机制。

---

## 3. 端点清单

### 3.1 面向客户端

| 方法 | 路径 | 用途 | 使用方 |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/specs` | 上传/提交 Spec，触发解析 | 前端、CLI |
| `GET` | `/api/specs` | Spec 列表 | 前端 |
| `GET` | `/api/specs/{spec_id}` | Spec 详情 | 前端、CLI |
| `GET` | `/api/specs/{spec_id}/operations` | Operation 列表 | 前端、CLI |
| `POST` | `/api/runs` | 创建并启动 Run | 前端、CLI |
| `GET` | `/api/runs` | Run 列表 | 前端 |
| `GET` | `/api/runs/{run_id}` | Run 状态 | 前端、CLI |
| `GET` | `/api/runs/{run_id}/events` | **SSE 事件流** | 前端、CLI |
| `GET` | `/api/runs/{run_id}/cases` | 用例列表（分页） | 前端 |
| `POST` | `/api/runs/{run_id}/decisions` | 提交 HITL 决策，恢复 IP-1 | 前端 |
| `GET` | `/api/runs/{run_id}/report` | 报告 | 前端、CLI |
| `GET` | `/api/credentials` | 凭证列表（**不含明文**） | 前端 |
| `POST` | `/api/credentials` | 添加凭证 | 前端 |
| `DELETE` | `/api/credentials/{cred_id}` | 删除凭证 | 前端 |
| `GET` | `/healthz` · `/readyz` | 存活 / 就绪探针 | 运维 |

### 3.2 内部

| 方法 | 路径 | 用途 |
| :--- | :--- | :--- |
| `POST` | `/internal/runs/{run_id}/wake` | Celery Worker 完成回调，唤醒图（`06` §6.1） |

内部端点**不出现在公开的 `openapi.json` 中**（FastAPI 的 `include_in_schema=False`），且使用独立的 Token——它只应被本系统的进程调用，不是 API 面的一部分。

---

## 4. 关键端点定义

### 4.1 `POST /api/specs`

提交 Spec。**同一份内容重复提交不会重复解析**（`SpecParser.md` §4.3 的指纹去重）。

**请求**：`multipart/form-data`（文件）或 `application/json`（粘贴内容）

| 字段 | 说明 |
| :--- | :--- |
| `content` | Spec 原文 |
| `filename` | 可选，仅作展示 |

**响应** `201 Created`（新建）或 `200 OK`（指纹命中已有 Spec）：

```json
{
  "spec_id": "0191...",
  "fingerprint": "a3f2...",
  "openapi_version": "3.0.3",
  "operation_count": 42,
  "declared_targets": ["https://api.example.com:443"],
  "reused": true,
  "warnings": [
    {"code": "relative_server_url", "detail": "servers[0].url 为相对路径 /api/v1，无法确定主机，相关请求将被 Guardrail 拒绝"}
  ]
}
```

**`warnings` 不可省略**：`SpecParser.md` SP3 与 §5.3 说明的降级情形（相对 URL、无 `servers`、`$ref` 超深）必须**在响应中告知用户**——否则用户后续看到的 `blocked_by_policy` 归因会被误认为是平台故障。

**错误**：

| 状态 | `type` | 触发 |
| :--- | :--- | :--- |
| `422` | `spec-invalid` | 不符合 OpenAPI 规范（`errors` 数组给出定位） |
| `422` | `spec-parse-failed` | 语法错误、`$ref` 悬空（`SpecParser.md` SP4 的硬失败） |
| `415` | `unsupported-version` | 非 3.0/3.1（MVP 见 `SpecParser.md` Q2） |

### 4.2 `POST /api/runs`

创建并启动 Run。

**请求**：

```json
{
  "spec_id": "0191...",
  "operation_ids": ["0192...", "0193..."]   // 省略则覆盖全部 Operation
}
```

**响应** `202 Accepted`：

```json
{
  "run_id": "0194...",
  "phase": "init",
  "expected_operation_count": 42
}
```

**为什么是 `202` 而非 `201`**：Run 的创建只是**受理**，实际工作在异步队列里（`06` §1.2 的短跳推进）。`202` 准确表达了"已接受、尚未完成"。

**错误**：

| 状态 | `type` | 触发 |
| :--- | :--- | :--- |
| `404` | `spec-not-found` | |
| `422` | `operation-not-in-spec` | 传入了不属于该 Spec 的 operation_id |
| `503` | `orchestrator-unavailable` | 编排队列不可用——**受理失败必须如实返回**，不能返回一个永远不推进的 Run |

### 4.3 `GET /api/runs/{run_id}`

Run 状态。这是 CLI `status` 与前端轮询的共用接口。

```json
{
  "run_id": "0194...",
  "phase": "awaiting_review",
  "spec_id": "0191...",
  "spec_fingerprint": "a3f2...",
  "expected_case_count": 118,
  "settled_case_count": 0,
  "approved_case_count": 0,
  "created_at": "2026-10-13T10:00:00Z",
  "updated_at": "2026-10-13T10:02:11Z",
  "interrupt": {
    "type": "hitl_review",
    "since": "2026-10-13T10:02:11Z"
  },
  "failure_reason": null
}
```

`phase` 取值即 `06` §2.2 的 `RunPhase`。`interrupt` 字段仅在 Run 处于中断态时出现——**客户端据此决定是否提示用户去确认**，不需要自己推断 phase 与中断态的对应关系。

### 4.4 `GET /api/runs/{run_id}/events`（SSE）

事件流。**任何状态的权威来源都是 REST**（§5.1），SSE 只负责通知"有变化了"。

| 事件 | `data` | 含义 |
| :--- | :--- | :--- |
| `run.phase` | `{"phase": "generating"}` | 阶段变化 |
| `run.interrupted` | `{"type": "hitl_review"}` | 进入中断，需人工介入 |
| `run.progress` | `{"settled": 40, "expected": 118}` | 执行进度 |
| `run.completed` | `{}` | 正常结束 |
| `run.failed` | `{"reason": "..."}` | 异常结束 |
| `heartbeat` | `{}` | 保活（约 15s） |

**⚠️ M1 实现回写——M1 的实现方式是「短间隔轮询数据库」，不是真推送。**

真正的"事件推送"要求 Gateway 知道编排器/Worker 侧的进展，而那需要一条**跨进程的事件总线**。M1 没有它——所以实现是按固定间隔（1 秒）查 `runs` 表的 `phase`，变了就发一条事件。

**这是一个偏差，明确记录**：事件延迟是**轮询间隔的量级（秒）**，而真推送是毫秒。

**代价换来的是不引入一套总线**——Redis pub/sub 会因为断线丢消息，而"REST 是权威"（§5.1）这条退路让我们**丢了消息也不影响正确性**，只是晚一点收到通知。而一套自己维护的总线需要额外的运维与失效处理。

**M1 未实现的事件**：`run.interrupted` 与 `run.progress`。

* `run.interrupted` 依赖 IP-1（M1 没有确认门，见 `06` §4），M2 随确认门一起加
* `run.progress` 需要每完成一条用例就更新一次计数——那是**每 Run 上千次写**，而它提供的只是"进度条好看一点"。M1 用"阶段变化"已经能回答用户真正关心的问题（"跑完了没有"）

`heartbeat` 在 M1 是**注释行**（`: heartbeat`）而不是一个事件——SSE 规范里注释行同样能起到保活作用，而它不会被客户端当成业务事件处理。用事件的形式发心跳会让客户端多写一个"忽略它"的分支。

### 4.5 `POST /api/runs/{run_id}/decisions`

提交 HITL 决策，恢复 IP-1 中断。

```json
{
  "decisions": [
    {"case_id": "0195...", "decision": "approve"},
    {"case_id": "0196...", "decision": "reject"}
  ]
}
```

**语义要点**：

| 项 | 说明 |
| :--- | :--- |
| **必须完整** | 该 Run 的所有 `review_status = 'pending'` 用例都必须出现在本次请求中。部分提交会让图在"还有未决用例"的状态下恢复，与 IP-1 的语义矛盾 |
| **幂等** | 重复提交同一批决策无副作用（按 `(run_id, case_id)` upsert，`06` §3） |
| **前置条件** | 仅当 `phase = awaiting_review` 时接受 |

**错误**：

| 状态 | `type` | 触发 |
| :--- | :--- | :--- |
| `409` | `run-not-awaiting-review` | Run 不在待确认状态（可能已被恢复或已结束） |
| `422` | `decision-incomplete` | 有未决用例未出现在请求中（`detail` 列出数量） |

`409` 的存在是必要的：用户可能开了两个标签页，或点了两次"提交"。**第二次提交必须被明确拒绝并说明原因**，而不是静默地再次推进图。

### 4.6 `GET /api/runs/{run_id}/report`

报告。`Accept` 决定形态：

| `Accept` | 返回 |
| :--- | :--- |
| `application/json` | `reports.content`（结构化聚合，供前端） |
| `text/markdown` | `reports.rendered`（快照，供 CLI 与导出） |

**两者返回的是同一次生成的结果**（`04` §2.8 的 `content` 与 `rendered`），不会因请求格式不同而重新计算。这是 `ReportAgent.md` RA4/RA5 的直接体现。

若 Run 尚未产生报告 → `404 report-not-ready`。

### 4.7 `POST /internal/runs/{run_id}/wake`

见 `06` §6.1。**幂等**：无论调用多少次，行为一致。

```json
{"status": "pending"}     // 仍有未完成的用例，图保持等待
{"status": "resumed"}     // 全部到终态，已投递推进
```

不返回 `202` 而是返回一个明确的判定结果，使 Worker 侧的行为可观测——排查"图为什么没推进"时，这个响应是第一个要看的东西。

---

## 5. 设计决策

### 5.1 SSE 只发信号，不发数据

**API7：SSE 事件只携带轻量信号，客户端收到后通过 REST 拉取实际数据。**

理由：

- **避免第二个数据通道**：若 SSE 承载用例内容、报告数据，它就会变成与 REST 平行的数据源，两者的一致性问题立刻出现
- **断线重连天然正确**：客户端重连后按 REST 拉一次当前状态即可，**不需要事件回放机制**（`Last-Event-ID`、事件缓冲等）
- 与 `02` §2.1 的"所有状态由服务端持有，客户端不做状态推断"一致

代价是每次变化多一次 REST 调用。在 Run 的规模下（单 Run 数分钟、事件数十条）可以忽略。

### 5.2 短命资源的创建返回 `202`

**API8：启动 Run 返回 `202` 而非 `201`。**

`201` 意味着资源已创建完毕；而 Run 只是被受理，真正的工作在队列里。用 `201` 会让客户端以为可以立即读取完整状态。语义的准确性在这里有实际价值——它提示客户端"应当订阅事件或轮询，而不是立即读结果"。

### 5.3 内部端点的隔离

内部端点（`/internal/*`）有三重隔离：

| 隔离 | 做法 |
| :--- | :--- |
| schema | `include_in_schema=False`，不进入公开契约 |
| 认证 | 独立 Token，与用户侧 Token 不同 |
| 网络 | 部署时仅内网可达（`10-部署运维.md`） |

理由：`/internal/runs/{id}/wake` 能触发图推进。若它与用户接口共用认证，一个泄露的用户 Token 就能干扰任意 Run 的执行。

### 5.4 警告与错误分开

**API9：可继续的降级用 `warnings` 返回，不可继续的用错误状态码。**

`SpecParser.md` SP4 区分了"文档缺陷"（硬失败）与"信息不全"（降级继续）。这个区分必须体现在 API 上：

- 硬失败 → 4xx + `problem+json`
- 降级继续 → **2xx 成功响应 + `warnings` 数组**

把降级也做成错误会迫使客户端处理"部分成功"这种尴尬状态；把硬失败做成警告则会让用户以为可以继续。两者的处理路径不同，响应形态就该不同。

---

## 6. 决策汇总

| 编号 | 决策 | 要点 |
| :--- | :--- | :--- |
| **API1** | 契约的唯一事实来源是代码，`openapi.json` 是产物 | 纠正 PRD §7 的反向表述；手写 spec 会造出第二个事实来源 |
| **API2** | MVP 不加 URL 版本前缀 | 消费者与后端同版本发布，加版本不提供保护却暗示了未承诺的稳定性 |
| **API3** | 默认绑定环回；非环回时强制 Token | 默认安全；让暴露的代价显式化 |
| **API4** | 错误用 RFC 9457 + 扩展 `errors` 数组 | Spec 校验错误需多条定位，塞进 `detail` 字符串无法使用 |
| **API5** | 分页用 `limit`/`offset` | 用例生成后不再变动，游标分页要解决的问题不存在 |
| **API6** | `POST /api/runs` 支持 `Idempotency-Key` | CLI 提交超时后客户端不知道是否成功，重试会跑第二遍相同测试 |
| **API7** | SSE 只发信号不发数据 | 避免第二个数据通道；断线重连无需事件回放 |
| **API8** | 启动 Run 返回 `202` | 语义准确，提示客户端应订阅而非立即读结果 |
| **API9** | 可继续的降级用 `warnings`，不可继续的用 4xx | 两者的客户端处理路径不同 |

---

## 7. 开放问题

| 编号 | 问题 | 影响 | 待决 |
| :--- | :--- | :--- | :--- |
| Q3 | 前端如何管理 Token（localStorage 的 XSS 风险） | 影响前端设计 | MVP 单机场景风险有限；V1.0 重新评估 |
| Q4 | 是否需要 `GET /api/runs/{id}/cases/{case_id}` 单条查询 | 影响端点数量 | 前端实现时按需加 |
| Q5 | 报告导出的格式（对应 `ReportAgent.md` Q2） | 影响本接口的 `Accept` 分支 | V1.0 |

**已关闭问题**：

| 问题 | 关闭结论 |
| :--- | :--- |
| ~~`openapi.json` 提交路径~~（v0.1 的 Q1） | 定于仓库根目录 `openapi.json`，见 `08-工程规范.md` §1 的目录结构与 EN6 |
| ~~`Idempotency-Key` 的存储~~（v0.1 的 Q2） | 存 **Redis**，key 为 `idem:{sha256(key)}` → `run_id`，TTL 24 小时。**不新建 PG 表**——它是短期去重，不是业务事实，不该进入需要长期保留的事实层（`10-部署运维.md` §10） |

---

## 8. 与上游文档的追溯

| 上游条目 | 本文档落点 |
| :--- | :--- |
| PRD F1 Spec 上传/粘贴/本地路径 | §4.1（三种来源都走同一端点） |
| PRD F3 HITL 确认门 | §4.5 |
| PRD F6 测试报告 | §4.6 |
| PRD F8 可中断可恢复 | §4.5（恢复）、§4.3（`interrupt` 字段） |
| **PRD F9 命令行入口** | §3.1（CLI 使用的端点集合）、§2.1（Token 来源） |
| PRD §7 文档地图 | **§1.1（纠正该处的反向表述）** |
| PRD SR-6 凭证管理 | §3.1（凭证端点，列表不含明文） |
| 02 §2.1 接入层两个客户端 | §3.1 的"使用方"列 |
| 02 §2.2 Gateway 职责 | §2.1、§2.2 |
| 02 §5 规模假设 | §2.3（分页上限） |
| 04 §2.8 `reports` 的 `content` / `rendered` | §4.6 |
| 04 DM4 N2 幂等语义 | §2.3（用例不再变动，故用 offset 分页） |
| 05 §2.2 日志脱敏 | §2.1（Token 不走日志） |
| 06 §2.2 `RunPhase` | §4.3 |
| 06 §3 `apply_decisions` 幂等 | §2.4、§4.5 |
| 06 §6.1 唤醒端点 | §4.7 |
| 03 SpecParser SP3 / SP4 | §4.1 的 `warnings`、§5.4 |
| 03 ReportAgent RA4 / RA5 | §4.6 |
| 03 TestExecutor TE3 | §4.5（决策接口不提供破坏性授权入口） |

---

## 9. 变更记录

| 版本 | 日期 | 变更内容 |
| :--- | :--- | :--- |
| v0.1 | 2026-10-13 | 初稿：契约治理（代码优先，**纠正 PRD §7 的反向表述**）、通用约定（认证/错误/分页/幂等）、15 个客户端端点 + 1 个内部端点、关键端点详述、SSE 事件表，沉淀 API1~API9 |
| v0.2 | 2026-10-14 | **随 M1 实现回写（1 处）**：§4.4 补记 M1 的 SSE 是**短间隔轮询数据库**而非真推送（偏差与代价），并说明 `run.interrupted` / `run.progress` 两个事件 M1 未实现及其理由；`heartbeat` 用 SSE 注释行而非事件的形式。**M1 实际交付 5 个端点**（`POST /api/specs`、`POST /api/runs`、`GET /api/runs/{id}`、`GET /api/runs/{id}/report`、`GET /api/runs/{id}/events` + 内部唤醒 + `/healthz`），其余端点属 M2 及以后 |
