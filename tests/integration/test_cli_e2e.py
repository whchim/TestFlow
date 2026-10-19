"""CLI 打到**真实 Gateway** 上的端到端测试。**需要 PostgreSQL。**

（文件名带 `_e2e` 是必需的：pytest 默认的 import 模式下，两个目录里同名的
测试模块会互相覆盖。见 `tests/unit/test_cli_commands.py`。）

对应决策：`01-TestFlow_PRD.md` F9、`05-API契约.md`、`08-工程规范.md` EN1。

## 为什么单元测试之外还要这一条

`tests/unit/test_cli.py` 用 `MockTransport` 验 CLI 的**逻辑**——输出格式、
退出码、`--wait` 的状态机。它替掉的是网络，而被测的那一层在替身之上，
所以那一批测试是有效的。

但它们回答不了另一个问题：**CLI 对契约的假设对不对**。那一批测试里的
JSON 是照契约手写的，Gateway 真返回 `specId` 而 CLI 读 `spec_id` 的话，
那一整批测试**照样全绿**。

这条用例把 CLI 架在真的 `create_app()` 上跑一遍。它一次覆盖三件事：

* 字段名与状态码真的对得上（契约漂移会在这里红）
* `Authorization: Bearer` 的格式被真的解析（`gateway/auth._bearer`）
* 文档解析、指纹去重、Run 创建这条路真的通

## 用真 uvicorn 而不是 `TestClient`

`TestClient` 走的是 ASGI 直连，`Authorization` 头与 URL 都能被正确构造，
但它**不经过真实的 HTTP 栈**。而 CLI 的价值恰恰在于"它是一个外部进程
会用的东西"——一个只在 ASGI 直连下成立的客户端，某天会因为
`Content-Length`、`Accept` 或路径规范化而在真实网络下失败。

代价是多一个线程与一个端口。照 `conftest.py::fixture_server` 的做法起，
并为端口被占用准备一条明确的跳过。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from testflow.entrypoints import cli as cli_module
from testflow.entrypoints.gateway import create_app
from testflow.gateway.deps import GatewayContext

pytestmark = pytest.mark.integration

HOST = "127.0.0.1"
PORT = 9121
BASE_URL = f"http://{HOST}:{PORT}"

SPEC_BODY = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "cli fixture", "version": "1"},
        "servers": [{"url": "http://127.0.0.1:9000"}],
        "paths": {"/ok": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
)

#: 一份**没有 `servers`** 的文档。`declared_targets` 会因此为空，
#: 而那意味着所有请求都会被 Guardrail 拒绝——CLI 必须把这件事说出来。
SPEC_WITHOUT_SERVERS = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "no servers", "version": "1"},
        "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
)


class _Recorder:
    """记录被投递的推进指令（`gateway.deps.Producer` 协议）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def orchestrate(self, run_id, reason: str) -> None:
        self.calls.append((str(run_id), reason))


@pytest.fixture
def producer() -> _Recorder:
    return _Recorder()


@pytest.fixture
def gateway_base_url(session: Session, make_settings, producer: _Recorder) -> Iterator[str]:
    """把真实的 Gateway 起在一个真端口上，接到测试所在的连接。"""
    import uvicorn

    connection = session.get_bind()
    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    settings = make_settings()
    app = create_app(
        settings,
        context=GatewayContext(
            settings=settings,
            producer=producer,
            _engine=connection.engine,  # type: ignore[attr-defined]
            _sessions=factory,
        ),
    )

    if _reachable():
        # 端口上已经有东西在应答。**不能复用**——那多半是别的服务，
        # 而复用会让断言在一个完全无关的响应上运行。
        pytest.skip(f"{BASE_URL} 已被占用，无法起测试用的 Gateway")

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _reachable():
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        pytest.skip("测试用 Gateway 未能在 15 秒内就绪")

    yield BASE_URL

    server.should_exit = True
    thread.join(timeout=5)


def _reachable() -> bool:
    import httpx

    try:
        return httpx.get(f"{BASE_URL}/healthz", timeout=0.5).status_code == 200
    except Exception:  # noqa: BLE001 —— 连不上就是没就绪
        return False


@pytest.fixture
def run_cli(gateway_base_url: str):
    """跑真的 CLI，只把地址指过去。**不替换任何东西。**

    与单测里那个 fixture 的关键区别就在这里：那一版把
    `GatewayApiClient` 换成了接到假 Gateway 上的，这一版什么都不换——
    所以"CLI 造客户端的方式"本身也在被测范围内。
    """
    runner = CliRunner()

    def _run(*args: str):
        return runner.invoke(cli_module.app, [*args, "--base-url", gateway_base_url])

    return _run


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(SPEC_BODY, encoding="utf-8")
    return path


