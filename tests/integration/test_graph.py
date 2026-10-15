"""编排图的集成测试。**需要 PostgreSQL 与 Redis。**

对应决策：`06-Agent编排.md` §3/§4（节点与主链）、§5.1（IP-2）、§6.2（完成判定）、
§6.3（per-Run 锁）、`11-开发路线图.md` M1 专项检查 (a)。

## 这组测试验的是什么

单测能覆盖每个节点的逻辑，但覆盖不了**它们串起来之后的那些性质**：

* 中断真的发生了吗？挂起时状态是对的吗？
* **从另一个进程**（新的 checkpointer 实例）恢复，图能接着走吗？
* 拿不到锁时会不会重复推进？终态之后会不会被误唤醒？

最后一条尤其要紧：**这条链路的三个机制（回调、锁、兜底轮询）互相耦合**，
单独测每一个都通过，合起来仍然可能出错——而那种错的症状是
"偶发地重复执行"或"偶发地卡住"，在没有端到端测试时几乎无法定位。
"""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from testflow.agents.report_agent import build_report
from testflow.domain.graph import RunPhase
from testflow.graph.checkpoint import RedisCheckpointSaver
from testflow.graph.runner import GraphDeps, advance, build_graph
from testflow.storage.models import Operation, Run, Spec
from testflow.storage.repositories import results as results_repo
from testflow.storage.repositories import runs as runs_repo

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_client(settings):
    """Checkpoint 用的 Redis 连接。

    **用独立的 DB**（`redis_checkpoint_db`）——与 broker 分开，理由见 ADR-0009：
    混用同一个 DB 时一次 `FLUSHDB` 会同时删掉任务队列和图状态。
    """
    client = aioredis.from_url(f"{settings.redis_url.rstrip('/')}/{settings.redis_checkpoint_db}")
    try:
        await client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Redis 不可达（先运行 docker compose up -d）")
    yield client
    await client.aclose()


@pytest.fixture
def run_fixture(session: Session):
    """造一个可跑通的 Run：Spec + 两个接口 + Run。

    刻意用**两个**接口而不是一个：一个接口的图跑通了，可能是运气；
    两个能看出"逐 Operation 处理"这条路径对不对。
    """
    spec = Spec(
        fingerprint="e" * 64,
        openapi_version="3.0.3",
        raw_content="{}",
        byte_size=2,
        declared_targets=["http://127.0.0.1:9000"],
        operation_count=2,
    )
    session.add(spec)
    session.flush()

    operations = [
        Operation(spec_id=spec.id, method="GET", path="/ok", responses_schema={"200": {}}),
        Operation(spec_id=spec.id, method="GET", path="/no-content", responses_schema={"204": {}}),
    ]
    session.add_all(operations)
    session.flush()

    run = Run(spec_id=spec.id, spec_fingerprint=spec.fingerprint)
    session.add(run)
    session.flush()
    return run, operations


def _deps(dispatched: list[tuple[uuid.UUID, uuid.UUID]]) -> GraphDeps:
    """只把 dispatch 换成记录器——其余用真实实现。

    **不 mock 节点本身**：那样测的就是"我 mock 得对不对"，而不是"图跑不跑得通"。
    唯一需要替换的是"投递到 Celery"这一步——测试里没有 Celery，
    而我们真正关心的是**投递了什么**。
    """
    return GraphDeps(
        dispatch=lambda run_id, case_id: dispatched.append((run_id, case_id)),
        report_builder=build_report,
    )


def _fresh_graph(redis_client, dispatched):
    """每次都新建一个 checkpointer 实例——**模拟另一个进程**。"""
    return build_graph(RedisCheckpointSaver(redis_client), _deps(dispatched))


