"""仓储层的集成测试。**需要真实 PostgreSQL。**

对应决策：`04-数据模型.md` DM1（幂等）、DM4（N2 跳过语义）、DM5（派生缓存）、
`06-Agent编排.md` §6.2（完成判定）、§6.4（兜底轮询）。

**为什么这组必须是集成测试**

它测的三件事，用 mock 或 SQLite 都测不出真问题：

1. **`ON CONFLICT` 的真实行为** —— 冲突处理是数据库的语义，不是 ORM 的
2. **`uuidv7()` 服务端默认值** —— PG 18 的原生函数，别处没有
3. **`updated_at` 是否真的被更新** —— 它没有 `onupdate`，靠的是代码显式写；
   而"忘了写"在单元测试里表现为"值没变"，在集成测试里同样——但只有在真库上
   才谈得上"值是什么"

第 3 条是 S1 的核心验收项：**兜底轮询依赖 `updated_at` 判停滞，
它错了不会有任何报错，只会让轮询在一个正常运行的 Run 上强行推进。**
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from testflow.domain.graph import RunPhase
from testflow.storage.repositories import (
    cases as cases_repo,
)
from testflow.storage.repositories import (
    results as results_repo,
)
from testflow.storage.repositories import (
    run_operations as ro_repo,
)
from testflow.storage.repositories import (
    runs as runs_repo,
)
from testflow.storage.repositories import (
    specs as specs_repo,
)

pytestmark = pytest.mark.integration


class TestSetPhaseTouchesUpdatedAt:
    """`updated_at` 是兜底轮询的判断依据，必须每次改 phase 都动。

    ## ⚠️ 为什么这里的测试要"先拨回去"再断言

    **PostgreSQL 的 `now()` 返回的是事务开始时刻，在一个事务里是常量。**
    实测确认：同一事务内隔 0.3 秒连查两次 `now()`，值完全相同
    （`clock_timestamp()` 才会变）。

    而本套件的每个测试都跑在一个回滚事务里。所以"改之前取一次、改之后取一次、
    比较大小"这种写法**永远看不到差异**——它测不出任何东西。

    更糟的是，第一版写的是 `after >= before`，那个断言即使把 `updated_at`
    从更新语句里整个删掉也照样通过。**变异测试当场抓住了这一点**
    （删掉 `updated_at` 后 27 个测试仍然全绿）。

    正确做法：**先把时间戳拨回到过去**，制造一个真实的差异，
    再用**兜底轮询本身**（`list_stalled`）来断言——即验证"改完之后，
    轮询不再把它当成停滞"。这既绕开了 `now()` 的事务语义，
    又测的是真正要保证的行为，而不是一个实现细节。
    """

    @staticmethod
    def _backdate(session: Session, run_id: uuid.UUID, minutes: int = 10) -> None:
        session.execute(
            text("update runs set updated_at = now() - make_interval(mins => :m) where id = :i"),
            {"m": minutes, "i": run_id},
        )
        session.flush()

    def test_set_phase_advances_the_timestamp(self, session, make_spec, make_run) -> None:
        """同阶段再写一次，隔离出"时间戳动了"这个效果（phase 没变，所以只有它起作用）。"""
        run = make_run(make_spec(), phase=RunPhase.AWAITING_RESULTS)
        self._backdate(session, run.id)

        stalled_before = runs_repo.list_stalled(
            session, phase=RunPhase.AWAITING_RESULTS, stale_after_s=60
        )
        assert run.id in {r.id for r in stalled_before}, "前置条件：它现在应当被判为停滞"

        runs_repo.set_phase(session, run.id, RunPhase.AWAITING_RESULTS)

        stalled_after = runs_repo.list_stalled(
            session, phase=RunPhase.AWAITING_RESULTS, stale_after_s=60
        )
        assert run.id not in {r.id for r in stalled_after}, (
            "改 phase 必须同时推进 updated_at，否则轮询会一直认为它停滞"
        )

    def test_touch_advances_the_timestamp_without_changing_phase(
        self, session, make_spec, make_run
    ) -> None:
        """`touch` 用于"Run 在动、但阶段没变"——例如正在投递 2000 条用例。

        不更新的话，一个正常工作的 Run 会被轮询误判为停滞并强行推进。
        这里 phase 全程不变，因此唯一能改变轮询结论的就是时间戳。
        """
        run = make_run(make_spec(), phase=RunPhase.DISPATCHING)
        self._backdate(session, run.id)
        assert runs_repo.list_stalled(session, phase=RunPhase.DISPATCHING, stale_after_s=60)

        runs_repo.touch(session, run.id)

        assert not runs_repo.list_stalled(session, phase=RunPhase.DISPATCHING, stale_after_s=60)
        session.expire_all()
        assert runs_repo.get(session, run.id).phase == RunPhase.DISPATCHING

    def test_terminal_phase_sets_finished_at(self, session, make_spec, make_run) -> None:
        run = make_run(make_spec())
        assert run.finished_at is None

        runs_repo.set_phase(session, run.id, RunPhase.DONE)
        session.expire_all()
        assert runs_repo.get(session, run.id).finished_at is not None

    def test_leaving_terminal_clears_finished_at(self, session, make_spec, make_run) -> None:
        """回到运行中时必须清掉结束时刻，否则报告里的耗时算错。"""
        run = make_run(make_spec())
        runs_repo.set_phase(session, run.id, RunPhase.DONE)
        runs_repo.set_phase(session, run.id, RunPhase.REPORTING)
        session.expire_all()
        assert runs_repo.get(session, run.id).finished_at is None

    def test_reading_back_after_write_is_not_stale(self, session, make_spec, make_run) -> None:
        """**这条挡的是"改完立刻读拿到旧值"。**

        批量 `UPDATE` 默认不同步 ORM 的身份映射。少了 `synchronize_session`，
        `set_phase` 之后 `session.get()` 会从缓存里返回**改之前**的对象——
        不报错，只是读到幻觉。
        """
        run = make_run(make_spec())
        session.get(type(run), run.id)  # 先把它装进身份映射

        runs_repo.set_phase(session, run.id, RunPhase.GENERATING)

        assert runs_repo.get(session, run.id).phase == RunPhase.GENERATING


class TestListStalled:
    """兜底轮询的扫描入口。"""

    def test_finds_only_the_stale_run_in_the_right_phase(
        self, session, make_spec, make_run
    ) -> None:
        spec = make_spec()
        stale = make_run(spec, phase=RunPhase.AWAITING_RESULTS)
        fresh = make_run(spec, phase=RunPhase.AWAITING_RESULTS)
        other_phase = make_run(spec, phase=RunPhase.ANALYZING)

        # 把 stale 的时间戳拨回去——直接改库是刻意的：`touch()` 只能往前推，
        # 而这里要造的是"很久没动过"这个状态。
        session.execute(
            text("update runs set updated_at = now() - interval '10 minutes' where id = :i"),
            {"i": stale.id},
        )
        session.flush()

        found = runs_repo.list_stalled(session, phase=RunPhase.AWAITING_RESULTS, stale_after_s=60)
        ids = {r.id for r in found}

        assert stale.id in ids
        assert fresh.id not in ids, "刚更新过的 Run 不该被判为停滞"
        assert other_phase.id not in ids, "阶段不匹配的不该被扫到"


class TestResultsUpsert:
    """DM1：幂等由**数据库唯一约束**保证，不是应用层的先查后写。"""

    def _row(self, run, case, **kw: object) -> dict:
        base = {
            "run_id": run.id,
            "case_id": case.id,
            "status": "passed",
            "assertion_outcomes": [],
        }
        base.update(kw)
        return base

    def test_same_key_twice_produces_one_row(
        self, session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """重投的任务会写入同一个 `(run_id, case_id)`——必须只有一行。"""
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        case = make_case(run, op)

        results_repo.upsert_many(session, [self._row(run, case, status="failed")])
        results_repo.upsert_many(session, [self._row(run, case, status="passed")])

        assert results_repo.count_results(session, run.id) == 1

    def test_last_write_wins(self, session, make_spec, make_operation, make_run, make_case) -> None:
        """用 `DO UPDATE` 而非 `DO NOTHING`：第一次可能写的是残缺结果。"""
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        case = make_case(run, op)

        results_repo.upsert_many(session, [self._row(run, case, status="timeout")])
        results_repo.upsert_many(session, [self._row(run, case, status="passed", http_status=200)])

        stored = results_repo.get(session, run.id, case.id)
        assert stored.status == "passed"
        assert stored.http_status == 200

    def test_batch_upsert(self, session, make_spec, make_operation, make_run, make_case) -> None:
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        created = [make_case(run, op, name=f"case-{i}") for i in range(5)]

        written = results_repo.upsert_many(
            session, [self._row(run, c, status="passed") for c in created]
        )

        assert written == 5
        assert results_repo.count_results(session, run.id) == 5

    def test_empty_input_is_a_noop(self, session) -> None:
        """空列表不该构造出一条 `VALUES ()` 的非法 SQL。"""
        assert results_repo.upsert_many(session, []) == 0

    def test_error_kind_survives(
        self, session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """`guardrail_denied` 这类 `error_kind` 要能真的落库。

        它是 M3 归因的依据——被策略拦下的请求应当判成 `blocked_by_policy`
        而不是"服务端错误"，而区分它们的唯一信息就是这个字段。
        """
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        case = make_case(run, op)

        results_repo.upsert_many(
            session,
            [self._row(run, case, status="error", error_kind="guardrail_denied")],
        )

        assert results_repo.get(session, run.id, case.id).error_kind == "guardrail_denied"


class TestMarkUnfinishedAsTimeout:
    """`06` §6.4：回调可能全部丢失，系统不能无限等下去。"""

    def test_unfinished_cases_become_timeout(
        self, session, make_spec, make_operation, make_run, make_case
    ) -> None:
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        done = make_case(run, op, name="已执行")
        missing = make_case(run, op, name="回调丢了")

        results_repo.upsert_many(
            session, [{"run_id": run.id, "case_id": done.id, "status": "passed"}]
        )

        filled = results_repo.mark_unfinished_as_timeout(session, run.id)

        assert filled == 1
        assert results_repo.get(session, run.id, missing.id).status == "timeout"
        assert results_repo.get(session, run.id, done.id).status == "passed", "已有的结果不该被覆盖"

    def test_is_idempotent(self, session, make_spec, make_operation, make_run, make_case) -> None:
        """兜底轮询会被反复触发（锁没抢到、扫描重叠），必须可重复执行。"""
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        make_case(run, op)

        assert results_repo.mark_unfinished_as_timeout(session, run.id) == 1
        assert results_repo.mark_unfinished_as_timeout(session, run.id) == 0

    def test_nothing_missing_returns_zero(
        self, session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """0 是"无需补记"的信号，调用方据此知道不该推进。"""
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        case = make_case(run, op)
        results_repo.upsert_many(
            session, [{"run_id": run.id, "case_id": case.id, "status": "passed"}]
        )

        assert results_repo.mark_unfinished_as_timeout(session, run.id) == 0


class TestSpecFingerprintDedupe:
    """SP 幂等性的落点：重复上传同一份文档不重复解析。"""

    def test_same_fingerprint_is_found(self, session, make_spec) -> None:
        spec = make_spec(fingerprint="a" * 64)
        found = specs_repo.get_by_fingerprint(session, "a" * 64)
        assert found is not None and found.id == spec.id

    def test_unknown_fingerprint_is_none(self, session) -> None:
        assert specs_repo.get_by_fingerprint(session, "b" * 64) is None

    def test_duplicate_fingerprint_is_rejected_by_the_database(self, session, make_spec) -> None:
        """`UNIQUE` 约束是真的——否则去重会静默失效，产生两份内容相同的 Spec。"""
        from sqlalchemy.exc import IntegrityError

        make_spec(fingerprint="c" * 64)
        with pytest.raises(IntegrityError):
            make_spec(fingerprint="c" * 64)


class TestRunOperationIdempotency:
    """DM4：**N2 的幂等是"已完成则跳过"，不是"重放"。**

    对比 N1：那是真幂等（同输入必得同输出，重跑无害）。
    两个节点的语义不同，这里是本项目最容易搞混的一点。
    """

    def test_succeeded_is_not_regenerated(
        self, session, make_spec, make_operation, make_run
    ) -> None:
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        ro_repo.mark_succeeded(session, run.id, op.id, case_count=3)

        # LLM 是非确定性的：重跑会得到**不同**的用例，静默替换掉用户已确认的结果。
        assert ro_repo.pending_operation_ids(session, run.id, [op.id]) == []

    def test_failed_is_retried(self, session, make_spec, make_operation, make_run) -> None:
        """失败通常是瞬时的（限流、网络），重跑一次是合理的。"""
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        ro_repo.mark_failed(session, run.id, op.id, error="限流")

        assert ro_repo.pending_operation_ids(session, run.id, [op.id]) == [op.id]

    def test_no_record_means_pending(self, session, make_spec, make_operation, make_run) -> None:
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        assert ro_repo.pending_operation_ids(session, run.id, [op.id]) == [op.id]

    def test_retry_clears_the_previous_error(
        self, session, make_spec, make_operation, make_run
    ) -> None:
        """成功之后不该留着上一次的错误信息——报告会显示一个已解决的错误。"""
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        ro_repo.mark_failed(session, run.id, op.id, error="限流")
        ro_repo.mark_succeeded(session, run.id, op.id, case_count=1)

        assert ro_repo.list_by_run(session, run.id)[0].error is None


class TestCaseReview:
    """M1 没有 IP-1，但 `review_status` 仍要写对——N3 的路由依据是它。"""

    def test_approve_all_pending(
        self, session, make_spec, make_operation, make_run, make_case
    ) -> None:
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        for i in range(3):
            make_case(run, op, name=f"c{i}")

        assert cases_repo.approve_all_pending(session, run.id) == 3
        assert cases_repo.list_by_run(session, run.id, review_status="approved")

    def test_rejected_cases_are_left_alone(
        self, session, make_spec, make_operation, make_run, make_case
    ) -> None:
        """只批准 `pending`。若把已拒绝的也一并批准，用户的拒绝就白做了。"""
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        rejected = make_case(run, op, name="被拒绝的", review_status="rejected")
        make_case(run, op, name="待审的")

        assert cases_repo.approve_all_pending(session, run.id) == 1
        session.expire_all()
        assert cases_repo.get(session, rejected.id).review_status == "rejected"

    def test_approve_is_idempotent(
        self, session, make_spec, make_operation, make_run, make_case
    ) -> None:
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        make_case(run, op)
        cases_repo.approve_all_pending(session, run.id)
        assert cases_repo.approve_all_pending(session, run.id) == 0


class TestRunForeignKeyCascade:
    """删 Run 时子行应当一起走——否则会留下指向不存在 Run 的孤儿行。"""

    def test_cases_are_deleted_with_the_run(
        self, session, make_spec, make_operation, make_run, make_case
    ) -> None:
        spec = make_spec()
        op = make_operation(spec)
        run = make_run(spec)
        make_case(run, op)
        run_id = run.id

        session.execute(text("delete from runs where id = :i"), {"i": run_id})
        session.flush()

        assert cases_repo.count_by_run(session, run_id) == 0


class TestUuidv7Default:
    def test_ids_are_generated_by_the_database(self, session, make_spec) -> None:
        """`uuidv7()` 是 PG 18 的原生函数，时间有序且无应用侧依赖（DM8）。"""
        spec = make_spec()
        assert isinstance(spec.id, uuid.UUID)

    def test_ids_are_time_ordered(self, session, make_spec) -> None:
        """**这是选 v7 而非 v4 的全部理由**：随机 UUID 会让 B-tree 插入到处乱跳。"""
        ids = [make_spec().id for _ in range(5)]
        assert ids == sorted(ids), "UUIDv7 应当单调递增"
