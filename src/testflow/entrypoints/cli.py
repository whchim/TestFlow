"""CLI 入口（PRD F9）。

对应决策：`01-TestFlow_PRD.md` F9、`08-工程规范.md` EN1、`05-API契约.md`。

⚠️ **CLI 是 Gateway 的纯 HTTP 客户端**，不重复实现解析、生成、执行逻辑
（PRD F9 约束）。它只能 import `testflow.client` 与 `testflow.domain`，
由 `tests/architecture/` 强制。

## 输出格式：`key=value` 行，人机两用

```
run_id=0195...
phase=init
```

选它而不是表格或 JSON，是因为两种读者都要照顾：

* 人要的是"一眼看到 run_id 好在下一步复制粘贴"
* 脚本要的是"能 `grep '^run_id='` 直接取出来"

表格对人是好的，但脚本解析要用 `awk`——而 `awk` 的写法依赖于列宽，
改一次列宽所有脚本就悄悄坏了。纯 JSON 对脚本最好，但人在终端里读到
一坨括号+citation 的 JSON 体验很差，而且 `run_id` 得靠眼睛找引号。

`key=value` 两边都够用，代价是**多行输出**。而 CLI 的输出本来就要进
日志与工单，多几行是无害的。

## 退出码

| 码 | 含义 |
| :-- | :-- |
| `0` | 成功 |
| `1` | Gateway 拒绝、不可达，或本地文件读不了 |
| `2` | 用法错误（typer/click 自己用的，例如选项拼错） |

**不细分"404 和 500 用不同码"**：脚本能据此分支的只有"成没成"，
而"为什么没成"已经写在 `error=<code>` 那一行里了。
两套机制表达同一件事，只会让它们迟早不一致。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer

from testflow import __version__
from testflow.client.api import DEFAULT_BASE_URL, GatewayApiClient, GatewayError

__all__ = ["app", "main"]

#: 轮询间隔（秒）。与 Gateway 的 SSE 轮询间隔一致——两个"多久看一眼"
#: 的数值不同，只会让人怀疑其中一个有讲究。
POLL_INTERVAL_S = 1.0

#: `--wait` 的默认上限（秒）。
#:
#: **必须有上限。** 一个没有上限的等待会让"Run 卡住了"表现为
#: "终端什么都不输出"，而用户分不清是自己等得不够久还是系统挂了。
#: 默认 30 分钟对 2000 条用例足够；不够时可以用 `--timeout` 显式调大。
DEFAULT_WAIT_TIMEOUT_S = 1800.0

app = typer.Typer(
    name="testflow",
    no_args_is_help=True,
)

#: 全局选项。**用 `Annotated` 而不是"默认值 = `typer.Option(...)`"。**
#:
#: 后一种写法（老教程里的）会在函数签名里执行一次函数调用作为默认值——
#: ruff 的 B008 会拦下它，而那条规则的道理在这里是成立的：
#: 默认值在**导入时**求值一次，于是"同一个选项对象被多个命令共享"
#: 就不再是显式的事。`Annotated` 把选项定义放在类型的位置上，
#: 每个命令写自己的那一个。
BaseUrl = Annotated[
    str,
    typer.Option(
        "--base-url",
        envvar="TESTFLOW_BASE_URL",
        help="Gateway 地址。默认取环境变量 TESTFLOW_BASE_URL。",
    ),
]
Token = Annotated[
    str | None,
    typer.Option(
        "--token",
        envvar="TESTFLOW_TOKEN",
        help="用户令牌。默认取环境变量 TESTFLOW_TOKEN。",
    ),
]


@app.callback()
def _root() -> None:
    """TestFlow —— 基于多智能体编排的 API 自动化测试平台。

    注意这个 callback 不能删：typer 在 app 只有一个命令时会把该命令塌缩为
    主命令，导致 `testflow <command>` 报"多余参数"。callback 的存在强制它
    进入多命令模式。
    """


def _client(base_url: str, token: str | None) -> GatewayApiClient:
    return GatewayApiClient(base_url=base_url, token=token)


def _fail(exc: GatewayError) -> None:
    """把失败打成一行可解析的输出，然后以 1 退出。

    `error=` 与其余 `key=value` 行同一种格式：脚本不必为"成功"和"失败"
    各写一套解析。
    """
    typer.echo(f"error={exc.code}")
    typer.echo(f"detail={exc.message}", err=True)
    raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """打印版本号。"""
    typer.echo(__version__)


@app.command()
def run(
    spec_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    base_url: BaseUrl = DEFAULT_BASE_URL,
    token: Token = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="等待到「需要人工确认」或运行结束（默认只发起，立刻返回）"),
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="--wait 的上限秒数")
    ] = DEFAULT_WAIT_TIMEOUT_S,
) -> None:
    """上传一份 OpenAPI 文档并发起运行。

    两步合成一条命令是刻意的：单独上传文档没有任何用处（没有人会因为
    "文档存进库里了"而高兴），而 `spec_id` 是内部标识、用户不该被迫
    在两行命令之间搬运它。

    顺带的好处是**指纹去重（DM2）自动生效**：重复运行同一份文档时，
    第二次是 `reused=true`，不重新解析、不重复落 `operations`。
    """
    try:
        content = spec_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # 读文件失败是**本地**问题，不是 Gateway 的问题。用同一个
        # `error=` 格式汇报，但码是 `local_io`——两者混在一起的话，
        # 用户会先去查 Gateway 日志。
        typer.echo("error=local_io")
        typer.echo(f"detail=无法读取 {spec_path}：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    with _client(base_url, token) as client:
        try:
            uploaded = client.upload_spec(content, filename=spec_path.name)
        except GatewayError as exc:
            _fail(exc)

        # `warnings` 打到 **stderr**：它们是给**人**看的提示，而 stdout
        # 是给脚本解析的。混在一起会让 `grep '^spec_id='` 之类的用法
        # 偶尔抓到一句中文说明。
        for warning in uploaded.warnings:
            typer.echo(f"warning[{warning.get('code')}] {warning.get('detail')}", err=True)
        if not uploaded.declared_targets:
            typer.echo(
                "warning[no_targets] 文档里没有可用的 servers，"
                "所有请求都会被 Guardrail 拒绝——请先在文档里声明目标地址。",
                err=True,
            )

        typer.echo(f"spec_id={uploaded.spec_id}")
        typer.echo(f"operation_count={uploaded.operation_count}")
        typer.echo(f"reused={str(uploaded.reused).lower()}")

        try:
            run_id = client.create_run(uploaded.spec_id)
            typer.echo(f"run_id={run_id}")
            status = client.get_run(run_id)
        except GatewayError as exc:
            _fail(exc)

        if not wait:
            typer.echo(f"phase={status.phase}")
            return

        _wait_for(client, run_id, status.phase, timeout)


def _wait_for(client: GatewayApiClient, run_id: str, phase: str, timeout: float) -> None:
    """轮询到"需要人工确认"或终态。**每次阶段变化打一行。**

    只打变化的那几行，不打每一次轮询的结果：Run 可能跑十分钟，
    而每秒钟一行会把终端刷满，让"阶段变了"这个真正的信号淹没在噪音里。

    `awaiting_review` **不是"继续等"的情况**（见下）。
    """
    typer.echo(f"phase={phase}")
    deadline = time.monotonic() + timeout

    while True:
        if time.monotonic() > deadline:
            # 超时**不是失败**：Run 还在跑，只是我们不等了。所以不打
            # `error=`，而是给一个明确的说明与退出码 1（脚本仍需知道
            # "没等到结果"）。用 0 退出会让脚本以为跑完了。
            typer.echo("error=wait_timeout")
            typer.echo(
                f"detail=等待超过 {timeout:.0f} 秒，Run 仍在 {phase}。"
                f"它没有被取消——用 `testflow status {run_id}` 继续查看。",
                err=True,
            )
            raise typer.Exit(code=1)

        time.sleep(POLL_INTERVAL_S)
        try:
            status = client.get_run(run_id)
        except GatewayError as exc:
            _fail(exc)

        if status.phase != phase:
            phase = status.phase
            typer.echo(f"phase={phase}")

        if status.awaiting_review:
            # ★ **到此为止。** 人工确认门按定义需要人做决定，
            # 而 CLI 不是那个做决定的地方——继续等下去会等到
            # `review_timeout_days`（默认 7 天）的 abandoned。
            # 所以这里打印下一步怎么做，然后正常退出（码 0：
            # 我们等到了该等的东西）。
            typer.echo(f"confirm_url={client.base_url}/runs/{run_id}/confirm")
            typer.echo("detail=用例已生成，等待人工确认。", err=True)
            return

        if status.is_terminal:
            if status.failure_reason:
                typer.echo(f"failure_reason={status.failure_reason}")
            typer.echo(f"cases={status.settled_case_count}/{status.expected_case_count}", err=True)
            return


@app.command()
def status(
    run_id: Annotated[str, typer.Argument(help="run_id（由 `testflow run` 输出）")],
    base_url: BaseUrl = DEFAULT_BASE_URL,
    token: Token = None,
) -> None:
    """查询运行状态。"""
    with _client(base_url, token) as client:
        try:
            result = client.get_run(run_id)
        except GatewayError as exc:
            _fail(exc)

    typer.echo(f"run_id={result.run_id}")
    typer.echo(f"phase={result.phase}")
    typer.echo(f"cases={result.settled_case_count}/{result.expected_case_count}")
    if result.failure_reason:
        typer.echo(f"failure_reason={result.failure_reason}")

    if result.awaiting_review:
        # **`status` 是用户回来查看时的入口**，所以提示必须在这里出现。
        # 只放在 `run --wait` 里，用户下次开终端就再也找不到确认页面了。
        typer.echo(f"confirm_url={client.base_url}/runs/{result.run_id}/confirm")
        typer.echo("detail=等待人工确认：用例已生成，未确认的用例不会被执行。", err=True)


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="run_id")],
    base_url: BaseUrl = DEFAULT_BASE_URL,
    token: Token = None,
    format: Annotated[str, typer.Option("--format", help="markdown 或 json")] = "markdown",
) -> None:
    """打印报告。

    **默认 markdown**：大多数时候人是要读它、粘进工单或评审材料里。
    要喂给别的程序时才切 `--format json`。
    """
    if format not in {"markdown", "json"}:
        typer.echo("error=invalid_format")
        typer.echo(f"detail=--format 只接受 markdown 或 json，收到 {format!r}", err=True)
        raise typer.Exit(code=1)

    with _client(base_url, token) as client:
        try:
            payload = client.get_report(run_id)
        except GatewayError as exc:
            _fail(exc)

    if format == "json":
        import json

        typer.echo(json.dumps(payload.content, ensure_ascii=False, indent=2))
        return

    # Markdown 走 `typer.echo` 的原文输出，**不做任何包装**——
    # 而报告里可能有代码块与表格，加一层缩进或前缀会破坏渲染。
    typer.echo(payload.rendered)


def main() -> None:
    app()


# 与其他入口点（`gateway.py` / `orchestrator.py`）保持一致。
# 少了它，`python -m testflow.entrypoints.cli run x.json` 会**安静地什么都不做**
# 并以 0 退出——一个"成功"的假象，比报错难查得多。
if __name__ == "__main__":  # pragma: no cover
    main()
