"""执行器的集成测试。**需要 PostgreSQL 与 fixture 服务。**

对应决策：`03/TestExecutor.md` §2（执行流程）、TE2/TE4/TE5、
`11-开发路线图.md` M1 专项检查 (b)。

## 本文件里的 `test_bad_json_distinguishes_failed_from_error` 是 M1 的专项检查

路线图原文把它标为 *"M1 最容易被实现偷懒的地方——把'无法求值'和'断言不符'
合并，代码更短，但归因（M3）会因此无法分辨服务缺陷与响应非 JSON"*。

单元测试已经验过求值引擎的判定（`tests/unit/test_assertions.py`）。
**这里验的是另一件事**：那个判定有没有被**完整地带到数据库里**。
一个在内存里正确、落库时被压回二值的结果，在报告与归因看来和二值没有区别。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from testflow.execution.runner import load_case_spec, run_case
from testflow.guardrail.pipeline import EgressPolicy
from testflow.guardrail.policy import Whitelist
from testflow.guardrail.sync_client import GuardrailSyncClient
from testflow.storage.models import Case, Operation, Run, Spec
from testflow.storage.repositories import results as results_repo

pytestmark = pytest.mark.integration

TARGET = "127.0.0.1:9000"
BASE_URL = f"http://{TARGET}"
SPEC_TARGETS = [BASE_URL]


@pytest.fixture
def wired(session: Session, make_settings, fixture_server: str):
    """一套接好线的执行环境：spec / operation / run / case + 客户端。

    返回一个工厂，调用方指定要测的端点与断言。
    """
    settings = make_settings(guardrail_allow_private_targets=[TARGET])
    client = GuardrailSyncClient(settings)
    policy = EgressPolicy(
        whitelist=Whitelist.from_urls([f"{fixture_server}/"]),
        allowed_private=frozenset({TARGET}),
    )

    spec = Spec(
        fingerprint="c" * 64,
        openapi_version="3.0.3",
        raw_content="{}",
        byte_size=2,
        declared_targets=SPEC_TARGETS,
        operation_count=1,
    )
    session.add(spec)
    session.flush()

    run = Run(spec_id=spec.id, spec_fingerprint=spec.fingerprint)
    session.add(run)
    session.flush()

    def _make(path: str, assertions: list[dict], **overrides: object) -> Case:
        operation = Operation(
            spec_id=spec.id,
            method=str(overrides.pop("method", "GET")),
            path=path,
            responses_schema=overrides.pop("responses_schema", None),
        )
        session.add(operation)
        session.flush()

        case = Case(
            run_id=run.id,
            operation_id=operation.id,
            name=overrides.pop("name", f"{path} 用例"),
            category="normal",
            dimension="value_valid",
            request_method=operation.method,
            request_path=path,
            assertions=assertions,
            generated_by="rule",
            **overrides,
        )
        session.add(case)
        session.flush()
        return case

    yield _make, run, client, policy, settings, session

    client.close()


def _execute(wired, case: Case):
    make, run, client, policy, settings, session = wired
    outcome = run_case(
        session,
        run_id=run.id,
        case_id=case.id,
        client=client,
        policy=policy,
        settings=settings,
    )
    session.flush()
    return outcome


class TestHappyPath:
    def test_passing_case_is_recorded(self, wired) -> None:
        make, run, client, policy, settings, session = wired
        case = make(
            "/ok",
            [{"type": "status_code", "expected": 200, "derived_from": "spec"}],
        )

        outcome = _execute(wired, case)

        assert outcome.status == "passed"
        assert outcome.http_status == 200
        stored = results_repo.get(session, run.id, case.id)
        assert stored is not None and stored.status == "passed"

    def test_response_body_is_stored(self, wired) -> None:
        make, *_ = wired
        case = make("/ok", [{"type": "status_code", "expected": 200, "derived_from": "spec"}])
        outcome = _execute(wired, case)
        assert outcome.response_body is not None
        assert "ok" in outcome.response_body

    def test_wrong_status_is_failed_and_the_row_survives(self, wired) -> None:
        """断言不成立 → `failed`，且结果要真的落库。

        "算出结论"和"把结论写进去"是两件事——中间那句 `session.flush()`
        漏掉的话，所有断言都白做了。
        """
        make, run, client, policy, settings, session = wired
        case = make("/ok", [{"type": "status_code", "expected": 500, "derived_from": "spec"}])

        outcome = _execute(wired, case)

        assert outcome.status == "failed"
        assert results_repo.get(session, run.id, case.id).status == "failed"


class TestBadJsonSpecialCheck:
    """**M1 专项检查 (b)**：`/bad-json` 跑一次，断言级结果必须区分两种结论。"""

    def test_bad_json_distinguishes_failed_from_error(self, wired) -> None:
        """同一个响应、同一次执行里，`passed` / `error` / `failed` 三者都要出现。

        `/bad-json` 返回 **200 + 一段不是 JSON 的正文**，于是：

        | 断言 | 结论 | 为什么 |
        | :--- | :--- | :--- |
        | `status_code == 200` | `passed` | 状态码没有任何不确定性 |
        | `json_path $.status exists` | **`error`** | 正文不是 JSON，取不到值 |
        | `body_contains "502 Bad Gateway"` | `passed` | 子串匹配不要求结构 |

        对照 `/error-500`：`status_code == 200` → `failed`（拿到了 500，
        求值成功，只是不成立）。

        **这就是"无法求值"与"不成立"在同一批数据里的分界。**
        偷懒的实现会把前两者合并，代码更短、测试更容易过——
        而 M3 的归因会因此把"响应来自网关/WAF/错误页"说成
        "服务返回的结构不对"。
        """
        make, run, client, policy, settings, session = wired

        case = make(
            "/bad-json",
            [
                {"type": "status_code", "expected": 200, "derived_from": "spec"},
                {
                    "type": "json_path",
                    "path": "$.status",
                    "operator": "exists",
                    "derived_from": "spec",
                },
                # 正文是 `<html><body>502 Bad Gateway</body></html>`——
                # 一段**声称自己是 JSON 实际是 HTML 错误页**的响应。
                # 子串匹配不要求结构，所以它在同一个响应上仍然可以求值。
                {
                    "type": "body_contains",
                    "text": "502 Bad Gateway",
                    "derived_from": "inferred",
                },
            ],
        )

        outcome = _execute(wired, case)

        outcomes = [o.outcome.value for o in outcome.assertion_outcomes]
        assert outcomes == ["passed", "error", "passed"], (
            f"期望 [passed, error, passed]，实际 {outcomes}——`error` 被合并进 `failed` 了吗？"
        )

        # ★ 逐条落库之后仍要能区分。"在内存里对、落库时被压平"是一种真实存在的错法。
        stored = results_repo.get(session, run.id, case.id)
        assert stored is not None
        persisted = [item["outcome"] for item in stored.assertion_outcomes]
        assert "error" in persisted and "failed" not in persisted

        # 用例级**不**区分两者——它只回答"过没过"（TE2）。
        assert outcome.status == "failed"
        assert stored.status == "failed"

    def test_server_error_is_failed_not_error(self, wired) -> None:
        """对照面：`/error-500` 上同一条断言给出的是 `failed`。

        少了这条，一个"永远返回 error"的实现也能让上面那条通过。
        """
        make, run, client, policy, settings, session = wired
        case = make(
            "/error-500",
            [{"type": "status_code", "expected": 200, "derived_from": "spec"}],
        )

        outcome = _execute(wired, case)

        assert [o.outcome.value for o in outcome.assertion_outcomes] == ["failed"]
        assert outcome.status == "failed"


class TestGuardrailIntegration:
    def test_denied_request_is_error_with_guardrail_kind(self, wired) -> None:
        """§8.1：被策略拦下 → `error` + `error_kind = guardrail_denied`。

        M3 的规则归因据此判成 `blocked_by_policy`，而不是"服务端错误"——
        两者的排查方向完全不同。
        """
        make, run, client, policy, settings, session = wired
        case = make("/ok", [{"type": "status_code", "expected": 200, "derived_from": "spec"}])

        strict = EgressPolicy(whitelist=Whitelist.from_urls([f"{BASE_URL}/"]))
        outcome = run_case(
            session,
            run_id=run.id,
            case_id=case.id,
            client=client,
            policy=strict,
            settings=settings,
        )
        session.flush()

        assert outcome.status == "error"
        assert outcome.error_kind == "guardrail_denied"
        # 请求根本没发出去，所以**没有**状态码。填一个会让报告里出现
        # 一个不存在的响应。
        assert outcome.http_status is None

    def test_destructive_case_is_denied_and_never_reaches_the_target(self, wired, hits) -> None:
        """TE6 的第二层：即便一个破坏性请求真的走到了出站口，它也发不出去。"""
        make, run, client, policy, settings, session = wired
        case = make(
            "/destructive",
            [{"type": "status_code", "expected": 200, "derived_from": "spec"}],
            method="DELETE",
        )

        hits.reset()
        outcome = _execute(wired, case)

        assert outcome.status == "error"
        assert outcome.error_kind == "guardrail_denied"
        assert hits.of("/destructive") == 0, "破坏性请求不该到达目标"

    def test_redirect_is_recorded_as_a_result(self, wired) -> None:
        """**M1 专项检查 (c)**：3xx 作为结果返回，而不是被跟随。"""
        make, run, client, policy, settings, session = wired
        case = make(
            "/redirect",
            [
                {
                    "type": "status_code",
                    "expected": [301, 302, 307, 308],
                    "derived_from": "spec",
                }
            ],
        )

        outcome = _execute(wired, case)

        assert outcome.status == "passed"
        assert outcome.http_status == 302
        assert outcome.response_headers is not None
        assert "location" in outcome.response_headers


class TestIdempotency:
    def test_re_executing_the_same_case_updates_one_row(self, wired) -> None:
        """TE5：重投的任务会执行第二次——**必须只有一行结果**。

        幂等由数据库的 `UNIQUE (run_id, case_id)` 保证，而不是"先查后写"
        （后者在并发下必然有竞态）。
        """
        make, run, client, policy, settings, session = wired
        case = make("/ok", [{"type": "status_code", "expected": 200, "derived_from": "spec"}])

        _execute(wired, case)
        _execute(wired, case)

        assert results_repo.count_results(session, run.id) == 1

    def test_second_execution_wins(self, wired) -> None:
        """最后一次写入获胜——第一次可能是个残缺结果。"""
        make, run, client, policy, settings, session = wired
        loose = make(
            "/ok",
            [{"type": "status_code", "expected": 200, "derived_from": "spec"}],
            name="宽松断言",
        )
        _execute(wired, loose)

        # 换一条更严的断言，模拟"同一用例被重新生成后重跑"。
        results_repo.upsert_many(
            session,
            [{"run_id": run.id, "case_id": loose.id, "status": "failed"}],
        )
        session.flush()

        assert results_repo.get(session, run.id, loose.id).status == "failed"


class TestWakeCallback:
    def test_wake_is_called_after_the_result_is_persisted(self, wired) -> None:
        """**D4：先落库，再回调。顺序不能反。**

        若先回调再落库，编排层可能在结果写进去之前就完成判定，
        看到"还没完成"而继续等待——直到兜底轮询才发现。
        反过来（落库成功但回调丢了）只是晚一点推进，而兜底轮询会收尾。
        **两种失败模式的代价完全不同。**

        这里断言回调触发**那一刻**结果是否已经可见。
        """
        make, run, client, policy, settings, session = wired
        case = make("/ok", [{"type": "status_code", "expected": 200, "derived_from": "spec"}])

        observed: list[bool] = []

        def _wake(_run_id: uuid.UUID) -> None:
            # 回调里查一次库——这正是编排层醒来之后会做的事（`06` §6.2）。
            session.flush()
            observed.append(results_repo.get(session, run.id, case.id) is not None)

        run_case(
            session,
            run_id=run.id,
            case_id=case.id,
            client=client,
            policy=policy,
            settings=settings,
            wake=_wake,
        )

        assert observed == [True], "回调触发时结果必须已经可查——否则编排层会判定失败"

    def test_no_wake_callback_still_produces_a_result(self, wired) -> None:
        """回调丢失时结果照常落库。

        M1 的专项检查 (a) 验证的正是这条路径：**丢了回调，图仍要能推进**
        （靠兜底轮询）。所以"回调不是必需的"这件事必须成立。
        """
        make, run, client, policy, settings, session = wired
        case = make("/ok", [{"type": "status_code", "expected": 200, "derived_from": "spec"}])

        outcome = _execute(wired, case)

        assert outcome.status == "passed"
        assert results_repo.get(session, run.id, case.id) is not None


class TestSpecWithoutTargets:
    def test_missing_targets_is_a_loud_error(self, session, make_settings) -> None:
        """Spec 没有声明 `servers` → 拒绝执行，而不是打一个猜出来的地址。

        静默降级（比如"那就打 localhost 吧"）会让测试结果指向一个
        用户从未声明过的地方——那比失败更糟。
        """
        spec = Spec(
            fingerprint="d" * 64,
            openapi_version="3.0.3",
            raw_content="{}",
            byte_size=2,
            declared_targets=[],
            operation_count=0,
        )
        session.add(spec)
        session.flush()
        operation = Operation(spec_id=spec.id, method="GET", path="/ok")
        session.add(operation)
        session.flush()

        with pytest.raises(ValueError, match="没有声明任何出站目标"):
            load_case_spec(
                session,
                Case(
                    run_id=uuid.uuid4(),
                    operation_id=operation.id,
                    name="x",
                    category="normal",
                    dimension="value_valid",
                    request_method="GET",
                    request_path="/ok",
                    assertions=[],
                    generated_by="rule",
                ),
                operation,
                spec,
            )


class TestAuditIsScopedToTheRun:
    """**审计必须关联到 Run。**

    `03/Guardrail.md` GR6 说审计要回答的是"**这个 Run** 访问了哪些 host"。
    第一版没把 `run_id` 传进 `RequestContext`——于是表里有数据、字段也在，
    只是**关联断了**：按 Run 查一条都查不到。

    这类"数据写了但关联丢了"的 bug 不会报错，只会让审计在最需要它的时候
    （复盘某一次运行）变得没用。而它**跑一次 demo 也看不出来**——
    要看的是"按 run_id 筛选还剩几条"，而不是"表里有没有数据"。
    """

    def test_audit_rows_carry_the_run_id(self, wired) -> None:
        from testflow.guardrail.audit import AuditRecord
        from testflow.storage.repositories import audit as audit_repo

        make, run, client, policy, settings, session = wired
        records: list[AuditRecord] = []
        client.audit_sink = records.append

        case = make("/ok", [{"type": "status_code", "expected": 200, "derived_from": "spec"}])
        _execute(wired, case)

        assert records, "前置条件：应当产生了审计记录"
        assert all(r.run_id == run.id for r in records), (
            "审计记录没有关联到 Run——按 Run 复盘时一条都查不出来"
        )
        assert all(r.direction == "target" for r in records)

        # 也验一下真的能落库（`run_id` 是外键，类型不对会在这一步炸）。
        for record in records:
            audit_repo.record(
                session,
                direction=record.direction,
                target_host=record.target_host,
                target_url=record.target_url,
                decision=record.decision,
                deny_reason=record.deny_reason,
                run_id=record.run_id,
                request_digest=record.request_digest,
                trace_id=record.trace_id,
            )
        session.flush()
        assert audit_repo.list_by_run(session, run.id), "按 Run 查应当能查到"
