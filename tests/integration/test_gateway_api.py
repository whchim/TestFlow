"""Gateway 的集成测试。**需要 PostgreSQL 与 fixture 服务。**

对应决策：`05-API契约.md`（全部接口）、API1（代码优先）、API4（problem+json）、
API7（SSE 只传信号）、`08` §6（CI 第 ④ 步的契约快照）。

## 这里验的三类东西

1. **接口形状** —— 状态码、响应体字段、内容类型。这些是**对外契约**，
   而契约错了不会有任何东西失败——只会让调用方解析不出来。
2. **`/internal` 的隔离** —— 它不该出现在接口契约里，也需要独立的令牌。
3. **`create_app()` 不建连接** —— 这是 CI 第 ④ 步能跑起来的前提，
   而它失败的方式很隐蔽：导出契约时去连数据库，超时后才报一个
   与"契约不一致"毫无关系的错误。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from testflow.entrypoints.gateway import create_app
from testflow.gateway.deps import GatewayContext

pytestmark = pytest.mark.integration


SPEC_BODY = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "gw fixture", "version": "1"},
        "servers": [{"url": "http://127.0.0.1:9000"}],
        "paths": {"/ok": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
)


class _Recorder:
    """记录被投递的推进指令。

    它实现的是 `gateway.deps.Producer` 那个协议——**网关只认这个协议，
    不认 Celery**。这正是"薄层"的收益：测试里不需要起一个 broker。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def orchestrate(self, run_id, reason: str) -> None:
        self.calls.append((str(run_id), reason))


class _BrokenProducer:
    def orchestrate(self, run_id, reason: str) -> None:
        raise RuntimeError("队列不可达")


@pytest.fixture
def producer() -> _Recorder:
    return _Recorder()


@pytest.fixture
def client(session: Session, make_settings, producer: _Recorder) -> TestClient:
    """把网关接到**测试用的那个连接**上。

    网关自己会开 session（`context.session()`），而测试的事务是绑在一条连接
    上的。所以这里产出的 session 必须也绑到同一条连接——
    否则网关的写入在另一个事务里，测试**看不到**，而症状是
    "提交了 Spec，紧接着查询说它不存在"。
    """
    connection = session.get_bind()
    factory = sessionmaker(
        bind=connection,
        # 与 `create_session_factory` 保持一致。**工厂的配置是契约的一部分**——
        # 少了这一项，测试会暴露一个生产环境不会出现的行为差异。
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    context = GatewayContext(
        settings=make_settings(),
        producer=producer,
        _engine=connection.engine,  # type: ignore[attr-defined]
        _sessions=factory,
    )
    app = create_app(make_settings(), context=context)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestSpecUpload:
    def test_new_spec_returns_201(self, client: TestClient) -> None:
        response = client.post("/api/specs", json={"content": SPEC_BODY})
        assert response.status_code == 201
        body = response.json()
        assert body["reused"] is False
        assert body["operation_count"] == 1
        assert body["declared_targets"] == ["http://127.0.0.1:9000"]

    def test_same_content_is_reused_not_reparsed(self, client: TestClient) -> None:
        """**DM2 指纹去重的落点。**

        同一份内容重复提交不该重复解析、不该重复落 `operations`。
        `reused: true` 是给调用方看的信号——它据此知道"这次没有产生新的解析"。
        """
        first = client.post("/api/specs", json={"content": SPEC_BODY}).json()
        second = client.post("/api/specs", json={"content": SPEC_BODY})

        assert second.json()["reused"] is True
        assert second.json()["spec_id"] == first["spec_id"]

    def test_declared_targets_expose_the_whitelist_source(self, client: TestClient) -> None:
        """`declared_targets` 就是 Guardrail 白名单的来源。

        它为空意味着**所有请求都会被拒绝**——所以这个字段必须出现在响应里，
        让用户能在提交之后立刻发现"白名单是空的"。
        """
        no_servers = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "x", "version": "1"},
                "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}},
            }
        )
        body = client.post("/api/specs", json={"content": no_servers}).json()

        assert body["declared_targets"] == []
        assert any(w["code"] == "no_servers" for w in body["warnings"]), (
            "白名单为空必须通过 warnings 明确告知——否则用户会以为平台坏了"
        )

    def test_invalid_spec_is_problem_json(self, client: TestClient) -> None:
        """SR-6 / API4：错误响应是 `application/problem+json`。

        内容类型写错不会报错，只会让所有自动化的错误处理静默失效。
        """
        response = client.post("/api/specs", json={"content": '{"openapi": "3.0.3"}'})

        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["code"] == "invalid_openapi"
        assert body["type"].startswith("https://")

    def test_oversize_upload_is_rejected_before_reading(self, client: TestClient) -> None:
        huge = "x" * (11 * 1024 * 1024)
        response = client.post("/api/specs", json={"content": huge})
        assert response.status_code == 413

    def test_missing_content_field_is_422(self, client: TestClient) -> None:
        response = client.post("/api/specs", json={"nope": 1})
        assert response.status_code == 422


