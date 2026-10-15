# TestFlow 运行镜像。
#
# 一个镜像跑四个进程（gateway / orchestrator / worker / beat）——它们共享
# 同一份代码与依赖，区别只在启动命令（`10` §2.1）。
# 拆成四个镜像会让"版本不一致"成为一个随时可能发生的问题，
# 而它们的构建输入完全相同。
#
# 分层顺序是刻意的：**依赖先装、代码后拷**。代码每天都在变，依赖很少变——
# 这个顺序让日常构建只重建最上面两层。
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