class TestFullChain:
    async def test_run_advances_to_the_interrupt(self, session, run_fixture, redis_client) -> None:
        """推进一次应当跑到 IP-2 挂起，而不是跑完。"""
        run, operations = run_fixture
        dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []
        graph = _fresh_graph(redis_client, dispatched)

        result = await advance(run.id, graph=graph, redis=redis_client, session=session)

        assert result == "started"
        session.expire_all()
        reloaded = runs_repo.get(session, run.id)
        assert reloaded.phase == RunPhase.AWAITING_RESULTS, f"应当挂在 IP-2，实际 {reloaded.phase}"
        assert dispatched, "已批准的用例应当被投递"

    async def test_cases_are_generated_and_approved(
        self, session, run_fixture, redis_client
    ) -> None:
        """N2 生成了用例，N3 把它们全部批准（M1 无 IP-1）。"""
        run, operations = run_fixture
        graph = _fresh_graph(redis_client, [])

        await advance(run.id, graph=graph, redis=redis_client, session=session)

        from testflow.storage.repositories import cases as cases_repo

        cases = cases_repo.list_by_run(session, run.id)
        assert len(cases) == len(operations), "每个接口应当至少产出一条用例"
        assert all(c.review_status == "approved" for c in cases)

    async def test_resume_after_results_completes_the_run(
        self, session, run_fixture, redis_client
    ) -> None:
        """**从另一个进程恢复**，跑完剩下的节点，产出报告。

        这里刻意新建了一个 checkpointer 实例——那正是"Worker 在别的进程里
        写完结果、回调唤醒编排层"这个场景。用同一个实例的话，
        测的就不是恢复，而是内存里那份状态。
        """
        run, operations = run_fixture
        dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []

        # ── 第一个"进程"：跑到 IP-2
        await advance(
            run.id,
            graph=_fresh_graph(redis_client, dispatched),
            redis=redis_client,
            session=session,
        )

        # ── 模拟 Worker 写结果（真实链路里这件事发生在别的进程、别的连接上）
        for _, case_id in dispatched:
            results_repo.upsert_many(
                session,
                [
                    {
                        "run_id": run.id,
                        "case_id": case_id,
                        "status": "passed",
                        "http_status": 200,
                        "assertion_outcomes": [
                            {"index": 0, "type": "status_code", "outcome": "passed"}
                        ],
                    }
                ],
            )
        session.flush()

        # ── 第二个"进程"：全新的 checkpointer，恢复并跑完
        result = await advance(
            run.id,
            graph=_fresh_graph(redis_client, dispatched),
            redis=redis_client,
            session=session,
            reason="results_ready",
        )

        assert result == "resumed"
        session.expire_all()
        assert runs_repo.get(session, run.id).phase == RunPhase.DONE

    async def test_report_is_written(self, session, run_fixture, redis_client) -> None:
        """跑完之后报告存在，且通过率与写入的结果一致。"""
        run, _ = run_fixture
        dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []
        await advance(
            run.id,
            graph=_fresh_graph(redis_client, dispatched),
            redis=redis_client,
            session=session,
        )
        for _, case_id in dispatched:
            results_repo.upsert_many(
                session,
                [{"run_id": run.id, "case_id": case_id, "status": "passed"}],
            )
        session.flush()
        await advance(
            run.id,
            graph=_fresh_graph(redis_client, dispatched),
            redis=redis_client,
            session=session,
        )

        from testflow.storage.repositories import reports as reports_repo

        report = reports_repo.get_by_run(session, run.id)
        assert report is not None
        assert report.content["summary"]["passed"] == len(dispatched)
        assert report.rendered, "Markdown 快照不能为空（RA4：它是快照，不是视图）"


