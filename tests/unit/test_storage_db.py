"""数据库连接构造的单元测试。

**不连库**——只验证引擎/会话工厂按配置构造。真正的连通性由 CI 的
`alembic check` 步骤覆盖（它必须连上库才能比对模型与迁移）。

这组测试挡的是"接线错误"：传错配置项、池大小没生效、`expire_on_commit`
被改回默认——这些都**不会报错**，只会在运行时表现为难查的行为异常。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine

from testflow.config import Settings
from testflow.storage.db import create_db_engine, create_session_factory

MakeSettings = Callable[..., Settings]


class TestCreateDbEngine:
    def test_uses_configured_url(self, make_settings: MakeSettings) -> None:
        settings = make_settings()
        engine = create_db_engine(settings)
        try:
            assert engine.url.render_as_string(hide_password=False) == settings.database_url
        finally:
            engine.dispose()

    def test_str_representation_hides_password(self, make_settings: MakeSettings) -> None:
        """SQLAlchemy 默认在 `str(url)` 中把口令渲染为 `***`。

        **这条行为值得固化。** 它是防止口令通过异常堆栈、日志、错误信息
        泄露的一道防线（SR-6）——而"为了调试方便"把它改成明文，是一个
        看起来无害的改动。这行断言让那个改动直接失败。
        """
        engine = create_db_engine(make_settings())
        try:
            rendered = str(engine.url)
            assert "***" in rendered
            assert ":p@" not in rendered, "口令不应出现在 URL 的字符串表示中"
        finally:
            engine.dispose()

    def test_respects_configured_pool_size(self, make_settings: MakeSettings) -> None:
        engine = create_db_engine(make_settings(db_pool_size=3))
        try:
            assert engine.pool.size() == 3
        finally:
            engine.dispose()

    def test_returns_engine(self, make_settings: MakeSettings) -> None:
        engine = create_db_engine(make_settings())
        try:
            assert isinstance(engine, Engine)
        finally:
            engine.dispose()


class TestCreateSessionFactory:
    def test_binds_the_given_engine(self, make_settings: MakeSettings) -> None:
        engine = create_db_engine(make_settings())
        try:
            factory = create_session_factory(engine)
            assert factory.kw["bind"] is engine
        finally:
            engine.dispose()

    def test_disables_expire_on_commit(self, make_settings: MakeSettings) -> None:
        """提交后仍可读对象属性。

        默认的 `expire_on_commit=True` 会让"提交 → 序列化响应"这类流程触发
        意外的延迟加载——轻则 N+1 查询，重则在事务已关闭时直接报错。
        """
        engine = create_db_engine(make_settings())
        try:
            factory = create_session_factory(engine)
            assert factory.kw["expire_on_commit"] is False
        finally:
            engine.dispose()
