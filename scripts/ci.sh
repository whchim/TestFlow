#!/usr/bin/env bash
#
# TestFlow CI 流水线。对应 `08-工程规范.md` §6。
#
# 为什么是 bash 而不是 Makefile：Windows 默认没有 make，装它是多余的依赖；
# 而 bash 脚本在 Git Bash（开发机）与 GitHub Actions（Ubuntu）上一致。
#
# 顺序即依赖关系，任一步失败即中止。`architecture` 排在 `test` 之前是刻意的：
# 依赖违规是结构性缺陷、修起来影响面大；测试失败通常是局部问题。先跑便宜且
# 结论明确的检查，能让开发者在最短时间内收到最有价值的反馈。

set -euo pipefail

cd "$(dirname "$0")/.."

# 统一 UTF-8。必要性见 alembic.ini 顶部：Alembic 用 locale 编码读配置文件，
# 而中文版 Windows 的 locale 是 GBK。同时让重定向到文件的日志也是 UTF-8。
export PYTHONUTF8=1

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'

step()  { printf "\n%s==> %s%s\n" "$BOLD" "$1" "$OFF"; }
info()  { printf "    %s%s%s\n" "$DIM" "$1" "$OFF"; }
ok()    { printf "    %s✓%s %s\n" "$GREEN" "$OFF" "$1"; }
skip()  { printf "    %s⊘ 跳过%s %s\n" "$YELLOW" "$OFF" "$1"; }
fail()  { printf "\n%s✗ 失败于：%s%s\n" "$RED" "$1" "$OFF" >&2; exit 1; }

trap 'fail "第 ${STEP:-?} 步"' ERR

# ─────────────────────────────────────────────────────────── ① lint
STEP="① lint"
step "① lint — ruff format --check && ruff check"
uv run ruff format --check . || fail "格式检查：请运行 uv run ruff format ."
uv run ruff check .            || fail "lint 检查"
ok "无 lint 问题"

# ─────────────────────────────────────────────────────────── ② type
STEP="② type"
step "② type — mypy"
uv run mypy || fail "类型检查"
ok "无类型错误"

# ─────────────────────────────────────────────────────────── ③ architecture
STEP="③ architecture"
step "③ architecture — 依赖白名单（08 §2.2）"
info "强制三条已在别处承诺、否则只是自觉的约束："
info "  · Guardrail 是服务端唯一出站通道（ADR-0002）"
info "  · LLM SDK 只在 llm/ 中（ADR-0005）"
info "  · CLI 不重复实现业务逻辑（PRD F9）"
uv run pytest tests/architecture -q || fail "架构约束被违反"
ok "依赖边界完好"

# ─────────────────────────────────────────────────────────── ④ contract
STEP="④ contract & schema"
step "④ contract & schema — 接口契约快照 + 模型/迁移一致性"
if [ -f src/testflow/entrypoints/gateway.py ] && grep -q "^def create_app" src/testflow/entrypoints/gateway.py 2>/dev/null; then
  ./scripts/export-openapi.sh || fail "契约快照不一致（请运行 scripts/export-openapi.sh 并提交）"
  ok "契约快照与代码一致"
else
  skip "Gateway 尚未实现（M1），无接口契约可导出"
fi
# 模型与迁移必须一致。这是**比行覆盖率更有力的检查**：声明式模型没有可执行
# 逻辑，覆盖率对它们是噪声；而 alembic check 能发现"模型改了但没生成迁移"——
# 那正是 08 EN7 要防的漂移。
if uv run alembic check > /dev/null 2>&1; then
  ok "模型与迁移一致"
elif ! uv run python -c "
import sys
from sqlalchemy import create_engine, text
from testflow.config import load_settings
try:
    create_engine(load_settings().database_url).connect().close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
  skip "数据库不可达（运行 docker compose up -d）"
else
  fail "模型与迁移不一致——请运行 uv run alembic revision --autogenerate"
