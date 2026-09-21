# -*- coding: utf-8 -*-
"""测试公共夹具与全局守卫。

本文件承担三件事，都是为了让"测试通过"这句话**在别人的机器上同样成立**：

1. **密封性（hermetic）** —— `.env` 文件、本机代理、真实网络都不允许影响测试结果。
   - `_hermetic_dotenv`：测试期间把 `dotenv.load_dotenv` 变成空操作。原因见下方注释。
   - `_hermetic_proxy`：代理解析（含 Windows 注册表里的系统代理）返回空。原因见下方注释。
   - `_no_real_network`：只放行回环地址，任何外呼（模型 API、搜索引擎、
     HuggingFace 下载）都会立刻抛错，而不是"悄悄真的调了一次付费接口"。
   - `isolate_state`：每个用例前隔离目录 + 重置模块级单例（checkpointer、
     agent 缓存、检索缓存、上传索引、审批表、编码器）。

2. **可复用的假模型服务** —— `fake_llm` 起一个本地 OpenAI 兼容服务，
   响应可脚本化编排，用于在**不联网、不花钱、结果确定**的前提下驱动真实的
   ReAct 工具循环（见 tests/test_agent_loop.py）。

3. **反向守护** —— 见 tests/test_metatest.py：断言上述守卫本身没被改坏。
"""
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 会被"重新导入以应用 monkeypatch"的模块：config 在 import 时读环境变量，
# 因此改环境后必须让它重新导入，否则读到的是上一次的旧值。
APP_MODULES = (
    "config", "prompts", "memory", "retriever", "tools",
    "approvals", "runterm", "graph", "server", "mcp_server", "logging_setup",
)

# 测试期间使用的假凭据：非空（保证代码走"已配置"分支）但一定无效，
# 且配合指向本机闲置端口的 base_url，任何漏网的真实调用都会立刻连接失败。
FAKE_API_KEY = "sk-test-not-a-real-key"
DEAD_BASE_URL = "http://127.0.0.1:9/v1"

# 网络守卫的放行开关（单元素列表当可变全局用）：
# 由 `_network_marker_gate` 按用例的 `allow_network` 标记置位，
# 由 `_no_real_network` 里的 connect 包装在**连接时刻**读取。
# 之所以不在包装函数里直接读用例标记：会话级夹具拿不到"当前用例"，
# 而连接发生在用例执行过程中，用开关中转最简单可靠。
# 默认必须为 False —— 默认放行等于没有守卫。
_NETWORK_ALLOWED = [False]


def forget_app_modules() -> None:
    """重置模块级单例后从 sys.modules 移除，使下次 import 读到最新环境变量。"""
    for name in APP_MODULES:
        mod = sys.modules.get(name)
        reset = getattr(mod, "reset_state", None) if mod is not None else None
        if callable(reset):
            try:
                reset()
            except Exception:  # noqa: BLE001 - 重置失败不应掩盖用例本身的失败
                pass
        sys.modules.pop(name, None)


# ---------- 1. 密封性 ----------

@pytest.fixture(scope="session", autouse=True)
def _hermetic_dotenv():
    """测试期间禁用 `load_dotenv()`，堵住那条会把 `.env` 灌回环境的暗路。

    背景（这是本套件曾经的 P0 缺陷）：`_clean_env` 用 `monkeypatch.delenv`
    删掉 `DEEPSEEK_API_KEY`，紧接着又 `sys.modules.pop("config")` 强制重导入；
    而 `config.py` 模块级会执行 `load_dotenv()`，于是把开发者本机 `.env` 里的
    真实 Key **又灌回 `os.environ`**，隔离被击穿 —— 结果是本地"全绿"、CI 上
    必红，且本地那次全绿其实是真去调用了付费接口。

    这里在会话级把 `load_dotenv` 换成空操作：既然它不该在测试里生效，
    就从源头让它不生效，而不是靠"记得删除某个环境变量"来维持。
    """
    import dotenv

    original = dotenv.load_dotenv

    def _noop(*args, **kwargs):
        return False

    _noop._pytest_noop = True  # type: ignore[attr-defined]
    dotenv.load_dotenv = _noop
    try:
        yield
    finally:
        dotenv.load_dotenv = original
        forget_app_modules()


