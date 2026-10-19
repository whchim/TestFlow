"""架构约束的静态检查。

对应决策：`08-工程规范.md` §2（EN2）、ADR-0002、ADR-0005、PRD F9。

**为什么这组约束需要机械强制，而不是靠约定**

它们都是"破坏时不会报错"的那类风险：

- 有人在某个模块里直接 `import httpx`，绕过了 Guardrail 的白名单与内网阻断
  → 代码能跑、测试全绿，直到某次恶意 Spec 被上传（ADR-0002）
- 有人在业务代码里直接调 LLM SDK，厂商差异渗进业务逻辑
  → 换模型时才发现要改几十处（ADR-0005）
- 有人让 CLI 直接 import 内部模块，绕过 Gateway 的统一脱敏
  → 本地跑得好好的（PRD F9）

**约定会被遗忘，构建失败不会。**

实现方式见 `08` §2.4：用自制 AST 检查而非 `import-linter`——约束本身很短，
写在测试里比写成工具配置更透明，读者能直接看到到底禁止了什么。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

# src/testflow 的绝对路径。parents[2] = 仓库根（tests/architecture/xxx.py）
PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "testflow"
PACKAGE_NAME = "testflow"


# --------------------------------------------------------------- 规则定义


@dataclass(frozen=True, slots=True)
class Rule:
    """一条依赖边界规则。"""

    name: str
    banned: frozenset[str]
    """禁止的**顶级**包名。`httpx.transport` 会被归到 `httpx`。"""

    allowed_in: frozenset[str]
    """允许使用它们的模块前缀（完全限定名）。其余模块一律禁止。"""

    why: str
    """违反时展示给开发者的理由——只说"禁止"没用，要说清"为什么"。"""


HTTP_CLIENTS = frozenset(
    {
        "httpx",
        "requests",
        "aiohttp",
        "urllib3",
        "httpcore",
        # M1 补充：`openai` 3.x 依赖的是 **httpx2**（一个独立包，内含 httpcore2），
        # 而不是 `httpx`。它同样能发起出站请求，因此必须同等禁止——否则
        # "Guardrail 是唯一出站通道" 就多了一条没人看着的路径。
        # （这个洞是实测发现的：`openai 3.29.0` 的依赖声明是 `httpx2<3,>=2.12.0`。）
        "httpx2",
        "httpcore2",
    }
)
LLM_SDKS = frozenset({"openai", "dashscope", "anthropic", "langchain_openai"})

RULES: tuple[Rule, ...] = (
    Rule(
        name="服务端出站必须经 Guardrail",
        banned=HTTP_CLIENTS,
        allowed_in=frozenset({"testflow.guardrail", "testflow.client"}),
        why=(
            "所有出站 HTTP 必须经 Guardrail 发出，否则白名单（SR-1）与内网阻断（SR-2）"
            "对该流量失效，且不会有任何测试报警。见 ADR-0002。\n"
            "  · guardrail/ —— 服务端唯一的出站通道\n"
            "  · client/    —— CLI 专用，只访问用户配置的单一 endpoint，不接收来自 Spec 的 URL\n"
            "注意 `httpx2`/`httpcore2` 也在名单内：它们是独立于 httpx 的另一套实现，"
            "同样能出站。漏掉它们等于名单上有个洞。"
        ),
    ),
    Rule(
        name="LLM SDK 只在 llm/ 中",
        banned=LLM_SDKS,
        allowed_in=frozenset({"testflow.llm"}),
        why=(
            "业务代码不得直接依赖 LLM SDK，否则厂商差异（如结构化输出的参数名）会渗进"
            "业务逻辑，换模型时改动面失控。见 ADR-0005。\n"
            "  注意：llm/ 虽允许 import SDK，但其构造的客户端**必须注入 Guardrail 的"
            " http_client**——那条约束静态检查不到，由 tests/integration 的行为测试覆盖。"
        ),
    ),
)

#: `client/` 只允许被**进程入口点**引用，防止服务端代码借道它绕过 Guardrail。
#:
#: M1 把这个名单从「只有 CLI」扩到了「CLI + Worker」。理由：
#: Worker 需要调用 Gateway 的内部唤醒端点（`06` §6.1），而那是一次
#: **控制面**通信——它打的是我们自己、用配置里的固定地址，
#: 不是"代替用户访问外部服务"，因此不属于 Guardrail 的管辖范围。
#:
#: **扩展是有界的**，不是放宽：`client/` 里只有打固定地址的客户端，
#: 没有任何接收 URL 参数的方法。要再扩大范围，得先改这一行——
#: 而改一行需要写的理由，就是它的价值所在。
CLIENT_MODULE = "testflow.client"
CLIENT_IMPORTERS_ALLOWED = frozenset({"testflow.entrypoints.cli", "testflow.entrypoints.worker"})

#: CLI 能引用的包内模块（PRD F9）。
#:
#: F9 要求 CLI **不重复实现业务逻辑**：它只把 HTTP 请求发出去、把响应读回来。
#: 而这条约束有一个很自然的破坏方式——"顺手 import 一下 `SpecParser`
#: 在本地先看看文档合不合法"。那看起来是个贴心的优化，实际制造了
#: **第二份解释器**：CLI 跑在用户的机器上，于是"这份文档会被怎么理解"
#: 有了两个实现，而两者不一致的部分表现为"CLI 说没问题，平台说解析失败"。
#:
#: **`testflow` 本身必须是精确匹配**（不能写成前缀）：一旦写成前缀，
#: 它会把 `testflow.storage`、`testflow.graph` 等等全部放进来——
#: 而那条规则的值恰好在于那些前缀是**被排除**的。CLI 需要它的唯一理由
#: 是 `from testflow import __version__`。
CLI_MODULE = "testflow.entrypoints.cli"
CLI_ALLOWED_EXACT = frozenset({"testflow"})
CLI_ALLOWED_PREFIXES = ("testflow.client", "testflow.domain")


# --------------------------------------------------------------- 检查实现


@dataclass(frozen=True, slots=True)
class Violation:
    module: str
    line: int
    imported: str
    rule: str
    why: str

    def __str__(self) -> str:
        return (
            f"{self.module}:{self.line} 导入了 {self.imported!r}\n  违反：{self.rule}\n  {self.why}"
        )


def iter_local_modules() -> Iterator[tuple[str, Path]]:
    """遍历 `src/testflow/` 下的全部 Python 模块，产出 (完全限定名, 路径)。"""
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        parts = list(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        yield ".".join(parts), path


def top_level_imports(source: str) -> Iterator[tuple[int, str]]:
    """产出 (行号, 顶级包名)。相对导入与本地 `testflow.*` 不算外部依赖。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                if name != PACKAGE_NAME:
                    yield node.lineno, name
        elif isinstance(node, ast.ImportFrom):
            # level > 0 表示相对导入（from . import x），不构成外部依赖
            if node.level or not node.module:
                continue
            name = node.module.split(".")[0]
            if name != PACKAGE_NAME:
                yield node.lineno, name


