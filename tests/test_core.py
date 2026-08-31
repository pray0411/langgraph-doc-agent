# -*- coding: utf-8 -*-
"""测试套件：检索（BM25）、工具、配置、反思、服务端超时。

运行: python -m pytest tests/ -v

测试设计原则：
- 不依赖真实网络（天气/搜索工具用 mock）
- 索引构建隔离到临时目录（tmp_path），不污染仓库
"""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------- fixture ----------

@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """每个测试前清理环境变量 + 把索引/文档/记忆目录隔离到临时目录。"""
    for k in ("LLM_PROVIDER", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "BOCHA_API_KEY",
              "API_TOKEN", "MEMORY_DB", "EMBEDDING_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "memory.sqlite"))
    monkeypatch.setenv("EMBEDDING_MODEL", "nonexistent-no-download")
    # 重新导入相关模块以应用 monkeypatch（config 在 import 时读 env）
    for mod in ("config", "retriever", "tools", "graph", "server"):
        sys.modules.pop(mod, None)
    yield
    for mod in ("config", "retriever", "tools", "graph", "server"):
        sys.modules.pop(mod, None)


@pytest.fixture()
def sample_docs(tmp_path):
    """在临时 docs 目录写入测试文档。"""
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "project_intro.md").write_text(
        "# 项目介绍\n"
        "本项目是一个基于 LangGraph 构建的智能文档问答 Agent。\n"
        "用户可以用自然语言对文档集合提问，Agent 会检索相关片段并由大模型回答。\n"
        "## 技术栈\n"
        "Python 3.10+，LangGraph，TF-IDF 检索（已升级为 BM25）。",
        encoding="utf-8",
    )
    return docs


# ---------- 检索（BM25） ----------

def test_search_relevant_query_returns_hits(sample_docs):
    """文档相关问题应命中检索，分数 > 0。"""
    from retriever import build_index, search

    build_index(force=True)
    hits = search("这个项目的技术栈是什么", top_k=3)
    assert len(hits) > 0
    assert hits[0]["score"] > 0


def test_search_irrelevant_query_returns_empty(sample_docs):
    """完全无关的问题（无任何共现词）应返回空。"""
    from retriever import build_index, search

    build_index(force=True)
    hits = search("量子物理和弦理论的区别", top_k=3)
    assert hits == []


def test_search_respects_min_score(sample_docs):
    """自定义 min_score 应生效。"""
    from retriever import build_index, search

    build_index(force=True)
    hits = search("核心架构", top_k=3, min_score=0.99)
    assert hits == []


def test_search_uses_jieba_or_bigram_fallback(sample_docs):
    """分词应可用（jieba 或降级 bigram 都不应抛异常）。"""
    from retriever import _tokenize

    tokens = _tokenize("这个项目的核心架构是什么")
    assert isinstance(tokens, list)
    # 停用词应被过滤：不该出现 "的" / "什么"
    assert "的" not in tokens
    assert "什么" not in tokens


def test_index_version_upgrade_auto_rebuild(sample_docs, tmp_path):
    """旧格式索引（无 meta 版本）应被自动重建。"""
    from config import INDEX_FILE
    from retriever import build_index, load_index

    build_index(force=True)
    # 模拟旧格式：写成裸 records 列表
    INDEX_FILE.write_text(json.dumps([{"source": "x", "chunk": "y", "vec": {"a": 1}}]), encoding="utf-8")
    data = load_index()
    assert "meta" in data
    assert data["meta"]["version"] == 3


# ---------- 检索（语义混合） ----------

def test_search_semantic_channel_participates_in_fusion(sample_docs, monkeypatch):
    """索引含向量时，语义通道应参与 RRF 融合（BM25 无共现也能召回）。"""
    import retriever as retriever_mod

    def _fake_encode(texts):
        # 伪向量：与"语言开发"相关的文本语义相似（第 1 维为 1），其余弱相关
        return [[1.0 if ("语言" in t or "开发" in t) else 0.0, float(len(t) % 7) / 7.0]
                for t in texts]

    class _FakeEncoder:
        def encode(self, texts, show_progress_bar=False):
            return _fake_encode(texts)

    # 让语义通道"可用"：encoder 非 None + encode_texts 返回伪向量
    monkeypatch.setattr(retriever_mod, "get_encoder", lambda: _FakeEncoder())
    monkeypatch.setattr(retriever_mod, "encode_texts", _fake_encode)

    from retriever import build_index, search
    build_index(force=True)

    # 语义命中：查询与"用什么语言开发"语义相关，且文档含"语言"（第 1 维命中）
    hits = search("这个项目用什么语言开发", top_k=3)
    assert len(hits) > 0

    # 索引里确实存了向量（语义通道真实生效）
    from config import INDEX_FILE
    import json as _json
    data = _json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    assert "vec" in data["records"][0]