class TestRunLifecycle:
    def _create_spec(self, client: TestClient) -> str:
        return client.post("/api/specs", json={"content": SPEC_BODY}).json()["spec_id"]

    def test_create_run_returns_202_and_enqueues(
        self, client: TestClient, producer: _Recorder
    ) -> None:
        spec_id = self._create_spec(client)
        response = client.post("/api/runs", json={"spec_id": spec_id})

        assert response.status_code == 202, "Run 是异步创建的——202 而不是 201"
        assert producer.calls == [(response.json()["run_id"], "run_created")]

    def test_unknown_spec_is_404(self, client: TestClient) -> None:
        import uuid

        response = client.post("/api/runs", json={"spec_id": str(uuid.uuid4())})
        assert response.status_code == 404
        assert response.json()["code"] == "spec_not_found"

    def test_malformed_spec_id_is_422_not_500(self, client: TestClient) -> None:
        """输入格式不对是**调用方的问题**（422），不是服务器的问题（500）。

        返回 500 会让调用方去查服务端日志，而那里什么也没有。
        """
        response = client.post("/api/runs", json={"spec_id": "not-a-uuid"})
        assert response.status_code == 422

    def test_unavailable_queue_is_503_not_a_fake_202(self, session: Session, make_settings) -> None:
        """**队列不可用时必须明确失败。**

        返回 202（"已接受"）而实际什么都没投递，会让用户等一个永远不会开始的
        Run——那比"明确失败"糟得多，因为它看起来一切正常。
        """
        factory = sessionmaker(
            bind=session.get_bind(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        app = create_app(
            make_settings(),
            context=GatewayContext(
                settings=make_settings(),
                producer=_BrokenProducer(),
                _engine=session.get_bind().engine,  # type: ignore[attr-defined]
                _sessions=factory,
            ),
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            spec_id = client.post("/api/specs", json={"content": SPEC_BODY}).json()["spec_id"]
            response = client.post("/api/runs", json={"spec_id": spec_id})

        assert response.status_code == 503
        assert response.json()["code"] == "orchestrator_unavailable"

    def test_get_run_returns_status(self, client: TestClient) -> None:
        spec_id = self._create_spec(client)
        run_id = client.post("/api/runs", json={"spec_id": spec_id}).json()["run_id"]

        body = client.get(f"/api/runs/{run_id}").json()

        assert body["run_id"] == run_id
        assert body["phase"] == "init"
        assert body["interrupt"] is None, "非中断态不该出现 interrupt 字段"

    def test_unknown_run_is_404_problem_json(self, client: TestClient) -> None:
        import uuid

        response = client.get(f"/api/runs/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["code"] == "run_not_found"

    def test_report_not_ready_is_404_not_an_empty_report(self, client: TestClient) -> None:
        """**"还没生成"与"零用例"含义完全不同。**

        用同一个响应（比如一份空报告）表达两者，会让用户以为测试跑完了。
        """
        spec_id = self._create_spec(client)
        run_id = client.post("/api/runs", json={"spec_id": spec_id}).json()["run_id"]

        response = client.get(f"/api/runs/{run_id}/report")

        assert response.status_code == 404
        assert response.json()["code"] == "report_not_ready"


class TestWakeEndpoint:
    def _run(self, client: TestClient) -> str:
        spec_id = client.post("/api/specs", json={"content": SPEC_BODY}).json()["spec_id"]
        return client.post("/api/runs", json={"spec_id": spec_id}).json()["run_id"]

    def test_wake_without_results_reports_pending(
        self, client: TestClient, make_settings, producer: _Recorder
    ) -> None:
        run_id = self._run(client)
        producer.calls.clear()

        response = client.post(
            f"/internal/runs/{run_id}/wake",
            headers={"x-internal-token": make_settings().internal_token},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "pending", "结果没到齐时应当什么都不做"
        assert producer.calls == []

    def test_wake_requires_the_internal_token(self, client: TestClient) -> None:
        """三重隔离的第二层。**用户凭证拿到也触发不了图推进。**"""
        run_id = self._run(client)
        response = client.post(
            f"/internal/runs/{run_id}/wake", headers={"x-internal-token": "wrong"}
        )
        assert response.status_code == 401

    def test_wake_is_not_in_the_public_contract(self) -> None:
        """三重隔离的第一层：**它不对外可见**。

        （单独靠这一层不够——那只是隐晦式安全。但三层都必须在。）
        """
        from testflow.entrypoints.gateway import create_app

        paths = create_app().openapi()["paths"]
        assert not any("internal" in path for path in paths)

    def test_wake_on_a_terminal_run_is_a_noop(
        self, client: TestClient, session, make_settings, producer
    ) -> None:
        """已经跑完的 Run 再被唤醒时**什么都不做**。

        这是一个**正常的竞态**：兜底轮询是周期性的，而回调是即时的。
        轮询可能在 Run 已经跑完之后才扫到它——那时再推进一次会重跑整张图。
        """
        import uuid

        from testflow.domain.graph import RunPhase
        from testflow.storage.repositories import runs as runs_repo

        run_id = self._run(client)
        runs_repo.set_phase(session, uuid.UUID(run_id), RunPhase.DONE)
        session.flush()
        producer.calls.clear()

        response = client.post(
            f"/internal/runs/{run_id}/wake",
            headers={"x-internal-token": make_settings().internal_token},
        )

        assert response.json()["status"] == "pending"
        assert producer.calls == [], "终态之后不该再投递推进指令"


class TestAppConstruction:
    def test_create_app_does_not_connect_to_anything(self, monkeypatch) -> None:
        """**CI 第 ④ 步能跑起来的前提。**

        `export-openapi.sh` 调用 `create_app()` 导出契约快照。那一步的目的是
        "比对契约与代码是否一致"——**不是"检查数据库在不在"**。

        实现方式是：把三个连接工厂全部换成会抛异常的桩。若 `create_app()`
        在任何一步碰了它们，这条测试就会红——**而不是等到 CI 里表现为
        "契约不一致"**（那会让人往错误的方向查）。
        """
        import testflow.config
        import testflow.storage.db

        def _explode(*args, **kwargs):
            raise AssertionError("create_app() 不该建立任何连接")

        monkeypatch.setattr(testflow.storage.db, "create_db_engine", _explode)
        monkeypatch.setattr(testflow.config, "load_settings", _explode)

        from testflow.entrypoints.gateway import create_app

        app = create_app()  # 不抛异常即为通过
        assert app.openapi()["paths"], "契约里应当有路径"

    def test_healthz_exists_and_does_not_check_dependencies(self, client: TestClient) -> None:
        """`/healthz` 只报"进程活着"。

        若它去连数据库，数据库抖动时 Gateway 会被健康检查判死并重启——
        而重启一个正常的进程解决不了数据库的问题，只会中断正在处理的请求。
        """
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
