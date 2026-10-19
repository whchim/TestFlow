"""数据清理：保留期、级联、孤儿判定、审计留存。

对应决策：`04-数据模型.md` §5、`10-部署运维.md` §7（M5）。

## 这些测试必须在真库上跑

清理的正确性主要由**外键的 `ON DELETE` 行为**决定：CASCADE 删子表、
SET NULL 保留审计。这些是数据库的语义，不是 Python 的——用 mock 测，
测到的是"我们以为外键会这么做"。
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from testflow.storage.models import AuditLog, Case, CaseResult, LLMUsage, Run, Spec
from testflow.storage.repositories import runs as runs_repo
from testflow.storage.repositories import specs as specs_repo

pytestmark = pytest.mark.integration

_NOW = datetime.datetime.now(datetime.UTC)
_OLD = _NOW - datetime.timedelta(days=100)
_CUTOFF = _NOW - datetime.timedelta(days=90)


def _count(session: Session, model, *conditions) -> int:
    """按条件计数。**不数全表**：本地库里可能还有上一次 demo 的数据。"""
    statement = select(func.count()).select_from(model)
    for condition in conditions:
        statement = statement.where(condition)
    return int(session.scalar(statement) or 0)


def _audit(session: Session, run_id: uuid.UUID) -> AuditLog:
    row = AuditLog(
        run_id=run_id,
        direction="target",
        target_host="127.0.0.1",
        decision="allow",
    )
    session.add(row)
    session.flush()
    return row


def test_only_runs_older_than_the_cutoff_are_deleted(session: Session, make_spec, make_run) -> None:
    spec = make_spec()
    old_run = make_run(spec, created_at=_OLD)
    new_run = make_run(spec, created_at=_NOW)
    old_id, new_id = old_run.id, new_run.id

    assert runs_repo.delete_older_than(session, cutoff=_CUTOFF) == 1
    # 必须 expunge 而不是 expire：被删掉的对象在 expand 后再访问属性会
    # 触发一次刷新，而那一行已经不存在——于是 `session.get` 自己抛出
    # ObjectDeletedError，测试失败在断言之前。
    session.expunge_all()

    assert session.get(Run, old_id) is None
    assert session.get(Run, new_id) is not None


def test_deleting_a_run_cascades_to_cases_results_and_usage(
    session: Session, make_spec, make_run, make_operation, make_case
) -> None:
    spec = make_spec()
    run = make_run(spec, created_at=_OLD)
    run_id = run.id
    operation = make_operation(spec)
    case = make_case(run, operation)
    session.add(CaseResult(run_id=run_id, case_id=case.id, status="passed"))
    session.add(
        LLMUsage(
            run_id=run_id,
            purpose="case_generation",
            model="qwen3.7-flash",
            prompt_tokens=10,
            completion_tokens=5,
            cached_tokens=0,
            cache_write_tokens=0,
            total_tokens=15,
            attempt=1,
            elapsed_ms=100,
        )
    )
    session.flush()

    runs_repo.delete_older_than(session, cutoff=_CUTOFF)
    session.expunge_all()

    assert _count(session, Case, Case.run_id == run_id) == 0
    assert _count(session, CaseResult, CaseResult.run_id == run_id) == 0
    assert _count(session, LLMUsage, LLMUsage.run_id == run_id) == 0


def test_audit_log_survives_run_deletion_without_the_run_reference(
    session: Session, make_spec, make_run
) -> None:
    """`SET NULL` 而不是 CASCADE：审计要回答"曾经发生过什么"。

    这也是 M5 **不**自动删除 `audit_logs` 的原因——它是唯一的追溯凭证。
    """
    spec = make_spec()
    run = make_run(spec, created_at=_OLD)
    audit = _audit(session, run.id)
    audit_id = audit.id

    runs_repo.delete_older_than(session, cutoff=_CUTOFF)
    session.expunge_all()

    row = session.get(AuditLog, audit_id)
    assert row is not None
    assert row.run_id is None
    assert row.target_host == "127.0.0.1"


def test_orphan_specs_are_deleted_but_referenced_and_recent_ones_are_kept(
    session: Session, make_spec, make_run
) -> None:
    # 过期且没有 Run → 孤儿，应删
    orphan = make_spec(created_at=_OLD)

    # 过期但有 Run → 不是孤儿，保留（Run 还没到期）
    referenced = make_spec(created_at=_OLD)
    make_run(referenced, created_at=_NOW)

    # 没有 Run 但还没到期 → 保留
    recent = make_spec(created_at=_NOW)
    orphan_id, referenced_id, recent_id = orphan.id, referenced.id, recent.id

    assert specs_repo.delete_orphans_older_than(session, cutoff=_CUTOFF) == 1
    session.expunge_all()

    assert session.get(Spec, orphan_id) is None
    assert session.get(Spec, referenced_id) is not None
    assert session.get(Spec, recent_id) is not None


def test_spec_becomes_an_orphan_after_its_last_run_is_deleted(
    session: Session, make_spec, make_run
) -> None:
    """清理顺序必须是先 Run 后 Spec。同一事务里也应立即生效。"""
    spec = make_spec(created_at=_OLD)
    spec_id = spec.id
    make_run(spec, created_at=_OLD)

    runs_repo.delete_older_than(session, cutoff=_CUTOFF)
    deleted = specs_repo.delete_orphans_older_than(session, cutoff=_CUTOFF)
    session.expunge_all()

    assert deleted == 1
    assert session.get(Spec, spec_id) is None


def test_cleanup_is_idempotent(session: Session, make_spec, make_run) -> None:
    """重复触发不应报错，也不应再删到东西（`10` §7）。"""
    spec = make_spec(created_at=_OLD)
    make_run(spec, created_at=_OLD)

    assert runs_repo.delete_older_than(session, cutoff=_CUTOFF) == 1
    assert specs_repo.delete_orphans_older_than(session, cutoff=_CUTOFF) == 1

    assert runs_repo.delete_older_than(session, cutoff=_CUTOFF) == 0
    assert specs_repo.delete_orphans_older_than(session, cutoff=_CUTOFF) == 0


def test_batching_deletes_everything_across_batches(session: Session, make_spec, make_run) -> None:
    """一批 500 的小批量循环不能漏掉第 501 条。"""
    spec = make_spec()
    for _ in range(5):
        make_run(spec, created_at=_OLD)

    assert runs_repo.delete_older_than(session, cutoff=_CUTOFF, batch_size=2) == 5


# ------------------------------------------------------------ 定时任务本体


class _Ctx:
    """`cleanup_expired_data` 只需要 `.settings` 与 `.sessions()`。"""

    def __init__(self, session: Session, settings) -> None:
        self.settings = settings
        self._session = session

    def sessions(self):
        return self._session


def test_cleanup_task_deletes_old_runs_and_their_orphan_specs(
    session: Session, make_spec, make_run, make_settings, monkeypatch
) -> None:
    """通过任务入口跑一遍：**先 Run 后 Spec** 的顺序在同一事务里生效。"""
    from testflow.graph import tasks_beat

    old_spec = make_spec(created_at=_OLD)
    make_run(old_spec, created_at=_OLD)
    new_spec = make_spec(created_at=_NOW)
    make_run(new_spec, created_at=_NOW)

    # 任务会 commit 并 close session。给它一份独立的 session（同一连接/事务），
    # 验证用原来的 session——与 `abandon_stale_reviews` 的测试同一理由。
    task_session = Session(bind=session.get_bind(), join_transaction_mode="create_savepoint")
    settings = make_settings(run_retention_days=90)
    monkeypatch.setattr(tasks_beat, "_get_context", lambda: _Ctx(task_session, settings))

    result = tasks_beat.cleanup_expired_data()

    assert result == {"runs_deleted": 1, "specs_deleted": 1}


def test_cleanup_task_second_run_deletes_nothing(
    session: Session, make_spec, make_run, make_settings, monkeypatch
) -> None:
    """幂等：重复触发（进程重启后的常见情形）不应再删东西。"""
    from testflow.graph import tasks_beat

    spec = make_spec(created_at=_OLD)
    make_run(spec, created_at=_OLD)

    settings = make_settings(run_retention_days=90)

    def _run() -> dict:
        task_session = Session(bind=session.get_bind(), join_transaction_mode="create_savepoint")
        monkeypatch.setattr(tasks_beat, "_get_context", lambda: _Ctx(task_session, settings))
        return tasks_beat.cleanup_expired_data()

    assert _run() == {"runs_deleted": 1, "specs_deleted": 1}
    assert _run() == {"runs_deleted": 0, "specs_deleted": 0}