class TestRunAgainstTheRealGateway:
    def test_upload_and_create_run(self, run_cli, spec_file, producer: _Recorder) -> None:
        """★ 契约一致性的核心断言。

        CLI 从响应里读的每一个字段（`spec_id` / `operation_count` /
        `reused` / `run_id`）都在这里被真 Gateway 的响应检验一次。
        """
        result = run_cli("run", str(spec_file))

        assert result.exit_code == 0, f"stdout={result.stdout} stderr={result.stderr}"
        lines = result.stdout.splitlines()

        spec_line = next(line for line in lines if line.startswith("spec_id="))
        run_line = next(line for line in lines if line.startswith("run_id="))
        assert "operation_count=1" in lines
        assert "reused=false" in lines

        # `run_id` 必须是**真 UUID**——CLI 只是把服务端的字符串转述出来，
        # 而一个"看着像 id 的东西"会让后续所有 `status` 调用都 404。
        import uuid

        run_id = run_line.split("=", 1)[1]
        uuid.UUID(run_id)

        assert producer.calls == [(run_id, "run_created")]
        assert spec_line.split("=", 1)[1]

    def test_repeat_upload_reuses_the_spec(self, run_cli, spec_file) -> None:
        """DM2 指纹去重在**真实解析路径**上生效。

        这条只能在集成测试里验：指纹是 `SpecParser` 算的，而单测里的
        假 Gateway 只是照契约回了个 `reused: false`。
        """
        first = run_cli("run", str(spec_file)).stdout
        second = run_cli("run", str(spec_file)).stdout

        first_id = next(line for line in first.splitlines() if line.startswith("spec_id="))
        second_lines = second.splitlines()
        assert first_id in second_lines
        assert "reused=true" in second_lines

    def test_an_empty_whitelist_is_warned_about_on_the_real_path(self, run_cli, tmp_path) -> None:
        """★ 白名单为空必须被说出来——**这一条最容易在真实路径上漏掉**。

        `SpecParser` 对"没有 servers"的文档是**降级继续**（返回空
        `declared_targets` + 一条 warning），而不是硬失败。于是用户能
        成功发起一个"所有请求都会被拒绝"的 Run——只有把那条 warning
        打到终端上，他才知道为什么跑出来一片红。
        """
        path = tmp_path / "no-servers.json"
        path.write_text(SPEC_WITHOUT_SERVERS, encoding="utf-8")

        result = run_cli("run", str(path))

        assert result.exit_code == 0
        assert "no_targets" in result.stderr
        assert "no_targets" not in result.stdout, "提示不该混进给脚本解析的那一路"

    def test_the_bearer_token_is_accepted(self, session: Session, spec_file, monkeypatch) -> None:
        """带令牌的端到端。**只有真的走一遍才验得到 `Bearer` 的格式。**

        `gateway/auth._bearer` 期望的是 `Bearer <token>`；CLI 写成
        `bearer <token>` 或漏掉空格的话，本地 MockTransport 那批测试
        发现不了——因为它只断言"请求里有这个头"，不解析它。
        """
        import uvicorn

        connection = session.get_bind()
        factory = sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        from testflow.config import Settings

        settings = Settings(
            _env_file=None,
            database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
            internal_token="internal-token",
            credential_encryption_key="fernet-key-placeholder",
            llm_base_url="https://example.invalid/v1",
            llm_api_key="sk-placeholder",
            api_token="user-token",
        )
        app = create_app(
            settings,
            context=GatewayContext(
                settings=settings,
                producer=_Recorder(),
                _engine=connection.engine,  # type: ignore[attr-defined]
                _sessions=factory,
            ),
        )

        if _reachable():
            pytest.skip(f"{BASE_URL} 已被占用")
        server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _reachable():
                break
            time.sleep(0.05)
        else:  # pragma: no cover
            pytest.skip("测试用 Gateway 未能在 15 秒内就绪")

        runner = CliRunner()
        try:
            # 不带令牌 → 401
            missing = runner.invoke(cli_module.app, ["run", str(spec_file), "--base-url", BASE_URL])
            assert missing.exit_code == 1
            assert "error=http_401" in missing.stdout.splitlines()

            # 带令牌 → 通
            with_token = runner.invoke(
                cli_module.app,
                ["run", str(spec_file), "--base-url", BASE_URL, "--token", "user-token"],
            )
            assert with_token.exit_code == 0, with_token.stderr
        finally:
            server.should_exit = True
            thread.join(timeout=5)


class TestStatusAgainstTheRealGateway:
    def test_status_reports_the_phase_of_a_freshly_created_run(self, run_cli, spec_file) -> None:
        created = run_cli("run", str(spec_file)).stdout
        run_id = next(line for line in created.splitlines() if line.startswith("run_id=")).split(
            "=", 1
        )[1]

        result = run_cli("status", run_id)

        assert result.exit_code == 0
        assert f"run_id={run_id}" in result.stdout.splitlines()
        assert "phase=init" in result.stdout.splitlines()

    def test_status_echoes_the_problem_code_for_an_unknown_run(self, run_cli) -> None:
        """★ `error=` 那一行的取值来自 Gateway 的 problem+json。

        本地 MockTransport 那批测试是**照契约手写** `run_not_found` 的，
        所以它们验不了这个字符串真的来自服务端。这里验。
        """
        result = run_cli("status", "00000000-0000-0000-0000-000000000000")

        assert result.exit_code == 1
        assert "error=run_not_found" in result.stdout.splitlines()


class TestReportAgainstTheRealGateway:
    def test_a_not_ready_report_reports_the_current_phase(self, run_cli, spec_file) -> None:
        created = run_cli("run", str(spec_file)).stdout
        run_id = next(line for line in created.splitlines() if line.startswith("run_id=")).split(
            "=", 1
        )[1]

        result = run_cli("report", run_id)

        assert result.exit_code == 1
        assert "error=report_not_ready" in result.stdout.splitlines()
        assert "init" in result.stderr, "要说清当前阶段——用户据此判断还要不要等"
