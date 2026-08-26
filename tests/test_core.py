# -*- coding: utf-8 -*-
"""基础测试：覆盖检索阈值、离线模式、工具、配置。

运行: python -m pytest tests/ -v
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------- 检索 ----------

def test_search_relevant_query_returns_hits():
    """文档相关问题应命中检索。"""
    from retriever import build_index, search

    build_index(force=True)
    hits = search("核心架构是什么", top_k=3)
    assert len(hits) > 0
    assert hits[0]["score"] > 0


def test_search_irrelevant_query_returns_empty():
    """完全无关的问题应被阈值拒答，返回空。"""
    from retriever import build_index, search

    build_index(force=True)
    hits = search("量子物理和弦理论的区别", top_k=3)
    assert hits == []


def test_search_respects_min_score():
    """自定义 min_score 应生效。"""
    from retriever import build_index, search

    build_index(force=True)
    # 用很高的阈值，应全部被过滤
    hits = search("核心架构", top_k=3, min_score=0.99)
    assert hits == []


# ---------- 离线模式 ----------

def test_offline_mode_no_crash():
    """离线模式应能正常返回（不崩溃）。"""
    os.environ["LLM_PROVIDER"] = "offline"
    try:
        from graph import ask

        answer, result = ask("核心架构是什么？")
        assert isinstance(answer, str)
        assert len(answer) > 0
        assert "messages" in result
        assert "reflection" in result
    finally:
        os.environ.pop("LLM_PROVIDER", None)


def test_offline_mode_returns_retrieval():
    """离线模式应返回检索结果（含'离线演示'标记）。"""
    os.environ["LLM_PROVIDER"] = "offline"
    try:
        from graph import ask

        answer, _ = ask("这个项目的核心架构是什么？")
        assert "离线演示" in answer
    finally:
        os.environ.pop("LLM_PROVIDER", None)


# ---------- 配置 ----------

def test_config_defaults():
    """配置默认值应合理。"""
    from config import LLM_PROVIDER, TOP_K

    assert TOP_K > 0
    assert LLM_PROVIDER in ("deepseek", "openai", "offline")


# ---------- 工具 ----------

def test_search_documents_tool():
    """文档检索工具应返回格式化结果。"""
    from tools import search_documents

    out = search_documents.invoke({"query": "核心架构"})
    assert isinstance(out, str)


def test_weather_tool_signature():
    """天气工具应可调用（网络可用时返回天气，不可用时返回错误信息而非崩溃）。"""
    from tools import get_weather

    out = get_weather.invoke({"city": "北京"})
    assert isinstance(out, str)
    assert len(out) > 0


# ---------- 编码完整性 ----------

def test_no_duplicate_methods_in_llm():
    """llm.py 不应有重复的方法定义。"""
    import ast

    tree = ast.parse(Path("llm.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            assert len(methods) == len(set(methods)), f"{node.name} 有重复方法"


def test_graph_v1_is_valid_python():
    """graph_v1.py 应为合法 Python（UTF-8 编码）。"""
    import ast

    src = Path("graph_v1.py").read_text(encoding="utf-8")
    ast.parse(src)  # 不应抛异常
