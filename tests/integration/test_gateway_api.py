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

from testflow.domain.graph import RunPhase
from testflow.entrypoints.gateway import create_app
from testflow.gateway.deps import GatewayContext
from testflow.storage.repositories import results as results_repo

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


class _DecisionAwareProducer:
    """在"被叫醒"的那一刻去查库，记录用例的 `review_status`。

    ## 它守的是一条**顺序**约束

    `submit_decisions` 必须**先 commit、再投递**（ADR-0009）。若顺序反了，
    推进者可能在决策还没落库时就醒来，读到的是一个空集合——于是图
    带着"全部未批准"的状态继续，而用户看到的是 200 和一堆批准计数。

    顺序错了**不会有任何报错**：两条语句各自都对，只是次序不对。
    所以只能在一个"卡在中间"的探针里看——而这个 producer 就是那个探针。
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self.calls: list[tuple[str, str]] = []
        self.statuses_at_wake: dict[str, str] = {}

    def orchestrate(self, run_id, reason: str) -> None:
        from sqlalchemy.orm import sessionmaker as _sessionmaker

        from testflow.storage.repositories import cases as cases_repo

        self.calls.append((str(run_id), reason))
        factory = _sessionmaker(
            bind=self._session.get_bind(), join_transaction_mode="create_savepoint"
        )
        with factory() as s:
            self.statuses_at_wake = {
                str(c.id): c.review_status for c in cases_repo.list_by_run(s, run_id)
            }


class TestRunCases:
    """`GET /api/runs/{id}/cases`——人工确认界面的数据源。"""

    def test_low_confidence_cases_come_first(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """★ 排序本身就是接口语义：**最该被人细看的排在前面。**

        低置信度意味着期望值有一部分是**推断**出来的（`TestCaseGen` §7），
        而"推断"正是会出现"期望值本身写错"的那一类。把它们埋在第 3 页，
        人工确认就退化成一路点"全部批准"。
        """
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        # ⚠️ 名字的**词法顺序必须与期望的置信度顺序相反**，否则这条测试会
        # 因为巧合而通过：排序里 `created_at` 并列时会退到 `name`
        # （同一个事务里 `now()` 是常量），于是名字排出来的顺序会被误认为
        # 置信度排出来的顺序。
        #
        # 第一版用的是中文名「高 / 未标注 / 低」——它们的 Unicode 码点恰好是
        # 高 > 未标注 > 低，即词法升序等于置信度升序。删掉置信度排序后
        # 测试**照样全绿**，而它看起来验的正是排序。
        for name, confidence in [("a-high", "high"), ("b-none", None), ("c-low", "low")]:
            make_case(run, op, name=name, confidence=confidence)

        body = client.get(f"/api/runs/{run.id}/cases").json()

        assert [item["confidence"] for item in body["items"]] == ["low", None, "high"]
        assert [item["name"] for item in body["items"]] == ["c-low", "b-none", "a-high"], (
            "顺序必须由置信度决定，而不是由名字的词法顺序"
        )

    def test_total_counts_all_cases_not_just_the_page(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """`total` 是全部用例数——少了它前端显示不出"共 N 条"。"""
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        for i in range(7):
            make_case(run, op, name=f"c{i:02d}")

        body = client.get(f"/api/runs/{run.id}/cases", params={"limit": 3}).json()

        assert body["total"] == 7
        assert len(body["items"]) == 3
        assert body["limit"] == 3 and body["offset"] == 0

    def test_paging_covers_every_case_exactly_once(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """翻页不能漏、不能重。

        ⚠️ 这条在**同一页内看起来永远是对的**——只有跨页才暴露。
        而本项目里它很容易出错：同一个事务里 `now()` 是常量
        （见 `test_repositories.py` 的说明），所以 `created_at` 分不开这些用例，
        真正决定顺序的是后面那两个排序键。少了它们，PostgreSQL 没有义务
        保证两次查询给出同一顺序，同一条用例就可能在两页里各出现一次。
        """
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        names = [f"c{i:02d}" for i in range(10)]
        for name in names:
            make_case(run, op, name=name)

        seen: list[str] = []
        for offset in (0, 4, 8):
            body = client.get(
                f"/api/runs/{run.id}/cases", params={"limit": 4, "offset": offset}
            ).json()
            seen.extend(item["name"] for item in body["items"])

        assert sorted(seen) == names, "翻页必须恰好覆盖每一条一次"
        assert len(seen) == len(set(seen)), "同一条用例不该出现在两页里"

    def test_a_limit_above_the_cap_is_rejected_not_silently_truncated(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """超上限要**明确拒绝**。

        静默截断到 500 的话，请求 5000 条的调用方会以为自己拿全了——
        而"我拿到的就是全部"这个错误认知会导致漏看用例，
        且它看起来完全正常（200 + 一批数据）。
        """
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        make_case(run, op)

        response = client.get(f"/api/runs/{run.id}/cases", params={"limit": 5000})

        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")

    def test_unknown_run_is_404(self, client: TestClient) -> None:
        import uuid

        response = client.get(f"/api/runs/{uuid.uuid4()}/cases")
        assert response.status_code == 404
        assert response.json()["code"] == "run_not_found"


class TestDecisions:
    """`POST /api/runs/{id}/decisions`——IP-1 的 HTTP 面。"""

    @staticmethod
    def _awaiting_run(session: Session, make_spec, make_operation, make_run, make_case, count: int):
        """造一个停在确认门上的 Run，带 `count` 条待确认用例。"""
        from testflow.domain.graph import RunPhase
        from testflow.storage.repositories import runs as runs_repo

        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        cases = [make_case(run, op, name=f"c{i:02d}") for i in range(count)]
        # 用 `set_phase` 而不是直接赋值：它是**唯一**的相位入口，
        # 顺手把 `updated_at` 推进（兜底轮询据此判停滞）。
        runs_repo.set_phase(session, run.id, RunPhase.AWAITING_REVIEW)
        session.flush()
        return run, cases

    def test_approve_and_reject_are_applied_and_counted(
        self,
        client: TestClient,
        session: Session,
        producer: _Recorder,
        make_spec,
        make_operation,
        make_run,
        make_case,
    ) -> None:
        run, cases = self._awaiting_run(session, make_spec, make_operation, make_run, make_case, 4)
        producer.calls.clear()

        response = client.post(
            f"/api/runs/{run.id}/decisions",
            json={
                "decisions": [
                    {"case_id": str(cases[0].id), "decision": "approve"},
                    {"case_id": str(cases[1].id), "decision": "approve"},
                    {"case_id": str(cases[2].id), "decision": "reject"},
                    {"case_id": str(cases[3].id), "decision": "reject"},
                ]
            },
        )

        assert response.status_code == 200, "决策同步生效——200 而不是 202"
        body = response.json()
        assert body["run_id"] == str(run.id)
        assert body["applied"] == {"approved": 2, "rejected": 2}
        assert producer.calls[-1] == (str(run.id), "decisions_submitted")

    def test_the_decision_is_committed_before_the_wake_is_sent(
        self, session: Session, make_settings, make_spec, make_operation, make_run, make_case
    ) -> None:
        """★ ADR-0009：**先落库、再叫醒推进者。**

        用 `_DecisionAwareProducer` 在"被叫醒的那一刻"去查库——
        顺序反了的话，它读到的会是一批 `pending`，而接口照样返回 200。
        """
        probe = _DecisionAwareProducer(session)
        factory = sessionmaker(
            bind=session.get_bind(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        app = create_app(
            make_settings(),
            context=GatewayContext(
                settings=make_settings(),
                producer=probe,
                _engine=session.get_bind().engine,  # type: ignore[attr-defined]
                _sessions=factory,
            ),
        )
        run, cases = self._awaiting_run(session, make_spec, make_operation, make_run, make_case, 2)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                f"/api/runs/{run.id}/decisions",
                json={"decisions": [{"case_id": str(c.id), "decision": "approve"} for c in cases]},
            )

        assert response.status_code == 200
        assert probe.calls == [(str(run.id), "decisions_submitted")]
        assert set(probe.statuses_at_wake.values()) == {"approved"}, (
            "推进者被叫醒时读到的还是 pending——决策没有在投递之前提交"
        )

    def test_a_second_submit_is_409(
        self,
        client: TestClient,
        session: Session,
        make_spec,
        make_operation,
        make_run,
        make_case,
    ) -> None:
        """双击 / 两个标签页。

        **必须明确拒绝**，而不是静默地再推进一次图——那会让"这个 Run
        正在被谁推进"变得不可知。
        """
        run, cases = self._awaiting_run(session, make_spec, make_operation, make_run, make_case, 1)
        payload = {"decisions": [{"case_id": str(cases[0].id), "decision": "approve"}]}

        first = client.post(f"/api/runs/{run.id}/decisions", json=payload)
        second = client.post(f"/api/runs/{run.id}/decisions", json=payload)

        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json()["code"] == "run_not_awaiting_review"

    def test_a_run_that_is_not_awaiting_review_is_409(
        self, client: TestClient, make_spec, make_operation, make_run, make_case, session
    ) -> None:
        """阶段不对时提交决策 → 409，且 `detail` 里说明当前阶段。

        只说"冲突了"是不够的：用户需要知道**现在处于什么状态**才能决定
        下一步（刷新看看？还是这个 Run 已经结束了？）。
        """
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)  # 默认 phase = init
        case = make_case(run, op)

        response = client.post(
            f"/api/runs/{run.id}/decisions",
            json={"decisions": [{"case_id": str(case.id), "decision": "approve"}]},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "run_not_awaiting_review"
        assert "init" in response.json()["detail"]

    def test_an_incomplete_decision_set_is_422(
        self,
        client: TestClient,
        session: Session,
        make_spec,
        make_operation,
        make_run,
        make_case,
    ) -> None:
        """少报一条 → 拒绝，并说清少了几条。

        **不能默认放行没被提到的**：那些用例会停在 pending，
        既不执行也不出现在报告里，而用户以为自己全批准了。
        """
        run, cases = self._awaiting_run(session, make_spec, make_operation, make_run, make_case, 3)

        response = client.post(
            f"/api/runs/{run.id}/decisions",
            json={
                "decisions": [
                    {"case_id": str(cases[0].id), "decision": "approve"},
                    {"case_id": str(cases[1].id), "decision": "approve"},
                ]
            },
        )

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "decision_incomplete"
        assert "1" in body["detail"], "要说清还有几条没提交"

    def test_an_unknown_case_id_is_422_not_a_silent_noop(
        self,
        client: TestClient,
        session: Session,
        make_spec,
        make_operation,
        make_run,
        make_case,
    ) -> None:
        """提交了一条不属于这个 Run 的用例 → 拒绝。

        静默忽略它会让用户以为自己批了一条**别的 Run 的**用例——
        而那正是"批错了对象"这类事故的形态。
        """
        import uuid

        run, cases = self._awaiting_run(session, make_spec, make_operation, make_run, make_case, 1)

        response = client.post(
            f"/api/runs/{run.id}/decisions",
            json={
                "decisions": [
                    {"case_id": str(cases[0].id), "decision": "approve"},
                    {"case_id": str(uuid.uuid4()), "decision": "reject"},
                ]
            },
        )

        assert response.status_code == 422
        assert response.json()["code"] == "decision_incomplete"

    def test_a_malformed_case_id_is_422_with_a_useful_message(
        self,
        client: TestClient,
        session: Session,
        make_spec,
        make_operation,
        make_run,
        make_case,
    ) -> None:
        """非法 UUID 必须报"格式不对"，**不能报"少提交了 N 条"**。

        后者会让用户去数自己的请求（数出 118 条，一条不少）然后彻底迷路
        ——而真正的原因是他把一个 id 复制错了。
        """
        run, cases = self._awaiting_run(session, make_spec, make_operation, make_run, make_case, 1)

        response = client.post(
            f"/api/runs/{run.id}/decisions",
            json={
                "decisions": [
                    {"case_id": str(cases[0].id), "decision": "approve"},
                    {"case_id": "not-a-uuid", "decision": "reject"},
                ]
            },
        )

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_request"

    def test_a_misspelled_decision_is_rejected_at_the_boundary(self, client: TestClient) -> None:
        """`"approved"` 不是合法取值——在边界上就拒绝。

        落库之后才发现的话，用户看到的是 `CHECK` 约束失败（500），
        而错误信息里没有一个字提到他拼错了。
        """
        response = client.post(
            "/api/runs/00000000-0000-0000-0000-000000000000/decisions",
            json={"decisions": [{"case_id": "x", "decision": "approved"}]},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_request"

    def test_there_is_no_way_to_authorize_destructive_cases(self) -> None:
        """ADR-0021 / TE3：**决策接口不提供破坏性授权入口**。

        这里用 `extra="forbid"` 把它做成一个**能被说出口的错误**：
        有人试图在请求里带 `destructive_authorized` 时，会拿到 422，
        而不是一个被静默忽略、让他以为授权生效了的 200。
        """
        from testflow.gateway.schemas import DecisionItem, DecisionRequest

        assert set(DecisionItem.model_fields) == {"case_id", "decision"}
        assert set(DecisionRequest.model_fields) == {"decisions"}

        app = create_app()
        path = "/api/runs/{run_id}/decisions"
        schema = app.openapi()["paths"][path]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        assert schema["$ref"].endswith("/DecisionRequest")

    def test_the_producer_failing_does_not_undo_the_decision(
        self, session: Session, make_settings, make_spec, make_operation, make_run, make_case
    ) -> None:
        """★ 投递失败**不是**接口失败——用户看到 200 是对的。

        返回 503 会诱导用户重试，而重试会命中 409（决策已经生效）——
        那时他会以为自己刚才提交的决策丢了。而实际上决策已经落库、
        也已经能被执行；兜底轮询会在审阅超时前扫到它（`06` §6.4）。

        所以这里断言的是"返回成功，且库里的状态确实改了"。
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
        run, cases = self._awaiting_run(session, make_spec, make_operation, make_run, make_case, 1)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                f"/api/runs/{run.id}/decisions",
                json={"decisions": [{"case_id": str(cases[0].id), "decision": "approve"}]},
            )

        assert response.status_code == 200
        from testflow.storage.repositories import cases as cases_repo

        session.expire_all()
        assert cases_repo.get(session, cases[0].id).review_status == "approved"


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

    def test_wake_resumes_once_every_dispatchable_case_is_settled(
        self,
        client: TestClient,
        session,
        make_settings,
        producer: _Recorder,
        make_spec,
        make_operation,
        make_run,
        make_case,
    ) -> None:
        """★★ 完成判定必须**数数**，不能读 `runs.expected_case_count`（DM5）。

        第一版读的是那个派生缓存，而**没有任何节点维护它**——恒为 0。
        于是 `expected == 0` 让这个端点**永远返回 `pending`**：
        每条用例完成时的回调唤醒全部空转，Run 只能靠兜底轮询往前挪。

        ## 为什么这个 bug 极难被发现

        它的表现是**慢**，不是错——Run 最终仍然会完成（兜底轮询会兜住），
        只是每次唤醒都白跑一趟。没有测试会因为"慢了 30 秒"而变红，
        而一个正常工作的系统看起来就是这样：有点慢，但能跑完。

        这一条覆盖了从 `pending` 到 `resumed` 的**跃迁**——
        而那正是"结果到齐了"这个判定的全部内容。
        """
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec, phase=RunPhase.AWAITING_RESULTS)
        cases = [make_case(run, op, name=f"c{i}", review_status="approved") for i in range(2)]
        # 多加一条被拒绝的：它**不算**"应当有结果"，所以不该拖住判定。
        make_case(run, op, name="被拒绝的", review_status="rejected")
        session.flush()

        headers = {"x-internal-token": make_settings().internal_token}

        # 只有一条结果 → 还没到齐
        results_repo.upsert_many(
            session, [{"run_id": run.id, "case_id": cases[0].id, "status": "passed"}]
        )
        session.flush()
        producer.calls.clear()
        early = client.post(f"/internal/runs/{run.id}/wake", headers=headers)
        assert early.json()["status"] == "pending"
        assert producer.calls == []

        # 两条都到齐 → 推进（被拒绝的那条不参与分母）
        results_repo.upsert_many(
            session, [{"run_id": run.id, "case_id": cases[1].id, "status": "failed"}]
        )
        session.flush()
        resumed = client.post(f"/internal/runs/{run.id}/wake", headers=headers)

        assert resumed.json()["status"] == "resumed", "该推进时不推进——缓存口径与投递口径对不上了"
        assert producer.calls[-1] == (str(run.id), "results_ready")

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


