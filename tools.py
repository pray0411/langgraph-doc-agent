"""Agent 工具集：通过 LangChain @tool 定义，供 ReAct Agent 自主调用。

- search_documents: 检索本地文档索引（RAG）
- web_search: 联网搜索实时信息（天气/新闻/事实）
- get_weather: 查询指定城市实时天气
- write_file: 把内容写入本地文件（限制在 WRITE_DIR 目录内）
"""
from pathlib import Path

import urllib.request

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


def _safe_target(file_path: str, *, must_exist: bool) -> tuple[Path, Path] | str:
    """校验 file_path 位于 WRITE_DIR 内，返回 (write_root, target)；不合法返回错误串。"""
    from config import WRITE_DIR

    write_root = Path(WRITE_DIR).resolve()
    target = (write_root / file_path).resolve()
    if not target.is_relative_to(write_root):
        return f"拒绝访问：路径 {file_path} 超出允许目录 {write_root}"
    if must_exist and not target.exists():
        return f"文件不存在: {file_path}"
    return write_root, target


@tool
def read_file(file_path: str, start_line: int = 1, max_lines: int = 200) -> str:
    """读取本地文件内容（带行号，供修改/分析代码用）。

    当你需要查看已落盘文件的内容（如用户让你修改 guess_game.py 前先看它现在
    写了什么、排查代码问题）时调用。**写→跑→修闭环的关键一步**：先读再改，
    不要凭记忆盲改。

    安全边界：只允许读取 WRITE_DIR 目录内的文件（相对或绝对路径均可，不得
    逃逸出该目录）。文件过大时分段读取：默认从第 1 行开始最多读 200 行，
    超长文件会提示剩余行数，用 start_line 继续读下一段。

    Args:
        file_path: 文件路径（如 "guess_game.py" 或 "scripts/calc.py"）
        start_line: 起始行号（从 1 开始，默认 1）
        max_lines: 最多读取多少行（默认 200，最大 1000）
    """
    import os

    checked = _safe_target(file_path, must_exist=True)
    if isinstance(checked, str):
        return checked
    write_root, target = checked

    # 二进制检测：含 null 字节视为二进制，拒绝按文本读
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return f"读取失败: {exc}"
    if b"\x00" in raw[:4096]:
        return f"{file_path} 是二进制文件，无法按文本读取（大小 {len(raw)} 字节）"

    lines = raw.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    start_line = max(1, int(start_line))
    max_lines = min(max(1, int(max_lines)), 1000)
    end_line = min(start_line + max_lines - 1, total)
    if start_line > total:
        return f"{file_path} 共 {total} 行，起始行 {start_line} 超出范围"

    body = "\n".join(f"{i:4d}| {lines[i-1]}" for i in range(start_line, end_line + 1))
    head = f"{file_path}（共 {total} 行，显示 {start_line}-{end_line} 行，{os.path.getsize(target)} 字节）"
    tail = f"\n… 还有 {total - end_line} 行未显示，可用 start_line={end_line + 1} 继续读" if end_line < total else ""
    return head + "\n" + body + tail


@tool
def list_files(path: str = "") -> str:
    """列出本地目录下的文件（路径、大小、修改时间）。

    当用户问"项目里有哪些文件"、或你想知道 WRITE_DIR 里已生成/已上传了什么、
    或需要确认要操作的文件是否存在时调用。递归列出子目录，自动跳过
    .git/node_modules/__pycache__ 等缓存目录。

    Args:
        path: WRITE_DIR 内的相对目录（空字符串 = 根目录，如 "scripts"）
    """
    import os
    import time as _time

    checked = _safe_target(path, must_exist=False)
    if isinstance(checked, str):
        return checked
    write_root, target = checked
    if not target.exists():
        return f"目录不存在: {path or '.'}"
    if not target.is_dir():
        return f"{path or '.'} 不是目录"

    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}
    entries: list[str] = []
    limit = 200  # 条目上限，防刷屏

    def _walk(d: Path, depth: int):
        if len(entries) >= limit or depth > 4:
            return
        try:
            items = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for p in items:
            if len(entries) >= limit:
                return
            rel = p.relative_to(write_root)
            if p.is_dir():
                if p.name in skip_dirs:
                    continue
                entries.append(f"[目录] {rel}/")
                _walk(p, depth + 1)
            else:
                try:
                    size = p.stat().st_size
                    mtime = _time.strftime("%m-%d %H:%M", _time.localtime(p.stat().st_mtime))
                except OSError:
                    size, mtime = 0, "?"
                entries.append(f"{mtime} {size:>9,}  {rel}")

    _walk(target, 0)
    if not entries:
        return f"{path or '.'} 目录为空"
    head = f"{path or '.'}（{len(entries)} 个条目" + ("，仅显示前 200 个" if len(entries) >= limit else "") + "）："
    return head + "\n" + "\n".join(entries)


