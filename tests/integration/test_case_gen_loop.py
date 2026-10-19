"""`LlmCaseGenerator` 作为 `case_generator` 放进**真实生成循环**。**需要 PostgreSQL。**

对应决策：`03/模块详细设计/TestCaseGen.md` TG1/TG4、`06-Agent编排.md` §4、
`04-数据模型.md` DM4。

## 这条测试抓的是 M2 的一个真实 bug

第一版把 `QwenProvider` 直接当 `case_generator` 传进图，而它的签名是
`generate(*, prompt, schema)`——图里那句调用是
`await active.generate(operation, dimensions)`，**纯位置参数**。

于是每调用一次就抛：

    TypeError: QwenProvider.generate() takes 1 positional argument but 3 were given

## 为什么它必须在"真实循环"里测，而不是在适配器自己的单测里

`tests/unit/test_llm_case_gen.py` 里那批测试调用适配器时写的是
`generator.generate(_Operation(), [_dimension()])`——**和循环一样的调用形状**，
所以它们其实能抓住签名问题。但那是运气：适配器单测完全可以把参数写成关键字，
而那正好把"循环怎么调它"这件事漏掉。

这一条不靠运气：它经过 `generate_for_operations` 的**真实代码路径**
（`active.generate(operation, dimensions)` 那句就在那个函数里），
并且用真的 ORM 行、真的落库。

## 为什么用 `FakeLLMProvider` 而不是真模型

它实现了同一个 `LLMClient` 协议，输出确定性，不花钱、不需要网络。
而这里要验的是**接线**（契约对不对得上），不是生成质量——
后者归 `09` §5 的 L3 Eval，那才需要真实模型。
"""

from __future__ import annotations

import pytest

from testflow.agents.llm_case_gen import LlmCaseGenerator
from testflow.agents.test_case_gen import generate_for_operations
from testflow.storage.repositories import cases as cases_repo
from testflow.storage.repositories import runs as runs_repo
from testflow.testing.fake_llm import FakeLLMProvider

pytestmark = pytest.mark.integration


async def test_the_loop_can_call_the_llm_generator(session, make_spec, make_operation) -> None:
    """★ 一次调用跑通，且用例真的落库了。

    `failed_operations == []` 是核心断言：签名对不上时 `generate_for_operations`
    的 `except Exception` 会把 `TypeError` 吞掉、把这个 Operation 标成
    `failed`——**而它不会抛异常**。所以"没报错"证明不了什么，
    必须断言失败列表是空的。
    """
    spec = make_spec()
    op = make_operation(spec, method="POST", path="/echo")
    run = runs_repo.create(session, spec_id=spec.id, spec_fingerprint=spec.fingerprint)

    fake = FakeLLMProvider()
    outcome = await generate_for_operations(
        session,
        run_id=run.id,
        operation_ids=[op.id],
        generator=LlmCaseGenerator(fake),
        # 刻意**不传** `fallback_rule_generator`：那会让无信息接口绕过 LLM，
        # 而这里要测的正是 LLM 那条路。
    )

    assert outcome.failed_operations == [], (
        "适配器放进真实循环时失败了。注意：它的异常被「部分失败不阻塞全局」"
        "接住了，所以不会有堆栈——去查 run_operations.error。"
    )
    assert fake.calls == ["POST /echo"], "应当恰好调用一次，且带上正确的路径"

    stored = cases_repo.list_by_run(session, run.id)
    assert stored, "用例应当真的落库"
    assert {row.generated_by for row in stored} == {"llm"}
    assert {row.llm_model for row in stored} == {"fake-llm-0"}, (
        "`llm_model` 必须来自底层 Provider——L3 Eval 靠它比对档位"
    )


async def test_a_provider_failure_marks_only_that_operation_failed(
    session, make_spec, make_operation
) -> None:
    """DM4 的"部分失败不阻塞全局"在 LLM 路径上同样成立。

    一个接口的模型调用失败，不该让整份 Spec 的生成全部作废——
    而且它必须被记成 `failed` 而不是 `succeeded`：失败可重试，
    "成功但零用例"不可重试且不会有人发现。
    """
    from testflow.storage.repositories import run_operations as ro_repo

    spec = make_spec()
    broken = make_operation(spec, method="POST", path="/echo")
    healthy = make_operation(spec, method="POST", path="/ok")
    run = runs_repo.create(session, spec_id=spec.id, spec_fingerprint=spec.fingerprint)

    fake = FakeLLMProvider(fail_for=frozenset({"POST /echo"}))
    outcome = await generate_for_operations(
        session,
        run_id=run.id,
        operation_ids=[broken.id, healthy.id],
        generator=LlmCaseGenerator(fake),
    )

    assert outcome.failed_operations == [broken.id]
    assert outcome.generated_case_ids, "另一个接口照样要产出用例"

    statuses = ro_repo.status_of(session, run.id)
    assert statuses[broken.id] == "failed"
    assert statuses[healthy.id] == "succeeded"
