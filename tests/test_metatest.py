"""测试套件的自检（meta test）：用测试来守护测试本身。

为什么需要这个文件：本项目的测试套件曾经有三类"看起来绿、其实不可信"的问题——

1. **同名用例被静默覆盖**：文件里 `def test_` 出现 99 次，pytest 只收集 98 条。
   Python 的模块命名空间后定义者胜出，第一份定义永远不会执行。
   而这个差值当时就写在 commit message 的"108 passed"里，没人发现。
2. **环境不密封**：`monkeypatch.delenv` 删掉的 Key 会被 `load_dotenv()`
   在模块重导入时灌回来，导致本地"全绿"靠的是开发者 `.env` 里的真实 Key
   （并且真的产生了 API 费用），而在 CI 的干净检出上必然失败。
3. **名不副实的用例**：有的用例名字写着"验证 /ask 超时"，实际一行 server
   代码都没碰。这类用例比"没有用例"更危险 —— 它制造了虚假的覆盖率。

因此这里把"元规则"变成可执行的断言：以后谁再写坏，CI 会直接红。
"""
import ast
import collections
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent


def _iter_test_files() -> list[Path]:
    return sorted(p for p in TESTS_DIR.glob("test_*.py"))


def _defined_tests(path: Path) -> list[str]:
    """用 AST 取文件里定义的所有 test_ 函数名（不依赖导入，不受遮蔽影响）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                names.append(node.name)
    return names


# ---------- 规则 1：不允许同名用例 ----------

def test_no_duplicate_test_names():
    """同名 test_ 函数会被 Python 静默覆盖，这一条把它变成硬失败。"""
    duplicates = {}
    for path in _iter_test_files():
        counter = collections.Counter(_defined_tests(path))
        dupes = {name: n for name, n in counter.items() if n > 1}
        if dupes:
            duplicates[path.name] = dupes
    assert duplicates == {}, (
        f"存在同名用例，后定义的会覆盖先定义的、前者永不执行：{duplicates}"
    )


def test_collected_tests_match_defined_tests(request):
    """每个用例文件里定义了什么，pytest 就必须真的收集到什么。

    这条覆盖"定义存在但从未被收集"的情形（例如被同名函数遮蔽、
    或者函数被放进类里但没有 `self`、或者被 `__test__ = False` 之类忽略）。

    注意作用域：只校验**本次真的被收集到的文件**。因为需要支持
    `pytest tests/test_metatest.py` 这类"只跑一个文件"的子集运行 ——
    若拿全集的定义去比对子集的收集结果，任何子集运行都会误报一堆"缺失"，
    门禁就变成了噪声（没人会用的门禁等于没有）。全集运行时，
    collected 覆盖所有文件，强度不变。
    """
    collected = set()
    for item in request.session.items:
        node = item.nodeid
        if "::" not in node:
            continue
        fname = Path(node.split("::")[0]).name
        func = node.split("::")[-1].split("[")[0]  # 去掉参数化后缀
        collected.add((fname, func))

    collected_files = {fname for fname, _ in collected}
    defined = set()
    for path in _iter_test_files():
        if path.name not in collected_files:
            continue  # 本次未运行该文件（子集运行），不参与比对
        defined.update((path.name, name) for name in _defined_tests(path))

    missing = defined - collected
    assert missing == set(), f"以下用例定义了但没有被 pytest 收集：{sorted(missing)}"


# ---------- 规则 2：环境必须密封（.env 与真实网络都进不来） ----------

def test_dotenv_is_neutralized_during_tests():
    """`load_dotenv()` 在测试期间必须是空操作。

    这是对历史 P0 的直接守护：只要它还生效，任何一次模块重导入都可能把开发者
    本机 `.env` 里的真实 API Key 灌回 `os.environ`，让"隔离 fixture"形同虚设。
    这里**显式调用一次** `load_dotenv()`（指向一个真的存在、且含哨兵键的临时
    `.env`），断言哨兵键没有被写进环境。
    """
    import dotenv

    assert getattr(dotenv.load_dotenv, "_pytest_noop", False) is True, (
        "dotenv.load_dotenv 没有被 conftest 的 _hermetic_dotenv 替换掉"
    )

    env_file = TESTS_DIR / ".env.sentinel"
    sentinel = "PRAY_TEST_DOTENV_SENTINEL"
    os.environ.pop(sentinel, None)
    env_file.write_text(f"{sentinel}=leaked\n", encoding="utf-8")
    try:
        dotenv.load_dotenv(env_file)  # 显式调用，应当无效
        assert os.getenv(sentinel) is None, "load_dotenv 在测试中生效了，环境不密封"
    finally:
        env_file.unlink(missing_ok=True)


def test_real_api_key_cannot_leak_into_tests():
    """测试期间模型凭据必须是桩值——不允许出现"看似配置了真实 Key"的状态。

    历史故障：`_clean_env` 删掉 Key 之后，`config` 被重新导入时执行了
    `load_dotenv()`，把开发者本机 `.env` 的真实 Key 又灌回来，
    于是被测代码会去真实调用付费 API。
    """
    from config import DEEPSEEK_API_KEY, OPENAI_API_KEY

    for name, value in (("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY),
                        ("OPENAI_API_KEY", OPENAI_API_KEY)):
        assert value == "" or value.startswith("sk-test"), (
            f"{name} 不是测试桩值（疑似读到了真实凭据）：{value[:6]}…"
        )


def test_model_base_url_points_at_localhost():
    """base_url 必须指向本机（桩服务或必然连不上的端口），不能是真实厂商域名。"""
    from config import LLM_BASE_URL

    assert LLM_BASE_URL, "LLM_BASE_URL 未设置，无法保证不外呼真实网关"
    host = urlsplit(LLM_BASE_URL).hostname or ""
    assert host in ("127.0.0.1", "localhost", "::1"), (
        f"base_url 指向了非本机地址：{LLM_BASE_URL}"
    )


def test_network_guard_blocks_non_loopback():
    """非回环地址的连出必须被拦下，而不是"悄悄访问了外网"。

    这条把 README 里"测试不依赖真实网络"的承诺变成可执行断言：
    以前它只是文档上的一句话，而真实情况是有一条用例会去调付费接口。
    """
    with pytest.raises(RuntimeError) as excinfo:
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    assert "真实网络" in str(excinfo.value)


def test_network_guard_allows_loopback():
    """回环地址必须放行（否则所有起真端口的 HTTP 测试都会失效）。

    连一个必然没人监听的回环端口：应当得到连接被拒绝（或成功），
    而不是被守卫拦下的 RuntimeError。
    """
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    except RuntimeError as exc:  # 守卫误伤
        pytest.fail(f"回环地址被网络守卫误拦：{exc}")
    except OSError:
        pass  # 连接被拒/超时都说明"放行了，只是没人监听"


@pytest.mark.allow_network
def test_allow_network_marker_actually_opens_the_gate():
    """`@pytest.mark.allow_network` 必须真的能放行，而不是个假开关。

    历史问题：这个标记只出现在文档字符串里，**代码从未读取它** ——
    加不加行为完全一样。对使用者来说，"以为能放行、实际照旧被拦"比
    "明确不支持"更糟：他会先怀疑自己的用例写错了。

    这里连一个非回环地址但**必然失败**的目标（TEST-NET-1，保留给文档示例，
    不可路由），断言得到的是**连接错误**而不是守卫的 RuntimeError ——
    从而证明"守卫确实放行了"且"不需要真的访问到外网"。
    """
    try:
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    except RuntimeError as exc:
        pytest.fail(f"allow_network 标记没有生效，仍被守卫拦截：{exc}")
    except OSError:
        pass  # 超时/不可达，说明守卫已放行


def test_proxy_is_neutralized_during_tests():
    """代理解析必须被中和 —— 否则整套测试会随开发者机器上的代理软件变红/变绿。

    这是继 `.env` 之后的第二条外呼暗路，而且更隐蔽：
    `urllib.request.getproxies()` 在 Windows 上会**回退读注册表**里的系统代理，
    因此"把 `os.environ` 里的代理变量清干净"根本挡不住它 ——
    httpx 的 `get_environment_proxies()` 正是建立在它之上。

    实测症状：本机装了 7890 端口这类代理工具时，连指向 127.0.0.1 的本地假模型
    服务都会被绕出去并拿到 502，失败信息看起来完全像产品缺陷。
    """
    import urllib.request

    assert urllib.request.getproxies() == {}, (
        "urllib 的代理未被中和：测试结果会受本机系统代理影响"
    )
    try:
        import httpx
    except ImportError:  # httpx 未安装则无从谈起
        return
    assert httpx._utils.get_environment_proxies() == {}, (
        "httpx 的代理未被中和：模型请求会被本机代理接管（典型症状是 502）"
    )


def test_handle_release_fixture_exists():
    """句柄回收夹具必须存在，否则 ResourceWarning 会散落成噪声。"""
    import conftest

    assert hasattr(conftest, "_release_handles"), (
        "conftest 缺少 _release_handles 夹具：句柄泄漏会变成随机噪声"
    )


# ---------- 规则 3：用例必须真的碰到了被测对象 ----------

def test_timeout_tests_actually_touch_their_target_module():
    """守护"自证式用例"不再出现。

    历史用例 `test_ask_timeout_wait_returns_504_semantics` 里 `import server`
    之后从未使用过 server 的任何符号 —— 它只是重新实现了一遍
    `threading.Event.wait` 的语义再断言它，等于"自己出题自己答"。

    这里用 AST 检查：凡是导入了 server 模块、且用例名含 timeout 的用例，
    必须真的访问过 server 的某个成员（属性或常量）。
    注意"导入了 server"只看真正的 Import 节点 —— 不看文档字符串里的文字，
    否则描述这条规则本身的用例会把自己误伤。
    """
    offenders = []
    for path in _iter_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "timeout" not in node.name:
                continue

            imported = any(
                "server" in ast.unparse(child)
                for child in ast.walk(node)
                if isinstance(child, (ast.Import, ast.ImportFrom))
            )
            if not imported:
                continue

            body = "\n".join(
                ast.unparse(stmt) for stmt in node.body
                if not isinstance(stmt, (ast.Import, ast.ImportFrom))
            )
            if "server_mod" not in body and not re.search(r"\bserver\s*\.", body):
                offenders.append(f"{path.name}::{node.name}")
    assert offenders == [], (
        f"以下用例导入了 server 却从未访问它的任何成员（典型的自证式用例）：{offenders}"
    )


# ---------- 规则 4：目录隔离必须真的把测试副作用挡在仓库之外 ----------

def test_test_working_dirs_are_outside_the_repo():
    """所有由夹具注入的工作目录都必须落在仓库之外。

    这条比"仓库里没多出目录"更精确：它不依赖运行时机，也不受开发者
    本地是否跑过应用影响，直接约束不变量本身。
    """
    from config import DOCS_DIR, INDEX_DIR, WRITE_DIR

    inside = []
    candidates = {
        "DOCS_DIR": DOCS_DIR,
        "INDEX_DIR": INDEX_DIR,
        "WRITE_DIR": WRITE_DIR,
        "MEMORY_DB": os.environ.get("MEMORY_DB", ""),
        "GLOBAL_MEMORY_DB": os.environ.get("GLOBAL_MEMORY_DB", ""),
        "LOG_DIR": os.environ.get("LOG_DIR", ""),
    }
    for name, value in candidates.items():
        assert str(value), f"{name} 没有被夹具隔离"
        if Path(str(value)).resolve().is_relative_to(REPO_ROOT):
            inside.append(f"{name}={value}")
    assert inside == [], f"以下测试目录落在了仓库内（会产生副作用）：{inside}"


def test_gitignore_covers_runtime_artifacts():
    """.gitignore 必须覆盖所有运行期产物，否则它们会被误提交。"""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    missing = [
        pattern for pattern in
        (".env", "generated/", "logs/", "index/", "data/", "models/", ".coverage")
        if pattern not in text
    ]
    assert missing == [], f".gitignore 缺少以下条目：{missing}"


def test_gitignore_has_no_personal_interview_material():
    """给面试官看的仓库里不应出现"面试准备材料"这类个人条目。

    历史问题：`.gitignore` 里写着 `# 个人面试准备材料（仅本地参考，禁止提交）`
    与 `resume-materials.md`。排除个人文件是对的，但**把这条留在仓库里**
    等于告诉阅览者"这里有面向面试的东西" —— 与项目本身无关的噪声。
    """
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    offenders = [kw for kw in ("面试", "resume", "简历") if kw in text]
    assert offenders == [], f".gitignore 含个人材料条目：{offenders}"


# ---------- 规则 5：工程配置的一致性（版本号 / 依赖分层 / CI 存在） ----------

def test_version_has_a_single_source():
    """版本号必须只有一个来源（VERSION 文件），三处不得各写各的。

    历史问题：`desktop.py`("1.1.0")、`pyproject.toml`(0.9.0) 与 Release tag
    三处独立维护，随时可能不一致 —— 而"当前是什么版本"不该有歧义。
    """
    import config

    version_file = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert version_file, "VERSION 文件为空"
    assert config.APP_VERSION == version_file, (
        f"config.APP_VERSION({config.APP_VERSION}) 与 VERSION({version_file}) 不一致"
    )

    # desktop.py 不允许再硬编码版本号字符串
    desktop_src = (REPO_ROOT / "desktop.py").read_text(encoding="utf-8")
    hardcoded = re.search(r'APP_VERSION\s*=\s*["\']\d', desktop_src)
    assert hardcoded is None, "desktop.py 仍在硬编码版本号，应改为从 config 导入"

    # pyproject 的版本必须动态引用 VERSION 文件，而不是另写一个值
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = { file = ["VERSION"] }' in pyproject, (
        "pyproject.toml 未把 version 指向 VERSION 文件（版本号仍有第二处来源）"
    )


def test_read_version_reads_file_and_falls_back(monkeypatch, tmp_path):
    """`config._read_version` 的两条路径都要成立：读到就用，读不到不能炸。"""
    import config

    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    # 文件不存在（裁剪/打包环境）→ 退回占位版本，而不是让 import 失败
    assert config._read_version() == "0.0.0"

    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    assert config._read_version() == "9.9.9", "应去掉首尾空白"


def test_runtime_requirements_exclude_test_tooling():
    """运行时依赖里不能混入测试框架（部署镜像不该带 pytest）。"""
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    code_lines = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
    offenders = [ln for ln in code_lines if ln.lower().startswith(("pytest", "ruff", "pip-audit"))]
    assert offenders == [], f"requirements.txt 混入了开发依赖：{offenders}"

    dev_text = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest" in dev_text, "requirements-dev.txt 应包含 pytest"


def test_ci_workflows_exist_and_are_valid_yaml():
    """CI 工作流必须存在、语法合法，且真的会跑 pytest 与依赖扫描。

    语法校验很值得：一个 YAML 缩进错误会让整个工作流静默不触发，
    而"挂在 README 上的绿 badge"就成了假的 —— 这正是 B1 要避免的事。
    """
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "没有任何 GitHub Actions 工作流"

    try:
        import yaml
    except ImportError:  # PyYAML 是 langchain 的传递依赖，正常应存在
        pytest.skip("PyYAML 不可用，跳过 YAML 语法校验")

    for path in workflows:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        assert any(isinstance(d, dict) and d for d in docs), f"{path.name} 无法解析出内容"

    ci_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pytest" in ci_text, "CI 未运行 pytest"
    assert "pip-audit" in ci_text, "CI 未做依赖漏洞扫描"
    assert "--cov" in ci_text, "CI 未采集覆盖率（覆盖率门禁无从生效）"


def test_infra_files_present():
    """仓库基础设施文件必须齐备（LICENSE 与 README 的声明要对得上）。"""
    missing = [
        name for name in ("LICENSE", "CHANGELOG.md", "Dockerfile", ".dockerignore", "VERSION")
        if not (REPO_ROOT / name).exists()
    ]
    assert missing == [], f"缺少基础设施文件：{missing}"

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "MIT" in readme, "README 未声明 MIT 许可"
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT" in license_text, "LICENSE 文件不是 MIT"