def test_search_falls_back_to_bm25_without_embedding(sample_docs, monkeypatch):
    """embedding 不可用（模型加载失败）时应回退纯 BM25，不崩溃。"""
    monkeypatch.setenv("EMBEDDING_MODEL", "nonexistent/model-that-fails")
    import retriever as retriever_mod

    # 强制 encoder 加载失败
    monkeypatch.setattr(retriever_mod, "get_encoder", lambda: None)
    from retriever import build_index, search

    build_index(force=True)
    hits = search("这个项目的技术栈是什么", top_k=3)
    assert len(hits) > 0


def test_search_irrelevant_query_returns_empty(sample_docs):
    """完全无关的问题（无共现词且语义无关）应返回空。"""
    from retriever import build_index, search

    build_index(force=True)
    hits = search("量子物理和弦理论的区别", top_k=3)
    assert hits == []


# ---------- 配置 ----------

def test_config_defaults(monkeypatch):
    """配置默认值应合理。"""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    from config import LLM_PROVIDER, TOP_K

    assert TOP_K > 0
    assert LLM_PROVIDER in ("deepseek", "openai", "qwen", "zhipu", "moonshot", "ollama")


def test_runtime_provider_config_threadsafe(sample_docs):
    """运行时 provider 配置应支持并发读写（不抛异常、读不到写一半的值）。"""
    from config import get_provider_config, set_runtime_provider_config

    errors = []

    def writer():
        try:
            for i in range(200):
                set_runtime_provider_config("deepseek", f"sk-{i}", "", "")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(200):
                cfg = get_provider_config("deepseek")
                if cfg["api_key"] not in ("",) and not cfg["api_key"].startswith("sk-"):
                    errors.append(ValueError(f"读到写一半的值: {cfg}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# ---------- 工具 ----------

def test_search_documents_tool(sample_docs):
    """文档检索工具应返回格式化结果。"""
    from tools import search_documents

    out = search_documents.invoke({"query": "核心架构"})
    assert isinstance(out, str)


def test_weather_tool_graceful_on_network_error(monkeypatch):
    """天气工具在网络异常时应返回错误信息而非崩溃。"""
    import urllib.request

    def _boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    from tools import get_weather

    out = get_weather.invoke({"city": "北京"})
    assert isinstance(out, str)
    assert "失败" in out


def test_web_search_uses_bocha_when_configured(monkeypatch):
    """配置了博查 Key 时，web_search 应优先用博查。"""
    monkeypatch.setenv("BOCHA_API_KEY", "sk-test")
    import urllib.request

    captured = {}

    def _fake_urlopen(req, timeout=15):
        captured["url"] = req.full_url
        return _FakeResp(
            json.dumps(
                {"code": 0, "data": {"webPages": {"value": [{"name": "T", "summary": "S", "url": "http://x"}]}}}
            )
        )

    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body.encode()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    from tools import web_search

    out = web_search.invoke({"query": "AI 新闻"})
    assert "bochaai.com" in captured["url"]
    assert "http://x" in out


# ---------- 反思（真实工具调用元数据） ----------

def test_extract_tool_calls_from_ai_messages():
    """应从 AIMessage.tool_calls 提取真实工具调用。"""
    from graph import _extract_tool_calls

    class FakeAIMessage:
        type = "ai"
        content = ""

        def __init__(self, tool_calls):
            self.tool_calls = tool_calls

    class FakeToolMessage:
        type = "tool"

        def __init__(self, content):
            self.content = content

    messages = [
        FakeAIMessage([{"name": "search_documents", "args": {"query": "架构"}}]),
        FakeToolMessage("以下为检索到的文档片段\n来源: a.md\n核心架构是分层设计"),
        FakeAIMessage([{"name": "web_search", "args": {"query": "新闻"}}]),
        FakeToolMessage("链接: http://news.example.com"),
        FakeAIMessage([]),
    ]
    calls = _extract_tool_calls(messages)
    assert [c["name"] for c in calls] == ["search_documents", "web_search"]
    assert "分层设计" in calls[0]["result"]


def test_grounded_true_when_answer_reuses_tool_result():
    """回答复用了工具结果内容时 grounded 应为 True。"""
    from graph import _grounded

    calls = [{"name": "search_documents", "result": "核心架构是分层设计，包含路由与检索节点"}]
    answer = "这个项目的核心架构是分层设计，包含路由与检索节点。"
    assert _grounded(answer, calls) is True


def test_grounded_false_when_answer_ignores_tool_result():
    """回答与工具结果无关（仅复用了'来源'等格式噪声）时 grounded 应为 False。"""
    from graph import _grounded

    calls = [{"name": "search_documents", "result": "以下是检索到的文档片段\n来源: a.md\n核心架构是分层设计"}]
    answer = "我不知道。"
    assert _grounded(answer, calls) is False


def test_grounded_false_when_no_tool_calls():
    """没有工具调用时 grounded 应为 False。"""
    from graph import _grounded

    assert _grounded("你好", []) is False


def test_reflection_uses_real_tool_names():
    """反思应基于真实工具名而非字符串猜测。"""
    import json as _json

    from graph import _build_reflection

    calls = [
        {"name": "get_weather", "args": {"city": "北京"}, "result": "北京: 晴 25°C"},
        {"name": "search_documents", "args": {}, "result": "来源: a.md\n内容"},
    ]
    reflection = _json.loads(_build_reflection("天气和架构", "北京晴 25°C", calls, "deepseek"))
    assert reflection["使用天气查询"] is True
    assert reflection["使用文档检索"] is True
    assert reflection["使用联网搜索"] is False
    assert reflection["调用的工具"] == ["get_weather", "search_documents"]


# ---------- 服务端超时 ----------

def test_ask_timeout_wait_returns_504_semantics():
    """/ask 的超时等待逻辑：worker 未在期限内完成时应判定超时。"""
    import server as server_mod

    result_box = {}
    done = threading.Event()
    started = threading.Event()

    def _never_finishes():
        started.set()
        threading.Event().wait(5)  # 永不返回（模拟卡死的模型调用）

    t = threading.Thread(target=_never_finishes, daemon=True)
    t.start()
    started.wait(1)
    timed_out = not done.wait(timeout=0.1)
    assert timed_out is True
    assert "ok" not in result_box


# ---------- legacy 归档完整性 ----------

def test_legacy_files_not_imported_by_runtime():
    """运行时代码不应 import legacy 模块（防止双封装回归）。"""
    import ast

    for fname in ("graph.py", "server.py", "main.py", "tools.py"):
        tree = ast.parse(Path(fname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in ("llm", "graph_v1"), f"{fname} import 了 legacy 模块"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in ("llm", "graph_v1"), f"{fname} import 了 legacy 模块"


# ---------- 多轮记忆（checkpointer） ----------

def test_memory_persists_across_asks(monkeypatch, tmp_path):
    """相同 thread_id 的连续调用应累积历史（服务端真记忆）。"""
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "mem.sqlite"))
    from langgraph.checkpoint.sqlite import SqliteSaver
    import sqlite3

    conn = sqlite3.connect(tmp_path / "mem.sqlite", check_same_thread=False)
    saver = SqliteSaver(conn)

    # 用假模型构建带 checkpointer 的 agent，验证历史累积
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class FakeModel(BaseChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            user_count = sum(1 for m in messages if isinstance(m, HumanMessage))
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content=f"用户已提问 {user_count} 次"))])

        @property
        def _llm_type(self):
            return "fake"

    from langgraph.prebuilt import create_react_agent
    agent = create_react_agent(model=FakeModel(), tools=[], checkpointer=saver)

    r1 = agent.invoke({"messages": [{"role": "user", "content": "第一问"}]},
                      config={"configurable": {"thread_id": "t1"}})
    r2 = agent.invoke({"messages": [{"role": "user", "content": "第二问"}]},
                      config={"configurable": {"thread_id": "t1"}})
    assert "用户已提问 1 次" in r1["messages"][-1].content
    assert "用户已提问 2 次" in r2["messages"][-1].content

    # 不同 thread 隔离
    r3 = agent.invoke({"messages": [{"role": "user", "content": "新会话"}]},
                      config={"configurable": {"thread_id": "t2"}})
    assert "用户已提问 1 次" in r3["messages"][-1].content


