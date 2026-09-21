"""前端最小 E2E（待办 C2）：一条金路径 + 一组 XSS 注入用例。

为什么值得做：`static/index.html` 有 1582 行，其中包含一个**自己手写的 Markdown
渲染器**（`renderInline` / `renderMarkdown` / `renderCodeBlock`）。这类"零依赖自写
渲染器"最典型的两类缺陷是 ——
  1. **XSS**：模型（或工具抓到、或用户上传的文档）返回的 HTML 被当标记渲染；
  2. **畸形输入崩溃**：半个表格、未闭合的代码块把渲染器带崩，整个回答区白屏。
后端全覆盖也发现不了这两类问题：它们只存在于浏览器里。

因此这里用真浏览器（Playwright + Chromium）跑，而不是用 jsdom 之类做替身 ——
要验的就是"浏览器最终把什么放进了 DOM"。

运行前提：
    pip install playwright==1.56.0
    python -m playwright install chromium

未安装时本目录整体 skip（不阻断主测试套件），CI 里有独立的 e2e job 会装。
"""
import os
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

pw = pytest.importorskip(
    "playwright.sync_api",
    reason="需要 `pip install playwright` 并 `playwright install chromium`",
)
sync_playwright = pw.sync_playwright
expect = pw.expect


def _normalize_browsers_path() -> None:
    """修正 `PLAYWRIGHT_BROWSERS_PATH` 与实际安装位置不一致的情况。

    为什么需要这一步（CI 实测出来的错配）：`.github/workflows/ci.yml` 的 e2e job
    只在**跑测试**那一步设了 `PLAYWRIGHT_BROWSERS_PATH: "0"`，而
    `python -m playwright install --with-deps chromium` 那一步**没设** ——
    于是浏览器被装进 `~/.cache/ms-playwright/`，测试却去
    `<site-packages>/playwright/driver/package/.local-browsers/` 找，报
    `Executable doesn't exist at .../chromium_headless_shell-1194/...`，
    6 条用例全 ERROR。**报错信息看起来像"前端坏了"，实际是环境变量只在半步生效。**

    这里不去掩盖它，也不硬编码任何路径：只在**确认目标目录里确实没有 chromium**
    时才撤销这个变量，让 Playwright 回落到它自己的默认缓存目录；
    真正两处都没有的机器仍会在下面 skip，并提示装法。
    """
    val = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not val:
        return
    if val == "0":
        # "0" 的语义是"装在 playwright 包目录内"，据此还原出实际路径
        target = Path(pw.__file__).resolve().parent / "driver" / "package" / ".local-browsers"
    else:
        target = Path(val)
    try:
        has_chromium = target.is_dir() and any(target.glob("chromium*"))
    except OSError:
        has_chromium = False
    if not has_chromium:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


_normalize_browsers_path()


@pytest.fixture()
def live_server(monkeypatch):
    """起真实 web 服务，并把 `graph.ask_stream` 换成**脚本化事件流**。

    这样做的好处：前端走的是完全真实的 HTTP/SSE 路径（鉴权、CSRF、事件解析、
    渲染全都在），而"模型说什么"是确定的 —— 于是 XSS 用例可以稳定地注入
    恶意载荷，不会因为模型措辞变化而 flaky。
    """
    from http.server import ThreadingHTTPServer

    import graph
    import server as server_mod

    scripted: list[list[dict]] = []

    def fake_ask_stream(question, mode=None, thread_id=None):
        events = scripted.pop(0) if scripted else [{"type": "done", "answer": "", "sources": []}]
        yield from events

    monkeypatch.setattr(graph, "ask_stream", fake_ask_stream)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", scripted
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(scope="module")
def browser():
    """模块级共享一个 Chromium 实例（起浏览器是本目录里最贵的一步）。

    起不来时 **skip 而不是 ERROR**：`playwright` 驱动装好了、但浏览器二进制没装，
    和"前端真的有 bug"是两件事，在 CI 上必须能区分开。已经有 `importorskip`
    挡住"驱动都没装"的情况，这里补上"驱动在、浏览器不在"这一档。
    """
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except pw.Error as exc:  # 二进制缺失 / 启动失败
            pytest.skip(
                "Chromium 不可用，跳过浏览器用例。"
                "请执行 `python -m playwright install --with-deps chromium`。"
                f"原因：{exc}"
            )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture()
def page(browser, live_server):
    base, _ = live_server
    context = browser.new_context()
    page = context.new_page()
    page.goto(base, wait_until="domcontentloaded")
    try:
        yield page
    finally:
        context.close()


def _ask(page, text: str):
    page.fill("#question", text)
    page.click("#askBtn")


# ---------- 金路径 ----------

