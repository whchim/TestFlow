"""CLI 入口（PRD F9）。

对应决策：`01-TestFlow_PRD.md` F9、`08-工程规范.md` EN1。

⚠️ **CLI 是 Gateway 的纯 HTTP 客户端**，不重复实现解析、生成、执行逻辑
（PRD F9 约束）。它只能 import `testflow.client` 与 `testflow.domain`，
由 `tests/architecture/` 强制。

M0 状态：仅骨架。命令（`run` / `status` / `report`）在 M2 实现。
"""

from __future__ import annotations

import typer

from testflow import __version__

app = typer.Typer(
    name="testflow",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """TestFlow —— 基于多智能体编排的 API 自动化测试平台。

    注意这个 callback 不能删：typer 在 app 只有一个命令时会把该命令塌缩为
    主命令，导致 `testflow <command>` 报"多余参数"。callback 的存在强制它
    进入多命令模式。
    """


@app.command()
def version() -> None:
    """打印版本号。"""
    typer.echo(__version__)


def main() -> None:
    app()
