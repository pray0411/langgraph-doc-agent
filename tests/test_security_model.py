"""安全模型的契约测试（第二轮外部评审后的整改验收）。

这个文件存在的理由：`test_core.py` 里原有的 CSRF 用例**测的是一个错误的前提**
（"Origin 与 Host 头彼此相等即视为同源"），而 Host 头是客户端自己给的，
于是 DNS rebinding 场景下校验必然通过——测试全绿，漏洞依旧。命令高危判定同理：
旧用例里那句 `python -c "print('ok')"` 不命中任何规则，被当成"普通命令"，
而它恰恰就是绕过确认闸的那条路。

所以本文件不复述"实现现在怎么写"，而是直接断言**攻击者的动作应该失败**：
- 写一个脚本再跑它（本项目主推用法）必须经过用户批准
- Origin 与 Host 同时指向攻击者域名（DNS rebinding）必须被拒
- 连通性自检不能被打成内网/云元数据探测器
- 执行面不能被打满
- 来源卡片的预览必须能区分不同来源
"""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest


# ---------- 夹具：真实服务 + 临时目录 ----------

@pytest.fixture()
def web(monkeypatch, tmp_path):
    from http.server import ThreadingHTTPServer

    import approvals
    import config
    import retriever
    import runterm
    import server as server_mod

    write_dir = tmp_path / "generated"
    uploads_dir = write_dir / "uploads"
    write_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "WRITE_DIR", write_dir)
    monkeypatch.setattr(config, "UPLOADS_DIR", uploads_dir)

    retriever.reset_state()
    runterm.reset_state()
    approvals.reset_state()
    server_mod.reset_state()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{port}", write_dir=write_dir,
                              uploads_dir=uploads_dir, module=server_mod)
    finally:
        srv.shutdown()
        srv.server_close()
        retriever.reset_state()
        runterm.reset_state()
        approvals.reset_state()


def _req(method, url, data=None, headers=None, timeout=15):
    """发请求，HTTPError 也当正常返回值，返回 (状态码, 响应体)。"""
    body = data if isinstance(data, bytes) else (data.encode() if data else None)
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------- 1. 命令执行授权：默认拒绝 ----------

def test_classify_command_default_deny_table():
    """分类器必须**默认拒绝**：只读白名单之外一律 high。

    这条是整块安全改造的地基——旧实现反过来做（"只拦长得像危险命令的"），
    所以每漏掉一种解释器就漏一条任意代码执行路径。
    """
    from tools import classify_command

    # blocked：破坏性操作，不给确认机会
    for cmd in ("rm -rf /", "format C:", "shutdown /s", "mkfs /dev/sda"):
        assert classify_command(cmd)[0] == "blocked", cmd

    # high：一切能执行代码的写法（旧实现全部漏过）
    for cmd in (
        "python script.py", "python3 script.py", "python -c \"print(1)\"",
        "py script.py", "pytest tests/", "node app.js", "npm start",
        "powershell -Command Get-ChildItem", "cmd /c dir", "bash run.sh",
        "perl -e 'print 1'", "ruby x.rb", "java -jar x.jar", "go run main.go",
        "./build.sh", "script.ps1", "task.bat", "app.vbs",
    ):
        assert classify_command(cmd)[0] == "high", f"应判高危：{cmd}"

    # high：其它有副作用的操作
    for cmd in ("move a b", "copy a b", "curl http://x", "reg query HKLM",
                "taskkill /F /IM x.exe", "pip install requests"):
        assert classify_command(cmd)[0] == "high", cmd

    # safe：只读白名单
    for cmd in ("dir", "ls -la", "type readme.md", "cat a.txt", "findstr foo a.txt",
                "where python", "echo hello", "pwd"):
        assert classify_command(cmd)[0] == "safe", cmd

    # safe 不能被"前缀 + shell 元字符"绕过
    assert classify_command("type a.txt && python evil.py")[0] == "high"
    assert classify_command("dir | more")[0] == "high"
    assert classify_command("echo x > out.txt")[0] == "high"

    # safe 也不能被"惰性首词 + 危险选项"绕过
    # find 是最容易漏的一个：`find / -delete` 与 `find . -exec rm -rf {} +`
    # 都不含 shell 元字符，只看首词会被判成只读。
    for cmd in ("find / -delete", "find . -exec rm -rf {} +",
                "find . -execdir sh -c 'x' ;", "find . -ok rm {} +",
                "find . -okdir rm {} ;"):
        level, reason = classify_command(cmd)
        assert level == "high", f"{cmd} 竟被判为 {level}（{reason}）"

    # 只读 find 仍然放行（白名单不能因噎废食）
    assert classify_command("find . -name '*.py'")[0] == "safe"
    assert classify_command("find docs -type f")[0] == "safe"


