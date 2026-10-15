# TestFlow

基于多智能体编排的 API 自动化测试平台。

> **当前状态**：设计阶段已完成，实现进行中（M0 地基）。
> **设计文档**：[`docs/`](docs/) —— 需求、架构、编排、模块、数据、接口、非功能、工程、测试、部署、路线图，以及 [`docs/adr/`](docs/adr/)（27 篇架构决策记录）。

## 它做什么

把一份 OpenAPI 文档变成一份可执行的测试报告：

```
解析 Spec → 生成用例 → 人工确认 → 异步执行 → 失败归因 → 汇总报告
```

核心主张是 **"AI 只负责它能负责的部分"**——凡是确定性算法能解决的（规范解析、维度派生、断言求值、报告聚合），一律不交给 LLM；LLM 只用在三处需要语义理解的地方：用例取值生成、错误归因兜底、以及 V1.0 的报告结论摘要。

## 快速开始

```bash
uv sync                        # 安装依赖
cp .env.example .env           # 配置（需填 LLM key 等）
docker compose up -d           # 启动 PostgreSQL 与 Redis
./scripts/ci.sh                # 跑完整检查
```

## 目录结构

```
src/testflow/
├── entrypoints/   四个进程入口（gateway / orchestrator / worker / cli）
├── gateway/       HTTP 接口与 SSE
├── graph/         LangGraph 编排
├── agents/        智能体节点（SpecParser / TestCaseGen / LogAnalyzer / ReportAgent）
├── execution/     执行层（分发、执行、断言求值）
├── guardrail/     出站校验 —— 服务端唯一的出站通道
├── llm/           LLM Provider 抽象与适配器
├── storage/       数据模型与迁移
└── observability/ 日志与追踪
```

**注意 `guardrail/`**：所有出站 HTTP 请求（含对 LLM 的调用）必须经它发出。这条约束由 `tests/architecture/` 中的静态检查强制，而非靠自觉——见 [ADR-0002](docs/adr/0002-guardrail-single-egress-path.md)。

## 开发

```bash
./scripts/ci.sh                          # 完整检查（lint / 类型 / 架构 / 契约 / 测试）
uv run pytest tests/unit                 # 单元测试
uv run pytest tests/architecture         # 依赖规则检查
uv run alembic upgrade head              # 数据库迁移
uv run pytest -m integration             # 集成测试（需 Docker）
```

## 文档导航

| 文档 | 内容 |
| :--- | :--- |
| [01-PRD](docs/01-TestFlow_PRD.md) | 要做什么、给谁、做到什么程度 |
| [02-架构设计](docs/02-架构设计.md) | 分层、数据流、技术选型 |
| [03-模块详细设计](docs/03-模块详细设计/) | 六个模块的职责边界与契约 |
| [04-数据模型](docs/04-数据模型.md) | 表结构与关键约束 |
| [05-API契约](docs/05-API契约.md) | 接口设计意图（契约以代码为准） |
| [06-Agent编排](docs/06-Agent编排.md) | 状态机、中断与恢复 |
| [07-非功能需求](docs/07-非功能需求.md) | 性能、容量、可观测性 |
| [08-工程规范](docs/08-工程规范.md) | 目录、依赖约束、CI |
| [09-测试策略](docs/09-测试策略.md) | 平台测试与 AI 输出评测 |
| [10-部署运维](docs/10-部署运维.md) | 部署、配置、备份 |
| [11-开发路线图](docs/11-开发路线图.md) | 里程碑与验收 |
| [adr/](docs/adr/) | 27 篇架构决策记录 |