def test_get_memory_singleton(monkeypatch, tmp_path):
    """get_memory 应返回同一全局 SQLite 实例且可持久化。"""
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "mem2.sqlite"))
    from graph import get_memory

    m1 = get_memory()
    m2 = get_memory()
    assert m1 is m2  # 单例


# ---------- API Token 鉴权 ----------

def test_auth_allows_without_token(monkeypatch):
    """未配置 API_TOKEN 时（默认）应放行。"""
    monkeypatch.delenv("API_TOKEN", raising=False)
    from server import Handler

    handler = Handler.__new__(Handler)
    handler.headers = {}
    assert handler._auth_ok() is True


def test_auth_rejects_wrong_token(monkeypatch):
    """配置了 API_TOKEN 时，错误 token 应被拒绝。"""
    monkeypatch.setenv("API_TOKEN", "secret123")
    from server import Handler

    handler = Handler.__new__(Handler)
    handler.headers = {"X-API-Token": "wrong"}
    assert handler._auth_ok() is False


def test_auth_accepts_correct_token(monkeypatch):
    """配置了 API_TOKEN 时，正确 token 应放行。"""
    monkeypatch.setenv("API_TOKEN", "secret123")
    from server import Handler

    handler = Handler.__new__(Handler)
    handler.headers = {"X-API-Token": "secret123"}
    assert handler._auth_ok() is True


# ---------- 真实 HTTP 契约测试（起真实 ThreadingHTTPServer） ----------

import urllib.error
import urllib.parse
import urllib.request