@tool
def get_current_time() -> str:
    """获取当前本地日期与时间。

    当用户问"现在几点/今天几号/星期几"、或需要为回答提供时间上下文
    （如"最近"、"昨天"）时调用，不要凭记忆猜测时间。

    Returns:
        形如 "2026-08-31 14:30:05 星期一（UTC+08:00）" 的字符串
    """
    import time as _time

    now = _time.localtime()
    weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    return (
        f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d} "
        f"{now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d} "
        f"{weekdays[now.tm_wday]}（UTC{_time.strftime('%z', now)}）"
    )


@tool
def edit_file(file_path: str, old_text: str, new_text: str, occurrence: int = 1) -> str:
    """精确替换文件中的一段内容（修改已有代码/文档）。

    当需要修改已落盘文件的局部内容（改 bug、调参数、换文案）时调用——
    比整文件重写更省 token 且不易误伤其他部分。**改代码前先用 read_file
    查看当前内容**，确保 old_text 与文件中的原文逐字符一致（含缩进）。

    安全与规则：
    - 只允许修改 WRITE_DIR 目录内的文件
    - old_text 必须在文件中精确出现；默认替换第 1 处，出现多次时用
      occurrence 指定（超出次数会报错），**绝不批量静默替换**
    - 替换前会先校验（找不到/超次数则报错），不会写坏文件

    Args:
        file_path: 文件路径（如 "guess_game.py"）
        old_text: 要替换的原文片段（必须与文件内容逐字符一致）
        new_text: 替换后的新内容
        occurrence: 替换第几处出现（默认 1）
    """
    checked = _safe_target(file_path, must_exist=True)
    if isinstance(checked, str):
        return checked
    write_root, target = checked

    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return f"读取失败: {exc}"

    count = content.count(old_text)
    if count == 0:
        # 常见原因提示：缩进/引号不一致，给出可复制的错误定位
        return (
            f"替换失败：在 {file_path} 中未找到该片段（共 {len(content.splitlines())} 行）。"
            "请先用 read_file 查看文件当前内容，确保 old_text 与原文逐字符一致（含缩进与引号）。"
        )
    if occurrence < 1 or occurrence > count:
        return f"替换失败：片段在文件中出现 {count} 次，occurrence={occurrence} 超出范围（可选 1-{count}）"

    # 定位第 occurrence 处
    idx = -1
    for _ in range(occurrence):
        idx = content.find(old_text, idx + 1)
    new_content = content[:idx] + new_text + content[idx + len(old_text):]
    try:
        target.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return f"写入失败: {exc}"
    old_lines = old_text.count("\n") + 1
    new_lines = new_text.count("\n") + 1
    return (
        f"已替换 {file_path} 第 {occurrence}/{count} 处（{old_lines} 行 → {new_lines} 行）："
        f"\n- 原: {old_text[:60]!r}"
        f"\n+ 新: {new_text[:60]!r}"
    )


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
    - 高危命令（删除/移动/安装包/联网下载等）必须**用户在前端确认**后才执行：
      首次调用返回 NEED_CONFIRM，用户确认后系统会自动续问；届时再以
      confirmed=True 调用。**不要自己把 confirmed 设为 True**——未经用户
      确认的 confirmed=True 会被后端拒绝（无批准记录）。
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
    if is_high_risk:
        if not confirmed:
            return (
                "NEED_CONFIRM 需要用户确认：高危命令 "
                f"[{command}] 是否执行？请等待用户确认。"
            )
        # confirmed=True 必须命中后端批准登记（用户在前端确认后经 /api/approve 登记）
        # ——防止模型自填参数绕过授权。批准一次性消费，仅对当前命令有效。
        from approvals import is_approved

        if not is_approved(command):
            return (
                "NEED_CONFIRM 未获用户批准：高危命令 "
                f"[{command}] 没有对应的批准记录。请等待用户在前端确认。"
            )

    # 3. 执行（沙箱目录 + 超时 + 截断 + 标准输入）
    import os

    # 风险说明（Codex 评审记录）：shell=True 把命令交给系统 shell 解析——
    # 支持管道/通配符/环境变量等便捷语法，但也意味着黑名单正则不是安全边界：
    # `python -c "..."` 等写法可绕过关键词匹配，真正的人为护栏是上方的高危
    # 确认闸（approvals 一次性消费）+ 30s 超时强杀 + 限 WRITE_DIR 目录。
    # 本项目定位单机个人开发辅助，不做沙箱隔离；若用于不受信环境请改为
    # 非 shell 参数列表执行或容器/虚拟机隔离（见 README 安全边界说明）。
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


