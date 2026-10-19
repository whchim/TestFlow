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
  "type": "https://testflow.local/problems/spec_invalid",
  "title": "文档无法解析",
  "status": 400,
  "detail": "`$ref` 指向不存在的位置：'#/components/schemas/User'",
  "code": "dangling_ref",
  "errors": [
    {"pointer": "/paths/~1users/get/responses/200", "message": "缺少 content 定义"}
  ]
}
```

**`type` 与 `code` 分工不同，多数时候同词、有需要时不同词：**

| 字段 | 是什么 | 给谁 |
| :--- | :--- | :--- |
| `type` | RFC 9457 要求的**问题类型 URI**：`https://testflow.local/problems/` + 一个**稳定的类别词** | 机器识别"这是哪一类问题" |
| `code` | **具体错误码** | 调用方做分支判断（**不要把 `title` 当错误码**，它是给人读的，改文案不该影响调用方逻辑） |

多数错误两者同词——`run_not_found` 既是 `type` 的后缀也是 `code`。
**Spec 解析失败是刻意的例外**：那时 `type` 恒为 `spec_invalid`，
而 `code` 说明**具体是哪一种文档缺陷**（`syntax_error` / `not_an_object` /
`dangling_ref` / `invalid_openapi` / `missing_openapi_version` /
`external_ref_unsupported`）。理由是这一类的 `errors` 数组是**逐条**的，
类别必须粗粒度且稳定，具体原因才有必要细分。本文档各端点的错误表里，
那一列写的是 `type`（表格列名沿用 `type`）。

**两者一律用小写字母加下划线**（`spec_invalid`、`run_not_found`），
不混用中划线——同一份契约里两种拼法对消费者是实打实的负担
（ADR-0023：接口契约以代码为准，本约定从 M2 起统一；此前文档里的中划线
写法已全部回改）。

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
| `GET` | `/api/runs/{run_id}/attributions` | 失败归因列表（分页） | 前端、CLI |
| `PATCH` | `/api/runs/{run_id}/attributions/{attribution_id}` | 人工归因表态 | 前端 |
| `GET` | `/api/runs/{run_id}/report` | 报告 | 前端、CLI |
| `GET` | `/api/credentials` | 凭证列表（**不含明文**） | 前端 |
| `POST` | `/api/credentials` | 添加凭证 | 前端 |
| `DELETE` | `/api/credentials/{cred_id}` | 删除凭证 | 前端 |
| `GET` | `/healthz` · `/readyz` | 存活 / 就绪探针 | 运维 |

两个探针**不需要认证**：运维探针与部署脚本往往拿不到用户令牌，而它们
只返回进程状态与依赖可达性（失败时也只给异常类型，不给异常消息）。
`/healthz` 不碰依赖；`/readyz` 依赖不可达时返回 **503**。

### 3.2 内部

| 方法 | 路径 | 用途 |
| :--- | :--- | :--- |
| `POST` | `/internal/runs/{run_id}/wake` | Celery Worker 完成回调，唤醒图（`06` §6.1） |

内部端点**不出现在公开的 `openapi.json` 中**（FastAPI 的 `include_in_schema=False`），且使用独立的 Token——它只应被本系统的进程调用，不是 API 面的一部分。

### 3.3 前端产物（非 API）

| 路径 | 用途 |
| :--- | :--- |
| `/` 及任意**不以 `/api` 开头**的未知路径 | 返回前端 `index.html`（SPA 兜底路由） |
| `/assets/*` | 前端静态资源 |

M2 起前端由 Gateway 自己托管（同源，没有跨域问题）。这条兜底路由
`include_in_schema=False`——它不是业务接口，写进契约会让前端的类型生成器
凭空多出一个"什么路径都匹配"的操作。

