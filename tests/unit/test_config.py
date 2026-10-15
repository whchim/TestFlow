"""配置的单元测试。

对应决策：`10-部署运维.md` §3、§5、OP1，ADR-0022，`05-API契约.md` API3。

**为什么这些测试重要**

`config.py` 的校验器是"启动期就失败"的实现（`10` §5）。它们挡住的是三种
**配置错误导致的安全事故**——而这类错误在任何功能测试里都不会浮现，
因为功能测试用的永远是正确配置。所以必须直接测校验器本身。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from testflow.config import LOOPBACK_HOSTS, Settings

MakeSettings = Callable[..., Settings]


class TestDefaults:
    def test_bind_defaults_to_loopback(self, make_settings: MakeSettings) -> None:
        """默认只监听环回——"默认安全"而非"默认开放"（ADR-0022）。"""
        assert make_settings().bind_host in LOOPBACK_HOSTS

    def test_redis_dbs_are_separate_by_default(self, make_settings: MakeSettings) -> None:
        """Broker 与 Checkpoint 必须分库（02 §6 / ADR-0009）。"""
        s = make_settings()
        assert s.redis_broker_db != s.redis_checkpoint_db

    def test_response_body_limit_is_256kb(self, make_settings: MakeSettings) -> None:
        """与 `04` DM6 一致。改动它需要同步改文档。"""
        assert make_settings().response_body_max_bytes == 256 * 1024

    def test_http_timeout_is_below_celery_soft_limit(self, make_settings: MakeSettings) -> None:
        """TE7 要求三层超时严格递增，否则任务被中断前来不及写「超时」结果。"""
        assert make_settings().http_timeout_s < 30  # Celery soft_time_limit

    def test_guardrail_qps_exceeds_default_concurrency(self, make_settings: MakeSettings) -> None:
        """GR9 / `10` §3.4：QPS 上限是**异常兜底**而非常规节流，
        因此它应高于正常并发能产生的吞吐，否则会持续压制自己。"""
        s = make_settings()
        assert s.guardrail_qps_per_host > s.worker_concurrency


class TestBindSafety:
    """API3 / ADR-0022：绑定非环回地址时强制要求 Token。"""

    def test_loopback_without_token_is_allowed(self, make_settings: MakeSettings) -> None:
        assert make_settings(bind_host="127.0.0.1").api_token is None

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "10.0.0.5"])
    def test_exposed_without_token_is_rejected(
        self, make_settings: MakeSettings, host: str
    ) -> None:
        """非法配置必须在**启动时**失败。

        一个"服务起来了但对局域网裸奔"的状态，比"启动失败并说明原因"
        危险得多——后者用户会去修，前者不会。
        """
        with pytest.raises(ValidationError, match="不是环回地址"):
            make_settings(bind_host=host)

    def test_exposed_with_token_is_allowed(self, make_settings: MakeSettings) -> None:
        s = make_settings(bind_host="0.0.0.0", api_token="user-token")
        assert s.api_token == "user-token"


class TestTokenSeparation:
    """`05` §5.3：内部端点 Token 与用户 Token 权限不同，不得复用。"""

    def test_identical_tokens_are_rejected(self, make_settings: MakeSettings) -> None:
        with pytest.raises(ValidationError, match="不能与 api_token 相同"):
            make_settings(bind_host="0.0.0.0", api_token="same", internal_token="same")

    def test_distinct_tokens_are_allowed(self, make_settings: MakeSettings) -> None:
        s = make_settings(bind_host="0.0.0.0", api_token="user", internal_token="internal")
        assert s.api_token != s.internal_token

    def test_no_api_token_means_no_conflict(self, make_settings: MakeSettings) -> None:
        """环回部署下 api_token 为空，此时不构成复用。"""
        assert make_settings(internal_token="x").api_token is None


class TestRedisDbSeparation:
    """`02` §6：混用同一 DB 会让 FLUSHDB 类运维操作误删对方数据。"""

    def test_same_db_is_rejected(self, make_settings: MakeSettings) -> None:
        with pytest.raises(ValidationError, match="不能相同"):
            make_settings(redis_broker_db=1, redis_checkpoint_db=1)


class TestSafetyBaselinesAreNotConfigurable:
    """**ADR-0022 的可执行断言。**

    "安全底线不做成配置项"若只写在文档里，会被后来者当成"遗漏"而顺手加上。
    这组测试让"加开关"这个动作直接失败——它的存在本身就是那条决策的守卫。
    """

    #: 这些名字**必须不存在**。它们对应的是没有例外的硬编码行为。
    FORBIDDEN_FIELDS = (
        "guardrail_allow_metadata",  # 元数据地址永久拒绝（GR4）
        "guardrail_block_metadata",
        "guardrail_follow_redirects",  # 重定向永不跟随（GR5）
        "guardrail_allow_redirects",
        "guardrail_allow_destructive",  # 破坏性方法无授权入口（ADR-0021）
        "guardrail_enable_destructive",
        "guardrail_block_private_ips",  # 内网阻断本身不可关闭（例外是另一回事）
        "guardrail_enforce_whitelist",
        "guardrail_match_subdomains",  # 白名单不做子域继承（GR3）
        "guardrail_pin_resolved_ip",  # IP 钉死不可关闭（GR2）
        "log_credentials",  # 日志永不写凭证（SR-6）
        "log_request_body",
        "mask_sensitive_data",
    )

    @pytest.mark.parametrize("name", FORBIDDEN_FIELDS)
    def test_field_does_not_exist(self, name: str) -> None:
        assert name not in Settings.model_fields, (
            f"配置项 {name!r} 不应存在。安全底线没有开关（ADR-0022）——\n"
            "  可关闭的安全开关本身就是漏洞：会被误设、被复制到生产、\n"
            "  或被「临时」关掉后遗忘，而三种情况都不会有任何报错。\n"
            "  判断标准：改了这个值会不会让安全性变差？会 → 硬编码。"
        )

    def test_private_target_exception_still_exists(self) -> None:
        """例外**列表**是可配置的——它受控地调整边界，而不是关闭边界。

        与上一条的区别：这份名单只能被显式输入，且永不从 Spec 派生（ADR-0018）。
        """
        assert "guardrail_allow_private_targets" in Settings.model_fields

    def test_unknown_field_is_rejected(self, make_settings: MakeSettings) -> None:
        """`extra="forbid"`：字段名写错时直接报错，而非静默用默认值。

        `TESTFLOW_LLM_KEY` 与 `TESTFLOW_LLM_API_KEY` 差一个词，
        不报错的话会表现为"运行时才认证失败"，排查成本高得多。
        """
        with pytest.raises(ValidationError):
            make_settings(unknown_field=1)