class TestIdempotencyAndGuards:
    async def test_advancing_a_terminal_run_is_a_noop(
        self, session, run_fixture, redis_client
    ) -> None:
        """终态之后不再推进。

        **这条不能省**：兜底轮询是周期性的，而推进是即时的——它可能在
        Run 已经跑完之后才扫到它。那时再唤醒一次会把整张图从头跑一遍。

        代价不只是浪费：N2 会重新生成用例（对已经确认过的那些来说是数据丢失），
        N4 会重新投递（给目标服务凭空多打一轮请求）。
        """
        run, _ = run_fixture
        runs_repo.set_phase(session, run.id, RunPhase.DONE)
        session.flush()

        dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []
        result = await advance(
            run.id,
            graph=_fresh_graph(redis_client, dispatched),
            redis=redis_client,
            session=session,
        )

        assert result == "terminal"
        assert dispatched == []

    async def test_lock_contention_returns_lock_held(
        self, session, run_fixture, redis_client
    ) -> None:
        """锁被占着时**直接返回，不等待、不报错**。

        "别人正在推进"是完全正常的情况：N 条用例完成会有 N 次唤醒，
        其中 N-1 次会遇到这个分支。把它当错误处理会让日志里充满假警报。
        """
        run, _ = run_fixture
        from testflow.graph.locks import run_lock_key

        # 手工占住锁——模拟"另一个推进者正在跑"。
        await redis_client.set(run_lock_key(run.id), "someone-else")

        dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []
        result = await advance(
            run.id,
            graph=_fresh_graph(redis_client, dispatched),
            redis=redis_client,
            session=session,
        )

        assert result == "lock_held"
        assert dispatched == [], "拿不到锁就不该做任何事"

    async def test_destructive_cases_are_not_dispatched(
        self, session, run_fixture, redis_client
    ) -> None:
        """TE3：破坏性用例在 N4 被过滤——**这是主防线**。

        Worker 侧的 Guardrail 是冗余的那一层（TE6）。两层都要有：
        本层是应用逻辑，可能因 bug 或后续改动而失效。
        """
        run, operations = run_fixture
        operations[0].is_destructive = True
        session.flush()

        dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []
        await advance(
            run.id,
            graph=_fresh_graph(redis_client, dispatched),
            redis=redis_client,
            session=session,
        )

        from testflow.storage.repositories import cases as cases_repo

        destructive_ids = {
            c.id for c in cases_repo.list_by_run(session, run.id) if c.is_destructive
        }
        assert destructive_ids, "前置条件：应当生成了一条破坏性用例"
        assert not (destructive_ids & {cid for _, cid in dispatched}), "破坏性用例不该被投递"


class TestOperationsAreIdempotent:
    async def test_resuming_twice_does_not_regenerate_cases(
        self, session, run_fixture, redis_client
    ) -> None:
        """**DM4：N2 的幂等是"跳过"，不是"重放"。**

        LLM 输出非确定，重跑会得到不同的用例——对用户已经确认过的那些来说，
        那是一次静默的数据替换，而不是一次幂等重试。
        """
        run, _ = run_fixture
        from testflow.storage.repositories import cases as cases_repo

        await advance(
            run.id, graph=_fresh_graph(redis_client, []), redis=redis_client, session=session
        )
        first = {c.id for c in cases_repo.list_by_run(session, run.id)}

        # 直接再跑一次 N2（不经过图）——模拟"同一批 Operation 被重新处理"
        from testflow.agents.test_case_gen import RuleBasedGenerator, generate_for_operations
        from testflow.storage.repositories import operations as operations_repo

        ops = operations_repo.list_by_spec(session, run.spec_id)
        outcome = await generate_for_operations(
            session,
            run_id=run.id,
            operation_ids=[op.id for op in ops],
            generator=RuleBasedGenerator(),
            fallback_rule_generator=RuleBasedGenerator(),
        )

        assert outcome.generated_case_ids == [], "已成功的 Operation 不该重新生成"
        assert {c.id for c in cases_repo.list_by_run(session, run.id)} == first, (
            "用例集合不该发生变化"
        )