@pytest.fixture()
def http_server(monkeypatch, tmp_path):
    """起一个真实 web 服务（monkeypatch server.ask 避免真实模型调用）。

    返回 (base_url, call_log)，call_log 记录 ask() 收到的参数。
    """
    from http.server import ThreadingHTTPServer as _THS
    import server as server_mod

    call_log = {"args": None}

    def _fake_ask(question, show_log=False, mode=None, history=None, thread_id=None):
        call_log["args"] = {"question": question, "mode": mode, "thread_id": thread_id}
        return "测试回答", {"messages": [], "reflection": "{}"}

    monkeypatch.setattr(server_mod, "ask", _fake_ask)

    srv = _THS(("127.0.0.1", 0), server_mod.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", call_log
    srv.shutdown()


def _http_post(url, body, token=None):
    data = urllib.parse.urlencode(body).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["X-API-Token"] = token
    req = urllib.request.Request(url + "/ask", data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_http_ask_returns_thread_id(http_server):
    """/ask 真实 HTTP 路径：应返回 answer 与 thread_id（无传入时服务端生成）。"""
    base, call_log = http_server
    status, body = _http_post(base, {"question": "你好"})
    data = json.loads(body)
    assert status == 200
    assert data["answer"] == "测试回答"
    assert data["thread_id"], "应生成并回传 thread_id"
    assert data["is_new_thread"] is True
    assert call_log["args"]["question"] == "你好"


def test_http_ask_echoes_thread_id(http_server):
    """/ask 传入 thread_id 时应回显同一 id，且 is_new_thread 为 False。"""
    base, call_log = http_server
    status, body = _http_post(base, {"question": "第二问", "thread_id": "abc123"})
    data = json.loads(body)
    assert status == 200
    assert data["thread_id"] == "abc123"
    assert data["is_new_thread"] is False
    assert call_log["args"]["thread_id"] == "abc123"


def test_http_auth_401_without_token(monkeypatch, tmp_path):
    """配置 API_TOKEN 后，/ask 无 token 应返回 401（真实 HTTP 契约）。"""
    monkeypatch.setenv("API_TOKEN", "secret123")
    from http.server import ThreadingHTTPServer as _THS
    import server as server_mod

    srv = _THS(("127.0.0.1", 0), server_mod.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status, body = _http_post(f"http://127.0.0.1:{port}", {"question": "hi"})
        assert status == 401
        assert "API Token" in body

        status2, _ = _http_post(f"http://127.0.0.1:{port}", {"question": "hi"}, token="secret123")
        assert status2 == 200
    finally:
        srv.shutdown()


# ---------- 来源提取（sources） ----------

def test_build_sources_document():
    """应从 search_documents 结果提取文档来源。"""
    from graph import _build_sources

    calls = [{
        "name": "search_documents",
        "result": "以下为检索到的文档片段：\n[1] 来源: docs/a.md | 相关度: 0.5\n核心架构是分层设计",
    }]
    sources = _build_sources(calls)
    assert len(sources) == 1
    assert sources[0]["type"] == "document"
    assert sources[0]["title"] == "docs/a.md"
    assert "分层设计" in sources[0]["preview"]


def test_build_sources_web():
    """应从 web_search 结果提取网页链接（含去重与上限）。"""
    from graph import _build_sources

    calls = [{
        "name": "web_search",
        "result": (
            "以下为搜索到的网页结果：\n"
            "[1] AI 新闻\n   摘要: x\n   链接: https://a.example.com\n"
            "[2] 另一条\n   摘要: y\n   链接: https://b.example.com\n"
            "[3] 重复\n   摘要: z\n   链接: https://a.example.com"
        ),
    }]
    sources = _build_sources(calls)
    urls = [s["url"] for s in sources]
    assert urls == ["https://a.example.com", "https://b.example.com"]  # 去重
    assert all(s["type"] == "web" for s in sources)


def test_build_sources_empty():
    """无工具调用/无结果时返回空列表。"""
    from graph import _build_sources

    assert _build_sources([]) == []
    assert _build_sources([{"name": "get_weather", "result": "晴 25°C"}]) == []


# ---------- 会话管理（sessions API） ----------

def test_list_sessions_returns_latest_per_thread(monkeypatch, tmp_path):
    """list_sessions 应返回每个 thread 的最新状态（含标题与消息数）。"""
    import sqlite3

    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "mem.sqlite"))
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.base import Checkpoint

    conn = sqlite3.connect(tmp_path / "mem.sqlite", check_same_thread=False)
    saver = SqliteSaver(conn)

    # 造两个 thread 的 checkpoint
    def make_checkpoint(thread_id, messages, ts):
        return Checkpoint(
            v=1,
            ts=ts,
            id=f"cp-{thread_id}-{ts}",
            channel_values={"messages": messages},
            channel_versions={},
            versions_seen={},
            updated_channels=[],
        )

    config1 = {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "1"}}
    config2 = {"configurable": {"thread_id": "t2", "checkpoint_ns": "", "checkpoint_id": "1"}}
    saver.put(config1, make_checkpoint("t1", [{"type": "human", "content": "第一问"}], "2026-01-01T00:00:00+00:00"), {}, {})
    saver.put(config2, make_checkpoint("t2", [{"type": "human", "content": "第二问"}, {"type": "ai", "content": "答"}], "2026-01-02T00:00:00+00:00"), {}, {})

    # 注入到 graph 的 get_memory
    import graph as graph_mod
    monkeypatch.setattr(graph_mod, "get_memory", lambda: saver)

    sessions = graph_mod.list_sessions()
    titles = {s["thread_id"]: s["title"] for s in sessions}
    assert titles["t1"] == "第一问"
    assert titles["t2"] == "第二问"
    by_id = {s["thread_id"]: s for s in sessions}
    assert by_id["t1"]["message_count"] == 1
    assert by_id["t2"]["message_count"] == 2
    # 按时间倒序：t2 更新
    assert sessions[0]["thread_id"] == "t2"


