"""集成测试的共享夹具。

**这组测试需要一个真实的 PostgreSQL。** 它们测的是那些"只有真库才能验证"的
东西：`uuidv7()` 服务端默认值、`ON CONFLICT` 的冲突处理、`CHECK` 约束、
联合索引的可用性。用 SQLite 或 mock 顶上，测的就成了"我们的假设"。

Docker 未就绪时整体跳过（`pyproject.toml` 里注册了 `integration` 标记）——
CI 的 ⑤ 步同时跑 unit 与 integration，在没有 Docker 的环境里会安静跳过，
而不是变红。

## 事务回滚

每个测试跑在一个**外层事务**里，结束时回滚。这让测试之间天然隔离，
不需要清理脚本，也不会因为某个测试失败而留下脏数据影响后续。

用 `join_transaction_mode="create_savepoint"`：被测代码内部调 `commit()` 时，
实际提交的是 SAVEPOINT 而不是真事务，外层的回滚仍然有效。
没有它，一次 `commit()` 就会把测试数据永久写进库。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from testflow.config import Settings, load_settings
from testflow.storage.db import create_db_engine
from testflow.storage.models import Case, Operation, Run, Spec


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    """会话级引擎。连不上就跳过整个集成测试组。"""
    eng = create_db_engine(settings)
    try:
        with eng.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 —— 连不上是环境问题，不是测试失败
        eng.dispose()
        pytest.skip(f"数据库不可达（先运行 docker compose up -d）：{type(exc).__name__}")

    # schema 必须已迁移到位，否则测的是"表不存在"而不是被测逻辑。
    try:
        with eng.connect() as conn:
            conn.execute(text("select count(*) from specs"))
    except Exception as exc:  # noqa: BLE001
        eng.dispose()
        pytest.skip(f"schema 未初始化（先运行 alembic upgrade head）：{type(exc).__name__}")

    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """每个测试一个回滚事务。"""
    connection = engine.connect()
    transaction = connection.begin()
    sess = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        sess.close()
        transaction.rollback()
        connection.close()


# ------------------------------------------------------------------ 数据构造
#
# 这些 helper 存在的理由是**测试的可读性**：一个测试要断言"完成判定"，
# 它关心的是"造 3 条结果、其中 2 条到了终态"，而不是那 5 张表的外键顺序。


@pytest.fixture
def make_spec(session: Session):
    """造一行 Spec。默认带一个可用的 `declared_targets`。"""

    def _make(**overrides: object) -> Spec:
        spec = Spec(
            fingerprint=overrides.pop("fingerprint", uuid.uuid4().hex + uuid.uuid4().hex[:32]),
            openapi_version="3.0.3",
            raw_content="{}",
            byte_size=2,
            declared_targets=["http://127.0.0.1:9000"],
            operation_count=0,
            **overrides,
        )
        session.add(spec)
        session.flush()
        return spec

    return _make


@pytest.fixture
def make_operation(session: Session):
    def _make(
        spec: Spec, *, method: str = "GET", path: str = "/ok", **overrides: object
    ) -> Operation:
        op = Operation(spec_id=spec.id, method=method, path=path, **overrides)
        session.add(op)
        session.flush()
        return op

    return _make


@pytest.fixture
def make_run(session: Session):
    def _make(spec: Spec, **overrides: object) -> Run:
        run = Run(spec_id=spec.id, spec_fingerprint=spec.fingerprint, **overrides)
        session.add(run)
        session.flush()
        return run

    return _make


@pytest.fixture
def make_case(session: Session):
    def _make(run: Run, operation: Operation, **overrides: object) -> Case:
        case = Case(
            run_id=run.id,
            operation_id=operation.id,
            name=overrides.pop("name", "GET /ok 正常请求"),
            category="normal",
            dimension="value_valid",
            request_method="GET",
            request_path=operation.path,
            assertions=[{"type": "status_code", "expected": 200, "derived_from": "spec"}],
            generated_by="rule",
            **overrides,
        )
        session.add(case)
        session.flush()
        return case

    return _make


# ------------------------------------------------------- fixture 服务

FIXTURE_HOST = "127.0.0.1"
FIXTURE_PORT = 9000
FIXTURE_BASE_URL = f"http://{FIXTURE_HOST}:{FIXTURE_PORT}"


@pytest.fixture(scope="session")
def fixture_server() -> Iterator[str]:
    """在后台线程里拉起 fixture 服务，返回它的基地址。

    **为什么在测试进程内起，而不是要求先手工跑一个**：端到端断言必须能
    在 CI 里自动执行。要求"先运行 `testflow-fixture`"会让这些测试在实际
    CI 中永远处于"被跳过"状态——而跳过和通过看起来一样绿。

    用 `uvicorn.Server` + 线程而不是子进程：子进程在 Windows 上的退出与
    端口回收都不太可靠，而测试进程结束时线程会被一并回收。
    """
    import threading
    import time

    import httpx
    import uvicorn

    from testflow.testing.fixture_server import app, reset_hits

    # 端口已被占用（例如开发者手工起了 fixture）时直接复用，不重复启动。
    if _reachable():
        yield FIXTURE_BASE_URL
        return

    server = uvicorn.Server(
        uvicorn.Config(app, host=FIXTURE_HOST, port=FIXTURE_PORT, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _reachable():
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        pytest.skip("fixture 服务未能在 10 秒内就绪")

    # 每个会话从干净的计数开始，否则断言 `/__hits` 会受上次运行影响。
    with httpx.Client(base_url=FIXTURE_BASE_URL) as c:
        c.post("/__reset")

    yield FIXTURE_BASE_URL

    server.should_exit = True
    thread.join(timeout=5)
    _ = reset_hits  # 保留引用，说明它是 fixture 服务的一部分


def _reachable() -> bool:
    import httpx

    try:
        return httpx.get(f"{FIXTURE_BASE_URL}/ok", timeout=0.5).status_code == 200
    except Exception:  # noqa: BLE001 —— 连不上就是没就绪
        return False


@pytest.fixture
def hits(fixture_server: str) -> Iterator[object]:
    """访问计数器的读/写助手。

    用法：`hits.of("/ok")` 读计数，`hits.reset()` 清零。

    **它存在的理由**：验证"某个请求根本没发出去"不能看返回值（没发就没有
    返回值），只能去问被调用方。而安全功能大部分时间都在做"不让某事发生"，
    所以这是最常用的一类断言。
    """
    import httpx

    class _Hits:
        def __init__(self) -> None:
            self._client = httpx.Client(base_url=FIXTURE_BASE_URL, timeout=2.0)

        def all(self) -> dict[str, int]:
            return self._client.get("/__hits").json()

        def of(self, path: str) -> int:
            return self.all().get(path, 0)

        def reset(self) -> None:
            self._client.post("/__reset")

    helper = _Hits()
    try:
        yield helper
    finally:
        helper._client.close()
