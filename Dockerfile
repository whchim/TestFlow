# TestFlow 运行镜像。
#
# 一个镜像跑四个进程（gateway / orchestrator / worker / beat）——它们共享
# 同一份代码与依赖，区别只在启动命令（`10` §2.1）。
# 拆成四个镜像会让"版本不一致"成为一个随时可能发生的问题，
# 而它们的构建输入完全相同。
#
# 分层顺序是刻意的：**依赖先装、代码后拷**。代码每天都在变，依赖很少变——
# 这个顺序让日常构建只重建最上面两层。

# ---------------------------------------------------------------- 前端构建
#
# 多阶段而不是"在本地构建好 dist 再 COPY"：后者让**构建产物进入了镜像
# 的构建输入**，于是"这个镜像里的前端是哪份源码编出来的"变成一个
# 无法从镜像本身回答的问题。多阶段让源码到产物的这条链留在 Dockerfile 里，
# 每次构建都重新走一遍。
#
# 前端镜像层**不进最终镜像**：node + node_modules 有几百 MB，而运行期
# 只需要几个静态文件。
FROM node:24-slim AS frontend

WORKDIR /build

# 同样先拷 manifest 再拷源码：`npm ci` 是最慢的一步，而它只依赖这两个文件。
# `package-lock.json` 必须一起拷——`npm ci` 要求 lock 与 manifest 一致，
# 缺了 lock 会直接失败（这正是它比 `npm install` 更适合 CI 的原因：
# 它不会悄悄装一个与 lock 不符的版本）。
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS base

# `PYTHONUNBUFFERED`：容器里的 stdout 默认是块缓冲的，而出事时我们最想看的就是
# 最后几行日志。不设它，那些日志会卡在缓冲区里，直到进程正常退出才吐出来——
# 而进程被强杀时它们永远不会到达。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_LINK_MODE=copy

# `curl` 用于健康检查；`uv` 用于装依赖。
# ⚠️ 不装 `build-essential`：本项目所有依赖都有 wheel（psycopg 用 `[binary]`
# 变体、cryptography 有预编译包），装一个几百 MB 的编译链只为极少数场景
# 不划算，而它会显著增大镜像与攻击面。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# ---- 依赖层（缓存友好）----
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- 代码层 ----
COPY src ./src
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# ---- 前端产物 ----
#
# 落在 `/app/frontend/dist`，而工作目录是 `/app`——这正是
# `entrypoints/gateway.py::DEFAULT_FRONTEND_DIST`（`frontend/dist`，
# 相对工作目录）成立的两个前提。两处任何一处改了，网关就会
# 找不到前端并返回 `frontend_not_built`。
COPY --from=frontend /build/dist ./frontend/dist

# 非 root 运行。容器逃逸的门槛因此抬高一层——
# 而这个进程会处理**用户上传的文档**并持有加密凭证，
# 它不该以 root 身份做这些事。
RUN useradd --create-home --uid 10001 testflow \
    && chown -R testflow:testflow /app
USER testflow

EXPOSE 3000

# 默认命令只是"能起来"，具体跑哪个进程由 compose 覆盖。
# 用 `testflow-gateway` 而不是 `python -m ...`：入口点由 `[project.scripts]`
# 声明，用控制台脚本能顺带验证"入口点被正确安装"（M0 就立下的约定）。
CMD ["testflow-gateway"]