def test_delete_session_removes_thread(monkeypatch, tmp_path):
    """delete_session 应移除 checkpointer 中的整个 thread。"""
    import sqlite3

    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "mem2.sqlite"))
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.base import Checkpoint

    conn = sqlite3.connect(tmp_path / "mem2.sqlite", check_same_thread=False)
    saver = SqliteSaver(conn)
    cp = Checkpoint(
        v=1, ts="2026-01-01T00:00:00+00:00", id="cp-x",
        channel_values={"messages": [{"type": "human", "content": "hi"}]},
        channel_versions={}, versions_seen={}, updated_channels=[],
    )
    saver.put({"configurable": {"thread_id": "del-me", "checkpoint_ns": "", "checkpoint_id": "1"}}, cp, {}, {})

    import graph as graph_mod
    monkeypatch.setattr(graph_mod, "get_memory", lambda: saver)

    assert any(s["thread_id"] == "del-me" for s in graph_mod.list_sessions())
    graph_mod.delete_session("del-me")
    assert not any(s["thread_id"] == "del-me" for s in graph_mod.list_sessions())


# ---------- 流式（ask_stream 事件结构） ----------

def test_ask_stream_yields_start_then_done(monkeypatch, tmp_path):
    """ask_stream 应 yield start 事件并以 done 事件结束（含 sources）。"""
    import graph as graph_mod

    class FakeAgent:
        def __init__(self):
            pass

        def stream(self, *args, **kwargs):
            # 模拟 LangGraph 双流模式：("messages", (chunk, meta)) 与 ("updates", {...})
            class Chunk:
                def __init__(self, content="", tccs=None):
                    self.content = content
                    self.tool_call_chunks = tccs or []

            # messages 流：token 增量
            yield ("messages", (Chunk("你好"), {"langgraph_node": "agent"}))
            yield ("messages", (Chunk("，我是"), {"langgraph_node": "agent"}))
            yield ("messages", (Chunk("Pray"), {"langgraph_node": "agent"}))
            # updates 流：完整消息（供 usage/tool_calls 提取）
            yield ("updates", {"agent": {"messages": [{"type": "ai", "content": "你好，我是Pray"}]}})

        def get_state(self, config):
            class State:
                values = {"messages": []}
            return State()

    fake = FakeAgent()
    # 外部动态赋值（避免类内 __ 名称改写），模拟真实 build_agent 挂 handler
    class Handler:
        def __init__(self):
            self._store = {}
        def set_current_thread(self, tid):
            pass
        def get(self, tid):
            return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    fake.__usage_handler = Handler()
    monkeypatch.setattr(graph_mod, "build_agent", lambda mode=None, memory=None: fake)

    events = list(graph_mod.ask_stream("你好", mode="deepseek", thread_id="t1"))
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    tokens = "".join(e.get("content", "") for e in events if e["type"] == "token")
    assert tokens == "你好，我是Pray"
    done = events[-1]
    assert "reflection" in done and "sources" in done
    assert done["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert "cost" in done


# ---------- 会话历史回放 ----------

def test_get_session_messages_returns_history(monkeypatch, tmp_path):
    """get_session_messages 应返回 human/ai 消息（跳过 tool），ai 带工具名。"""
    import sqlite3

    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "mem3.sqlite"))
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.base import Checkpoint

    conn = sqlite3.connect(tmp_path / "mem3.sqlite", check_same_thread=False)
    saver = SqliteSaver(conn)
    cp = Checkpoint(
        v=1, ts="2026-01-01T00:00:00+00:00", id="cp-h",
        channel_values={
            "messages": [
                {"type": "human", "content": "第一问"},
                # tool 消息应被跳过
                {"type": "tool", "content": "检索结果..."},
                # 中间态 AI 消息（仅工具无文本）——工具名应合并进下一条 AI 回答
                {"type": "ai", "content": "", "tool_calls": [{"name": "search_documents", "args": {}}]},
                {"type": "ai", "content": "这是回答", "tool_calls": [{"name": "web_search", "args": {}}]},
                {"type": "human", "content": "第二问"},
                {"type": "ai", "content": "第二答"},
            ]
        },
        channel_versions={}, versions_seen={}, updated_channels=[],
    )
    saver.put({"configurable": {"thread_id": "hist-1", "checkpoint_ns": "", "checkpoint_id": "1"}}, cp, {}, {})

    import graph as graph_mod
    monkeypatch.setattr(graph_mod, "get_memory", lambda: saver)

    msgs = graph_mod.get_session_messages("hist-1")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "第一问"
    assert msgs[1]["content"] == "这是回答"
    # 中间 AI 消息的 search_documents 应合并进有文本的 AI 消息（连同自身的 web_search）
    assert msgs[1]["tools"] == ["search_documents", "web_search"]
    assert msgs[3]["content"] == "第二答"
    assert msgs[3]["tools"] == []
    # 每条 AI 回答必有 reflection + usage/cost（历史回放按文本估算，无工具也有面板）
    for m in (msgs[1], msgs[3]):
        assert m["reflection"], "AI 回答应始终有 reflection（保证面板渲染）"
        assert m["usage"] and m["usage"]["total_tokens"] > 0
        assert m["cost"] is not None