class TestDispatchIdempotency:
    """N4 的幂等：**恢复安全性最关键的一点**（`06` §5.3）。

    为什么这条必须有测试：N4 正常只跑一次，所以它的幂等分支**在常规路径上
    永远不会被走到**——而它存在的意义恰恰是异常路径（编排器在 N4 写完之后、
    checkpoint 落盘之前崩溃，或者锁 TTL 到期让两个推进者并行）。

    **一个只在异常路径上生效的保护，如果没测试，就等于没有**——
    因为它错了不会有人发现，而它对了也不会有人注意到。

    （这条是变异测试逼出来的：把跳过逻辑删掉之后，8 个图测试全绿。）
    """

    def _seed_results(self, session, run, dispatched, statuses: dict[int, str]) -> None:
        """只给 `statuses` 里**点名**的那几条写结果。

        ⚠️ 第一版写的是 `statuses.get(index, "passed")`——那个默认值让**每一条**
        都拿到了结果，于是"其余用例仍应被投递"的断言失败。
        测试 helper 里一个"合理的默认值"就这样把测试要验的场景整个抹掉了。
        """
        for index, status in statuses.items():
            _, case_id = dispatched[index]
            results_repo.upsert_many(
                session,
                [{"run_id": run.id, "case_id": case_id, "status": status}],
            )
        session.flush()

    def _state_for(self, session, run) -> dict:
        from testflow.storage.repositories import cases as cases_repo

        approved = cases_repo.list_by_run(session, run.id, review_status="approved")
        return {
            "run_id": str(run.id),
            "target_operation_ids": [],
            "case_ids": [str(c.id) for c in approved],
            "approved_case_ids": [str(c.id) for c in approved],
            "expected_case_count": len(approved),
            "settled_case_count": 0,
            "node_failures": [],
            "retry_counts": {},
        }

    async def test_already_settled_cases_are_not_dispatched_again(
        self, session, run_fixture, redis_client
    ) -> None:
        """第二次投递时，**已经有结果的用例必须被跳过**。

        少了这一步，恢复会把全部用例再投一遍——幂等性保证结果不会写重，
        但白做一遍工，而且会给目标服务**凭空多打一倍的请求**。
        对一个"测量接口行为"的工具来说，后者不只是浪费：多出来的那些请求
        会出现在对方的监控里，可能触发限流或告警。
        """
        from testflow.graph.nodes import dispatch_cases

        run, _ = run_fixture
        first: list[tuple[uuid.UUID, uuid.UUID]] = []
        await advance(
            run.id,
            graph=_fresh_graph(redis_client, first),
            redis=redis_client,
            session=session,
        )
        assert len(first) >= 2, "前置条件：至少投递两条用例"

        # 其中一条已经有结果了（模拟"它跑完了，然后编排器崩了"）。
        self._seed_results(session, run, first, {0: "passed"})

        second: list[tuple[uuid.UUID, uuid.UUID]] = []
        await dispatch_cases(
            self._state_for(session, run),
            session=session,
            dispatch=lambda r, c: second.append((r, c)),
        )

        already = {first[0][1]}
        assert not (already & {cid for _, cid in second}), (
            "已经有结果的用例被重新投递了——恢复时会凭空多打一轮请求"
        )
        assert second, "其余没有结果的用例仍应被投递"


