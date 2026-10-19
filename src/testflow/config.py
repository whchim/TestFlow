"""配置的唯一定义与读取处。

对应决策：`08-工程规范.md` EN3、`10-部署运维.md` §3。

**只有本模块读取环境变量。** 其余模块接收 `Settings` 实例，不直接访问
`os.environ`——否则测试无法注入配置，且配置项散落各处无法盘点。

⚠️ **刻意的缺失**（`10` OP1 / ADR-0022）

以下行为是安全底线，**不提供开关**，因此*不*在此处定义。实现它们时
不要顺手加配置项：

    1. 云元数据地址一律拒绝（即使配置了内网例外）
    2. 不跟随重定向
    3. 破坏性方法未授权时拒绝
    4. 白名单为 scheme + host + port 精确匹配，不通配、不继承子域
    5. 校验作用于解析后的 IP 并钉死连接目标
    6. 日志不写入凭证与请求/响应正文

判断标准：**改了这个值，会不会让安全性变差？** 会 → 硬编码；不会
（只影响性能、体验、兼容性）→ 才放进下面的 `Settings`。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 视为环回的绑定地址。绑定到这些之外时强制要求 api_token（05 API3）。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class Settings(BaseSettings):
    """全部配置项的唯一清单。"""

    model_config = SettingsConfigDict(
        env_prefix="TESTFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",  # 环境变量名写错时直接报错，而不是静默用默认值
    )

    # ---------------------------------------------------------- 运行
    bind_host: str = "127.0.0.1"
    bind_port: int = 3000
    #: Gateway 对**内部消费者**（Worker 的唤醒回调）的可达地址。
    #:
    #: 与 `bind_host:bind_port` 分开，是因为两者在容器部署里**不一样**：
    #: Gateway 绑 `0.0.0.0:3000`，而 Worker 要访问的是服务名 `gateway:3000`
    #: ——它自己的 `127.0.0.1` 是它自己。
    #:
    #: 不配时回落到 `http://{bind_host}:{bind_port}`，那在单机部署下是对的。
    gateway_url: str | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ---------------------------------------------------------- 存储
    database_url: str
    db_pool_size: int = 5

    # 用 `127.0.0.1` 而不是 `localhost`：在部分环境里（Windows 的 hosts
    # 解析优先 IPv6）`localhost` 会先解析到 `::1`，而 Docker 只把端口发布
    # 在 IPv4 的 127.0.0.1 上——结果是连接一直等到超时，看起来像 Redis 挂了。
    # `/readyz` 的集成测试在 M5 就撞上了这个（`redis.exceptions.TimeoutError`）。
    redis_url: str = "redis://127.0.0.1:6379"
    # 必须分库（02 §6 / ADR-0009）：混用同一 DB 会让 FLUSHDB 类运维操作
    # 误删对方数据，且二者的淘汰策略需求不同。
    redis_broker_db: int = 0
    redis_checkpoint_db: int = 1

    # ---------------------------------------------------------- 认证
    # 仅当 bind_host 非环回时必填；由下方校验器强制。
    api_token: str | None = None
    # 内部端点专用，与 api_token 权限不同（05 §5.3 / ADR-0022）。
    internal_token: str
    # Fernet 密钥（SR-6）。07 NF9：不得经由应用进程间传递。
    credential_encryption_key: str

    # ---------------------------------------------------------- LLM
    llm_base_url: str
    llm_api_key: str
    llm_model: str = "qwen3.5-plus"
    llm_timeout_s: int = 120
    llm_max_retries: int = 2

    # 思考模式按用途分开。**默认值来自 M4 实测，不是设计稿的推测**：
    #
    #   qwen3.7-flash · 5 个接口 × 2 轮（样本 n=10）
    #   thinking=on ：首次通过 67%，输出 8,864 token，P95 ≈ 57s，¥0.0077/2 次
    #   thinking=off：首次通过 100%，输出 1,215 token，P95 ≈ 8s，¥0.0014/3 次
    #
    # 思考模式把延迟推到验收线（P95 ≤ 30s）之外，输出 token 是 7 倍，
    # 且首次通过率更低。`02` §5.3 原定"生成开、归因关"被这组数据推翻——
    # 生成也不需要思考：维度清单已经由阶段 A 确定性派生，模型只填取值。
    llm_enable_thinking_generation: bool = False
    llm_enable_thinking_attribution: bool = False

    # 采样参数。`None` 表示"不传，用提供商默认值"——传一个猜测值比不传更糟：
    # 它会让 M5 的 Golden Set 比较失去"默认基线"。M4 的基准脚本负责实测。
    llm_temperature: float | None = None
    llm_top_p: float | None = None
    llm_seed: int | None = None
    llm_max_tokens: int | None = None

    # 单价覆盖。键是模型名，值是按输入长度升序的档位列表。默认表在
    # `llm/pricing.py`（代码里可 review、可测试）；这里只用于不改代码地纠偏。
    llm_pricing_overrides: dict[str, list[dict[str, float]]] = Field(default_factory=dict)

    # ---------------------------------------------------------- Guardrail
    # 仅阈值与白名单。安全底线见模块 docstring。
    # 内网例外的唯一来源，永不从 Spec 派生（ADR-0018）。
    guardrail_allow_private_targets: list[str] = Field(default_factory=list)
    # 异常兜底而非常规节流——常规保护由 worker_concurrency 承担（10 §3.4）。
    guardrail_qps_per_host: float = 20.0
    guardrail_max_concurrency_per_host: int = 8
    guardrail_breaker_threshold: int = 5
    guardrail_breaker_cooldown_s: int = 60

    # ---------------------------------------------------------- 执行
    http_timeout_s: int = 10  # TE7 第一层；Celery 的 soft/hard limit 另设
    worker_concurrency: int = 8
    response_body_max_bytes: int = 256 * 1024  # 04 DM6

    # ---------------------------------------------------------- 规模上限
    # 超出时明确拒绝，不静默降级（07 NF2）。
    max_operations_per_spec: int = 200
    max_cases_per_run: int = 2000
    max_upload_bytes: int = 10 * 1024 * 1024  # 07 NF4

    # ---------------------------------------------------------- 生命周期
    run_retention_days: int = 90
    review_timeout_days: int = 7  # IP-1 挂起上限（06 §5.1）
    executor_stall_multiplier: float = 2.0  # 兜底轮询超时倍数（06 §6.4）

    # ---------------------------------------------------- 启动期校验

    @model_validator(mode="after")
    def _require_token_when_exposed(self) -> Settings:
        """绑定非环回地址时必须配置 Token（05 API3 / ADR-0022）。

        非法配置应在**启动时**失败，而非运行时——一个"服务起来了但对局域网
        裸奔"的状态，比"启动失败并给出明确原因"危险得多（10 §5）。
        """
        if self.bind_host not in LOOPBACK_HOSTS and not self.api_token:
            raise ValueError(
                f"bind_host={self.bind_host!r} 不是环回地址，此时必须设置 "
                "TESTFLOW_API_TOKEN。否则平台（含加密凭证存储）将对网络开放。"
            )
        return self

    @model_validator(mode="after")
    def _require_token_separation(self) -> Settings:
        """内部端点 Token 不得与用户 Token 相同（05 §5.3）。

        二者权限不同：内部 Token 能触发图推进，若可复用用户 Token，
        一个泄露的用户凭证就能干扰任意 Run 的执行。
        """
        if self.api_token and self.internal_token == self.api_token:
            raise ValueError(
                "internal_token 不能与 api_token 相同——二者权限不同，"
                "复用会让用户凭证获得内部端点的能力。"
            )
        return self

    @model_validator(mode="after")
    def _require_distinct_redis_dbs(self) -> Settings:
        """Broker 与 Checkpoint 必须分库（02 §6 / ADR-0009）。"""
        if self.redis_broker_db == self.redis_checkpoint_db:
            raise ValueError(
                f"redis_broker_db 与 redis_checkpoint_db 不能相同（都是 "
                f"{self.redis_broker_db}）。混用同一 DB 会让 FLUSHDB 类运维操作"
                "误删对方数据。"
            )
        return self


def load_settings() -> Settings:
    """构造配置。

    **刻意不做模块级单例**（08 §3）：在导入时实例化会让测试无法注入配置。
    调用方应显式调用本函数，并把结果传下去。
    """
    return Settings()  # type: ignore[call-arg]  # 必填项来自环境变量
