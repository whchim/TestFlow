"""SQLAlchemy 模型。

对应文档：`04-数据模型.md`（每张表的注释里标了对应小节）。

**migration 才是 schema 的唯一事实来源**（`08` EN7）。本文件的职责是让
Alembic 能自动比对出差异，因此**改动模型后必须生成 migration**，
不允许只在模型里改。

⚠️ 枚举一律用 `TEXT + CHECK` 而非 PG 原生 `ENUM`（`04` DM7）：原生 ENUM
增加取值需要 `ALTER TYPE ... ADD VALUE`，且不能删除取值；而本项目的枚举
（归因类别、断言类型）在早期必然演进。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Attribution",
    "AuditLog",
    "Base",
    "Case",
    "CaseResult",
    "Credential",
    "Operation",
    "Report",
    "Run",
    "RunOperation",
    "Spec",
]


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    """主键：由数据库生成 UUIDv7（`04` DM8）。

    用 `server_default` 而非应用侧生成——PG 18 起原生提供 `uuidv7()`，
    时间有序性由数据库保证，应用不必依赖额外的 UUID 库。

    `audit_logs` 例外，用 BIGSERIAL（追加型日志，无需 ID 语义，且更省空间）。
    """
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))


def _created_at() -> Mapped[datetime.datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ================================================================ 2.1 specs


class Spec(Base):
    """上传的 OpenAPI 文档。见 `04` §2.1。

    **不可变**：内容变了就是另一行（指纹不同）。这个性质让 `declared_targets`
    等派生字段可以安全地存在本表上——不会出现"同一个 spec_id 的声明变了"。
    """

    __tablename__ = "specs"

    id: Mapped[uuid.UUID] = _pk()
    #: sha256(规范化后的内容)。规范化在 `$ref` 展开**之后**进行（`04` DM2 / ADR-0012）
    fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    filename: Mapped[str | None] = mapped_column(Text)
    openapi_version: Mapped[str] = mapped_column(Text, nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 出站白名单的来源（SR-1）。值为 `scheme://host:port` —— 必须含 scheme，
    #: 否则 GR3 的"允许 http 等于允许明文传凭证"无法执行。
    #: 注：原设计名 `declared_hosts`，因只存 host:port 而丢失 scheme，M0 改名。
    declared_targets: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    operation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = _created_at()


# =========================================================== 2.2 operations


class Operation(Base):
    """Spec 中的一个接口。见 `04` §2.2。"""

    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("spec_id", "method", "path", name="uq_operations_spec_method_path"),
        CheckConstraint(
            "method IN ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS')",
            name="ck_operations_method",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    spec_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("specs.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    operation_id: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)

    # 整块 JSONB 存储，不按 parameter 拆行（`04` §2.10）——schema 是整体
    # （$ref、嵌套、条件约束），拆行会破坏结构完整性。裁剪在组装提示时进行，
    # 这是 Qwen 阶梯计费（02 §5.1）的要求。
    parameters_schema: Mapped[dict | None] = mapped_column(JSONB)
    request_body_schema: Mapped[dict | None] = mapped_column(JSONB)
    responses_schema: Mapped[dict | None] = mapped_column(JSONB)

    #: `$ref` 展开深度超限、保留了字面量（`SpecParser` SP1 / §3.2）。
    #: 下游据此把该接口生成的用例全部标为低置信度。
    has_unresolved_ref: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: SR-3。默认值由 method 派生，但**允许显式覆盖**（`04` DM3）——
    #: `POST /users/{id}/close` 在业务上可能是破坏性的。
    is_destructive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime.datetime] = _created_at()


# ================================================================ 2.3 runs


class Run(Base):
    """一次测试执行批次。见 `04` §2.3。"""

    __tablename__ = "runs"
    __table_args__ = (
        # 兜底轮询的扫描依据（`06` §6.4）：按 phase + 超时时间找停滞的 Run
        Index("ix_runs_phase_updated_at", "phase", "updated_at"),
        CheckConstraint(
            "phase IN ('init','parsing','generating','awaiting_review','dispatching',"
            "'awaiting_results','analyzing','reporting','done','failed','abandoned')",
            name="ck_runs_phase",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("specs.id"), nullable=False)
    #: Run 发起时刻的**事实快照**，与 spec_id 并存不是冗余：
    #: spec_id 是外键（引用完整性），fingerprint 是判定依据（不可随后变化）。
    spec_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'init'"))

    # 以下三个是**派生缓存**（`04` DM5）。任何正确性判断——
    # 尤其是"是否全部完成"（`06` §6.2）——一律 `SELECT count(*)` 查
    # case_results，不得基于这些字段跳转。
    expected_case_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    settled_case_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    approved_case_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


# ======================================================= 2.4 run_operations


class RunOperation(Base):
    """Run 与 Operation 的关联及生成状态。见 `04` §2.4。

    **这张表存在的唯一理由是承载 N2 的幂等语义。** LLM 输出非确定性，
    重跑会得到不同用例；因此 N2 的幂等只能是"已完成则跳过"，而
    "是否已完成"必须持久化。见 `04` DM4 / ADR-0011。
    """

    __tablename__ = "run_operations"
    __table_args__ = (
        CheckConstraint(
            "gen_status IN ('pending','succeeded','failed')", name="ck_run_operations_status"
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    gen_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)


# =============================================================== 2.5 cases


class Case(Base):
    """一条测试用例。见 `04` §2.5。"""

    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint("category IN ('normal','abnormal','boundary')", name="ck_cases_category"),
        CheckConstraint(
            "review_status IN ('pending','approved','rejected')", name="ck_cases_review_status"
        ),
        CheckConstraint("generated_by IN ('llm','rule')", name="ck_cases_generated_by"),
        CheckConstraint(
            "confidence IS NULL OR confidence IN ('high','low')", name="ck_cases_confidence"
        ),
        Index("ix_cases_run_review", "run_id", "review_status"),
        Index("ix_cases_run_operation", "run_id", "operation_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("operations.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    #: 测试维度标识，如 `boundary:age`（`TestCaseGen` §2.2）。
    #: 使覆盖率可查询、报告可按维度分布聚合。
    dimension: Mapped[str] = mapped_column(Text, nullable=False)

    request_method: Mapped[str] = mapped_column(Text, nullable=False)
    request_path: Mapped[str] = mapped_column(Text, nullable=False)
    request_query: Mapped[dict | None] = mapped_column(JSONB)
    #: **不含凭证**（SR-6）。凭证在执行时由 Worker 按 host 注入——
    #: 这不是靠"记得不要放"保证的，是靠数据模型：凭证根本不在会被序列化
    #: 给 LLM 的对象里。
    request_headers: Mapped[dict | None] = mapped_column(JSONB)
    request_body: Mapped[dict | None] = mapped_column(JSONB)
    #: 断言数组，结构见 `TestCaseGen` §3（判别联合 + `derived_from`）。
    assertions: Mapped[list] = mapped_column(JSONB, nullable=False)

    is_destructive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: `auth_missing` 维度的用例置 false——否则平台会注入凭证使其收到 200
    #: 而非预期的 401，测试结果完全错误。
    requires_auth: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    # 以下三个字段是为 L3 Eval 服务的（`09` §5.3）。没有它们，
    # "生成质量是否随模型档位下降"这个问题无法用数据回答。
    generated_by: Mapped[str] = mapped_column(Text, nullable=False)
    llm_model: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = _created_at()


# ======================================================== 2.6 case_results


class CaseResult(Base):
    """执行结果。见 `04` §2.6。"""

    __tablename__ = "case_results"
    __table_args__ = (
        # ⚠️ **全系统最重要的一个约束**（`04` DM1）：幂等由数据库唯一约束
        # 保证，而非应用层"先查后写"——后者在并发下必然有竞态。
        UniqueConstraint("run_id", "case_id", name="uq_case_results_run_case"),
        CheckConstraint(
            "status IN ('passed','failed','timeout','error')", name="ck_case_results_status"
        ),
        Index("ix_case_results_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    #: `error` 指**请求层面**失败（连接被拒、DNS、TLS），它同样是测试结论的一部分。
    #: 平台自身故障记在 runs.failure_reason，**不混入测试结果**。
    status: Mapped[str] = mapped_column(Text, nullable=False)

    http_status: Mapped[int | None] = mapped_column(Integer)
    response_headers: Mapped[dict | None] = mapped_column(JSONB)
    response_body: Mapped[str | None] = mapped_column(Text)  # 截断后（DM6）
    response_body_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    response_body_bytes: Mapped[int | None] = mapped_column(BigInteger)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    error_kind: Mapped[str | None] = mapped_column(Text)

    #: 逐条断言的求值结果，三态 `passed` / `failed` / `error`（`TestExecutor` TE2）。
    #: 此处 `error` 指**断言无法求值**（如响应体非 JSON），与 status 列中的
    #: `error` 是两回事。`status` 的粒度不足以承载这个区分，而它决定了归因
    #: 能否分辨"服务缺陷"与"响应根本不是 JSON"。
    assertion_outcomes: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    request_digest: Mapped[str | None] = mapped_column(CHAR(64))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    executed_at: Mapped[datetime.datetime] = _created_at()


# ======================================================= 2.7 attributions


class Attribution(Base):
    """失败归因。见 `04` §2.7 / `03/LogAnalyzer.md` §3。"""

    __tablename__ = "attributions"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", name="uq_attributions_run_case"),
        # 11 类封闭枚举（LogAnalyzer §3）。**LLM 兜底只能在其中选择**——
        # 自由生成的类别会漂移成同义词，使按类别聚合的统计失效（LA2）。
        CheckConstraint(
            "category IN ('request_invalid','auth_error','resource_not_found','conflict',"
            "'rate_limited','server_error','response_schema_mismatch','assertion_mismatch',"
            "'network_error','blocked_by_policy','unknown')",
            name="ck_attributions_category",
        ),
        # `method` 是 M4（归因认可率）的分母来源：不区分规则与 LLM 两条路径，
        # 就无法回答"是规则不够还是模型不行"。
        CheckConstraint("method IN ('rule','llm')", name="ck_attributions_method"),
        CheckConstraint(
            "human_verdict IS NULL OR human_verdict IN ('agree','disagree')",
            name="ck_attributions_human_verdict",
        ),
        Index("ix_attributions_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    rule_ref: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)

    human_verdict: Mapped[str | None] = mapped_column(Text)
    #: 人工判定的**正确类别**。只记"同意/不同意"无法回答"该补哪条规则"——
    #: 这一列是规则库改进的直接数据来源（LA6）。
    human_category: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = _created_at()


# ============================================================== 2.8 reports


class Report(Base):
    """报告。见 `04` §2.8 / `03/ReportAgent.md`。"""

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    #: 结构化聚合数据，供前端与导出复用。
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: 渲染快照。**不随模板变更重新渲染**（RA4）——报告会被引用（评审材料、
    #: 问题工单），三个月前的报告应保持原样，而不是被追溯性地改写。
    rendered: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = _created_at()


# =========================================================== 2.9 audit_logs


class AuditLog(Base):
    """出站审计（SR-1 ~ SR-5）。见 `04` §2.9 / `03/Guardrail.md` §10。

    **`allow` 与 `deny` 都要记录**（GR6）：只记拒绝就无法回答
    "这个 Run 到底访问了哪些 host"——而那正是安全复盘最有价值的问题。

    **不记录**凭证、请求体、响应体正文（SR-6）。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("direction IN ('llm','target')", name="ck_audit_logs_direction"),
        CheckConstraint("decision IN ('allow','deny')", name="ck_audit_logs_decision"),
        CheckConstraint(
            "deny_reason IS NULL OR deny_reason IN "
            "('whitelist','metadata','private_ip','destructive','redirect','rate_limit')",
            name="ck_audit_logs_deny_reason",
        ),
        Index("ix_audit_logs_run", "run_id"),
        Index("ix_audit_logs_occurred_at", "occurred_at"),
    )

    #: 追加型日志，BIGSERIAL 更省空间且无需 ID 语义（`04` DM8）。
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: 可为空：非 Run 关联的出站（如启动期的连通性检查）也要记。
    #: 用 SET NULL 而非 CASCADE——Run 删除后，审计仍应保留"发生过"的事实。
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    occurred_at: Mapped[datetime.datetime] = _created_at()
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    target_host: Mapped[str] = mapped_column(Text, nullable=False)
    target_url: Mapped[str | None] = mapped_column(Text)  # 已剥离 query 中的敏感值
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    deny_reason: Mapped[str | None] = mapped_column(Text)
    request_digest: Mapped[str | None] = mapped_column(CHAR(64))
    trace_id: Mapped[str | None] = mapped_column(Text)


# ========================================================= 2.10 credentials


class Credential(Base):
    """目标 API 凭证（SR-6）。见 `04` §2.10。"""

    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint("name", "scope_host", name="uq_credentials_name_host"),
        CheckConstraint(
            "auth_type IN ('bearer','basic','api_key_header','api_key_query')",
            name="ck_credentials_auth_type",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: 凭证适用的 host（含端口语义见 `03/TestExecutor.md` §5）。
    scope_host: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(Text, nullable=False)
    #: Fernet 加密（SR-6）。密钥经环境变量注入，**不经由应用进程间传递**
    #: （`07` NF9）——防 /proc 与异常堆栈泄露。
    encrypted_value: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    #: 密钥轮换用。
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime.datetime] = _created_at()
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