def test_golden_path_ask_renders_streamed_answer_and_sources(page, live_server):
    """金路径：提问 → SSE 流式渲染 → 来源卡片出现。"""
    _, scripted = live_server
    scripted.append([
        {"type": "start", "mode": "deepseek"},
        {"type": "tool_start", "name": "search_documents"},
        {"type": "tool_done", "result": "命中 3 个片段"},
        {"type": "token", "content": "根据文档，"},
        {"type": "token", "content": "项目使用 LangGraph 编排 ReAct 循环。"},
        {"type": "done", "answer": "根据文档，项目使用 LangGraph 编排 ReAct 循环。",
         "sources": [{"type": "doc", "title": "architecture.md", "preview": "分层设计……"}]},
    ])

    _ask(page, "项目用什么编排？")

    answer = page.locator("#chatInner .msg.assistant .answer-body").first
    expect(answer).to_contain_text("LangGraph", timeout=15000)

    # 用户消息也应渲染出来了（一问一答都在）
    expect(page.locator("#chatInner .msg.user")).to_have_count(1)

    # 来源卡片：这是"检索真的生效"在 UI 上的唯一可见证据
    cards = page.locator("#chatInner .msg.assistant .sources .source-card")
    expect(cards).to_have_count(1)
    expect(cards.first).to_contain_text("architecture.md")


def test_new_chat_clears_the_conversation(page, live_server):
    """切到新对话应清空回答区（会话隔离在 UI 上的体现）。"""
    _, scripted = live_server
    scripted.append([
        {"type": "start", "mode": "deepseek"},
        {"type": "token", "content": "第一轮的回答"},
        {"type": "done", "answer": "第一轮的回答", "sources": []},
    ])

    _ask(page, "第一个问题")
    expect(page.locator("#chatInner .msg.user")).to_have_count(1, timeout=15000)

    page.click("#clearBtn")
    expect(page.locator("#chatInner .msg")).to_have_count(0)
    expect(page.locator("#welcome")).to_be_visible()


# ---------- XSS 注入 ----------

XSS_PAYLOADS = [
    '<img src=x onerror="window.__xss=1">',
    "<script>window.__xss=2</script>",
    '<svg/onload="window.__xss=3">',
    '[点我](javascript:window.__xss=4)',
]


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_model_output_cannot_execute_scripts(page, live_server, payload):
    """模型输出里的 HTML/脚本必须被**转义成文本**，不能进 DOM 执行。

    这是自写 Markdown 渲染器最危险的一类缺陷：只要漏了 `esc()`，
    模型（可被提示词注入、可抓到不可信网页）就能在用户浏览器里执行任意脚本，
    进而读取 localStorage 里的 API Token 并向本机服务发请求。
    """
    _, scripted = live_server
    scripted.append([
        {"type": "start", "mode": "deepseek"},
        {"type": "token", "content": payload},
        {"type": "done", "answer": payload, "sources": []},
    ])

    _ask(page, "给我一段美化文本")
    expect(page.locator("#chatInner .msg.assistant .answer-body")).not_to_be_empty(timeout=15000)

    # 1) 没有任何脚本真的执行过
    assert page.evaluate("window.__xss") is None, "注入的脚本被执行了"

    # 2) 载荷没有被解析成活动元素（img/svg/script 都不应存在）
    assert page.locator("#chatInner img").count() == 0
    assert page.locator("#chatInner svg").count() == 0
    assert page.locator("#chatInner script").count() == 0

    # 3) javascript: 伪协议链接不得存在
    hrefs = page.eval_on_selector_all(
        "#chatInner a", "els => els.map(e => e.getAttribute('href'))"
    )
    assert not [h for h in hrefs if h and h.lower().startswith("javascript:")]

    # 4) 而载荷应当以**纯文本**形式可见（说明是被转义、而不是被丢弃）。
    #
    # 旧断言固定检查 "<"，但第 4 个载荷 `[点我](javascript:window.__xss=4)`
    # 里根本没有 "<" —— 该断言对它**无意义**，于是把一条其实完全安全的用例
    # （脚本没执行、没活动元素、没有 javascript: href）判成失败。
    # 改为断言"载荷原样可见"：对所有载荷都成立，且比原断言更强
    # （原来只验了一个字符，现在验整个载荷没被丢）。
    expect(page.locator("#chatInner .answer-body")).to_contain_text(payload, timeout=5000)
    if "<" in payload:
        expect(page.locator("#chatInner .answer-body")).to_contain_text("<", timeout=5000)


# ---------- 畸形输入不崩溃 ----------

def test_malformed_markdown_does_not_break_the_renderer(page, live_server):
    """畸形 Markdown（未闭合代码块 / 半张表格 / 超长行）不得让回答区白屏。"""
    _, scripted = live_server
    broken = "```python\nprint('未闭合\n\n| a | b |\n|---|\n| 1 |\n\n" + "长" * 3000
    scripted.append([
        {"type": "start", "mode": "deepseek"},
        {"type": "token", "content": broken},
        {"type": "done", "answer": broken, "sources": []},
    ])

    _ask(page, "给我一段畸形内容")
    expect(page.locator("#chatInner .msg.assistant .answer-body")).not_to_be_empty(timeout=15000)
    # 页面仍然可交互：发送按钮回到可用状态，说明渲染线程没被卡死
    expect(page.locator("#askBtn")).to_be_enabled()