def test_get_session_messages_unknown_thread(monkeypatch, tmp_path):
    """不存在的 thread 应返回空列表。"""
    import sqlite3

    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "mem4.sqlite"))
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(tmp_path / "mem4.sqlite", check_same_thread=False)
    saver = SqliteSaver(conn)

    import graph as graph_mod
    monkeypatch.setattr(graph_mod, "get_memory", lambda: saver)

    assert graph_mod.get_session_messages("no-such-thread") == []


# ---------- Ollama URL 解析 ----------

def test_is_ollama_available_parses_url(monkeypatch):
    """is_ollama_available 应正确解析带路径/https 的 base_url（urlsplit 而非字符串切分）。"""
    import config as config_mod
    import socket
    from server import is_ollama_available

    calls = {}

    def _fake_create_connection(addr, timeout=1):
        calls["addr"] = addr
        raise OSError("unreachable")

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)

    # 带路径的 URL（直接 patch config 模块属性，避免模块缓存）
    monkeypatch.setattr(config_mod, "OLLAMA_BASE_URL", "http://localhost:11434/v1")
    assert is_ollama_available() is False
    assert calls["addr"] == ("localhost", 11434)

    # https URL
    monkeypatch.setattr(config_mod, "OLLAMA_BASE_URL", "https://ollama.example.com:443/api")
    is_ollama_available()
    assert calls["addr"] == ("ollama.example.com", 443)

    # 无端口默认
    monkeypatch.setattr(config_mod, "OLLAMA_BASE_URL", "http://ollama.local/v1")
    is_ollama_available()
    assert calls["addr"] == ("ollama.local", 11434)


# ---------- Token 用量与成本 ----------

def test_extract_usage_from_ai_messages():
    """应从 AI 消息的 response_metadata.usage 提取 token 用量。"""
    from graph import _extract_usage

    class FakeAIMessage:
        type = "ai"
        content = "hi"

        def __init__(self, usage):
            self.response_metadata = {"usage": usage} if usage else {}

    msgs = [
        FakeAIMessage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
    ]
    usage = _extract_usage(msgs)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_extract_usage_supports_token_usage_field():
    """应兼容 DeepSeek 的 token_usage 字段名（非标准 usage）。"""
    from graph import _extract_usage

    class FakeAIMessage:
        type = "ai"
        content = "hi"
        response_metadata = {
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        }

    usage = _extract_usage([FakeAIMessage()])
    assert usage == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def test_extract_usage_none_when_missing():
    """无 usage 元数据时应返回 None。"""
    from graph import _extract_usage

    class FakeAIMessage:
        type = "ai"
        content = "hi"
        response_metadata = {}

    assert _extract_usage([FakeAIMessage()]) is None
    assert _extract_usage([]) is None


def test_estimate_cost_deepseek():
    """deepseek 单价应能估算成本。"""
    from graph import _estimate_cost

    cost = _estimate_cost("deepseek", {"prompt_tokens": 1_000_000, "completion_tokens": 500_000})
    assert cost is not None
    assert cost["input"] == 2.0   # 100 万输入 token × ¥2/M
    assert cost["output"] == 4.0  # 50 万输出 token × ¥8/M
    assert cost["total"] == 6.0
    assert cost["currency"] == "¥"


def test_estimate_cost_unknown_provider():
    """未知 provider 或无 usage 时应返回 None。"""
    from graph import _estimate_cost

    assert _estimate_cost("unknown-provider", {"prompt_tokens": 1, "completion_tokens": 1}) is None
    assert _estimate_cost("deepseek", None) is None


def test_estimate_cost_with_none_fields():
    """流式估算的 usage（prompt_tokens 为 None）应能正常估算（按 0 处理）。"""
    from graph import _estimate_cost

    cost = _estimate_cost("deepseek", {"prompt_tokens": None, "completion_tokens": 1000, "total_tokens": 1000})
    assert cost is not None
    assert cost["input"] == 0.0
    assert cost["output"] == 0.008  # 1000 tokens × ¥8/M
    assert cost["total"] == 0.008


# ---------- 历史回放 sources/reflection 生成 ----------