fi

# ─────────────────────────────────────────────────────────── ⑤ test
STEP="⑤ test"
step "⑤ test — 单元 + 集成 + 对抗性"
# 分层覆盖率（09 §2.3 TS2）：整体目标 ≥ 70%（PRD M6）。
# 安全与判定类模块另有 ≥ 90% 的单独门槛，随对应模块落地加入：
#   guardrail/policy（已就位）、execution/assertions、agents/rules
#
# ★ M2 起把 `tests/security` 并入这一步。它**不是**重复劳动：那组测试
# 断言的是"某一种绕过不可行"（`09` TS4/TS8），而不是"某个函数正确"。
# 分两步跑会让"对抗性测试被单独跳过"变得可能，而它恰恰是最不该被跳过的那组
# ——安全性质的失效在功能测试里看不出来。其中需要真实 PostgreSQL 的部分
# 会按既有约定自动跳过，不影响无 Docker 的环境。
uv run pytest tests/unit tests/integration tests/security \
  --cov=testflow \
  --cov-report=term-missing \
  --cov-report= \
  --cov-fail-under=70 \
  || fail "测试未通过或覆盖率不足"
ok "测试通过，覆盖率达标"

# 分层覆盖率（09 §2.3 TS2）：**安全与判定类模块单独设 ≥ 90% 门槛**。
#
# 为什么这些模块要单独提高门槛，而不是靠整体那个 70%：
# 它们错了**不会以失败的形式表现出来**——一个把"无法求值"并进"不成立"的断言
# 引擎，或者一条漏掉某个网段的 IP 判定，跑起来一切正常，
# 只是结论悄悄变错。整体覆盖率被大量平凡代码摊薄之后，看不出这些模块的薄弱点。
info "分层覆盖率门槛（安全与判定类模块 ≥ 90%）："
while IFS='|' read -r tests module; do
  [ -z "${tests:-}" ] && continue
  uv run pytest "$tests" --cov="$module" --cov-report= -q > /dev/null 2>&1 \
    || fail "$module 的测试未通过"
  pct=$(uv run coverage report --format=total 2>/dev/null)
  if [ "${pct:-0}" -lt 90 ]; then
    fail "$module 覆盖率 ${pct}% < 90%（09 §2.3 TS2）"
  fi
  info "  · $module  ${pct}%"
done << 'LAYERS'
tests/unit/test_guardrail_policy.py|testflow.guardrail.policy
tests/unit/test_assertions.py|testflow.execution.assertions
LAYERS
ok "分层覆盖率达标"

# ─────────────────────────────────────────────────────────── ⑥ build
STEP="⑥ build"
step "⑥ build — 镜像构建（含前端）"
# **前端编译正确性由这一步负责，不另开一步。** Dockerfile 是多阶段的，
# node 阶段跑 `npm ci && npm run build`——而 `build` 的定义是
# `vue-tsc --noEmit && vite build`，所以**类型检查与打包都在这条链上**。
#
# 为什么不把 npm 单独做成 ①–⑤ 里的一步（那会让 ci.sh 变成七步）：
# 核心那五步是纯 Python 的，加一个需要 Node 的步骤会让"CI 能不能跑"
# 取决于一个与后端无关的环境变量。而"六步"这个口径写在 `08` §6 里，
# 改它要同步改文档——收益只是让日志多一行。
#
# 代价是**没有 Docker 时前端完全不被验证**（这一步会跳过）。
# 那条跳过信息是明确的（⊘ 而不是 ✓），所以不会被误当成"验过了"。
if [ -f Dockerfile ]; then
  docker build -t testflow:ci . > /dev/null || fail "镜像构建（含前端构建与类型检查）"
  ok "镜像构建成功，前端已打包进 dist"
else
  skip "Dockerfile 尚未创建（前端构建未验证）"
fi

printf "\n%s✓ CI 全部通过%s\n\n" "$GREEN" "$OFF"