class TestFallbackPoll:
    """兜底轮询：**回调全部丢失时的唯一保障**（`06` §6.4）。

    这也是 M1 专项检查 (a) 的主体：*"人为丢弃一次回调，图仍能推进"*。

    ## 为什么不用 monkeypatch 模拟"回调丢了"

    更简单的做法是打桩掉回调函数。但那样测的是"我打桩得对不对"，
    而不是"真实世界里回调丢了会怎样"。

    这里用**真实的手段**制造真实的失败：让 Run 停在 `awaiting_results`
    且时间戳足够旧——那正是"回调丢了、没人来唤醒"在数据库里留下的样子。
    然后调用轮询，看它能不能把 Run 救回来。
    """

    async def test_stalled_run_is_picked_up_and_advanced(
        self, session, run_fixture, redis_client, monkeypatch
    ) -> None:
        from sqlalchemy import text

        from testflow.graph import tasks_beat

        run, _ = run_fixture
        dispatched: list[tuple[uuid.UUID, uuid.UUID]] = []

        # ── 推进到 IP-2，然后把回调"丢掉"（不写结果、不唤醒）
        await advance(
            run.id,
            graph=_fresh_graph(redis_client, dispatched),
            redis=redis_client,
            session=session,
        )
        session.expire_all()
        assert runs_repo.get(session, run.id).phase == RunPhase.AWAITING_RESULTS

        # ── 模拟"很久没人来过"：把时间戳拨回去。
        # 用 `now() - interval` 而不是 `touch`——我们要造的是**过去**。
        session.execute(
            text("update runs set updated_at = now() - interval '1 hour' where id = :i"),
            {"i": run.id},
        )
        session.flush()

        # ── 调用轮询。它只负责"发现并投递"，真正的推进由编排器做，
        # 而测试里没有 Celery，所以把"投递"换成直接推进——
        # **这与 `06` §6.4 描述的链路一致**：轮询发现 → 投递 → 编排器推进。
        enqueued: list[str] = []

        class _FakeOrchestrate:
            @staticmethod
            def apply_async(args, queue=None):
                _ = queue
                enqueued.append(args[0])

        monkeypatch.setattr(tasks_beat, "orchestrate", _FakeOrchestrate)
        monkeypatch.setattr(tasks_beat, "_get_context", lambda: _Ctx(session))

        result = tasks_beat.fallback_poll()

        assert result["scanned"] == 1, "应当扫到那个停滞的 Run"
        assert enqueued == [str(run.id)], "应当把推进指令投递出去"

    async def test_fresh_run_is_not_picked_up(self, session, run_fixture, redis_client) -> None:
        """刚推进过的 Run **不该**被扫到。

        少了这条，一个"永远返回全部 Run"的实现也能让上面那条通过——
        而它的后果是：每个正在正常等待的 Run 都被反复推进。
        """
        from testflow.graph import tasks_beat

        run, _ = run_fixture
        await advance(
            run.id,
            graph=_fresh_graph(redis_client, []),
            redis=redis_client,
            session=session,
        )

        _ = tasks_beat  # 见上一条测试的说明
        from testflow.storage.repositories import runs as runs_repo_local

        assert (
            runs_repo_local.list_stalled(
                session, phase=RunPhase.AWAITING_RESULTS, stale_after_s=3600
            )
            == []
        ), "刚更新过的 Run 不该被判为停滞"


class _Ctx:
    """给 `fallback_poll` 用的最小上下文替身。

    它只需要 `.settings` 与 `.sessions()`——**这正是把配置与 session 工厂
    注入而不是让模块去 import 的好处**：替身只要实现两个属性就够了。
    """

    def __init__(self, session) -> None:
        from testflow.config import load_settings

        self.settings = load_settings()
        self._session = session

    def sessions(self):
        """返回 session 本身，不是上下文管理器。

        `_OrchestratorContext.sessions` 是一个 `sessionmaker`——它**可调用**，
        调用结果是 `Session`。替身必须长得一样，否则测试测的是替身的形状，
        而不是被测代码的逻辑。（第一版写成了返回上下文管理器，于是
        `fallback_poll` 里的 `session.close()` 直接炸了。）
        """
        return self._session