def test_write_then_run_is_gated(monkeypatch, tmp_path):
    """**漏洞回归（本文件最重要的一条）**：写脚本 + 跑脚本必须被拦。

    这正是外部评审给出的攻击链：`write_file(任意代码)` 再
    `run_command("python xxx.py")`，旧实现下全程零确认。而且这不是"攻击者
    才会用"的路径——它就是这个项目 README 主推的"写→跑→修"闭环。
    """
    import approvals
    import config as config_mod
    from tools import run_command, write_file

    approvals.reset_state()
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))

    write_file.invoke({"file_path": "payload.py",
                       "content": "print('pwned')\n"})

    # 1) 模型自己调，不带确认 → 拒绝
    r1 = run_command.invoke({"command": "python payload.py", "confirmed": False})
    assert "NEED_CONFIRM" in r1, f"未确认就执行了：{r1[:200]}"

    # 2) 模型自填 confirmed=True（试图自封权限）→ 仍然拒绝
    r2 = run_command.invoke({"command": "python payload.py", "confirmed": True})
    assert "NEED_CONFIRM" in r2, f"模型自封权限竟然放行：{r2[:200]}"

    # 3) 用户在前端确认（/api/approve 登记）→ 才执行
    approvals.approve("python payload.py")
    r3 = run_command.invoke({"command": "python payload.py", "confirmed": True})
    assert "pwned" in r3, f"批准后应执行：{r3[:200]}"


