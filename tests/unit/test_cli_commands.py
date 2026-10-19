"""CLI 命令的行为测试。**不需要数据库、不需要 Gateway。**

（文件名带 `_commands` 是必需的：pytest 默认的 import 模式下，两个目录里
同名的测试模块会互相覆盖——`tests/integration/test_cli_e2e.py` 与本文件
刻意用了两个自描述的名字，而不是靠加 `__init__.py` 去绕。）

对应决策：`01-TestFlow_PRD.md` F9（CLI 是纯 HTTP 客户端）、`05-API契约.md`、
`08-工程规范.md` EN1。

## 用 `MockTransport` 顶掉网络，但不顶掉**被测的那一层**

被替换的是"字节怎么在网络上走"，而 CLI 的全部逻辑——输出格式、退出码、
`--wait` 的状态机、把 problem+json 翻译成人话——都在替换点之上。

这正是 `test_llm_egress_behavior.py` 那条测试的反面：那里必须用真的 HTTP，
因为被测的**就是**"请求从哪条路出去"。这里被测的是"拿到响应之后干什么"，
而 MockTransport 恰好保留了响应。

## 契约假设由集成测试兜底

这里返回的 JSON 是**我照契约手写的**——如果 Gateway 真的返回 `specId`
而我写成了 `spec_id`，这一整批测试都会绿。所以
`tests/integration/test_cli.py` 里还有一条对着**真实 Gateway** 跑的用例。
两者的分工：这里验 CLI 的逻辑，那里验 CLI 对契约的假设。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner, Result

from testflow.client.api import GatewayApiClient
from testflow.entrypoints import cli as cli_module

SPEC_BODY = '{"openapi": "3.0.3"}'


#: 路由键 = `(方法, 路径, 是否要 Markdown)`。
#:
#: **第三段不能省**：`GET /report` 这同一个路径在真实 Gateway 上按 `Accept`
#: 返回两种形态（`05` §4.6）。只按 `(方法, 路径)` 路由的话，后注册的
#: Markdown 响应会覆盖 JSON 那一份——于是"要 JSON 的那次请求"拿到一段
#: Markdown 并对它调 `response.json()`。测试会红，但红在一个与真实行为
#: 无关的地方（我们自己的替身造错了）。
_Route = tuple[str, str, bool]


class _Gateway:
    """一个照契约应答的假 Gateway。

    两个刻意的设计：

    * **未注册的路径返回 404 而不是抛异常**：那样测试失败时看到的是
      "CLI 打了一个我没预料到的地址"，而不是一个 `KeyError` 堆栈
    * **同一个键可以注册一串响应**（`add_sequence`）：`--wait` 的全部意义
      就是"同一个查询的返回值会变"，只存一个响应的话那条路径测不出来
    """

    def __init__(self) -> None:
        self.routes: dict[_Route, list[tuple[int, Any, str | None, str]]] = {}
        self.requests: list[httpx.Request] = []

    def add(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json: Any = None,
        text: str | None = None,
        content_type: str = "application/json",
        markdown: bool = False,
    ) -> None:
        self.routes[(method, path, markdown)] = [(status, json, text, content_type)]

    def add_sequence(
        self, method: str, path: str, responses: list[Any], *, markdown: bool = False
    ) -> None:
        """按调用次序逐个返回。**最后一条会一直重复。**

        重复最后一条是刻意的：轮询会在"阶段没变"时反复问同一个问题，
        而让测试去数它问了几次，就是把测试绑死在轮询间隔上。
        """
        self.routes[(method, path, markdown)] = [
            (200, payload, None, "application/json") for payload in responses
        ]

    def transport(self) -> httpx.BaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            wants_markdown = "text/markdown" in (request.headers.get("accept") or "")
            queue = self.routes.get((request.method, request.url.path, wants_markdown))
            if queue is None:
                return httpx.Response(404, json={"code": "not_stubbed", "title": "未注册的路径"})
            status, payload, text, content_type = queue[0] if len(queue) == 1 else queue.pop(0)
            if text is not None:
                return httpx.Response(status, text=text, headers={"content-type": content_type})
            return httpx.Response(status, json=payload)

        return httpx.MockTransport(handler)


def _problem(code: str, title: str, status: int, detail: str = "") -> dict:
    return {
        "type": f"https://testflow.local/problems/{code}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
    }


@pytest.fixture
def gateway() -> _Gateway:
    return _Gateway()


@pytest.fixture
def run_cli(monkeypatch, gateway: _Gateway) -> Callable[..., Result]:
    """把 CLI 造的客户端换成"接到假 Gateway 上"的那个。

    替换的是 `cli` 模块**名字空间里**的 `GatewayApiClient`（不是
    `testflow.client.api` 里的那个）——所以 CLI 自己那句
    `GatewayApiClient(base_url=..., token=...)` 会走到这里来。
    少了 `transport=` 的话这条接不上，测试会去连真的 127.0.0.1:3000，
    而症状是"测试超时"，很难联想到是注入没生效。
    """
    transport = gateway.transport()

    def _factory(*, base_url: str, token: str | None) -> GatewayApiClient:
        return GatewayApiClient(base_url=base_url, token=token, transport=transport)

    monkeypatch.setattr(cli_module, "GatewayApiClient", _factory)
    runner = CliRunner()

    def _run(*args: str) -> Result:
        return runner.invoke(cli_module.app, list(args))

    return _run


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(SPEC_BODY, encoding="utf-8")
    return path


def _upload_ok(gateway: _Gateway, **overrides: Any) -> None:
    gateway.add(
        "POST",
        "/api/specs",
        status=201,
        json={
            "spec_id": "spec-1",
            "fingerprint": "a" * 64,
            "openapi_version": "3.0.3",
            "operation_count": 3,
            "declared_targets": ["https://api.example.com:443"],
            "reused": False,
            "warnings": [],
            **overrides,
        },
    )


class TestRun:
    def test_prints_parseable_key_value_lines(self, run_cli, gateway: _Gateway, spec_file) -> None:
        """★ 输出格式是**契约**：脚本会 `grep '^run_id='` 取它。

        所以这里断言的是完整的一行，而不是"输出里包含 run_id"——
        后者在格式变成 `{'run_id': 'x'}` 时照样通过。
        """
        _upload_ok(gateway)
        gateway.add("POST", "/api/runs", status=202, json={"run_id": "run-1", "phase": "init"})
        gateway.add(
            "GET",
            "/api/runs/run-1",
            json={
                "run_id": "run-1",
                "phase": "init",
                "spec_fingerprint": "a" * 64,
                "expected_case_count": 0,
                "settled_case_count": 0,
            },
        )

        result = run_cli("run", str(spec_file))

        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert "spec_id=spec-1" in lines
        assert "run_id=run-1" in lines
        assert "phase=init" in lines

    def test_the_bearer_token_is_sent(self, run_cli, gateway: _Gateway, spec_file) -> None:
        """F9 / `05` §5.3：用户端点用 `Authorization: Bearer`。"""
        _upload_ok(gateway)
        gateway.add("POST", "/api/runs", status=202, json={"run_id": "run-1", "phase": "init"})
        gateway.add(
            "GET",
            "/api/runs/run-1",
            json={
                "run_id": "run-1",
                "phase": "init",
                "spec_fingerprint": "a" * 64,
                "expected_case_count": 0,
                "settled_case_count": 0,
            },
        )

        run_cli("run", str(spec_file), "--token", "secret-token")

        assert gateway.requests[0].headers["authorization"] == "Bearer secret-token"

    def test_warnings_go_to_stderr_not_stdout(self, run_cli, gateway: _Gateway, spec_file) -> None:
        """**降级提示不能混进 stdout。**

        stdout 是给脚本解析的，而提示是给人看的中文句子。混在一起会让
        `grep '^run_id='` 之类的用法偶尔抓到一句解释——那种 bug 只在
        特定文档上出现，很难复现。
        """
        _upload_ok(
            gateway,
            warnings=[{"code": "relative_server_url", "detail": "servers[0] 是相对路径"}],
        )
        gateway.add("POST", "/api/runs", status=202, json={"run_id": "run-1", "phase": "init"})
        gateway.add(
            "GET",
            "/api/runs/run-1",
            json={
                "run_id": "run-1",
                "phase": "init",
                "spec_fingerprint": "a" * 64,
                "expected_case_count": 0,
                "settled_case_count": 0,
            },
        )

        result = run_cli("run", str(spec_file))

        assert "relative_server_url" in result.stderr
        assert "relative_server_url" not in result.stdout

    def test_an_empty_whitelist_is_warned_about_loudly(
        self, run_cli, gateway: _Gateway, spec_file
    ) -> None:
        """★ 白名单为空时**必须明说**。

        它的后果是"所有请求都会被 Guardrail 拒绝"，而用户看到的是
        一条 100% 失败的报告——一个不说原因的平台看起来就是坏的。
        """
        _upload_ok(gateway, declared_targets=[])
        gateway.add("POST", "/api/runs", status=202, json={"run_id": "run-1", "phase": "init"})
        gateway.add(
            "GET",
            "/api/runs/run-1",
            json={
                "run_id": "run-1",
                "phase": "init",
                "spec_fingerprint": "a" * 64,
                "expected_case_count": 0,
                "settled_case_count": 0,
            },
        )

        result = run_cli("run", str(spec_file))

        assert "no_targets" in result.stderr

    def test_a_gateway_error_becomes_a_parseable_line(
        self, run_cli, gateway: _Gateway, spec_file
    ) -> None:
        """Gateway 拒绝 → `error=<code>` 一行 + `detail` 进 stderr + 退出码 1。

        `error=` 与成功时的 `key=value` 同一种格式：脚本不必为成功和失败
        各写一套解析。
        """
        gateway.add(
            "POST",
            "/api/specs",
            status=400,
            json=_problem("invalid_openapi", "文档不符合 OpenAPI 规范", 400, "paths 不能为空"),
        )

        result = run_cli("run", str(spec_file))

        assert result.exit_code == 1
        assert "error=invalid_openapi" in result.stdout.splitlines()
        assert "paths 不能为空" in result.stderr

    def test_a_non_utf8_file_reports_a_local_error(
        self, run_cli, gateway: _Gateway, tmp_path
    ) -> None:
        """读文件失败是**本地**问题，码必须与 Gateway 的错误分开。

        混在一起的话，用户会先去查 Gateway 日志——而那里什么也没有，
        因为请求根本没发出去。
        """
        binary = tmp_path / "api.yaml"
        binary.write_bytes(b"\xff\xfe\x00\x01")

        result = run_cli("run", str(binary))

        assert result.exit_code == 1
        assert "error=local_io" in result.stdout.splitlines()
        assert gateway.requests == [], "读不了文件就不该发出任何请求"


class TestWait:
    """`--wait` 的状态机。**它是 CLI 里唯一有状态的部分。**"""

    def _stub(self, gateway: _Gateway, phases: list[dict]) -> None:
        """注册一串状态查询。

        ⚠️ **`phases[0]` 是 `run` 里那次"发起后立刻查一次"的响应**，
        不是循环里的第一次。忘了这件事的话，断言会整体错位一格，
        而错位后的期望值看起来也是合理的——只是它测的是别的东西。
        """
        _upload_ok(gateway)
        gateway.add("POST", "/api/runs", status=202, json={"run_id": "run-1", "phase": "init"})
        gateway.add_sequence("GET", "/api/runs/run-1", phases)

    @staticmethod
    def _status(phase: str, **extra: Any) -> dict:
        return {
            "run_id": "run-1",
            "phase": phase,
            "spec_fingerprint": "a" * 64,
            "expected_case_count": 4,
            "settled_case_count": 0,
            **extra,
        }

    def test_it_stops_at_the_review_gate_and_prints_the_confirm_url(
        self, run_cli, gateway: _Gateway, spec_file, monkeypatch
    ) -> None:
        """★ `--wait` 遇到人工确认门时**必须停下，而不是继续等**。

        确认门按定义需要人做决定，而 CLI 不是做决定的地方。继续等下去
        会一直等到 `review_timeout_days`（默认 7 天）的 `abandoned`
        ——用户以为在等结果，实际在等超时。

        退出码是 **0**：我们等到了该等的东西（"轮到你了"），
        不是失败。
        """
        monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)
        self._stub(gateway, [self._status("generating"), self._status("awaiting_review")])

        result = run_cli("run", str(spec_file), "--wait")

        assert result.exit_code == 0
        assert "phase=awaiting_review" in result.stdout.splitlines()
        assert any(
            line.startswith("confirm_url=") and line.endswith("/runs/run-1/confirm")
            for line in result.stdout.splitlines()
        ), "必须给出下一步该去哪——否则用户不知道要做什么"

    def test_it_returns_when_the_run_finishes(
        self, run_cli, gateway: _Gateway, spec_file, monkeypatch
    ) -> None:
        monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)
        self._stub(
            gateway,
            [self._status("dispatching"), self._status("done", settled_case_count=4)],
        )

        result = run_cli("run", str(spec_file), "--wait")

        assert result.exit_code == 0
        assert result.stdout.splitlines()[-1] == "phase=done"
        assert "cases=4/4" in result.stderr

    def test_it_reports_the_failure_reason_on_a_failed_run(
        self, run_cli, gateway: _Gateway, spec_file, monkeypatch
    ) -> None:
        monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)
        self._stub(gateway, [self._status("failed", failure_reason="no_operations")])

        result = run_cli("run", str(spec_file), "--wait")

        assert result.exit_code == 0
        assert "failure_reason=no_operations" in result.stdout.splitlines()

    def test_a_timeout_does_not_claim_success_or_cancellation(
        self, run_cli, gateway: _Gateway, spec_file, monkeypatch
    ) -> None:
        """超时**不是失败也不是取消**——Run 还在跑。

        所以输出里必须说清"它没有被取消"，否则用户会重新发起一次，
        于是同一个 Run 库里有了两份数据。

        `--timeout 0` 让第一条的截止时间立刻到期，因此这里不需要真的等待。
        """
        monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)
        self._stub(gateway, [self._status("dispatching")])

        result = run_cli("run", str(spec_file), "--wait", "--timeout", "0")

        assert result.exit_code == 1
        assert "error=wait_timeout" in result.stdout.splitlines()
        assert "没有被取消" in result.stderr

    def test_phases_are_printed_only_when_they_change(
        self, run_cli, gateway: _Gateway, spec_file, monkeypatch
    ) -> None:
        """Run 可能跑十分钟，每秒一行会把真正的信号淹掉。"""
        monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)
        self._stub(
            gateway,
            [self._status("generating")] * 3 + [self._status("done")],
        )

        result = run_cli("run", str(spec_file), "--wait")

        phases = [line for line in result.stdout.splitlines() if line.startswith("phase=")]
        assert phases == ["phase=generating", "phase=done"], (
            "四次查询里阶段只变了两次——`--wait` 不该每次轮询都打一行"
        )


class TestStatus:
    def test_it_prints_the_confirm_url_when_awaiting_review(
        self, run_cli, gateway: _Gateway
    ) -> None:
        """★ `status` 是用户**回来查看**时的入口。

        确认提示只放在 `run --wait` 里的话，用户下次开一个终端就再也
        找不到确认页面了——而那个 Run 还在等他。
        """
        gateway.add(
            "GET",
            "/api/runs/run-1",
            json={
                "run_id": "run-1",
                "phase": "awaiting_review",
                "spec_fingerprint": "a" * 64,
                "expected_case_count": 8,
                "settled_case_count": 0,
                "interrupt": {"kind": "review"},
            },
        )

        result = run_cli("status", "run-1", "--base-url", "http://gw.internal:3000")

        assert result.exit_code == 0
        assert "confirm_url=http://gw.internal:3000/runs/run-1/confirm" in (
            result.stdout.splitlines()
        )
        assert "不会被执行" in result.stderr, "要说清未确认的后果"

    def test_a_normal_phase_has_no_confirm_hint(self, run_cli, gateway: _Gateway) -> None:
        """不处于确认门时**不该出现**确认链接——那会让用户去点一个无效页面。"""
        gateway.add(
            "GET",
            "/api/runs/run-1",
            json={
                "run_id": "run-1",
                "phase": "analyzing",
                "spec_fingerprint": "a" * 64,
                "expected_case_count": 8,
                "settled_case_count": 8,
            },
        )

        result = run_cli("status", "run-1")

        assert not any(line.startswith("confirm_url=") for line in result.stdout.splitlines())

    def test_an_unknown_run_is_reported_with_its_code(self, run_cli, gateway: _Gateway) -> None:
        gateway.add(
            "GET",
            "/api/runs/nope",
            status=404,
            json=_problem("run_not_found", "运行不存在", 404),
        )

        result = run_cli("status", "nope")

        assert result.exit_code == 1
        assert "error=run_not_found" in result.stdout.splitlines()


class TestReport:
    def test_markdown_is_printed_verbatim(self, run_cli, gateway: _Gateway) -> None:
        """**不加任何包装。**

        报告里可能有代码块与表格；加一层缩进或前缀会破坏渲染，
        而"渲染坏了"不会让任何测试失败——只会让人看到一份难读的报告。
        """
        rendered = "# 报告\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
        gateway.add("GET", "/api/runs/run-1/report", json={"run_id": "run-1", "content": {}})
        gateway.add(
            "GET",
            "/api/runs/run-1/report",
            text=rendered,
            content_type="text/markdown",
            markdown=True,
        )

        result = run_cli("report", "run-1")

        assert result.exit_code == 0
        # 恰好一个尾随换行——`echo` 会补一个，否则 shell 提示符会粘在
        # 表格最后一行上。除它之外**一个字符都不许多**。
        assert result.stdout == rendered + "\n"

    def test_json_format_is_pretty_printed_and_keeps_unicode(
        self, run_cli, gateway: _Gateway
    ) -> None:
        """`ensure_ascii=False`：否则中文全变成 `\\uXXXX`，
        而它进的是要给人看的工单。"""
        content = {
            "run_id": "run-1",
            "summary": {"total_cases": 2, "failed": 1},
            "failures": [{"case_name": "查询用户"}],
        }
        gateway.add("GET", "/api/runs/run-1/report", json=content)
        gateway.add(
            "GET",
            "/api/runs/run-1/report",
            text="",
            content_type="text/markdown",
            markdown=True,
        )

        result = run_cli("report", "run-1", "--format", "json")

        assert '"case_name": "查询用户"' in result.stdout

    def test_an_unknown_format_is_rejected_locally(self, run_cli, gateway: _Gateway) -> None:
        """格式写错是**用法**问题，在本地就拒绝——不该白跑一趟网络。"""
        result = run_cli("report", "run-1", "--format", "xml")

        assert result.exit_code == 1
        assert "error=invalid_format" in result.stdout.splitlines()
        assert gateway.requests == []

    def test_a_report_that_is_not_ready_is_reported(self, run_cli, gateway: _Gateway) -> None:
        gateway.add(
            "GET",
            "/api/runs/run-1/report",
            status=404,
            json=_problem("report_not_ready", "报告尚未生成", 404, "当前阶段：analyzing"),
        )

        result = run_cli("report", "run-1")

        assert result.exit_code == 1
        assert "error=report_not_ready" in result.stdout.splitlines()
        assert "analyzing" in result.stderr


class TestClientBehaviour:
    def test_a_redirect_is_an_error_not_a_silent_follow(self, gateway: _Gateway) -> None:
        """★ **不跟随重定向。**

        Gateway 的接口没有"请去别处"这个语义。收到 3xx 说明我们打的
        根本不是 Gateway（中间有代理）。跟随的话，`Authorization`
        头会被送到重定向目标——一次凭空的凭证泄露。
        """
        gateway.add(
            "GET",
            "/api/runs/run-1",
            status=302,
            json=None,
            text="<html>代理的错误页</html>",
            content_type="text/html",
        )
        client = GatewayApiClient(base_url="http://gw", transport=gateway.transport())

        with pytest.raises(Exception) as caught:
            client.get_run("run-1")
        client.close()

        from testflow.client.api import GatewayError

        assert isinstance(caught.value, GatewayError)
        assert caught.value.status == 302
        assert "代理的错误页" in caught.value.message, "要让用户看到原文——它能说明打到了哪里"

    def test_a_non_json_success_is_reported_as_such(self, gateway: _Gateway) -> None:
        """2xx 但不是 JSON：也说"你打的可能不是 Gateway"，而不是抛 `ValueError`。

        `ValueError` 看起来像"我们的代码坏了"，而真正的原因是地址配错了。
        """
        gateway.add(
            "GET",
            "/api/runs/run-1",
            status=200,
            text="<html>hello</html>",
            content_type="text/html",
        )
        client = GatewayApiClient(base_url="http://gw", transport=gateway.transport())

        from testflow.client.api import GatewayError

        with pytest.raises(GatewayError) as caught:
            client.get_run("run-1")
        client.close()

        assert caught.value.code == "unexpected_response"
        assert "text/html" in caught.value.message

    def test_an_unreachable_gateway_is_a_gateway_error(self) -> None:
        """连不上与"被拒绝"对 CLI 是同一件事：这次操作没成功。

        所以归成同一个异常类型——CLI 那一层的处理逻辑因此只有一条。
        """

        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = GatewayApiClient(base_url="http://gw", transport=httpx.MockTransport(_boom))

        from testflow.client.api import GatewayError

        with pytest.raises(GatewayError) as caught:
            client.get_run("run-1")
        client.close()

        assert caught.value.code == "gateway_unreachable"
        assert caught.value.status is None


def test_terminal_phases_match_the_domain() -> None:
    """★ `RunStatus.is_terminal` 里那组字面量必须与 `RunPhase` 一致。

    `client/api.py` **刻意复制**了终态的三个取值（而不是 import 那个枚举），
    理由是客户端不该解释服务端的状态机。复制的代价是它们会漂移——
    而漂移的表现是 `--wait` 在某个终态上**永远转下去**。

    所以复制必须配一条断言。这条就是。
    """
    from testflow.client.api import RunStatus
    from testflow.domain.graph import RunPhase

    for phase in RunPhase:
        status = RunStatus(
            run_id="r",
            phase=phase.value,
            expected_case_count=0,
            settled_case_count=0,
        )
        assert status.is_terminal == phase.is_terminal, (
            f"{phase.value} 的终态判定两边不一致——复制的那份已经漂移了"
        )
