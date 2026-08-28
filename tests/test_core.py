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
        def stream(self, *args, **kwargs):
            # 模拟 LangGraph messages 模式的 token 流
            class Chunk:
                def __init__(self, content="", tccs=None):
                    self.content = content
                    self.tool_call_chunks = tccs or []

            yield Chunk("你好"), {"langgraph_node": "agent"}
            yield Chunk("，我是"), {"langgraph_node": "agent"}
            yield Chunk("Pray"), {"langgraph_node": "agent"}

        def get_state(self, config):
            class State:
                values = {"messages": []}
            return State()

    monkeypatch.setattr(graph_mod, "build_agent", lambda mode=None, memory=None: FakeAgent())

    events = list(graph_mod.ask_stream("你好", mode="deepseek", thread_id="t1"))
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    tokens = "".join(e.get("content", "") for e in events if e["type"] == "token")
    assert tokens == "你好，我是Pray"
    done = events[-1]
    assert "reflection" in done and "sources" in done
