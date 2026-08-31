"""Agent 工具集：通过 LangChain @tool 定义，供 ReAct Agent 自主调用。

- search_documents: 检索本地文档索引（RAG）
- web_search: 联网搜索实时信息（天气/新闻/事实）
- get_weather: 查询指定城市实时天气
- write_file: 把内容写入本地文件（限制在 WRITE_DIR 目录内）
"""
from pathlib import Path

from langchain_core.tools import tool

from config import BOCHA_API_KEY, TOP_K
from retriever import search as _search_docs


@tool
def write_file(file_path: str, content: str) -> str:
    """把代码/内容写入本地文件（代码落盘）。

    【重要行为准则】当用户让你写代码、生成脚本、创建程序/游戏/工具、或产出
    任何可保存的文件内容时，**应当主动调用本工具落盘**——即使用户没有明确说
    "保存到文件"。判断标准：生成的内容属于"可独立保存的文件"（完整程序、
    脚本、配置文件、文档等）就写入；纯对话性回答（几句话、解释、建议）不写。
    这是你的默认职责，像开发者一样把产物落到磁盘，并在回答里告诉用户文件
    已保存到哪个路径。

    安全边界：只允许写入 WRITE_DIR 目录内（相对或绝对路径均可，但不得逃逸
    出该目录，如 ../ 会被拒绝）；父目录不存在时自动创建。文件名由你根据
    内容合理命名（如猜数字游戏 → guess_game.py）。

    Args:
        file_path: 目标文件路径（如 "guess_game.py" 或 "scripts/guess_game.py"）
        content: 要写入的文件完整内容
    """
    from config import WRITE_DIR

    write_root = Path(WRITE_DIR).resolve()
    target = (write_root / file_path).resolve()
    # 安全校验：目标路径必须位于 WRITE_DIR 内（防 ../ 逃逸）
    if not target.is_relative_to(write_root):
        return f"拒绝写入：路径 {file_path} 超出允许目录 {write_root}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入 {target.relative_to(write_root)}（{len(content)} 字符，{len(content.splitlines())} 行）"
    except OSError as exc:
        return f"写入失败: {exc}"


def _bocha_search(query: str, max_results: int = 5) -> list[dict]:
    """博查搜索（国内，中文质量高，Agent 专用）。"""
    import json as _json
    import urllib.request

    payload = _json.dumps(
        {"query": query, "freshness": "noLimit", "summary": True, "count": max_results}
    ).encode()
    req = urllib.request.Request(
        "https://api.bochaai.com/v1/web-search",
        data=payload,
        headers={
            "Authorization": f"Bearer {BOCHA_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=15)
    data = _json.loads(resp.read().decode("utf-8"))
    # 博查响应结构: {code, data: {webPages: {value: [...]}}}
    values = data.get("data", {}).get("webPages", {}).get("value", []) or []
    results = []
    for v in values:
        results.append(
            {
                "title": v.get("name", ""),
                "body": v.get("summary") or v.get("snippet", ""),
                "href": v.get("url", ""),
            }
        )
    return results

def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """DuckDuckGo 搜索（英文较好，中文一般）。"""
    from duckduckgo_search import DDGS

    with DDGS(timeout=10) as ddgs:
        raw = list(ddgs.text(query, region="cn-zh", max_results=max_results))
    if not raw:
        with DDGS(timeout=10) as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
    results = []
    for r in raw:
        results.append(
            {"title": r.get("title", ""), "body": r.get("body", ""), "href": r.get("href", "")}
        )
    return results


@tool
def search_documents(query: str) -> str:
    """在本地文档知识库中检索与 query 相关的内容。

    当用户问题涉及项目文档、技术架构、部署配置、README 内容时调用。
    返回检索到的文档片段（含来源），按相关度从高到低排列。
    """
    try:
        hits = _search_docs(query, top_k=TOP_K)
    except Exception as exc:  # noqa: BLE001
        return f"文档检索失败: {exc}"
    if not hits:
        return "没有在文档知识库中找到相关内容。"
    # 带编号的来源列表，指示模型回答时引用来源编号
    parts = ["以下为检索到的文档片段（回答时请用 [1][2]... 标注来源）："]
    for i, h in enumerate(hits, 1):
        parts.append(f"[{i}] 来源: {h['source']} | 相关度: {h['score']:.3f}\n{h['chunk']}")
    return "\n\n".join(parts)


@tool
def web_search(query: str) -> str:
    """联网搜索实时信息（天气、新闻、最新事件、事实查询等）。

    当用户问题需要当前时间/实时数据/最新信息，或本地文档无法回答时调用。
    返回搜索结果的标题与摘要。自动选择搜索引擎：配置了 Bocha 博查 API Key
    时优先用博查（中文搜索质量高），否则退回 DuckDuckGo。
    """
    results = []
    errors = []
    # 引擎1: 博查（配置了 Key 才用）
    if BOCHA_API_KEY:
        try:
            results = _bocha_search(query)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"博查: {exc}")
    # 引擎2: DuckDuckGo（博查失败或无 Key 时兜底）
    if not results:
        try:
            results = _ddg_search(query)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"DuckDuckGo: {exc}")
    if not results:
        detail = "；".join(errors) if errors else "无结果"
        return f"没有搜索到相关信息。（{detail}）"
    parts = ["以下为搜索到的网页结果（回答时请引用对应编号的链接作为来源）："]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        parts.append(f"[{i}] {title}\n   摘要: {body}\n   链接: {href}")
    return "\n".join(parts)


