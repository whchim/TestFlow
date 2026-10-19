"""TS8：内网例外**只能来自配置，永不从 Spec 派生**。

对应决策：`09-测试策略.md` §3.3（TS8）与 §4 表、`03/Guardrail.md` §4.6、
ADR-0018、GR3。

## 这个论断需要守门人，因为它的失效方式很隐蔽

`EgressPolicy` 有两个来源不同的字段，而**它们看起来是一回事**：

    whitelist       ← 从 Spec 派生（Spec 是用户上传的，不可信）
    allowed_private ← 从配置读（配置是我们自己写的）

把第二个也做成"从 Spec 派生"是一个**很自然**的改动：Spec 里既然声明了
`servers[].url = http://127.0.0.1:9000`，那"允许访问它"看起来很顺理成章。

但那样 SR-2 就退化了：攻击者只需在文档里写上 `http://169.254.169.254/`，
就自动获得了访问它的授权。**校验变成了"检查文档有没有声明自己要访问哪里"**。

所以这条测试要断言的正是那个区分本身：**同一份 Spec，只有配置不同，
结论就必须不同**。

## 为什么必须是集成测试

它要验的是生产装配路径（`execution/tasks._policy_for` 读 `Run → Spec`，
再与 `Settings` 合并），而不是某个人在测试里手拼的 `EgressPolicy`。
手拼的话，被测的恰恰就是那个最容易改错的地方——而它会被替换掉。

`hits`（fixture 服务的访问计数）那条断言也不能少：**"我们决定不发"和
"它确实没发出去"是两件事**，前者是判断，后者是事实。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from testflow.execution.tasks import _policy_for
from testflow.guardrail.errors import GuardrailDenied
from testflow.guardrail.pipeline import RequestContext
from testflow.guardrail.sync_client import GuardrailSyncClient
from testflow.storage.models import Spec

pytestmark = [pytest.mark.integration, pytest.mark.security]


class _WorkerContext:
    """`_WorkerContext` 的最小替身：`_policy_for` 只用到这两个字段。

    `sessions` 指向**测试所在的连接**（`join_transaction_mode="create_savepoint"`），
    这样它读到的是本测试刚写进去、还没提交的那行 Spec——不必为了测试
    去提交数据，污染也就不会留下来。
    """

    def __init__(self, session: Session, settings) -> None:
        self.settings = settings
        self.sessions: sessionmaker[Session] = sessionmaker(
            bind=session.get_bind(), join_transaction_mode="create_savepoint"
        )


@pytest.fixture
def spec_pointing_at_the_fixture_server(session: Session, make_spec) -> Iterator[Spec]:
    """一份**声明了内网目标**的 Spec。

    fixture 服务监听环回地址，所以它本身就是最真实的"内网目标"——
    不需要另外造一个假地址，也就不存在"测试里的内网和真实的内网不一样"的缝。
    """
    yield make_spec(declared_targets=["http://127.0.0.1:9000"])


def _send_once(settings, policy, audit: list) -> object:
    client = GuardrailSyncClient(settings, audit_sink=audit.append)
    try:
        return client.send(RequestContext(url="http://127.0.0.1:9000/ok", method="GET"), policy)
    finally:
        client.close()


class TestSpecDeclarationGrantsNothing:
    def test_the_spec_target_gets_into_the_whitelist(
        self, session, spec_pointing_at_the_fixture_server, make_run, make_settings
    ) -> None:
        """前提：Spec 声明的目标**确实**进了白名单（SR-1 是"从 Spec 派生"的）。

        这一条不是废话——它把变量的边界画清楚：本文件要证明的是
        "**白名单**来自 Spec" 与 "**例外**不来自 Spec" 这两件事同时成立。
        少了它，下面那条测试可能只是因为"白名单根本没生效"而通过。
        """
        run = make_run(spec_pointing_at_the_fixture_server)
        context = _WorkerContext(session, make_settings())

        policy = _policy_for(context, run.id)

        assert {str(t) for t in policy.whitelist.targets} == {"http://127.0.0.1:9000"}
        assert policy.allowed_private == frozenset(), "没有配置例外时，例外集合必须是空的"

    def test_the_request_is_denied_and_audited_and_never_sent(
        self, session, spec_pointing_at_the_fixture_server, make_run, make_settings, hits
    ) -> None:
        """★ TS8 的核心：Spec 声明了也不等于授权。

        三条断言各管一件事，缺一条就少一层证据：

        * `GuardrailDenied(private_ip)` —— **判断**是拒绝
        * 审计里 `deny_reason == 'private_ip'` —— 这个判断**被记下来了**
        * `hits == 0` —— 请求**确实没到达**（判断与事实一致）
        """
        spec = spec_pointing_at_the_fixture_server
        run = make_run(spec)
        settings = make_settings(guardrail_allow_private_targets=[])
        policy = _policy_for(_WorkerContext(session, settings), run.id)
        audit: list = []

        hits.reset()
        result = _send_once(settings, policy, audit)

        assert isinstance(result, GuardrailDenied)
        assert result.reason.value == "private_ip"

        assert [r.decision for r in audit] == ["deny"]
        assert audit[0].deny_reason == "private_ip"
        assert audit[0].direction == "target"

        assert hits.of("/ok") == 0, "被拒绝的请求不该到达目标"


class TestOnlyTheConfigurationOpensIt:
    def test_the_same_spec_is_allowed_once_the_configuration_says_so(
        self, session, spec_pointing_at_the_fixture_server, make_run, make_settings, hits
    ) -> None:
        """★ 与上一条**只在配置上不同**——而结论必须反过来。

        这是本文件最有说服力的一条断言：它把"Spec 不是授权的来源"这件事
        变成了一个**受控对照**。同一个 Spec、同一个目标、同一份代码路径，
        唯一变化的是 `guardrail_allow_private_targets`。

        少了它，上一条测试可能只是因为"内网地址永远发不出去"而通过——
        而那与"例外只能来自配置"是两回事。
        """
        spec = spec_pointing_at_the_fixture_server
        run = make_run(spec)
        settings = make_settings(guardrail_allow_private_targets=["127.0.0.1:9000"])
        policy = _policy_for(_WorkerContext(session, settings), run.id)

        hits.reset()
        result = _send_once(settings, policy, [])

        assert not isinstance(result, GuardrailDenied), "配了例外就该放行"
        assert result.status_code == 200  # type: ignore[union-attr]
        assert hits.of("/ok") == 1

    def test_the_exception_does_not_widen_the_whitelist(
        self, session, spec_pointing_at_the_fixture_server, make_run, make_settings
    ) -> None:
        """例外**只放开 SR-2**，不碰 SR-1。

        若它能顺带放开白名单，那份例外名单就变成了"可以访问任意地址"——
        而配置它的人以为自己只是开了个调试后门。
        """
        spec = spec_pointing_at_the_fixture_server
        run = make_run(spec)
        settings = make_settings(
            guardrail_allow_private_targets=["127.0.0.1:9000", "127.0.0.1:9001"]
        )
        policy = _policy_for(_WorkerContext(session, settings), run.id)

        # 9001 在例外名单里，但**不在白名单里**（Spec 只声明了 9000）
        result = _send_once(
            settings,
            policy,
            [],
        )
        assert not isinstance(result, GuardrailDenied), "前提：9000 是通的"

        client = GuardrailSyncClient(settings)
        try:
            other_port = client.send(
                RequestContext(url="http://127.0.0.1:9001/ok", method="GET"), policy
            )
        finally:
            client.close()

        assert isinstance(other_port, GuardrailDenied)
        assert other_port.reason.value == "whitelist", (
            "例外名单里的地址不该因此绕过白名单——白名单只认 Spec 声明过的目标"
        )
