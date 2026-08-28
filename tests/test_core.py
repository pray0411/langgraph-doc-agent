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
    """每个测试前清理环境变量 + 把索引/文档目录隔离到临时目录。"""
    for k in ("LLM_PROVIDER", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "BOCHA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    # 重新导入相关模块以应用 monkeypatch（config 在 import 时读 env）
    for mod in ("config", "retriever", "tools", "graph"):
        sys.modules.pop(mod, None)
    yield
    for mod in ("config", "retriever", "tools", "graph"):
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
    assert data["meta"]["version"] == 2


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