**`/api/*` 与 `/internal/*` 被显式排除在兜底之外**：不排除的话，
`GET /api/runs/不存在的id` 会返回 `index.html` 与 200，
而契约里写的是"404 + problem+json"。更坏的是它**只在构建产物存在时才发生**
——本地纯后端开发一切正常，上线之后调用方开始拿到一堆 HTML。

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
| `413` | `payload_too_large` | 超过上传上限（`07` NF4，默认 10 MiB）。**在读之前拦**——读完了再检查，那份内容已经进内存了 |
| `400` | `spec_invalid` | 不符合 OpenAPI 规范（`errors` 数组给出定位）。`code` 说明**具体缺陷**，见 §2.2 |
| `422` | `invalid_request` | 请求体既不是可解析的 JSON，也不含 `content` 字段 |

**两处尚未实现**（记在这里以免被当成已支持）：

- `spec_parse_failed` 这个码**从未被产出**。语法错误与悬空 `$ref` 都归在
  `spec_invalid` 下，具体原因走 `code`（`syntax_error` / `dangling_ref` …）。
  也就是说"格式错"与"规范性错"目前共用 `type`，粒度只在 `code` 与 `errors` 里。
- `415 unsupported_version` **没有实现**：`spec_parser` 只检查 `openapi` 字段
  **存在**（`missing_openapi_version`），不检查取值落在 3.0/3.1。
  一个 `openapi: 4.0` 的文档会走到规范校验并以 `invalid_openapi` 失败，
  而不是这条更明确的 415。属 `SpecParser.md` Q2 的范围。

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
| `404` | `spec_not_found` | |
| `422` | `operation_not_in_spec` | 传入了不属于该 Spec 的 operation_id |
| `503` | `orchestrator_unavailable` | 编排队列不可用——**受理失败必须如实返回**，不能返回一个永远不推进的 Run |

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
| `409` | `run_not_awaiting_review` | Run 不在待确认状态（可能已被恢复或已结束） |
| `422` | `decision_incomplete` | 有未决用例未出现在请求中（`detail` 列出数量） |

`409` 的存在是必要的：用户可能开了两个标签页，或点了两次"提交"。**第二次提交必须被明确拒绝并说明原因**，而不是静默地再次推进图。

**⚠️ M2 实现回写（4 处）：**

**① 返回 `200` 而不是 `202`。** 决策落库是**同步且权威**的：返回之前
`review_status` 已经提交，用户的决定**已经生效**。`202` 的"已受理、尚未处理"
语义反而不对——resume 的投递是内部细节，且失败可恢复（投递幂等 + 兜底轮询），
不该让它把"已生效"降格成"已受理"。响应体带**实际生效的条数**：

```json
{"run_id": "0194...", "applied": {"approved": 40, "rejected": 78}}
```

只回一个 `{"ok": true}` 是没用的——用户要确认的是"我批的那 40 条真的批了"。

**② `409` 覆盖两种情形，共用一个错误码。** 除了"阶段已离开 `awaiting_review`"，
还有**"待确认集合已经空了"**：决策已落库、只是推进者还没取走（resume 是异步的），
而这正是"双击提交"打中的窗口。两种情形的共同含义是
**"这个 Run 现在不需要你确认了"**，所以共用 `run_not_awaiting_review`，
具体情况写在 `detail` 里给人看。分两个码会让调用方多写一个分支，而他能做的事完全一样。

**③ `case_id` 非法时**报 `invalid_request` 而**不是** `decision_incomplete`。
后者会让用户去数自己的请求（数出 118 条，一条不少）然后彻底迷失方向——
而真正的原因是他把一个 id 复制错了。

**④ 请求体是 `extra="forbid"` 的。** 默认行为是**静默忽略**未知字段，
而对这里它是一个**误导性的成功**：有人写
`{"case_id": ..., "decision": "approve", "destructive_authorized": true}`
会拿到 200，并合理地以为自己授权了破坏性请求——而那个字段根本不存在
（ADR-0021：MVP 不提供授权入口），于是那几条用例仍然不会被执行。
报错比沉默好：它把"你想要的这个功能不存在"变成一句能读懂的话。