@pytest.fixture(scope="session", autouse=True)
def _hermetic_proxy():
    """测试期间屏蔽"代理"这条外呼暗路（Windows 注册表 / macOS System Configuration / 环境变量）。

    背景（这是本套件第二个 P0 级密封性缺陷，比 `.env` 那条更隐蔽）：
    `urllib.request.getproxies()` 在 **Windows 上会回退读取注册表**里的系统代理
    （Internet 选项 → 连接 → 局域网设置），macOS 上回退读 System Configuration。
    而 httpx 的 `get_environment_proxies()` 正是建立在它之上 —— 于是会出现：

    - `os.environ` 里**一个代理变量都没有**，清理环境变量的做法完全无效；
    - httpx 却仍然把请求发给系统代理，由代理代为转发。

    实测现象：本机装了 7890 端口这类代理工具时，连"指向 127.0.0.1 的本地假模型
    服务"都会被绕出去，代理对回环目标返回 502 —— 表现为同一套用例**在装了代理
    的机器上必红、在没装的机器上全绿**，而失败信息是 `openai.InternalServerError:
    502`，看起来完全像产品缺陷。`trust_env=False` 立刻恢复正常，可确诊。

    修法：从**代理解析层**堵死（而不是"记得清环境变量"），并兼容 `requests`/`urllib`
    这类同样走 `getproxies()` 的库。这样"测试不依赖开发者本机网络环境"才是真的。
    """
    import urllib.request

    saved_attrs: list[tuple[object, str, object]] = []

    def _patch(module, name: str, new) -> None:
        if module is None or not hasattr(module, name):
            return
        saved_attrs.append((module, name, getattr(module, name)))
        setattr(module, name, new)

    def _no_proxies():
        return {}

    # 1) 解析层：让所有"从环境/注册表推断代理"的入口都返回空
    _patch(urllib.request, "getproxies", _no_proxies)
    try:
        import httpx  # httpx._utils 用 `from urllib.request import getproxies` 绑定了名字，
        # _client 又 `from ._utils import get_environment_proxies` 绑定了名字，
        # 所以必须逐个改这两个模块的绑定，改 urllib 一个是不够的。
        _patch(getattr(httpx, "_utils", None), "getproxies", _no_proxies)
        _patch(getattr(httpx, "_utils", None), "get_environment_proxies", _no_proxies)
        _patch(getattr(httpx, "_client", None), "get_environment_proxies", _no_proxies)
    except ImportError:  # httpx 未安装时无需处理
        pass

    # 2) 环境变量层：连代理变量一起清掉，并显式声明空代理（双保险）
    saved_env = {}
    for key in list(os.environ):
        if key.lower().endswith("_proxy"):
            saved_env[key] = os.environ.pop(key)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

    try:
        yield
    finally:
        for module, name, old in reversed(saved_attrs):
            setattr(module, name, old)
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        os.environ.update(saved_env)


