# TestFlow

基于多智能体编排的 API 自动化测试平台。

输入一份 OpenAPI 文档，自动派生测试维度、生成测试用例并安全执行，输出通过率、
失败归因与成本质量报告。

> **当前状态**：MVP 已完成（M0–M5），单机 Docker Compose 部署。
> **设计文档**：[`docs/`](docs/) —— 需求、架构、编排、模块、数据、接口、非功能、
> 工程、测试、部署、路线图，以及 [`docs/adr/`](docs/adr/)（27 篇架构决策记录）。

## 它做什么

```
解析 Spec → 生成用例 → 人工确认 → 异步执行 → 失败归因 → 汇总报告
```

核心主张是 **“AI 只负责它能负责的部分”**——凡是确定性算法能解决的
（规范解析、维度派生、断言求值、报告聚合），一律不交给 LLM；LLM 只用在
需要语义理解的地方：用例取值生成与错误归因兜底。

主要能力：

- **两阶段用例生成**：确定性派生“该测什么”（维度），LLM 只填具体取值
- **人工确认门**：生成后暂停，等待逐条批准；可中断、可恢复
- **Guardrail 统一出站**：白名单、逐 IP 校验、IP 钉死 + TLS SNI、
  不跟随重定向、出站审计；由架构测试强制，无法绕过
- **三态断言**：区分“断言不成立（failed）”与“响应无法求值（error）”
- **失败归因**：规则优先、LLM 兜底，支持人工纠错
- **成本与质量**：token 级用量台账、分档计价、三层 Prompt 与前缀缓存；
  Golden Set + 多轮 L3 评测
- **运维**：三类幂等定时任务、备份与恢复演练、`/healthz` / `/readyz`

## 技术栈

Python 3.12+ · FastAPI · LangGraph · Celery · PostgreSQL 18 · Redis 7 ·
Docker Compose · Qwen（DashScope OpenAI 兼容模式）

## 快速开始

前置条件：Docker 与 Docker Compose、[uv](https://docs.astral.sh/uv/)、
一个可用的通义千问 API Key。

```bash
cp .env.example .env
#   填写 TESTFLOW_LLM_API_KEY
#   生成 TESTFLOW_CREDENTIAL_ENCRYPTION_KEY：
#   python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"

docker compose up -d --build
docker compose --profile tools run --rm migrate
./scripts/demo.sh
```

`scripts/demo.sh` 会走完整链路：上传 fixture 的 OpenAPI 文档 → 生成用例 →
自动批准人工确认门 → 执行 → 归因 → 输出报告，并校验通过率、归因覆盖率、
LLM 调用次数、缓存命中与成本。

启动后：

| 地址 | 内容 |
| :--- | :--- |
| <http://127.0.0.1:3000> | 前端 |
| <http://127.0.0.1:3000/docs> | API 文档（OpenAPI） |
| <http://127.0.0.1:3000/healthz> | 存活检查 |
| <http://127.0.0.1:3000/readyz> | 就绪检查（PG + Redis） |

首次打开前端时，在右上角填写 Token——与 `.env` 里的 `TESTFLOW_API_TOKEN` 相同
（compose 默认是 `dev-api-token`）。Token 只保存在本机浏览器的 `localStorage`，
之后刷新页面不需要重新输入。

想立刻试一下的话，可以直接上传
[`examples/fixture-openapi.json`](examples/fixture-openapi.json)——它是内置
fixture 服务的接口文档，包含一半刻意会失败的端点，详见
[`examples/README.md`](examples/README.md)。

## 使用

```bash
# 上传文档并发起运行，等待到“需要人工确认”或结束
uv run testflow run path/to/openapi.yaml --wait

# 查看状态与报告
uv run testflow status <run_id>
uv run testflow report <run_id>
```

也可以直接调用 HTTP 接口（`POST /api/specs`、`POST /api/runs`、
`GET /api/runs/{id}/report` 等）。接口定义以代码为准（ADR-0023），
完整契约见 `/docs` 或 [`openapi.json`](openapi.json)。

## 目录结构

```
src/testflow/
├── entrypoints/   进程入口（gateway / orchestrator / worker / beat / cli）
├── gateway/       HTTP 接口、SSE、认证
├── graph/         LangGraph 编排、状态机、Checkpoint
├── agents/        智能体节点（SpecParser / TestCaseGen / LogAnalyzer / ReportAgent）
├── execution/     分发、执行、三态断言
├── guardrail/     出站校验 —— 服务端唯一的出站通道
├── llm/           LLM Provider 抽象、Qwen 适配器、计价
├── storage/       数据模型、迁移、仓储
├── eval/          Golden Set 第一层比对
└── observability/ 日志、追踪、用量统计

eval/golden/       Golden Set（10 接口）与人工标注（39 条）
eval/results/      L3 评测历史结果
scripts/           CI、演示、基准、L3 评测、备份
tests/             单元 / 集成 / 架构约束 / 对抗性测试
frontend/          Vue 3 + Vite 前端
```

`guardrail/` 是服务端唯一的出站通道。所有 HTTP 客户端（包括 LLM SDK）都只能
在这里引入，这条约束由 [`tests/architecture/`](tests/architecture/) 中的 AST
静态检查强制——见 [ADR-0002](docs/adr/0002-guardrail-single-egress-path.md)。

## 开发

```bash
uv sync

./scripts/ci.sh                          # 完整检查：lint / 类型 / 架构 / 契约 / 测试 / 镜像

uv run pytest tests/unit                 # 单元测试
uv run pytest tests/architecture         # 依赖规则检查
uv run pytest -m integration             # 集成测试（需 PostgreSQL）
uv run alembic upgrade head              # 数据库迁移
```

质量与成本相关脚本：

```bash
# L3 第一层：人工标注 vs 维度派生（纯算法，零成本）
uv run python scripts/eval_golden.py

# L3 第二层：真实模型生成，3 轮重复（产生费用）
uv run python scripts/eval_golden.py --generate --repeat 3

# LLM 参数对比：思考模式、缓存命中、成本、P95（产生费用）
uv run python scripts/benchmark_llm.py --repeat 2

# 备份与恢复演练（恢复到一个临时库，不触碰生产库）
./scripts/pg_backup.sh drill
```

## 设计文档

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

## 已知限制

- 面向单机、单用户场景，不提供多租户与 SLA
- 破坏性方法（DELETE / PUT / PATCH）不自动执行，目前没有授权入口
- 审计日志不参与自动清理，避免定时任务批量删除追溯凭证
- 未配置单价的模型不计入成本（报告显示“未配置单价”，不估算）