@tool
def open_in_browser(file_path: str) -> str:
    """用系统默认浏览器打开本地文件（HTML 游戏/页面交付用）。

    当写完 HTML 文件（游戏/页面）后，调用本工具让用户在浏览器中直接体验——
    例如写完 minesweeper.html 后 open_in_browser("minesweeper.html")。
    只允许打开 WRITE_DIR 目录内的文件；非 HTML 文件会用系统默认程序打开。

    Args:
        file_path: WRITE_DIR 内的文件路径（如 "minesweeper.html"）
    """
    import os
    import subprocess as _sp

    from config import WRITE_DIR

    write_root = Path(WRITE_DIR).resolve()
    target = (write_root / file_path).resolve()
    if not target.is_relative_to(write_root):
        return f"拒绝打开：路径 {file_path} 超出允许目录 {write_root}"
    if not target.exists():
        return f"文件不存在: {file_path}"
    try:
        if os.name == "nt":
            os.startfile(str(target))  # Windows 默认程序打开（HTML → 浏览器）
        else:
            _sp.Popen(["xdg-open", str(target)], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        return f"已在浏览器/默认程序中打开 {target.relative_to(write_root)}"
    except Exception as exc:  # noqa: BLE001
        return f"打开失败: {exc}"


# ---------- fetch_url：只读抓取（GitHub 白名单自动放行，其他域名需用户确认） ----------

# 自动放行白名单：GitHub 相关域名。其他域名（如 arxiv.org / docs.python.org）
# 必须用户确认后才可抓取（NEED_CONFIRM → 前端弹窗 → /api/approve 登记 →
# 同 URL 重试放行），机制与 run_command 高危命令一致。
_FETCH_ALLOWED_HOSTS = {
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
}
_FETCH_TIMEOUT = 15          # 秒
_FETCH_MAX_BYTES = 200_000   # 响应体上限（防超大响应拖垮内存）
_FETCH_MAX_TEXT = 6000       # 返回给模型的文本上限（字符，防撑爆上下文）
_USER_AGENT = "Pray-DocAgent/1.0 (read-only fetch)"


def _validate_fetch_url(url: str) -> str:
    """校验 URL 非空、长度与协议（http/https），返回 hostname；不合法抛 ValueError。

    不做域名白名单拦截——域名放行由 fetch_url 入口统一判断（白名单自动放行
    或用户已批准）。
    """
    from urllib.parse import urlsplit

    if not url or len(url) > 2000:
        raise ValueError("URL 为空或过长")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https 协议")
    return (parts.hostname or "").lower()


def _host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    """host 是否在允许集合内（含其子域名，如 www.example.com 属于 example.com）。

    endswith(".base") 精确子域名匹配，杜绝 example.com.evil.com 这类后缀伪造。
    """
    if host in allowed_hosts:
        return True
    return any(host.endswith("." + base) for base in allowed_hosts)


def _fetch_needs_confirm(url: str, host: str) -> str | None:
    """非白名单域名：未获用户批准则返回 NEED_CONFIRM 文本，否则 None。"""
    if _host_allowed(host, _FETCH_ALLOWED_HOSTS):
        return None
    from approvals import is_approved

    if is_approved(url):
        return None
    # 文本格式与 run_command 的 NEED_CONFIRM 完全一致，前端确认弹窗零改动复用
    return (
        f"NEED_CONFIRM 需要用户确认：高危命令 [{url}] 是否执行？"
        "（抓取非 GitHub 域名需用户确认；确认后请用**完全相同的 URL** 重试）"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):  # noqa: N801
    """不自动跟随重定向：由 _http_get 逐跳手动校验域名白名单。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _http_get(url: str, allowed_hosts: set[str], timeout: float = _FETCH_TIMEOUT) -> tuple[str, bytes]:
    """逐跳 GET（每跳校验域名在允许集合内），返回 (最终URL, 原始字节)。"""
    import urllib.error
    import urllib.request
    from urllib.parse import urljoin

    current = url
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(6):
        host = _validate_fetch_url(current)
        if not _host_allowed(host, allowed_hosts):
            raise ValueError(f"拒绝访问：域名 {host} 不在允许范围")
        req = urllib.request.Request(
            current,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/json,*/*"},
        )
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = urljoin(current, exc.headers["Location"])
                continue
            raise
        data = resp.read(_FETCH_MAX_BYTES + 1)
        return resp.geturl(), data
    raise ValueError("重定向次数过多")


def _html_to_text(html: str) -> str:
    """粗略提取 HTML 文本：去脚本/样式/标签/注释，压缩空白。"""
    import html as _html
    import re as _re

    text = _re.sub(r"(?is)<script.*?</script>", " ", html)
    text = _re.sub(r"(?is)<style.*?</style>", " ", text)
    text = _re.sub(r"(?is)<!--.*?-->", " ", text)
    text = _re.sub(r"(?is)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _github_repo_path(url: str) -> tuple[str, str] | None:
    """识别 GitHub 仓库根 URL（https://github.com/<owner>/<repo>[/]），返回 (owner, repo)。"""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.hostname != "github.com":
        return None
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) == 2:
        return segs[0], segs[1]
    return None