@pytest.fixture(scope="session", autouse=True)
def _no_real_network(request):
    """只允许回环地址出网；任何真实外呼立即抛错。

    这条守卫的价值在于：把"测试不依赖真实网络"从**文档承诺**变成
    **可执行断言**。此前它只是 README 里的一句话，而真实情况是有用例
    会去调 `api.deepseek.com`。现在漏网的外呼会直接失败，而不是悄悄发生。

    需要真实网络时给用例加 `@pytest.mark.allow_network`（由下面的
    `_network_marker_gate` 读取并放行）。

    注意：**回环地址放行是刻意设计**（假模型服务起在 127.0.0.1 上），
    这也意味着"本机代理"是这条守卫的一个绕过口 —— 所以代理必须被单独
    封堵，见 `_hermetic_proxy`。两者是配套的，缺一不可。
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _allowed(host) -> bool:
        if not isinstance(host, str):
            return False
        h = host.strip().strip("[]").lower()
        return (
            h in ("", "localhost", "127.0.0.1", "::1", "0.0.0.0")
            or h.startswith("127.")
            or h.startswith("::1")
        )

    def _check(address):
        if _NETWORK_ALLOWED[0]:
            return
        if isinstance(address, tuple) and address:
            host = address[0]
            if host in ("", None):
                return
            if not _allowed(host):
                raise RuntimeError(
                    f"测试不允许真实网络访问（目标 {host!r}）。"
                    "若确实需要，请给用例加 @pytest.mark.allow_network 标记。"
                )

    def _guard_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def _guard_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    socket.socket.connect = _guard_connect
    socket.socket.connect_ex = _guard_connect_ex
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex


@pytest.fixture(autouse=True)
def _network_marker_gate(request):
    """让 `@pytest.mark.allow_network` 真正生效。

    此前这个标记只写在文档字符串里，**代码里从未读取它** —— 也就是说
    加不加标记行为完全一样，等于给使用者一个"以为能放行、实际照旧被拦"的
    假开关。要么实现它，要么删掉文档；保留一个骗人的开关是最坏的选择。
    """
    previously = _NETWORK_ALLOWED[0]
    _NETWORK_ALLOWED[0] = request.node.get_closest_marker("allow_network") is not None
    try:
        yield
    finally:
        _NETWORK_ALLOWED[0] = previously


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, tmp_path_factory, monkeypatch):
    """每个用例独立的环境与目录，并清空全部模块级可变状态。

    这里是全套件唯一的"状态复位点"：只要新增了模块级单例，就应该在这里
    一起复位 —— 否则用例顺序一变就会互相污染（flaky 的经典来源）。

    **注意隔离目录不放在 tmp_path 里面**：不少用例把 tmp_path 当作自己的
    工作目录并断言其内容（例如"拒绝路径逃逸后 tmp_path 里不应产生文件"），
    把 docs/index/logs 塞进去会让这类断言无辜失败。
    """
    work = tmp_path_factory.mktemp("sandbox")
    (work / "generated").mkdir(parents=True, exist_ok=True)

    # 目录隔离：索引、文档、可写目录、两个 SQLite 库、日志都指向独立沙箱
    monkeypatch.setenv("DOCS_DIR", str(work / "docs"))
    monkeypatch.setenv("INDEX_DIR", str(work / "index"))
    monkeypatch.setenv("WRITE_DIR", str(work / "generated"))
    monkeypatch.setenv("MEMORY_DB", str(work / "memory.sqlite"))
    monkeypatch.setenv("GLOBAL_MEMORY_DB", str(work / "global_memory.sqlite"))
    # 日志目录也隔离：审计日志是文件副作用，不该写进仓库或污染 tmp_path
    monkeypatch.setenv("LOG_DIR", str(work / "logs"))

    # 密封性：假凭据 + 指向必然连不上的地址，杜绝漏网的真实调用
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("LLM_BASE_URL", DEAD_BASE_URL)
    monkeypatch.setenv("API_TOKEN", "")
    monkeypatch.setenv("BOCHA_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "nonexistent-no-download")
    monkeypatch.setenv("PYTEST_RUNNING", "1")

    for name in ("LLM_MODEL", "TOP_K", "MIN_SCORE", "OLLAMA_BASE_URL", "OLLAMA_MODEL"):
        monkeypatch.delenv(name, raising=False)

    forget_app_modules()
    yield
    forget_app_modules()


@pytest.fixture(autouse=True)
def _release_handles():
    """每个用例结束时确定性地关闭测试自建的 HTTP 连接与残留句柄。

    背景：用例里大量使用 `urllib.request.urlopen()` 访问本机测试服务。
    `urllib` 每次请求新建一个 `http.client.HTTPConnection`，该对象在
    `do_open()` 返回后就没人引用了 —— socket 只等 GC 才会关闭。GC 时机不确定，
    于是 `ResourceWarning: unclosed <socket.socket ...>` 会**随机地**挂到某个
    用例名下（实测 130 个用例里散落 12~29 条）。噪声大到没人会去读，
    真正的泄漏（比如 runterm 不关子进程管道）反而被淹没。

    这里做两件事，各自对应一类问题：

    1. **真实泄漏** 由被测代码确定性修复，不靠这个夹具兜底：
       - `runterm.stop()` 显式关闭 stdin/stdout 管道并 `wait()` 回收子进程；
       - `memory._conn()` 建表失败时关闭连接；
       - 各模块的 `reset_state()` 关闭 sqlite 连接。
    2. **测试自身** 的临时连接在这里集中关闭：记录本用例内创建的
       `HTTPConnection`，teardown 时逐个 `close()`，再做一次受控 `gc.collect()`。

    说明：这是**噪声治理 + 测试侧确定性释放**，不是泄漏检测手段。
    要检测"是否新增了漏关的句柄"，用 `-W error::ResourceWarning` 单独跑一次
    并人工确认（README 有复现命令）。
    """
    import gc
    import http.client
    import warnings

    real_init = http.client.HTTPConnection.__init__
    created: list = []

    def _tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        created.append(self)

    http.client.HTTPConnection.__init__ = _tracking_init
    try:
        yield
    finally:
        http.client.HTTPConnection.__init__ = real_init
        for conn in created:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 已关闭/半关闭都属正常
                pass
        with warnings.catch_warnings():
            # 只在本夹具的回收点屏蔽：此刻释放的句柄来自**测试自身**的临时对象，
            # 不属于被测代码的资源管理责任。
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()


@pytest.fixture()
def sample_docs():
    """在**被隔离的 DOCS_DIR** 写入测试文档。

    注意：这里读环境变量而不是自己拼 tmp_path/"docs" —— 索引来自 config.DOCS_DIR，
    夹具必须写到同一个地方，否则会出现"文档写进了 A 目录、索引去 B 目录找"
    这种看起来像产品缺陷、实际是夹具自说自话的失败。
    """
    docs = Path(os.environ.get("DOCS_DIR", "")) or Path.cwd() / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "project_intro.md").write_text(
        "# 项目介绍\n"
        "本项目是一个基于 LangGraph 构建的智能文档问答 Agent。\n"
        "用户可以用自然语言对文档集合提问，Agent 会检索相关片段并由大模型回答。\n"
        "## 技术栈\n"
        "Python 3.10+，LangGraph，TF-IDF 检索（已升级为 BM25）。",
        encoding="utf-8",
    )
    return docs


# ---------- 2. 假 OpenAI 兼容模型服务 ----------

class FakeLLM:
    """本地 OpenAI 兼容假服务：让 ReAct 工具循环可以被确定性地测试。

    用法::

        fake_llm.push_tool_call("get_current_time", {})
        fake_llm.push_text("现在是 12 点")
        answer, result = graph.ask("几点了", thread_id="t1")

    设计要点：
    - **替身层级取最外层**：替换的是"模型这个外部依赖"，而不是 patch
      被测代码的内部方法链 —— 这样被验证的是真实的 agent 图与工具执行。
    - `requests` 记录每一次收到的请求体，可直接断言"模型看到了工具结果"。
    - 脚本耗尽时返回一条兜底文本，避免用例因脚本笔误而挂起。
    """

    def __init__(self):
        self._script: list[dict] = []
        self._lock = threading.Lock()
        self.requests: list[dict] = []

        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音访问日志
                pass

            def do_GET(self):  # noqa: N802
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                try:
                    req = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:  # noqa: BLE001
                    req = {}
                resp = outer.next_response(req)
                if req.get("stream"):
                    self._send_stream(resp, req)
                else:
                    self._send_json(resp, req)

            def _send_json(self, resp, req):
                body = json.dumps(
                    outer.completion_body(resp, req), ensure_ascii=False
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_stream(self, resp, req):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in outer.stream_chunks(resp, req):
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    # ---- 脚本编排 ----

    def push(self, **kwargs) -> None:
        """追加一条脚本化响应。可传 text / tool_calls / usage。"""
        with self._lock:
            self._script.append(dict(kwargs))

    def push_text(self, text: str, usage=(11, 7)) -> None:
        """追加一条"直接回答"响应。usage=(prompt_tokens, completion_tokens)。"""
        self.push(text=text, tool_calls=[], usage=usage)

    def push_tool_call(self, name: str, args: dict | None = None, call_id: str | None = None,
                       usage=(11, 7)) -> None:
        """追加一条"调用某个工具"响应（无正文，模拟真实的中间态 AI 消息）。"""
        self.push(
            text="",
            tool_calls=[{"name": name, "args": args or {}, "id": call_id or f"call_{len(self._script) + 1}"}],
            usage=usage,
        )

    def push_tool_calls(self, calls: list[tuple[str, dict]], usage=(11, 7)) -> None:
        """一条响应里并行调用多个工具。"""
        self.push(
            text="",
            tool_calls=[
                {"name": n, "args": a, "id": f"call_{i + 1}"} for i, (n, a) in enumerate(calls)
            ],
            usage=usage,
        )

    def next_response(self, req: dict) -> dict:
        with self._lock:
            self.requests.append(req)
            if self._script:
                return self._script.pop(0)
        return {"text": "(脚本已耗尽：未编排的模型调用)", "tool_calls": [], "usage": (1, 1)}

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def messages_of(self, index: int) -> list[dict]:
        """取第 index 次请求里送给模型的消息列表（用于断言模型"看到了什么"）。"""
        with self._lock:
            return list(self.requests[index].get("messages") or [])

    # ---- OpenAI 协议实现 ----

    @staticmethod
    def _tool_call_payload(call: dict, index: int) -> dict:
        return {
            "index": index,
            "id": call.get("id") or f"call_{index + 1}",
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
            },
        }

    def completion_body(self, resp: dict, req: dict) -> dict:
        calls = resp.get("tool_calls") or []
        message = {"role": "assistant", "content": resp.get("text") or ""}
        if calls:
            message["tool_calls"] = [
                {
                    "id": c.get("id") or f"call_{i + 1}",
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c.get("args") or {}, ensure_ascii=False),
                    },
                }
                for i, c in enumerate(calls)
            ]
        pt, ct = resp.get("usage") or (11, 7)
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model") or "fake-model",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else "stop",
            }],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        }

    def stream_chunks(self, resp: dict, req: dict):
        """按 OpenAI SSE 协议切分增量块。"""
        base = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": req.get("model") or "fake-model",
        }

        def chunk(delta, finish=None):
            return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        yield chunk({"role": "assistant", "content": ""})
        text = resp.get("text") or ""
        if text:
            # 切成小块，逼近真实的 token 级增量（便于断言事件顺序）
            step = max(1, len(text) // 3)
            for i in range(0, len(text), step):
                yield chunk({"content": text[i:i + step]})
        calls = resp.get("tool_calls") or []
        if calls:
            for i, c in enumerate(calls):
                yield chunk({"tool_calls": [self._tool_call_payload(c, i)]})
        yield chunk({}, "tool_calls" if calls else "stop")
        if (req.get("stream_options") or {}).get("include_usage"):
            pt, ct = resp.get("usage") or (11, 7)
            yield {**base, "choices": [],
                   "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}

    def close(self) -> None:
        try:
            self._srv.shutdown()
            self._srv.server_close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture()
def fake_llm(monkeypatch):
    """启动假模型服务，并把应用配置指向它（含模块重导入）。"""
    srv = FakeLLM()
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-for-local-server")
    monkeypatch.setenv("LLM_BASE_URL", srv.base_url)
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    forget_app_modules()
    try:
        yield srv
    finally:
        srv.close()