class TestFrontendHosting:
    """SPA 托管。**用合成的 dist，不依赖真实的构建产物。**

    `frontend/dist/` 是 gitignore 的（构建产物不进版本库），所以 CI 上
    没有它。拿真实 dist 写测试会让这几条在 CI 里静默跳过——
    而跳过和通过看起来一样绿。
    """

    @pytest.fixture
    def dist(self, tmp_path):
        """一份最小但**结构真实**的 dist：入口 + 一个带哈希的资源文件。"""
        root = tmp_path / "dist"
        (root / "assets").mkdir(parents=True)
        (root / "index.html").write_text(
            "<!doctype html><html><body><div id=app></div>"
            '<script src="/assets/app-abc123.js"></script></body></html>',
            encoding="utf-8",
        )
        (root / "assets" / "app-abc123.js").write_text("console.log('app')", encoding="utf-8")
        return root

    def _app(self, session: Session, make_settings, dist):
        factory = sessionmaker(
            bind=session.get_bind(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        return create_app(
            make_settings(),
            context=GatewayContext(
                settings=make_settings(),
                producer=_Recorder(),
                _engine=session.get_bind().engine,  # type: ignore[attr-defined]
                _sessions=factory,
            ),
            frontend_dist=dist,
        )

    def test_the_index_is_served_at_the_root(self, session, make_settings, dist) -> None:
        with TestClient(self._app(session, make_settings, dist)) as client:
            response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "<div id=app>" in response.text

    def test_a_real_asset_file_is_served(self, session, make_settings, dist) -> None:
        with TestClient(self._app(session, make_settings, dist)) as client:
            response = client.get("/assets/app-abc123.js")
        assert response.status_code == 200
        assert "console.log" in response.text

    def test_a_frontend_route_falls_back_to_the_index(self, session, make_settings, dist) -> None:
        """★ `/runs/01J.../confirm` 在磁盘上**不存在**。

        它是前端路由。返回 `index.html` 让浏览器端的路由接手——
        直接 404 的话，用户从 CLI 打印的那个链接点进来会看到空白页。
        """
        with TestClient(self._app(session, make_settings, dist)) as client:
            response = client.get("/runs/01JABC/confirm")
        assert response.status_code == 200
        assert "<div id=app>" in response.text

    @pytest.mark.parametrize(
        "path",
        [
            # 未注册的 /api 路径 —— **只有这一条会真的走到 SPA 兜底**。
            # 第一版用的是 `/api/runs/{uuid}`，那被 `get_run` 自己接住了，
            # 于是把 `_API_PREFIXES` 那道判断整个删掉，测试**照样全绿**。
            "/api/does-not-exist",
            "/api/runs/00000000-0000-0000-0000-000000000000/bogus",
            "/internal/whatever",
        ],
    )
    def test_unknown_api_paths_still_return_problem_json(
        self, session, make_settings, dist, path: str
    ) -> None:
        """★★ 这条是本节最重要的一条。

        SPA 兜底挂在 `/{full_path:path}` 上——它**会接住 `/api/*` 里没匹配
        上的路径**。不显式排除的话，这些路径返回 `index.html` 与 **200**，
        而契约里写的是"404 + problem+json"。

        更坏的是它**只在 dist 存在时才发生**：本地纯后端开发一切正常，
        上线之后调用方（CLI、外部集成）开始拿到一堆 HTML。
        """
        with TestClient(self._app(session, make_settings, dist)) as client:
            response = client.get(path)

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "not_found"

    def test_a_registered_api_path_keeps_its_own_error(self, session, make_settings, dist) -> None:
        """已注册路径的错误仍是它自己的（`run_not_found`），不该被兜底改写。

        与上一条分开写：两条路径在**不同的层**被拒绝，而合成一条会让人
        分不清"是路由自己报的错，还是兜底给的通用 404"。
        对调用方来说这个区别是实打实的——`run_not_found` 能让他去查
        run_id 拼错没有，`not_found` 只能让他去查路径。
        """
        with TestClient(self._app(session, make_settings, dist)) as client:
            response = client.get("/api/runs/00000000-0000-0000-0000-000000000000")

        assert response.status_code == 404
        assert response.json()["code"] == "run_not_found"

    def test_a_missing_dist_says_so_instead_of_a_blank_page(
        self, session, make_settings, tmp_path
    ) -> None:
        """没有 dist（纯后端部署 / CI）时**明确说明**。

        返回一个空白的 200 会让人以为前端坏了。而"没有前端产物"是
        一条能读懂、也能自己解决的信息。
        """
        with TestClient(self._app(session, make_settings, tmp_path / "nope")) as client:
            response = client.get("/")

        assert response.status_code == 404
        assert response.json()["code"] == "frontend_not_built"
        assert "npm" in response.json()["detail"]

    def test_traversal_out_of_the_dist_directory_is_refused(
        self, session, make_settings, tmp_path, dist
    ) -> None:
        """路径穿越。**`{full_path:path}` 会把 `..` 原样交给我们。**

        `(dist / "../../secret").resolve()` 会跑出 dist 之外——而 dist 之外
        是进程能读到的任何东西（`.env`、源码、凭证）。所以拼完之后必须
        校验它仍在 dist 里。

        用百分号编码绕开客户端的路径规范化（httpx/starlette 都会把 `..`
        归一掉，而攻击者当然不会乖乖归一）。
        """
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET", encoding="utf-8")

        with TestClient(self._app(session, make_settings, dist)) as client:
            response = client.get("/%2e%2e/secret.txt")

        # 实测确认 `%2e%2e` 会**原样**到达处理函数（收到的是 `../secret.txt`），
        # 所以这条验的确实是"拼完之后有没有校验它还在 dist 里"，
        # 而不是"客户端把 `..` 归一掉了"。
        assert "TOP-SECRET" not in response.text
        assert response.status_code == 200, "应当回退到 index.html，而不是报错"
        assert "<div id=app>" in response.text

    def test_the_fallback_is_not_in_the_contract(self, dist) -> None:
        """`include_in_schema=False`：SPA 兜底**不出现在 openapi.json 里**。

        它不是一个业务接口——把它写进契约会让前端的类型生成器
        凭空多出一个"什么路径都匹配"的操作。
        """
        app = create_app(frontend_dist=dist)
        paths = app.openapi()["paths"]
        assert not any("full_path" in path for path in paths)
        assert set(paths) == {
            "/api/specs",
            "/api/runs",
            "/api/runs/{run_id}",
            "/api/runs/{run_id}/cases",
            "/api/runs/{run_id}/decisions",
            "/api/runs/{run_id}/report",
            "/api/runs/{run_id}/events",
            "/api/runs/{run_id}/attributions",
            "/api/runs/{run_id}/attributions/{attribution_id}",
            "/healthz",
            # M5：就绪检查。它**要**出现在契约里——运维探针与部署脚本
            # 都是它的消费者，与只给进程存活用的 `/healthz` 不同。
            "/readyz",
        }, "契约里出现了意料之外的路径"

    def test_registering_the_fallback_does_not_break_the_contract_export(self) -> None:
        """CI 第 ④ 步导出契约时**不依赖 dist 是否存在**。

        路由集合与工作目录无关（没有 dist 时兜底路由照样注册，只是
        返回 404）。这样"开发机上有 dist、CI 上没有"就不会造成
        "契约在两边不一致"——那是最难查的一类失败。
        """
        with_dist = create_app(frontend_dist=__import__("pathlib").Path("frontend/dist"))
        without = create_app(frontend_dist=__import__("pathlib").Path("does/not/exist"))
        assert with_dist.openapi()["paths"] == without.openapi()["paths"]


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

    def test_readyz_checks_the_real_dependencies(self, client: TestClient) -> None:
        """真实 PG + Redis 都在时，`/readyz` 应当是 200。

        与上面的单测互补：那边验证状态码决策，这里验证**驱动真的能连上**。
        两个都缺一不可——只测决策会漏掉"DATABASE_URL 写错了但函数返回了
        ok"这类问题。
        """
        response = client.get("/readyz")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        assert {check["name"]: check["ok"] for check in body["checks"]} == {
            "database": True,
            "redis": True,
        }


class TestAttributions:
    """`GET/PATCH /api/runs/{id}/attributions`——M3 的 HTTP 面。"""

    @staticmethod
    def _seed(session: Session, make_spec, make_operation, make_run, make_case, *, count: int = 2):
        from testflow.storage.models import Attribution, CaseResult

        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        rows = []
        for i in range(count):
            case = make_case(run, op, name=f"c{i:02d}")
            session.add(
                CaseResult(
                    run_id=run.id,
                    case_id=case.id,
                    status="failed",
                    http_status=500,
                    assertion_outcomes=[],
                )
            )
            attribution = Attribution(
                run_id=run.id,
                case_id=case.id,
                category="server_error",
                method="rule",
                rule_ref="status.server_error",
                confidence="high",
                evidence={"rule_ref": "status.server_error", "http_status": 500},
            )
            session.add(attribution)
            rows.append((case, attribution))
        session.flush()
        return run, rows

    def test_list_returns_items_with_case_context(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        run, rows = self._seed(session, make_spec, make_operation, make_run, make_case)

        response = client.get(f"/api/runs/{run.id}/attributions")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert body["run_id"] == str(run.id)
        first = body["items"][0]
        assert first["case_name"] == "c00"
        assert first["status"] == "failed"
        assert first["http_status"] == 500
        assert first["category"] == "server_error"
        assert first["method"] == "rule"

    def test_list_unknown_run_is_problem_json_404(self, client: TestClient) -> None:
        response = client.get("/api/runs/not-a-run/attributions")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "run_not_found"

    def test_agree_marks_the_verdict(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        run, rows = self._seed(session, make_spec, make_operation, make_run, make_case, count=1)
        attribution = rows[0][1]

        response = client.patch(
            f"/api/runs/{run.id}/attributions/{attribution.id}",
            json={"verdict": "agree"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["human_verdict"] == "agree"
        assert body["human_category"] is None

    def test_disagree_requires_and_stores_the_correct_category(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        run, rows = self._seed(session, make_spec, make_operation, make_run, make_case, count=1)
        attribution = rows[0][1]

        response = client.patch(
            f"/api/runs/{run.id}/attributions/{attribution.id}",
            json={"verdict": "disagree", "human_category": "server_error"},
        )

        assert response.status_code == 200
        assert response.json()["human_category"] == "server_error"

    def test_disagree_without_a_category_is_422(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """LA6：只记「不同意」无法回答「该补哪条规则」。"""
        run, rows = self._seed(session, make_spec, make_operation, make_run, make_case, count=1)
        attribution = rows[0][1]

        response = client.patch(
            f"/api/runs/{run.id}/attributions/{attribution.id}",
            json={"verdict": "disagree"},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "human_category_required"

    def test_agree_with_a_category_is_422(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        run, rows = self._seed(session, make_spec, make_operation, make_run, make_case, count=1)
        attribution = rows[0][1]

        response = client.patch(
            f"/api/runs/{run.id}/attributions/{attribution.id}",
            json={"verdict": "agree", "human_category": "conflict"},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "human_category_not_allowed"

    def test_unknown_attribution_is_404(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run
    ) -> None:
        spec = make_spec()
        run = make_run(spec)

        response = client.patch(
            f"/api/runs/{run.id}/attributions/{'0' * 8}-0000-0000-0000-{'0' * 12}",
            json={"verdict": "agree"},
        )

        assert response.status_code == 404
        assert response.json()["code"] == "attribution_not_found"

    def test_attribution_from_another_run_is_404(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """不能通过 id 探测"这条归因存在，只是不属于这个 Run"。"""
        first_run, rows = self._seed(
            session, make_spec, make_operation, make_run, make_case, count=1
        )
        spec = make_spec()
        other_run = make_run(spec)

        response = client.patch(
            f"/api/runs/{other_run.id}/attributions/{rows[0][1].id}",
            json={"verdict": "agree"},
        )

        assert response.status_code == 404
        assert response.json()["code"] == "attribution_not_found"

    def test_verdict_can_be_changed(
        self, client: TestClient, session: Session, make_spec, make_operation, make_run, make_case
    ) -> None:
        run, rows = self._seed(session, make_spec, make_operation, make_run, make_case, count=1)
        attribution = rows[0][1]
        url = f"/api/runs/{run.id}/attributions/{attribution.id}"

        client.patch(url, json={"verdict": "disagree", "human_category": "conflict"})
        response = client.patch(url, json={"verdict": "agree"})

        assert response.status_code == 200
        assert response.json()["human_verdict"] == "agree"
        assert response.json()["human_category"] is None