@tool
def fetch_url(url: str) -> str:
    """只读获取网页/接口内容（GitHub 域名自动放行，其他域名需用户确认）。

    当需要**查看某个页面的真实内容**（锐评 GitHub 项目、读文档、核对接口
    返回）时调用——web_search 只能搜到标题摘要，拿不到正文。
    - GitHub 仓库根 URL（https://github.com/<owner>/<repo>）自动抓 README
    - https://api.github.com/repos/<owner>/<repo> 返回仓库元数据 JSON
    - 其余 GitHub 页面按文本提取返回

    域名规则与安全：
    - github.com / api.github.com / raw.githubusercontent.com（含子域名）**自动放行**
    - 其他域名（如 arxiv.org、docs.python.org）返回 NEED_CONFIRM，需用户确认；
      用户确认后请用**完全相同的 URL** 重新调用本工具（前端会登记批准并自动续问）
    - 只读设计：仅 GET、不执行任何代码、重定向逐跳校验域名、响应大小上限
    - **不要**改用 run_command + curl（无白名单保护）

    Args:
        url: 要读取的完整 URL（http/https）
    """
    try:
        host = _validate_fetch_url(url)
        confirm = _fetch_needs_confirm(url, host)
        if confirm:
            return confirm
        # 允许域名 = 白名单 ∪ 本次已批准域名（重定向到其他域名仍被拒）
        allowed = set(_FETCH_ALLOWED_HOSTS)
        if not _host_allowed(host, allowed):
            allowed.add(host)
        repo = _github_repo_path(url)
        if repo:
            # 仓库根 URL：优先抓 README（raw.githubusercontent 的 HEAD 分支），失败退回仓库主页
            owner, name = repo
            for readme in ("README.md", "readme.md", "Readme.md"):
                try:
                    raw_url = f"https://raw.githubusercontent.com/{owner}/{name}/HEAD/{readme}"
                    final_url, data = _http_get(raw_url, allowed)
                    text = data.decode("utf-8", errors="replace")
                    if text.strip():
                        return f"README（{final_url}）：\n" + text.strip()[:_FETCH_MAX_TEXT]
                except Exception:  # noqa: BLE001
                    continue
            final_url, data = _http_get(url, allowed)
            text = _html_to_text(data.decode("utf-8", errors="replace"))
        else:
            final_url, data = _http_get(url, allowed)
            raw = data.decode("utf-8", errors="replace")
            # api.github.com 返回 JSON（模型可直接读）；HTML 页面转纯文本
            text = raw if host == "api.github.com" else _html_to_text(raw)
        if not text.strip():
            return f"抓取成功但内容为空（{final_url}）"
        return f"内容（{final_url}）：\n" + text.strip()[:_FETCH_MAX_TEXT]
    except Exception as exc:  # noqa: BLE001
        return f"抓取失败: {exc}"
