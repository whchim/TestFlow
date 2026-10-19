"""`/healthz` 与 `/readyz` 的分工。

对应决策：`10-部署运维.md` §5（启动顺序与健康检查）。

**为什么这两个端点必须分开**

`/healthz` 一旦去连数据库，数据库抖动就会让编排器把**健康的 Gateway**
判死并重启。重启一个正常的进程解决不了数据库的问题，只会中断正在处理的
请求。所以：

* `/healthz` —— 只证明"进程活着"，不碰任何依赖
* `/readyz` —— 明确回答"现在能不能干活"，依赖不可达时返回 503

这组测试不需要真实 PG/Redis：依赖检查函数被替换成固定的结果，
被测的是**路由决策**（状态码与响应形状），而不是驱动本身。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from testflow.entrypoints import gateway as gateway_module
from testflow.entrypoints.gateway import create_app
from testflow.gateway.schemas import ReadinessCheck


def _patch_checks(monkeypatch, *, database: bool, redis: bool) -> None:
    async def _db(_ctx):
        return ReadinessCheck(name="database", ok=database, detail=None if database else "X")

    async def _redis(_settings):
        return ReadinessCheck(name="redis", ok=redis, detail=None if redis else "Y")

    monkeypatch.setattr(gateway_module, "_check_database", _db)
    monkeypatch.setattr(gateway_module, "_check_redis", _redis)


def test_healthz_does_not_depend_on_anything(monkeypatch, make_settings) -> None:
    """即使两项依赖都不可达，`/healthz` 仍然 200。"""

    def _boom(_ctx):
        raise AssertionError("/healthz 不应该碰数据库")

    monkeypatch.setattr(gateway_module, "_check_database", _boom)

    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_ok_when_both_dependencies_answer(monkeypatch, make_settings) -> None:
    _patch_checks(monkeypatch, database=True, redis=True)

    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {check["name"] for check in body["checks"]} == {"database", "redis"}


def test_readyz_returns_503_when_database_is_down(monkeypatch, make_settings) -> None:
    """**状态码是契约**：只看 200/503 的负载均衡与部署脚本必须被正确告知。"""
    _patch_checks(monkeypatch, database=False, redis=True)

    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    failed = {check["name"]: check for check in body["checks"]}
    assert failed["database"]["ok"] is False
    assert failed["redis"]["ok"] is True


def test_readyz_does_not_leak_exception_messages(monkeypatch, make_settings) -> None:
    """`detail` 只放异常类型——异常消息可能含连接串。"""

    async def _db(_ctx):
        return ReadinessCheck(name="database", ok=False, detail="OperationalError")

    async def _redis(_settings):
        return ReadinessCheck(name="redis", ok=True)

    monkeypatch.setattr(gateway_module, "_check_database", _db)
    monkeypatch.setattr(gateway_module, "_check_redis", _redis)

    app = create_app(make_settings())
    with TestClient(app) as client:
        body = client.get("/readyz").json()

    assert body["checks"][0]["detail"] == "OperationalError"
    assert "password" not in client.get("/readyz").text.lower()