**⚠️ 一处已了结的遗留偏差：** 本节表格曾把错误码写成中划线
（`run-not-awaiting-review`），而代码里历来是下划线
（`spec_not_found` / `run_not_found` / `report_not_ready` …）。
M2 按**代码**的口径新增 `run_not_awaiting_review` 与 `decision_incomplete`
（ADR-0023：接口契约以代码为准），随后把文档里**全部**错误码统一成下划线，
拼法约定写进 §2.2。**这是文档跟着代码改，不是代码跟着文档改**——
理由是接口契约以代码为准，而改动面只在文档侧。

### 4.6 `GET /api/runs/{run_id}/report`

报告。`Accept` 决定形态：

| `Accept` | 返回 |
| :--- | :--- |
| `application/json` | `reports.content`（结构化聚合，供前端） |
| `text/markdown` | `reports.rendered`（快照，供 CLI 与导出） |

**两者返回的是同一次生成的结果**（`04` §2.8 的 `content` 与 `rendered`），不会因请求格式不同而重新计算。这是 `ReportAgent.md` RA4/RA5 的直接体现。

若 Run 尚未产生报告 → `404 report_not_ready`。

**⚠️ M4 新增字段（向后兼容）**：`content.llm_usage` 是一个对象，含
`calls` / `generation_calls` / `attribution_calls` / `prompt_tokens` /
`completion_tokens` / `cached_tokens` / `cache_hit_rate` / `cost_cny` /
`unpriced_models` / `p95_latency_ms` / `generation_acceptance_rate` /
`usage_missing_calls`。全部有默认值；`content` 本身仍是无类型 dict，
因此 `openapi.json` 不变。`content.llm_calls` **保持原语义**（归因兜底条数），
真实调用次数看 `llm_usage.calls`。

### 4.7 `POST /internal/runs/{run_id}/wake`

见 `06` §6.1。**幂等**：无论调用多少次，行为一致。

```json
{"status": "pending"}     // 仍有未完成的用例，图保持等待
{"status": "resumed"}     // 全部到终态，已投递推进
```

不返回 `202` 而是返回一个明确的判定结果，使 Worker 侧的行为可观测——排查"图为什么没推进"时，这个响应是第一个要看的东西。

**⚠️ M2 实现回写：**"全部到终态"的判定**必须查 `case_results`**，
分母是"**应当有结果**的用例数"（`review_status = 'approved'` 且非破坏性），
**不得读 `runs.expected_case_count`**（`04` DM5）。M1 读了那个派生缓存，
而它没有任何节点维护、恒为 0——于是这个端点**永远返回 `pending`**，
回调唤醒整条路径从未生效。完整分析见 `06` §6.2。

### 4.8 `GET /api/runs/{run_id}/cases`

用例列表（分页）。**人工确认界面的数据源**——没有它，前端就只能靠
`GET /api/runs/{id}` 那三个计数去猜有哪些用例。

> 新增于 M2。**置于 §4 末尾而不是按端点清单的顺序插入**，是为了不改动
> 已有小节的编号：`§4.5` 与 `§4.7` 在代码与其他文档里被交叉引用，
> 重编号会让那些引用悄悄指向别的小节。

**查询参数**

| 参数 | 默认 | 范围 | 说明 |
| :--- | ---: | :--- | :--- |
| `limit` | 50 | 1–500 | 每页条数 |
| `offset` | 0 | ≥0 | 偏移 |

`limit` 超上限**直接拒绝**（`422`），不静默截断到上限——一个"我请求了 5000 条、
拿到 500 条且没有提示"的接口，会让调用方以为自己拿全了。

**响应**

```json
{
  "run_id": "0194...",
  "total": 118,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "case_id": "0195...",
      "name": "GET /users 边界：age=0",
      "category": "boundary",
      "dimension": "boundary:age",
      "confidence": "low",
      "review_status": "pending",
      "is_destructive": false,
      "request_method": "GET",
      "request_path": "/users",
      "assertions": [{"type": "status_code", "expected": 200, "derived_from": "inferred"}]
    }
  ]
}
```