def test_write_then_run_is_gated_at_http_layer(web):
    """同一件事在 HTTP 层（前端终端 ▶ 路径）也必须成立。"""
    s1, _ = _req("POST", web.base + "/api/run/write",
                 urllib.parse.urlencode({"path": "payload.py", "content": "print(1)\n"}),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    assert s1 == 200

    # 未登记批准就启动 → 400 + NEED_CONFIRM，且不应该有进程起来
    s2, body = _req("POST", web.base + "/api/run/start",
                    urllib.parse.urlencode({"command": "python payload.py"}),
                    {"Content-Type": "application/x-www-form-urlencoded"})
    assert s2 == 400 and "NEED_CONFIRM" in body

    import runterm

    assert runterm.list_active() == 0, "被拒的启动不应留下进程"


def test_runterm_shares_the_same_classifier():
    """runterm 与 run_command 必须共用分类器，否则两处判定迟早分叉。"""
    import inspect

    import runterm

    src = inspect.getsource(runterm)
    assert "classify_command" in src, "runterm 应复用 tools.classify_command"
    assert "_HIGH_RISK_PATTERNS" not in src, "runterm 不应再自己维护一份高危名单"


def test_registered_tool_surface_matches_code_and_docs():
    """交给模型的工具集合本身就是攻击面：代码 / 注册列表 / 文档三者必须一致。

    守的是"清单漂移"。改造前 README 的能力表只列了 7 个工具，而实际注册了 13 个
    —— 少列的每一个执行类工具都是使用者**不知情**的入口；一个用户看着文档以为
    "这玩意不会动我的磁盘"，实际 `write_file` + `run_command` 都在手上。
    文档漏报的危险不比代码漏洞小，所以这里把三者钉在一起。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    tool_src = (root / "tools.py").read_text(encoding="utf-8")
    tool_names = set(re.findall(r"^@tool\s*\ndef\s+(\w+)", tool_src, re.M))
    assert len(tool_names) >= 13, f"tools.py 里只解析出 {len(tool_names)} 个 @tool"

    graph_src = (root / "graph.py").read_text(encoding="utf-8")
    block = re.search(r"tools=\[(.*?)\]\s*,", graph_src, re.S)
    assert block, "未找到 create_agent(tools=[...])"
    registered = set(re.findall(r"\b\w+\b", block.group(1)))

    missing = tool_names - registered
    assert not missing, f"这些 @tool 没有注册给模型：{sorted(missing)}"
    unknown = registered - tool_names
    assert not unknown, f"注册列表里存在未定义的工具：{sorted(unknown)}"

    readme = (root / "README.md").read_text(encoding="utf-8")
    undocumented = sorted(n for n in tool_names if f"`{n}`" not in readme)
    assert not undocumented, f"README 能力表漏了这些工具：{undocumented}"


# ---------- 2. 来源校验：DNS rebinding ----------

def test_get_side_effect_route_is_origin_checked(web):
    """`/api/open` 是有副作用的 GET，跨站请求（img/script）必须被拒。

    跨站 `<img src="...api/open?file=x">` 不带 Origin，但会带
    `Sec-Fetch-Site: cross-site`；旧实现只对 POST 系列做校验，这条整条绕过。
    """
    s1, b1 = _req("GET", web.base + "/api/open?file=whatever.txt",
                  headers={"Sec-Fetch-Site": "cross-site"})
    assert s1 == 403 and "来源校验失败" in b1

    s2, b2 = _req("GET", web.base + "/api/open?file=whatever.txt",
                  headers={"Origin": "http://evil.com"})
    assert s2 == 403 and "来源校验失败" in b2

    # 同源请求应放行到处理函数（文件不存在由业务层报错，不是 403）
    s3, _ = _req("GET", web.base + "/api/open?file=whatever.txt",
                 headers={"Origin": web.base, "Sec-Fetch-Site": "same-origin"})
    assert s3 != 403


def test_request_with_foreign_host_header_is_rejected(web):
    """请求带着非本机 Host 时直接拒绝（域名解析劫持的信号）。"""
    s, b = _req("POST", web.base + "/api/mode", "mode=deepseek",
                {"Content-Type": "application/x-www-form-urlencoded", "Host": "evil.com"})
    assert s == 403 and "来源校验失败" in b


# ---------- 3. 连通性自检不能被当成 SSRF 原语 ----------

def test_gateway_probe_rejects_metadata_and_bad_schemes(web):
    """`/api/config/test` 会向客户端给的地址发请求并回显响应体——必须限制目标。

    云元数据地址（169.254.169.254 等）是 SSRF 拿实例凭据的首选目标。
    """
    for target, why in (
        ("http://169.254.169.254/latest/meta-data/", "云元数据"),
        ("http://metadata.google.internal/computeMetadata/v1/", "GCP 元数据"),
        ("http://100.100.100.200/latest/meta-data/", "阿里云元数据"),
        ("file:///etc/passwd", "非 http 协议"),
        ("gopher://127.0.0.1:6379/_INFO", "非 http 协议"),
    ):
        s, body = _req("POST", web.base + "/api/config/test",
                       urllib.parse.urlencode({"provider": "deepseek", "base_url": target}),
                       {"Content-Type": "application/x-www-form-urlencoded"})
        assert s == 400, f"{why} 目标应被拒绝：{target} → {s}"
        assert "目标地址被拒绝" in body


def test_gateway_probe_allows_local_gateway(web):
    """本地自建网关（正当用途）不应被误拦——只拦元数据/链路本地。"""
    from server import blocked_probe_target

    assert blocked_probe_target("http://127.0.0.1:11434/v1") == ""
    assert blocked_probe_target("https://api.deepseek.com/v1") == ""
    assert blocked_probe_target("http://192.168.1.10:8000/v1") == ""


# ---------- 4. 执行面资源上限 ----------

def test_runterm_rejects_beyond_session_cap(web, monkeypatch):
    """并发终端会话数必须有上限（否则反复调用可打满进程/句柄）。"""
    import approvals
    import runterm

    monkeypatch.setattr(runterm, "_MAX_SESSIONS", 1)
    cmd = "python -c \"import time; time.sleep(30)\""
    approvals.approve(cmd)

    r1 = runterm.start(cmd)
    assert "session_id" in r1, r1
    try:
        # 第二次：换一条命令避免批准被消费掉，但仍然超上限
        cmd2 = "python -c \"import time; time.sleep(30)\"  # 2"
        approvals.approve(cmd2)
        r2 = runterm.start(cmd2)
        assert "error" in r2 and "上限" in r2["error"], r2
    finally:
        runterm.reset_state()


def test_oversize_body_returns_413_not_400(web, monkeypatch):
    """请求体超限应回 413。旧实现静默返回 {}，上层把它报成 400「缺少参数」。"""
    monkeypatch.setattr(web.module, "MAX_BODY", 64)
    s, body = _req("POST", web.base + "/api/mode", "x" * 200,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    assert s == 413, f"应回 413，实际 {s}: {body[:120]}"
    assert "请求体过大" in body


def test_run_write_cannot_escape_write_dir(web):
    """`/api/run/write` 的目标必须始终落在 WRITE_DIR 内。"""
    for bad in ("../escape.py", "a/../../escape.py", "..\\escape.py"):
        s, _ = _req("POST", web.base + "/api/run/write",
                    urllib.parse.urlencode({"path": bad, "content": "x"}),
                    {"Content-Type": "application/x-www-form-urlencoded"})
        assert s == 400, f"路径穿越应被拒：{bad} → {s}"
    assert not (web.write_dir.parent / "escape.py").exists()


# ---------- 5. 来源卡片（多来源 + 各自片段） ----------

def test_build_sources_emits_every_document_and_own_preview():
    """来源卡片必须**每条来源一张**，且预览是各自片段。

    旧实现拿到第一条就 `break`（每次检索只出 1 张卡），且预览统一写成
    整段工具返回值的前 300 字符——多张卡片显示完全相同的文字。
    """
    from graph import _build_sources

    result = (
        "[1] 来源: docs/a.md | 相关度: 0.5\n"
        "这是 A 文档的片段内容。\n\n"
        "[2] 来源: docs/b.md | 相关度: 0.4\n"
        "这是 B 文档的片段内容，与 A 明显不同。\n"
    )
    sources = _build_sources([{"name": "search_documents", "args": {}, "result": result}])
    docs = [s for s in sources if s["type"] == "document"]
    assert len(docs) == 2, f"应产出 2 张来源卡片，实际 {len(docs)}"
    titles = [s["title"] for s in docs]
    assert titles == ["docs/a.md", "docs/b.md"], titles
    assert "A 文档的片段内容" in docs[0]["preview"]
    assert "B 文档的片段内容" in docs[1]["preview"]
    assert docs[0]["preview"] != docs[1]["preview"], "不同来源的预览不应雷同"


def test_build_sources_dedupes_and_caps():
    """同一来源重复出现应去重，总量封顶 5 条。"""
    from graph import _build_sources

    block = "[{i}] 来源: docs/same.md | 相关度: 0.5\n片段 {i}\n"
    result = "".join(block.format(i=i) for i in range(1, 8))
    sources = _build_sources([{"name": "search_documents", "args": {}, "result": result}])
    assert len(sources) == 1, "同一文件应合并为一条"


# ---------- 6. 配置健壮性 ----------

def test_bad_numeric_env_does_not_crash_import(monkeypatch):
    """`.env` 里数值写错不应让进程在 import 期崩掉。"""
    import importlib
    import sys

    monkeypatch.setenv("TOP_K", "3o")     # 典型笔误
    monkeypatch.setenv("MIN_SCORE", "abc")
    for name in ("config",):
        sys.modules.pop(name, None)
    try:
        cfg = importlib.import_module("config")
        assert cfg.TOP_K == 3, "非法值应回退默认，而不是抛 ValueError"
        assert cfg.MIN_SCORE == 0.0
    finally:
        sys.modules.pop("config", None)
        import config  # noqa: F401 - 还原模块，避免影响后续用例