def imported_modules(source: str) -> Iterator[tuple[int, str]]:
    """产出 (行号, 绝对模块名)，用于检查包内引用关系。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            yield node.lineno, node.module


def find_rule_violations(module: str, source: str) -> list[Violation]:
    """按 `RULES` 检查一个模块的源码。**与文件系统无关**，便于对合成样本测试。"""
    found: list[Violation] = []
    for line, name in top_level_imports(source):
        for rule in RULES:
            if name not in rule.banned:
                continue
            if any(
                module.startswith(f"{prefix}.") or module == prefix for prefix in rule.allowed_in
            ):
                continue
            found.append(Violation(module, line, name, rule.name, rule.why))
    return found


def find_client_violations(module: str, source: str) -> list[Violation]:
    """检查 `testflow.client` 只被 CLI 引用。"""
    if module in CLIENT_IMPORTERS_ALLOWED or module == CLIENT_MODULE:
        return []
    found: list[Violation] = []
    for line, name in imported_modules(source):
        if name == CLIENT_MODULE or name.startswith(f"{CLIENT_MODULE}."):
            found.append(
                Violation(
                    module,
                    line,
                    name,
                    "client/ 只允许被进程入口点引用",
                    "client/ 是不经过 Guardrail 的 HTTP 客户端（用于 CLI 与控制面）。"
                    "若服务端代码引用它，就多了一条不受校验的出站路径。"
                    "见 PRD F9 / ADR-0002。",
                )
            )
    return found


def find_cli_violations(module: str, source: str) -> list[Violation]:
    """检查 CLI 只引用 `testflow.client` / `testflow.domain`（PRD F9）。"""
    if module != CLI_MODULE:
        return []
    found: list[Violation] = []
    for line, name in imported_modules(source):
        if not name.startswith(PACKAGE_NAME):
            continue
        if name in CLI_ALLOWED_EXACT:
            continue
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in CLI_ALLOWED_PREFIXES):
            continue
        found.append(
            Violation(
                module,
                line,
                name,
                "CLI 只能引用 client/ 与 domain/",
                "CLI 是 Gateway 的**纯 HTTP 客户端**（PRD F9）。引用 graph/、"
                "execution/、storage/ 会让它获得「在本地把业务逻辑再实现一遍」的"
                "能力——而那正是 F9 要防的：同一个问题（这份文档怎么解释）"
                "一旦有两个实现，不一致的部分就表现为"
                "「CLI 说没问题，平台说解析失败」。",
            )
        )
    return found


# ------------------------------------------------------------------ 测试


def test_all_modules_respect_dependency_boundaries() -> None:
    """全量扫描 `src/testflow/`，一处违规都不允许。"""
    violations: list[Violation] = []
    for module, path in iter_local_modules():
        source = path.read_text(encoding="utf-8")
        violations.extend(find_rule_violations(module, source))
        violations.extend(find_client_violations(module, source))
        violations.extend(find_cli_violations(module, source))

    assert not violations, (
        f"发现 {len(violations)} 处架构违规：\n\n"
        + "\n\n".join(str(v) for v in violations)
        + "\n\n这些约束不是风格偏好，是安全与可维护性的前提。"
    )


def test_checker_scans_every_module() -> None:
    """防止检查器因路径写错而"空跑通过"。"""
    modules = [name for name, _ in iter_local_modules()]
    assert "testflow.guardrail.policy" in modules
    assert len(modules) > 10, f"只扫到 {len(modules)} 个模块，路径可能不对：{modules}"


# ---- 以下两个测试验证"检查器本身有效"。没有它们，上面那个测试可能因为
# ---- 检查器写错而永远通过——而那正是这组约束最危险的失效方式。


@pytest.mark.parametrize(
    ("source", "should_flag"),
    [
        ("import httpx\n", True),
        ("import httpx as hx\n", True),
        ("from httpx import Client\n", True),
        ("from httpx.transport import X\n", True),
        ("import httpx2\n", True),
        ("from httpx2 import AsyncClient\n", True),
        ("import httpcore2\n", True),
        ("import requests\n", True),
        ("import aiohttp\n", True),
        ("from openai import AsyncOpenAI\n", True),
        ("import dashscope\n", True),
        # 不算违规：
        ("import json\n", False),
        ("from collections import abc\n", False),
        ("from testflow.guardrail import policy\n", False),  # 包内引用不是外部依赖
        ("from . import policy\n", False),  # 相对导入
        ("from ..guardrail import async_client\n", False),
    ],
)
def test_checker_detects_banned_imports(source: str, should_flag: bool) -> None:
    # 用 agents 包（不在任何 allowed_in 中）作为"违规位置"的样本
    violations = find_rule_violations("testflow.agents.test_case_gen", source)
    assert bool(violations) is should_flag, f"对 {source!r} 的判定与预期不符"


def test_checker_allows_guardrail_itself() -> None:
    """guardrail/ 自己 import httpx / httpx2 是**设计意图**，不能被误报。

    `httpx2` 这条尤其重要：LLM 出站要交给 `openai` 的 `http_client=`，
    而 `openai` 3.x 要的正是一个 `httpx2.AsyncClient`。若这里误报，
    实现者会被迫把出站代码挪到 guardrail/ 之外——正好是本规则要禁止的事。
    """
    assert not find_rule_violations("testflow.guardrail.sync_client", "import httpx\n")
    assert not find_rule_violations("testflow.guardrail.async_client", "import httpx2\n")
    assert not find_rule_violations("testflow.llm.qwen", "from openai import AsyncOpenAI\n")


def test_llm_module_may_not_import_http_clients() -> None:
    """`llm/` 可以 import LLM SDK，但**不能** import 任何 HTTP 客户端。

    这条区分很关键：SDK 允许（D7 把厂商差异收敛在此），HTTP 客户端不允许——
    客户端必须由 Guardrail 注入，否则 LLM 流量绕开 ADR-0002。

    也是为什么 `qwen.py` 接收的 `http_client` 参数类型是 `Any` 而非具体类型：
    一旦写成 `httpx.AsyncClient`，这个文件就得 import httpx，本测试立刻变红。
    """
    assert find_rule_violations("testflow.llm.qwen", "import httpx\n")
    assert find_rule_violations("testflow.llm.qwen", "import httpx2\n")


def test_checker_flags_server_module_importing_client() -> None:
    """服务端模块借道 client/ 会形成第二条出站路径。"""
    violations = find_client_violations(
        "testflow.agents.test_case_gen", "from testflow.client import api\n"
    )
    assert len(violations) == 1
    assert "只允许被进程入口点引用" in violations[0].rule


def test_checker_allows_cli_importing_client() -> None:
    assert not find_client_violations(
        "testflow.entrypoints.cli", "from testflow.client import api\n"
    )


class TestCliImportWhitelist:
    """PRD F9：CLI 不重复实现业务逻辑（EN2 / ADR-0005 的同类约束）。"""

    def test_the_real_cli_stays_within_the_whitelist(self) -> None:
        """对**真实文件**跑一遍——上面那条全量扫描也会覆盖它，
        但单独一条能让失败信息直指"CLI 越界了"而不是淹没在全量报告里。"""
        source = (PACKAGE_ROOT / "entrypoints" / "cli.py").read_text(encoding="utf-8")
        assert not find_cli_violations(CLI_MODULE, source)

    @pytest.mark.parametrize(
        ("source", "should_flag"),
        [
            # 白名单内：
            ("from testflow.client import api\n", False),
            ("from testflow.client.api import GatewayApiClient\n", False),
            ("from testflow.domain.graph import RunPhase\n", False),
            ("from testflow import __version__\n", False),
            # 越界：
            ("from testflow.storage.repositories import runs\n", True),
            ("from testflow.graph import runner\n", True),
            ("from testflow.agents.spec_parser import parse_spec\n", True),
            ("from testflow.execution.runner import run_case\n", True),
            ("import testflow.config\n", True),
            # 连**别的入口点**也不行：`from testflow.entrypoints.gateway import
            # create_app` 会把整个服务端应用拖进一个只想发 HTTP 请求的进程。
            ("from testflow.entrypoints.gateway import create_app\n", True),
        ],
    )
    def test_the_checker_flags_what_it_should(self, source: str, should_flag: bool) -> None:
        violations = find_cli_violations(CLI_MODULE, source)
        assert bool(violations) is should_flag, f"对 {source!r} 的判定与预期不符"

    def test_the_bare_package_match_is_exact_not_a_prefix(self) -> None:
        """★ 这条守的是**检查器自己**的一个陷阱。

        `testflow` 若按前缀匹配，`testflow.storage` 之类会全部通过——
        而这条规则的**全部价值**就在于那些前缀是被排除的。
        一个"看起来在检查、实际什么都放行"的检查器，比没有检查器更糟。
        """
        assert not find_cli_violations(CLI_MODULE, "from testflow import __version__\n")
        assert find_cli_violations(CLI_MODULE, "from testflow.storage import models\n")