@tool
def get_weather(city: str) -> str:
    """查询指定城市的当前天气。

    当用户问某个城市的天气、气温、是否下雨等情况时调用。
    城市使用中文或拼音均可，例如 '北京' / 'beijing' / '上海'。
    返回天气描述、温度、风力等信息。
    """
    import urllib.parse
    import urllib.request

    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&lang=zh"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.read().decode("utf-8", errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        return f"天气查询失败: {exc}（请确认城市名，或稍后重试）"


# ---------- 命令执行工具（带安全防护） ----------

# 危险命令黑名单：任何包含这些模式的命令一律拒绝（无需确认）
# 覆盖：删除/格式化/关机/系统级破坏操作
_BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+[/~]",
    r"\bdel\s+/[sqf]\s+[a-z]:\\",
    r"\bformat\s+[a-z]:",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r"\bmkfs\b", r"\bdd\s+if=",
    r"rd\s+/s\s+[a-z]:\\",
    r"\brmdir\s+/s",
]

# 高危命令模式：需要用户在前端确认后才执行（confirmed=True）
# 覆盖：删除文件/目录、移动/重命名、安装包、联网下载、注册表、系统设置
_HIGH_RISK_PATTERNS = [
    r"\brm\b", r"\bdel\b", r"\bremove\b", r"\brmdir\b", r"\brmdir\b",
    r"\bmove\b", r"\bren\b", r"\brename\b", r"\bcopy\b", r"\bxcopy\b", r"\brobocopy\b",
    r"\bpip\s+install\b", r"\bnpm\s+install\b", r"\bconda\s+install\b",
    r"\bcurl\b", r"\bwget\b", r"\bInvoke-WebRequest\b",
    r"\breg\s+", r"\bsc\s+", r"\bnet\s+user\b", r"\bnet\s+localgroup\b",
    r"\battrib\b", r"\bicacls\b", r"\btaskkill\b",
]

_COMMAND_TIMEOUT = 30  # 秒
_MAX_OUTPUT = 2000  # 字符


@tool
def run_command(command: str, input_text: str = "", confirmed: bool = False) -> str:
    """执行命令行命令并返回输出（如运行 Python 脚本、查看目录、测试代码）。

    当需要运行/验证刚写好的代码、查看文件列表、执行脚本、安装依赖时调用——
    例如写完 guess_game.py 后运行 "python guess_game.py" 验证。
    只在 {cwd} 目录内执行。

    【交互式程序】如果程序用 input() 等待用户输入（如计算器、游戏、CLI），
    必须通过 input_text 提供输入（每行一个输入，用换行分隔），
    否则程序会卡在等待输入导致超时。例如运行计算器：
      command="python calculator.py", input_text="3+5\\n10-4\\nq\\n"

    安全机制：
    - 破坏性命令（rm -rf /、format、shutdown 等）直接拒绝
    - 高危命令（删除/移动/安装包/联网下载等）需要 confirmed=True 才会执行；
      未确认时返回 NEED_CONFIRM 标记，由用户在界面确认后再次调用
    - 30 秒超时自动终止；输出截断到 2000 字符

    Args:
        command: 要执行的命令字符串（如 "python guess_game.py"）
        input_text: 可选，通过标准输入喂给程序的文本（交互式程序需要，
            每行一个输入，换行分隔）
        confirmed: 高危命令的用户确认标记（首次调用传 False）
    """
    import re
    import subprocess

    from config import WRITE_DIR

    cwd = str(Path(WRITE_DIR).resolve())

    # 1. 黑名单硬拦截
    for pat in _BLOCKED_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return f"⛔ 已拦截：命令包含破坏性操作（{pat}），禁止执行"

    # 2. 高危命令需要确认
    is_high_risk = any(re.search(p, command, re.IGNORECASE) for p in _HIGH_RISK_PATTERNS)
    if is_high_risk and not confirmed:
        return (
            "NEED_CONFIRM 需要用户确认：高危命令 "
            f"[{command}] 是否执行？确认后请用 confirmed=True 重新调用本工具。"
        )

    # 3. 执行（沙箱目录 + 超时 + 截断 + 标准输入）
    import os

    # 强制子进程 UTF-8 输出：Windows 下 Python 默认 GBK 输出中文，
    # 与 subprocess 的 utf-8 解码不一致会导致乱码
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    try:
        proc = subprocess.Popen(
            command, cwd=cwd, shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=env,
        )
        try:
            # 通过 stdin 喂输入（交互式程序必需）；无输入时传空串立即关闭 stdin
            stdout, stderr = proc.communicate(input=input_text, timeout=_COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            # 超时：强杀进程树（Windows 用 taskkill /T，Linux 用 kill 组），
            # 防止 cmd 派生的子进程残留
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, timeout=5,
                    )
                else:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return (
                f"⏱ 命令超时（>{_COMMAND_TIMEOUT}s），已终止：{command}。"
                "若程序在等待 input() 输入，请通过 input_text 参数提供输入（每行一个，换行分隔）后重试。"
            )
        result = proc
    except Exception as exc:  # noqa: BLE001
        return f"执行失败: {exc}"

    out = (stdout or "") + (stderr or "")
    out = out.strip()
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + f"\n…（输出已截断，共 {len(out)} 字符）"
    status = f"✅ 执行成功（exit {result.returncode}）" if result.returncode == 0 else f"❌ 执行失败（exit {result.returncode}）"
    return f"{status}\n{out}" if out else status