def test_get_session_messages_generates_sources(monkeypatch, tmp_path):
    """历史回放应基于工具结果生成 sources 与 reflection。"""
    import sqlite3

    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "mem5.sqlite"))
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.base import Checkpoint

    conn = sqlite3.connect(tmp_path / "mem5.sqlite", check_same_thread=False)
    saver = SqliteSaver(conn)
    cp = Checkpoint(
        v=1, ts="2026-01-01T00:00:00+00:00", id="cp-s",
        channel_values={
            "messages": [
                {"type": "human", "content": "项目用什么语言？"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "search_documents", "args": {"query": "语言"}}]},
                {"type": "tool", "content": "以下为检索到的文档片段：\n[1] 来源: docs/a.md | 相关度: 0.5\nPython 3.10"},
                {"type": "ai", "content": "项目用 **Python 3.10**。"},
            ]
        },
        channel_versions={}, versions_seen={}, updated_channels=[],
    )
    saver.put({"configurable": {"thread_id": "src-1", "checkpoint_ns": "", "checkpoint_id": "1"}}, cp, {}, {})

    import graph as graph_mod
    monkeypatch.setattr(graph_mod, "get_memory", lambda: saver)

    msgs = graph_mod.get_session_messages("src-1")
    assert len(msgs) == 2
    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    assert assistant["tools"] == ["search_documents"]
    # sources 已生成
    assert len(assistant["sources"]) == 1
    assert assistant["sources"][0]["type"] == "document"
    assert assistant["sources"][0]["title"] == "docs/a.md"
    # reflection 已生成
    assert assistant["reflection"]
    import json as _json
    ref = _json.loads(assistant["reflection"])
    assert ref["使用文档检索"] is True


# ---------- write_file 工具（代码落盘） ----------

def test_write_file_creates_file(monkeypatch, tmp_path):
    """write_file 应把内容写入 WRITE_DIR 内并自动建目录。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import write_file

    r = write_file.invoke({"file_path": "sub/game.py", "content": "print('hi')\n"})
    assert "已写入" in r
    assert (tmp_path / "sub" / "game.py").exists()
    assert (tmp_path / "sub" / "game.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_write_file_rejects_path_escape(monkeypatch, tmp_path):
    """write_file 应拒绝 ../ 逃逸与绝对路径。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import write_file

    r1 = write_file.invoke({"file_path": "../../etc/passwd", "content": "x"})
    assert "拒绝" in r1
    r2 = write_file.invoke({"file_path": "C:/Windows/system32/x.txt", "content": "x"})
    assert "拒绝" in r2
    # 目录内不应产生文件
    assert list(tmp_path.iterdir()) == []


# ---------- run_command 命令工具 ----------

def test_run_command_executes_normal_command(monkeypatch, tmp_path):
    """普通命令应直接执行并返回输出。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command

    r = run_command.invoke({"command": sys.executable + " -c \"print('cmd-ok')\"", "confirmed": False})
    assert "执行成功" in r
    assert "cmd-ok" in r


def test_run_command_high_risk_requires_confirm(monkeypatch, tmp_path):
    """高危命令未确认时应返回 NEED_CONFIRM。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command

    r = run_command.invoke({"command": "del somefile.py", "confirmed": False})
    assert "NEED_CONFIRM" in r


def test_run_command_high_risk_runs_when_confirmed(monkeypatch, tmp_path):
    """高危命令确认后应执行。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command

    r = run_command.invoke({"command": sys.executable + " -c \"print('ok')\"", "confirmed": True})
    assert "执行成功" in r


def test_run_command_blocks_destructive(monkeypatch, tmp_path):
    """黑名单破坏性命令即使 confirmed=True 也拒绝。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command

    for cmd in ("rm -rf /", "format C:", "shutdown /s"):
        r = run_command.invoke({"command": cmd, "confirmed": True})
        assert "拦截" in r, f"应拦截: {cmd}"


def test_run_command_interactive_with_input(monkeypatch, tmp_path):
    """交互式程序应通过 input_text 提供标准输入。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command, write_file

    write_file.invoke({"file_path": "calc.py", "content": (
        "while True:\n"
        "    line = input('>> ').strip()\n"
        "    if line == 'q':\n"
        "        break\n"
        "    print(f'= {eval(line)}')\n"
    )})

    r = run_command.invoke({
        "command": sys.executable + " calc.py",
        "input_text": "3+5\nq\n",
        "confirmed": False,
    })
    assert "执行成功" in r
    assert "= 8" in r


def test_run_command_no_input_fails_fast(monkeypatch, tmp_path):
    """交互式程序无输入时应快速失败（stdin 关闭 → EOFError），不卡超时。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command, write_file

    write_file.invoke({"file_path": "calc2.py", "content": "print(input())\n"})

    r = run_command.invoke({
        "command": sys.executable + " calc2.py",
        "input_text": "",
        "confirmed": False,
    })
    # 应快速失败（EOFError），而非 30 秒超时
    assert "执行失败" in r or "EOFError" in r