class TestDataIsCommittedBeforeDispatch:
    """**投递之前，数据必须已经提交。**

    这是端到端跑起来才发现的 bug：N2 生成用例、N3 批准，但它们和 N4 在
    同一个事务里，而那个事务要到 `advance()` 跑完才提交。N4 一投递，
    Worker（**另一个进程**）就会按 `case_id` 去查库——却查不到。

    症状是一部分任务 `ValueError: 用例不存在`，而**整个集成测试套件全绿**：
    那边投递和读取用的是同一个 session，未提交的数据对它自己是可见的。

    ## ⚠️ 为什么这条测试**不能**用回滚夹具

    本套件其余测试都跑在一个最后回滚的事务里。而这里要验的恰恰是
    "数据对**另一个连接**可见"——在回滚夹具下，`commit()` 只释放 savepoint，
    外层事务始终没提交，**别的连接永远看不到**。

    所以这条测试自建数据、**真实提交**、结束时自己清理。
    形状和其他测试不一样，因为要验的东西本来就不一样。
    """

    async def test_dispatched_cases_are_visible_on_another_connection(
        self, engine, make_settings
    ) -> None:
        import uuid as _uuid

        from sqlalchemy import text
        from sqlalchemy.orm import Session as SyncSession

        from testflow.agents.test_case_gen import RuleBasedGenerator, generate_for_operations
        from testflow.graph.nodes import dispatch_cases
        from testflow.storage.models import Operation, Run, Spec
        from testflow.storage.repositories import cases as cases_repo

        # ── 自建数据，并**真实提交**（见类文档字符串）
        with SyncSession(bind=engine) as setup:
            spec = Spec(
                fingerprint=_uuid.uuid4().hex + "0" * 32,
                openapi_version="3.0.3",
                raw_content="{}",
                byte_size=2,
                declared_targets=["http://127.0.0.1:9000"],
                operation_count=1,
            )
            setup.add(spec)
            setup.flush()
            operation = Operation(
                spec_id=spec.id, method="GET", path="/ok", responses_schema={"200": {}}
            )
            setup.add(operation)
            setup.flush()
            run = Run(spec_id=spec.id, spec_fingerprint=spec.fingerprint)
            setup.add(run)
            setup.flush()
            # **先取出要用的 ID**，再让 session 关闭——ORM 对象在 session
            # 关闭后是 detached 的，访问任何属性都可能触发一次刷新。
            run_id, spec_id, operation_id = run.id, spec.id, operation.id
            setup.commit()

        try:
            # ★★ **N2 / N3 / N4 跑在同一个 session 里，中间不提交。**
            #
            # 这才还原了真实的节点顺序：`advance()` 从头到尾用一个 session，
            # 而它要到图跑完才提交。所以"投递时数据可见不可见"完全取决于
            # **N4 自己有没有先提交**——那正是这条测试要验的东西。
            #
            # 第一版把 N2/N3 放在一个 session 里、提交后再调 N4——
            # 那样数据早就可见了，节点里的那句 `commit()` 变成无关紧要，
            # 于是**把 commit 删掉测试照样通过**（变异测试当场抓到）。
            with SyncSession(bind=engine) as work:
                await generate_for_operations(
                    work,
                    run_id=run_id,
                    operation_ids=[operation_id],
                    generator=RuleBasedGenerator(),
                    fallback_rule_generator=RuleBasedGenerator(),
                )
                cases_repo.approve_all_pending(work, run_id)
                approved = cases_repo.list_by_run(work, run_id, review_status="approved")
                assert approved, "前置条件：应当生成并批准了用例"
                approved_ids = [str(c.id) for c in approved]

                # 此时数据**尚未提交**——与真实链路里 N4 面临的状态一致。
                seen: list[bool] = []

                def _dispatch(_run_id, case_id) -> None:
                    with SyncSession(bind=engine.connect()) as other:
                        seen.append(cases_repo.get(other, case_id) is not None)

                await dispatch_cases(
                    {
                        "run_id": str(run_id),
                        "target_operation_ids": [],
                        "case_ids": approved_ids,
                        "approved_case_ids": approved_ids,
                        "expected_case_count": len(approved_ids),
                        "settled_case_count": 0,
                        "node_failures": [],
                        "retry_counts": {},
                    },
                    session=work,
                    dispatch=_dispatch,
                )

            assert seen, "前置条件：应当投递了至少一条用例"
            assert all(seen), (
                "投递时数据对另一个连接不可见——Worker（另一个进程）会读不到用例，"
                "症状是随机的 `ValueError: 用例不存在`"
            )
        finally:
            # 自己清理：这条测试用的是真实提交，不清理会留下垃圾数据影响后续运行。
            with SyncSession(bind=engine) as cleanup:
                cleanup.execute(text("delete from runs where id = :i"), {"i": run_id})
                cleanup.execute(text("delete from specs where id = :i"), {"i": spec_id})
                cleanup.commit()
