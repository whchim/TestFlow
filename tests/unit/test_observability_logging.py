"""日志脱敏的单元测试。

对应决策：`08-工程规范.md` EN5 / PRD SR-6，以及 ADR-0022（安全底线不可关闭）。

**这组测试的性质和别处不同：它测的是"什么东西**不**能出现"。**

脱敏写错了不会报错——它只会让密钥安静地出现在日志里，而日志会被采集、
转发、存档。所以这里的断言都是**否定式**的：日志里不得包含某个片段。

**`Authorization: Bearer <token>` 那一组是回归测试。** 第一版实现的值匹配
只吃一个 token，于是只擦掉了 `Bearer`，把令牌原样留下——测试当时是绿的，
因为断言写的是"包含 [REDACTED]"而不是"不包含令牌"。**教训：脱敏测试必须断言
"秘密不在了"，而不是断言"有掩码出现"。**
"""

from __future__ import annotations

import io
import logging

import pytest

from testflow.observability.logging import RedactionFilter, configure_logging

#: 一组形状不同的凭证，全部都是真实日志里会出现的形态。
SECRETS = {
    "bearer": "sk-super-secret-value",
    "basic_b64": "Zm9vOmJhcg==",
    "session": "session=abc",
    "bare": "hunter2",
}


@pytest.fixture
def capture() -> io.StringIO:
    """把日志写进内存缓冲，并**只挂脱敏过滤器**（不带格式器噪声）。"""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(RedactionFilter())

    logger = logging.getLogger("testflow.tests.redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return buf


def _leaks(buf: io.StringIO) -> list[str]:
    """缓冲区里还残留哪些秘密片段。空列表 = 干净。"""
    out = buf.getvalue()
    return [name for name, value in SECRETS.items() if value in out]


class TestSecretsDoNotSurvive:
    @pytest.mark.parametrize(
        ("msg", "args"),
        [
            # ① 键名与值都在消息字面量里
            ("Authorization: Bearer sk-super-secret-value", ()),
            ("Proxy-Authorization: Basic Zm9vOmJhcg==", ()),
            # ② 值通过惰性格式化参数传入
            ("api_key=%s", ("hunter2",)),
            # ③ 请求头字典作为参数（repr 出来带引号）
            ("headers=%s", ({"Cookie": "session=abc", "accept": "json"},)),
            # ④ 凭证在字典的**值**里
            ("ctx=%s", ({"Authorization": "Bearer sk-super-secret-value"},)),
            # ⑤ 键名与值跨字面量/参数边界
            ("Authorization: %s", ("Bearer sk-super-secret-value",)),
        ],
    )
    def test_no_secret_fragment_remains(
        self, capture: io.StringIO, msg: str, args: tuple[object, ...]
    ) -> None:
        logging.getLogger("testflow.tests.redaction").info(msg, *args)
        assert _leaks(capture) == [], f"日志中残留了 {_leaks(capture)}：\n{capture.getvalue()}"

    def test_extra_fields_are_scrubbed(self, capture: io.StringIO) -> None:
        """`logger.info(..., extra={...})` 传进来的字段同样要擦。

        走 extra 的字段常被结构化日志采集器直接取走，绕过消息体——
        只擦消息是不够的。
        """
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", (), None)
        record.__dict__["authorization"] = "Bearer sk-super-secret-value"
        record.__dict__["request_body"] = {"password": "hunter2"}

        assert RedactionFilter().filter(record) is True

        assert "sk-super-secret-value" not in str(record.__dict__)
        assert "hunter2" not in str(record.__dict__)


class TestKeyNamesArePreserved:
    """擦的是值，不是键名——"哪个字段被擦了"本身是排障信息。"""

    def test_dict_keeps_its_key(self, capture: io.StringIO) -> None:
        logging.getLogger("testflow.tests.redaction").info(
            "h=%s", {"Cookie": "session=abc", "accept": "json"}
        )
        out = capture.getvalue()
        assert "Cookie" in out, "键名不该被擦掉——否则不知道发生了什么"
        assert "accept" in out, "无关字段不该被牵连"


class TestNoFalsePositives:
    """脱敏不能误伤正常内容，否则日志就没人看了。"""

    @pytest.mark.parametrize(
        "msg",
        [
            "authorization_notes 是一个字段名",
            "cookie_jar 与 tokenizer 都不是凭证",
            "GET /api/users/42 返回 200，耗时 87ms",
            "spec_fingerprint=a3f2... 已复用",
        ],
    )
    def test_ordinary_text_is_untouched(self, capture: io.StringIO, msg: str) -> None:
        assert "[REDACTED]" not in capture.getvalue()  # 前置条件：缓冲是干净的
        logging.getLogger("testflow.tests.redaction").info(msg)
        assert capture.getvalue().strip() == msg


class TestFailClosed:
    def test_unrepresentable_message_is_dropped_not_passed_through(self) -> None:
        """脱敏过程本身出错时，宁可丢日志也不放行（fail-closed）。

        这条与 `guardrail.policy` 的 fail-closed 是同一条原则：
        安全模块**不知道**的情况太多，而"不知道就放行"意味着攻击者
        只需要找到一个我们没预料到的形态。
        """

        class Exploding:
            def __str__(self) -> str:
                raise RuntimeError("boom")

            def __repr__(self) -> str:
                raise RuntimeError("boom")

        record = logging.LogRecord("t", logging.INFO, __file__, 1, "%s", (Exploding(),), None)
        assert RedactionFilter().filter(record) is True
        assert record.msg == "[REDACTION_FAILED]"


class TestConfigureLogging:
    def test_is_idempotent(self) -> None:
        """重复调用不叠加 handler——否则每条日志会打印多次。"""
        root = logging.getLogger()
        before = len(root.handlers)
        configure_logging("INFO")
        configure_logging("INFO")
        configure_logging("INFO")
        assert len(root.handlers) == before + 1

    def test_sets_level(self) -> None:
        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING
        configure_logging("INFO")  # 还原，避免影响其他测试