**排序是接口契约的一部分，不是实现细节。** 换个顺序展示，用户看到的
"最该先看的那几条"就变了——而那正是这个界面存在的理由。

| 序 | 依据 | 方向 |
| :-- | :--- | :--- |
| ① | `confidence` | `low` 在前，`NULL` 居中，`high` 在后 |
| ② | `created_at` | 升序 |
| ③ | `name` | 升序 |

低置信度在前，是因为它们的期望值有一部分是**推断**出来的
（`03/TestCaseGen.md` §7）——而"推断"正是会出现"期望值本身写错"的那一类。
把它们埋在第 3 页，人工确认就退化成一路点"全部批准"。
`NULL` 居中是因为它的含义是"不确定"：M1 生成的历史用例没有这个字段，
当成高置信是错的，当成低置信又会把一整批老用例灌进待细看的那一堆。

②③ 两个次序键保证**分页稳定**：置信度相同的用例之间必须有确定顺序，
否则同一条用例可能出现在两页里、也可能一条都不出现——而这类缺陷
只在数据量超过一页时才出现。

**`total` 与 `items` 的长度不是一回事**：前者是全部用例数，少了它前端
显示不出"共 N 条"，也判断不了"是不是最后一页"。

**不返回 `request_headers` / `request_body`**：前者可能含凭证（SR-6），
后者对"要不要批准"这个判断几乎没有帮助。需要看请求体的场景应当通过
`GET /api/runs/{id}/report` 的失败清单去定位具体问题，而不是把 2000 条
用例的请求体塞进列表接口。

**前端不重排。** 排序放在服务端是刻意的：分页与排序必须由同一个地方决定，
否则"第 2 页"的含义取决于客户端按什么排的，而那个契约没人写得下来
（PRD F9 要求前端不重复实现业务规则）。

### 4.9 `GET /api/runs/{run_id}/attributions`

失败归因列表（分页）。与 `/cases` 同一套分页约定（`limit` 1–500、默认 50）。

> 新增于 M3。同样**追加在 §4 末尾**，不改已有序号。

**响应**

```json
{
  "run_id": "0194...",
  "total": 9,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "attribution_id": "0195...",
      "case_id": "0195...",
      "case_name": "GET /users 正常请求",
      "dimension": "value_valid",
      "status": "failed",
      "http_status": 500,
      "category": "server_error",
      "method": "rule",
      "rule_ref": "status.server_error",
      "llm_model": null,
      "confidence": "high",
      "evidence": {"rule_ref": "status.server_error", "http_status": 500, "truncated": false},
      "human_verdict": null,
      "human_category": null
    }
  ]
}
```

`category` 是 11 类封闭枚举（`03/LogAnalyzer.md` §3.1）。`method` 区分
规则与 LLM 两条路径——它是规则覆盖率与 M4 认可率的分母来源。

排序：`created_at` 升序，`id` 升序（同一事务插入的行共享同一个 `now()`，
只用 `created_at` 会得到不稳定顺序）。

**错误**：`404 run_not_found`（运行不存在或 id 非法，两者不区分——
区分等于多给探测面）。

### 4.10 `PATCH /api/runs/{run_id}/attributions/{attribution_id}`

人工对一条归因表态（LA6）。

**请求**

```json
{"verdict": "agree"}
```

```json
{"verdict": "disagree", "human_category": "conflict"}
```

`human_category` 的必填性**取决于 verdict**：

| verdict | `human_category` | 结果 |
| :--- | :--- | :--- |
| `agree` | 必须缺省 | `422 human_category_not_allowed` |
| `disagree` | **必填** | `422 human_category_required` |

理由：只记"不同意"无法回答"该补哪条规则"——规则库改进需要知道
**正确类别**。反过来，`agree` 却带另一个类别，会让"同意这个归因"与
"我认为是另一类"两种含义混在同一行里。

**响应**：`200`，返回更新后的 `AttributionItem`（形状同 §4.9 的元素）。
**不是 202**：表态同步且权威，返回前已提交。

**错误**

