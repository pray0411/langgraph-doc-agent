# -*- coding: utf-8 -*-
"""MCP Server 与桌面端（pywebview）封装测试。

运行: python -m pytest tests/test_mcp_desktop.py -v

设计原则：
- MCP 工具函数直接单测（不经过 stdio 传输，避免子进程与真实模型调用）
- 网络调用（graph.ask / tools）全部 monkeypatch
- 桌面端只测"起服务 + 首页 200"（无窗口环境不开 WebView）
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ---------- MCP：工具注册与 ask ----------

def test_mcp_tools_registered():
    """MCP server 应注册预期工具集（整包 ask + 拆件只读工具）。"""
    import asyncio

    import mcp_server

    async def _list():
        tools = await mcp_server.mcp.list_tools()
        return {t.name for t in tools}

    names = asyncio.run(_list())
    assert {"ask", "search_documents", "web_search", "get_current_time",
            "list_memory", "remember", "forget"} <= names
    # 安全边界：执行类高危工具绝不暴露
    assert not names & {"run_command", "write_file", "edit_file",
                        "open_in_browser", "fetch_url", "read_file", "list_files"}


def test_mcp_ask_calls_graph_and_returns_answer(monkeypatch, tmp_path):
    """MCP ask 应调用 graph.ask（非流式）并返回答案文本。"""
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "m.sqlite"))
    monkeypatch.setenv("GLOBAL_MEMORY_DB", str(tmp_path / "gm.sqlite"))
    import graph as graph_mod
    import memory as memory_mod
    import mcp_server

    memory_mod.clear()
    graph_mod._agent_cache.clear()
    try:
        calls = {}

        def _fake_ask(question, show_log=False, mode=None, history=None, thread_id=None):
            calls["thread_id"] = thread_id
            return "这是测试回答", {"sources": [{"title": "x", "url": "http://x", "type": "web"}]}

        monkeypatch.setattr(graph_mod, "ask", _fake_ask)
        # 全局记忆已有一条 → 注入/记忆不报错即可（ask 是否利用由模型决定）
        memory_mod.remember("称呼", "测试")

        # 默认沿用同一会话 thread
        r1 = mcp_server.ask("第一问")
        assert "这是测试回答" in r1 and "参考资料" in r1
        tid1 = calls["thread_id"]

        r2 = mcp_server.ask("第二问")
        assert "这是测试回答" in r2
        assert calls["thread_id"] == tid1, "默认应沿用同一会话（多轮记忆）"

        # new_thread=True 应开新会话
        mcp_server.ask("新会话", new_thread=True)
        assert calls["thread_id"] != tid1, "new_thread 应切换 thread_id"
    finally:
        memory_mod.clear()
        graph_mod._agent_cache.clear()


def test_mcp_ask_handles_graph_error(monkeypatch, tmp_path):
    """graph.ask 抛异常时 MCP ask 应返回友好错误而非崩溃。"""
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "m2.sqlite"))
    import graph as graph_mod
    import mcp_server

    def _boom(*a, **k):
        raise RuntimeError("模型调用失败")

    monkeypatch.setattr(graph_mod, "ask", _boom)
    r = mcp_server.ask("问题")
    assert "处理失败" in r and "模型调用失败" in r


def test_mcp_memory_tools(monkeypatch, tmp_path):
    """MCP remember/list_memory/forget 应读写全局记忆。"""
    monkeypatch.setenv("GLOBAL_MEMORY_DB", str(tmp_path / "gm2.sqlite"))
    import memory as memory_mod
    import mcp_server

    memory_mod.clear()
    try:
        assert "暂无" in mcp_server.list_memory()
        r = mcp_server.remember("身份", "应届生")
        assert "已记住" in r
        listed = mcp_server.list_memory()
        assert "身份：应届生" in listed
        assert "已遗忘" in mcp_server.forget("身份")
        assert "暂无" in mcp_server.list_memory()
        assert "没有" in mcp_server.forget("身份")
    finally:
        memory_mod.clear()


def test_mcp_search_and_web_wrap_tools(monkeypatch, tmp_path):
    """MCP search_documents / web_search 应调用 tools 包装。"""
    import tools as tools_mod
    import mcp_server

    calls = {}

    class _FakeTool:
        def invoke(self, args):
            calls.update(args)
            return f"工具返回: {args}"

    monkeypatch.setattr(tools_mod, "search_documents", _FakeTool())
    monkeypatch.setattr(tools_mod, "web_search", _FakeTool())
    r = mcp_server.search_documents("Pray 是什么", top_k=5)
    assert "工具返回" in r and calls.get("query") == "Pray 是什么" and calls.get("top_k") == 5
    mcp_server.web_search("AI 新闻")
    assert calls.get("query") == "AI 新闻"


# ---------- 桌面端（pywebview 封装） ----------

def test_desktop_start_server_serves_index():
    """desktop.start_server 应起服务并返回可访问的 URL。"""
    import desktop

    srv, url = desktop.start_server(0)  # 随机空闲端口
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
        assert "chatArea" in body, "首页应含聊天界面"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- 桌面版更新检查 ----------

def test_desktop_parse_version():
    """版本号解析应支持 v 前缀/多段/异常输入。"""
    import desktop

    assert desktop._parse_version("v1.2.3") == (1, 2, 3)
    assert desktop._parse_version("1.10") == (1, 10)
    assert desktop._parse_version("v1.2.3-rc1") == (1, 2, 3)  # 预发布取主版本比较
    assert desktop._parse_version("garbage") == (0,)
    assert desktop._parse_version("") == (0,)
    assert desktop._parse_version("1.2.3") > desktop._parse_version("1.2.0")
    assert desktop._parse_version("1.2.3") < desktop._parse_version("1.2.10")


def test_desktop_check_for_update_newer(monkeypatch):
    """Release tag 高于本地版本时应返回更新信息。"""
    import json as _json
    import desktop

    class _Resp:
        def __init__(self, payload):
            self._p = _json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return self._p

    def _fake_urlopen(req, timeout=6):
        return _Resp({
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/pray0411/langgraph-doc-agent/releases/tag/v9.9.9",
        })

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    info = desktop.check_for_update()
    assert info is not None
    assert info["version"] == "9.9.9"
    assert "releases" in info["url"]


def test_desktop_check_for_update_current_or_fail(monkeypatch):
    """已是最新 / 网络失败 / 无 release 均应静默返回 None。"""
    import json as _json
    import desktop

    # 1) 与本地同版本 → None
    same = desktop.APP_VERSION

    class _Resp:
        def __init__(self, payload):
            self._p = _json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return self._p

    def _ok(req, timeout=6):
        return _Resp({"tag_name": "v" + same, "html_url": "x"})

    monkeypatch.setattr(urllib.request, "urlopen", _ok)
    assert desktop.check_for_update() is None

    # 2) 网络失败 → None（不崩溃）
    def _boom(req, timeout=6):
        raise OSError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert desktop.check_for_update() is None

    # 3) 404（无 release）→ None
    def _nope(req, timeout=6):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _nope)
    assert desktop.check_for_update() is None


def test_desktop_self_check_ok():
    """desktop --check 无窗口自检应返回 0。"""
    import desktop

    assert desktop.self_check(0) == 0
