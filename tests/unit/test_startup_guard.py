"""启动期配置校验的**端到端**验证：进程真的会拒绝启动。

对应决策：`10-部署运维.md` §5 / `05-API契约.md` API3 / ADR-0022。

`test_config.py` 测的是 `Settings` 的校验器本身。这一条测的是**入口点
真的会因为它而退出**——两者之间还隔着"入口点有没有调用 `load_settings()`"
这一步，而它恰恰是最容易被重构掉的一步：一个只在测试里构造 `Settings`
的系统，配置校验再严也不会在部署时生效。

用子进程而不是 `pytest.raises`：只有子进程能观察到"没起来"这个结果，
而不是"构造对象时抛了异常"。
"""

from __future__ import annotations

import os
import subprocess
import sys

from testflow.config import Settings


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "TESTFLOW_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:1/db",
            "TESTFLOW_INTERNAL_TOKEN": "internal-token",
            "TESTFLOW_CREDENTIAL_ENCRYPTION_KEY": "fernet-key-placeholder",
            "TESTFLOW_LLM_BASE_URL": "https://example.invalid/v1",
            "TESTFLOW_LLM_API_KEY": "sk-placeholder",
        }
    )
    return env


def test_load_settings_refuses_exposed_without_token_in_a_subprocess() -> None:
    """非环回 + 无 Token → 进程以非零码退出，并说明原因。"""
    env = _base_env()
    env["TESTFLOW_BIND_HOST"] = "0.0.0.0"
    # 显式置空，覆盖本机 `.env` 里可能存在的 Token——否则这条测试会在
    # 开发机上"通过"，在 CI 上失败（或反过来），而两边都测错了东西。
    env["TESTFLOW_API_TOKEN"] = ""

    process = subprocess.run(
        [sys.executable, "-c", "from testflow.config import load_settings; load_settings()"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert process.returncode != 0
    assert "不是环回地址" in (process.stdout + process.stderr)


def test_the_same_config_is_accepted_once_a_token_is_present() -> None:
    """反向对照：加了 Token 就应当启动。没有这一条，上面的测试可能因为
    任意其他原因失败（例如导入错误），从而给出一个假的安心。"""
    env = _base_env()
    env["TESTFLOW_BIND_HOST"] = "0.0.0.0"
    env["TESTFLOW_API_TOKEN"] = "user-token"

    process = subprocess.run(
        [sys.executable, "-c", "from testflow.config import load_settings; load_settings()"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert process.returncode == 0, process.stderr


def test_safety_baseline_fields_are_still_absent() -> None:
    """ADR-0022 的守卫：安全底线没有开关。

    与 `test_config.py` 重复是刻意的——那条检查的是 Settings 的字段集合，
    这条把"字段名清单"本身也钉住，免得有人把开关加在别的类里再注入进来。
    """
    forbidden = {
        "guardrail_allow_metadata",
        "guardrail_follow_redirects",
        "guardrail_allow_destructive",
        "log_credentials",
    }
    assert forbidden.isdisjoint(Settings.model_fields)
