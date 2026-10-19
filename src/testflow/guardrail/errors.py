"""Guardrail 的异常与拒绝结果。

对应决策：`03-模块详细设计/Guardrail.md` §8（拒绝后的行为）。

**拒绝不是"异常"，是"结果"。**

这条区分很重要。一个被策略拦下的请求，对**平台**来说是正常的（策略按预期工作了），
只是对**用例**来说是"没能执行"。所以它不该以异常的形式向上冒泡——
异常会被上层的通用错误处理捕获、记成"平台故障"，而实际上这是我们自己的判断。

它应当被表示为一个**返回值**，由执行器翻译成
`case_results.status = 'error'` + `error_kind = 'guardrail_denied'`（§8.1），
进而由 M3 的规则归因判成 `blocked_by_policy`（而非"服务端错误"）。

`GuardrailError` 保留给**平台自身**的故障（配置缺失、限流器状态损坏等），
那类才该中断任务。
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass

from testflow.guardrail.policy import DenyReason

__all__ = [
    "GuardrailDenied",
    "GuardrailError",
    "GuardrailTimeout",
    "GuardrailTransportError",
    "PeerVerificationError",
    "classify_transport_error",
    "translate_transport_error",
]


@dataclass(frozen=True, slots=True)
class GuardrailDenied:
    """请求被策略拒绝。**这是一个正常的结果，不是异常。**

    Attributes:
        reason: 对应 `audit_logs.deny_reason` 的取值（`04` §2.9 有 CHECK 约束）。
        detail: 给人看的说明。**不得含凭证或请求正文**（SR-6）。
    """

    reason: DenyReason
    detail: str = ""

    @property
    def error_kind(self) -> str:
        """落库时写进 `case_results.error_kind` 的值。

        固定为 `guardrail_denied`，而**不是**把 `reason` 直接写进去：
        `error_kind` 是"请求层面发生了什么"，而"被策略拦下"这一件事
        对应了六种不同的原因。把原因放 `detail`，把类别留在这里。
        """
        return "guardrail_denied"


class GuardrailError(Exception):
    """**平台自身的**故障——不是"请求被拒绝"。

    例如：白名单没配、限流器状态不可用、传输层配置错误。
    这类问题会中断任务，因为它们意味着 Guardrail 没有正常工作，
    而"Guardrail 没正常工作"时继续发请求是不可接受的。
    """


class GuardrailTransportError(GuardrailError):
    """请求**发出去了但没成功**：连接被拒、DNS 失败、TLS 失败、超时。

    ## 为什么 Guardrail 要暴露自己的异常类型，而不是让 httpx 的漏出去

    `execution/`（执行器）需要知道"这次失败是超时还是 DNS 问题"，
    因为它要把结论写进 `case_results.error_kind`（`04` §2.6）。
    若它直接 `except httpx.ConnectError`，它就得 import httpx——

    **而架构规则禁止 `execution/` 里出现任何 HTTP 客户端。**
    `tests/architecture/` 会拦下这个 import（实测确实拦下了，
    这正是这条规则的价值：它在写代码的当下就把依赖方向掰了回来）。

    于是正确的方向是：**httpx 的异常在 `guardrail/` 内部就被吸收，
    对外只暴露本项目自己的类型。** 调用方依赖 `guardrail`，
    而不是依赖 `guardrail` 恰好用了哪个 HTTP 库。

    附带的好处：换个 HTTP 库时，改动被限制在 `guardrail/` 内部。
    """

    #: `04` §2.6 里 `error_kind` 的取值。子类各自覆盖。
    error_kind: str = "transport_error"


class GuardrailTimeout(GuardrailTransportError):
    """请求超时。

    **注意它与 `GuardrailDenied` 的区别**：超时是"目标在时限内没回应"，
    是**测试结果**（TE7），会被记成 `timeout`；而被拒绝是我们主动不发，
    记成 `error`。
    """

    error_kind = "timeout"


#: 从异常（或其 `__cause__` 链）里能识别出的具体原因。
#:
#: 按**异常类型**而不是错误消息匹配：消息文本随库版本、随操作系统语言而变
#: ——在中文 Windows 上错误消息是中文的，匹配英文关键词会全部落空，
#: 于是所有网络错误被归成同一个笼统的类别。
_TRANSPORT_CAUSES: tuple[tuple[type[BaseException], str], ...] = (
    (ssl.SSLError, "tls_error"),
    (socket.gaierror, "dns_failure"),
    (ConnectionRefusedError, "connection_refused"),
    (ConnectionResetError, "connection_reset"),
    (TimeoutError, "timeout"),
)


def classify_transport_error(exc: BaseException) -> str:
    """把一个传输层异常归成一个可查询的 `error_kind`。

    顺着 `__cause__` / `__context__` 链找原始异常类型——`httpcore` 会把
    底层异常包一层再抛出来，直接看最外层只能看到一个笼统的"连接错误"。

    归不出来时用异常类名——**不猜**。一个笼统但真实的类别，
    好过一个看起来很确定但可能是错的类别：后者会让归因把一个网络问题
    指向一个不存在的原因，而排查会从那里开始。
    """
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for exc_type, kind in _TRANSPORT_CAUSES:
            if isinstance(current, exc_type):
                return kind
        current = current.__cause__ or current.__context__

    return f"transport_{type(exc).__name__.lower()}"


def translate_transport_error(exc: BaseException) -> GuardrailTransportError:
    """把一个**已确认为传输故障**的异常归成本项目的类型。

    "翻译"分两步，而只有第二步是这里的事：判断"这个异常是不是传输故障"
    由调用方（各自的传输层）负责——因为**两形态用的库不同**
    （同步路径是 `httpcore`/`httpx`，LLM 路径是 `httpcore2`/`httpx2`），
    只有它们自己知道该认哪些类型。而"认出来之后叫它什么"两边必须一致，
    所以那一步收敛在这里。

    这个函数放在 `errors.py` 而不是 `transport.py` 里，是因为**异步路径也要用它**。
    第一版它叫 `transport._translate`，于是异步路径要么 import 一个别的模块的
    私有函数，要么自己再写一份——而后者会让两条路径对同一类失败给出不同的
    `error_kind`，排查时表现为"同一个网络问题在目标路径和 LLM 路径上叫两个名字"。

    `GuardrailTimeout` 与 `GuardrailTransportError` 的区别是**可重试性**：
    超时重试会让最坏耗时变成乘积（见 `llm/qwen.py` 的 `max_retries=0` 注释），
    而连接被拒重试一次是合理的。所以这个区分必须准，不能含糊成"都是网络错误"。
    """
    kind = classify_transport_error(exc)
    if kind == "timeout":
        return GuardrailTimeout(str(exc))
    error = GuardrailTransportError(str(exc))
    error.error_kind = kind
    return error


class PeerVerificationError(GuardrailError):
    """连接建立后，对端地址不是我们要求的那一个。

    **属于 `GuardrailError` 而不是传输错误**：它不是"目标连不上"，
    而是"我们连上了一个不该连的地方"——防线自己报警了。

    因此执行器**不该**把它记成 `error_kind = network_error`：
    那会让报告说"被测系统有问题"，而实际上出问题的是我们与它之间的路径
    （透明代理、连接劫持）。

    定义在 `errors.py` 而不是 `transport.py`，是为了让调用方能按类型捕获它，
    而不必依赖传输层的具体实现。
    """