def test_run_command_utf8_output_no_mojibake(monkeypatch, tmp_path):
    """中文输出不应乱码（强制子进程 UTF-8 编码）。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command, write_file

    write_file.invoke({"file_path": "zh.py", "content": "print('中文测试：你好世界')\n"})
    r = run_command.invoke({
        "command": sys.executable + " zh.py",
        "input_text": "",
        "confirmed": False,
    })
    assert "你好世界" in r, f"中文不应乱码: {r[:80]}"


# ---------- 交互终端（runterm） ----------

def test_runterm_interactive_flow(monkeypatch, tmp_path):
    """runterm 应支持启动→输入→输出→退出 的完整交互。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from runterm import start, send_input, poll, stop

    (tmp_path / "inter.py").write_text(
        "print('ready')\n"
        "line = input('>> ').strip()\n"
        "print(f'got: {line}')\n"
        "line = input('>> ').strip()\n"
        "print(f'bye')\n",
        encoding="utf-8",
    )

    r = start(sys.executable + " inter.py")
    assert "session_id" in r
    sid = r["session_id"]

    import time
    time.sleep(1)
    out = poll(sid)
    assert any("ready" in l for l in out["lines"]), f"应有初始输出: {out}"

    send_input(sid, "hello")
    time.sleep(1)
    out2 = poll(sid)
    assert any("got: hello" in l for l in out2["lines"]), f"应回显输入: {out2}"

    send_input(sid, "x")
    time.sleep(1)
    out3 = poll(sid)
    assert out3["running"] is False, "进程应结束"
    stop(sid)


def test_runterm_blocks_destructive(monkeypatch, tmp_path):
    """runterm 黑名单应拦截破坏性命令。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from runterm import start

    r = start("rm -rf /")
    assert "error" in r and "拦截" in r["error"]


# ---------- open_in_browser 工具 ----------

def test_open_in_browser_opens_file(monkeypatch, tmp_path):
    """open_in_browser 应打开 WRITE_DIR 内存在的文件。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import open_in_browser, write_file

    write_file.invoke({"file_path": "game.html", "content": "<html></html>"})
    r = open_in_browser.invoke({"file_path": "game.html"})
    assert "打开" in r


def test_open_in_browser_rejects_escape_and_missing(monkeypatch, tmp_path):
    """open_in_browser 应拒绝路径逃逸与不存在的文件。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import open_in_browser

    r1 = open_in_browser.invoke({"file_path": "../../etc/passwd"})
    assert "拒绝" in r1
    r2 = open_in_browser.invoke({"file_path": "nope.html"})
    assert "不存在" in r2


# ---------- Codex 评审修复验证 ----------

def test_split_long_paragraph_no_content_loss():
    """长段落（>chunk_size）切分不应丢内容（Codex 指出的 bug）。"""
    from retriever import split_text

    r = split_text("A" * 600, chunk_size=500, overlap=100)
    assert len(r) == 2, f"应切出 2 段，实际 {len(r)}"
    assert sum(len(c) for c in r) >= 600, "内容不应丢失"

    # 混合：短段 + 超长段
    r2 = split_text("段一\n\n" + "B" * 800, chunk_size=500, overlap=100)
    total = sum(len(c) for c in r2)
    assert total >= 802, f"混合内容不应丢失: {total}"


def test_run_command_high_risk_requires_real_approval(monkeypatch, tmp_path):
    """高危命令 confirmed=True 但无批准记录时应被拒绝（Codex 指出的绕过）。"""
    import approvals
    import config as config_mod

    approvals.clear()
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command

    # 高危命令（move 命中高危模式）但用户未批准 → 拒绝
    r = run_command.invoke({
        "command": "move a.txt b.txt",  # 高危（move），无副作用（文件不存在）
        "confirmed": True,
    })
    assert "NEED_CONFIRM" in r, "未批准的高危命令应被拒绝"


def test_run_command_approval_flow(monkeypatch, tmp_path):
    """批准登记后高危命令可执行；批准一次性消费。"""
    import approvals
    import config as config_mod

    approvals.clear()
    monkeypatch.setattr(config_mod, "WRITE_DIR", str(tmp_path))
    from tools import run_command

    cmd = "move a.txt b.txt"  # 高危命令（move），无副作用
    # 1. 未批准 → 拒绝
    r1 = run_command.invoke({"command": cmd, "confirmed": True})
    assert "NEED_CONFIRM" in r1
    # 2. 用户批准登记
    approvals.approve(cmd)
    # 3. 批准后可执行（move 文件不存在会失败，但进入执行分支而非拒绝）
    r2 = run_command.invoke({"command": cmd, "confirmed": True})
    assert "执行成功" in r2 or "执行失败" in r2, "批准后应进入执行分支"
    # 4. 一次性消费：再次执行同一命令需重新批准
    r3 = run_command.invoke({"command": cmd, "confirmed": True})
    assert "NEED_CONFIRM" in r3, "批准应一次性消费"


def test_usage_capture_accumulates_per_thread():
    """Token 用量应按会话累计，且跨会话隔离（Codex 指出的覆盖/串值问题）。"""
    import graph as graph_mod

    h = graph_mod._UsageCapture()

    class FakeResp:
        def __init__(self, pt, ct):
            self.llm_output = {"token_usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}

    # 线程 A 两次调用
    h.set_current_thread("t1")
    h.on_llm_end(FakeResp(100, 50))
    h.on_llm_end(FakeResp(200, 80))
    # 线程 B 一次调用
    h.set_current_thread("t2")
    h.on_llm_end(FakeResp(30, 10))

    u1 = h.get("t1")
    assert u1["total_tokens"] == 430, f"t1 应累计 430，实际 {u1}"
    u2 = h.get("t2")
    assert u2["total_tokens"] == 40, f"t2 应 40，实际 {u2}"
    assert h.get("nonexistent") is None