| 状态 | `code` | 说明 |
| ---: | :--- | :--- |
| 404 | `run_not_found` | 运行不存在 |
| 404 | `attribution_not_found` | 归因不存在，**或属于另一个 Run**——不区分，避免泄漏"这个 id 存在" |
| 422 | `human_category_required` / `human_category_not_allowed` | 见上表 |

表态可以重复提交（覆盖上一次）——人可以改主意。它**不修改**
`case_results.status`：归因只是解释，不能反过来改写测试结论。

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
| v0.4 | 2026-10-16 | **错误码拼法统一（独立改动）**：① 此前本文档用中划线（`spec-not-found`）而代码用下划线（`spec_not_found`），本文档全部回改为下划线——依据 ADR-0023"接口契约以代码为准"，**代码一行未动**。② §2.2 补 `type` 与 `code` 的分工：`type` 是问题类型 URI（`https://testflow.local/problems/` + 稳定类别词），`code` 是具体错误码；并记录 `spec_invalid` 是**两层结构**（`type` 固定，`code` 细分 `syntax_error` / `dangling_ref` / `invalid_openapi` 等六种）。③ §4.1 更正与补齐：`spec_invalid` 的状态码由 `422` 改为代码实际返回的 `400`；补 `413 payload_too_large` 一行；标出两处**尚未实现**（`spec_parse_failed` 从未被产出、`415 unsupported_version` 无版本范围检查）。④ §2.2 示例去掉从未被输出的 `instance` 字段（RFC 9457 标准字段，但 `ProblemDetail.to_payload()` 不产出它） |
| v0.3 | 2026-10-15 | **随 M2 实现回写**：① §4.5 补四处实现语义（返回 `200` 而非 `202` 及响应体、`409` 覆盖"待确认集合已空"这一竞态、非法 `case_id` 与不完整决策必须用不同错误码、请求体 `extra="forbid"` 的理由）；② **新增 §4.8 `GET /api/runs/{run_id}/cases`**（原先只有端点清单里的一行）——分页参数与上限的意义、响应结构、**排序即契约**（置信度 low 优先，②③ 次序键保证分页稳定）、`total` 与 `items` 的区别、为什么不返回请求头与请求体、前端不重排；③ §4.7 补"全部到终态"的判定必须查 `case_results` 且分母是"应当有结果的用例数"，不得读 `runs.expected_case_count`（`04` DM5，M1 在此处读了派生缓存导致该端点永远返回 `pending`）。**并记下一处未处理的遗留偏差**：本节错误码用中划线而代码用下划线，M2 按代码口径新增了两个码，收敛方向应是改本文档但属独立改动。④ **新增 §3.3 前端产物**（SPA 兜底路由，非 API，`include_in_schema=False`），并记下"`/api/*` 与 `/internal/*` 必须被显式排除在兜底之外"这条硬约束及其只在有构建产物时才发作的特性。**M2 实际新增 2 个 API 端点**：`GET /api/runs/{run_id}/cases`、`POST /api/runs/{run_id}/decisions`；清单里其余端点（`GET /api/specs` 系列、凭证 CRUD、`/readyz`）仍属 M3 及以后 |
| v0.5 | 2026-10-17 | **随 M3 实现回写**：① 新增 §4.9 `GET /api/runs/{run_id}/attributions`（分页约定同 `/cases`，`category` 为 11 类封闭枚举，`method` 区分规则/LLM 两条路径，排序含 `id` 次序键）；② 新增 §4.10 `PATCH /api/runs/{run_id}/attributions/{attribution_id}`——LA6 在 HTTP 层的落点：`disagree` 必须给 `human_category`、`agree` 不得给，两条分别返回 422 `human_category_required` / `human_category_not_allowed`；跨 Run 的 id 一律 404 `attribution_not_found`（不泄漏存在性）；返回 200（表态同步生效）。③ §3.1 端点清单同步新增两行。**M3 实际新增 2 个 API 端点**；契约快照 `openapi.json` 已重新导出 |
