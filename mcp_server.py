"""Pray 的 MCP Server：把通用 Agent 能力暴露给任意 MCP 客户端（Claude Desktop 等）。

两种暴露模式：
- 整包模式：ask() —— 一次调用走 Pray 完整的 agent（检索/联网/工具/记忆/会话），
  客户端把它当"一个会干活的助手"
- 拆件模式：search_documents() / web_search() / list_memory() / remember() /
  forget() / get_current_time() —— 客户端（模型）自己编排轻量子任务

安全边界（重要）：
- **不暴露** run_command / write_file / edit_file / open_in_browser / fetch_url
  等有副作用的执行类工具——MCP 客户端无法弹 Pray 前端的高危确认闸，
  本 server 只开放"只读检索 + 问答 + 记忆"面
- 会话：模块级默认会话（thread_id）承载多轮上下文；ask(new_thread=True) 开新会话

用法:
    python -X utf8 mcp_server.py        # stdio 传输（Claude Desktop 默认）
    python -X utf8 mcp_server.py --http # 额外起 Streamable HTTP（MCP Inspector 等）
接入配置见 README「MCP 接入」一节。
"""
import argparse
import threading
import uuid

from fastmcp import FastMCP

import memory as memory_mod

# 默认会话：一次 MCP 进程内，ask 不带 new_thread 时沿用同一 thread（多轮记忆）
_thread_id = uuid.uuid4().hex
_thread_lock = threading.Lock()

mcp = FastMCP("Pray")


def _answer_text(answer: str, extra: str = "") -> str:
    """把 ask() 的结果整理成返回文本。"""
    answer = (answer or "").strip()
    if not answer:
        return "（Pray 未能生成回答，请重试或换个问法）"
    return answer + (("\n\n" + extra) if extra else "")


@mcp.tool
def ask(question: str, new_thread: bool = False) -> str:
    """向 Pray 提问（整包问答：自动检索本地文档/联网搜索/调用工具/带多轮记忆）。

    推荐入口：把 Pray 当作完整助手来用。它知道当前时间、会用工具核实再回答，
    并记得同一会话内之前的对话。问题复杂时它会自己拆解多步完成。

    Args:
        question: 用户的问题
        new_thread: 设为 True 开启全新会话（清空此前的对话记忆），默认沿用当前会话
    """
    global _thread_id
    from graph import ask as _ask

    if new_thread:
        with _thread_lock:
            _thread_id = uuid.uuid4().hex
    with _thread_lock:
        tid = _thread_id
    try:
        answer, result = _ask(question, thread_id=tid)
        sources = result.get("sources") or []
        if sources:
            extra = "参考资料：\n" + "\n".join(
                f"- {s.get('title') or s.get('url') or ''}（{s.get('type', '')}）"
                for s in sources[:5]
            )
        else:
            extra = ""
        return _answer_text(answer, extra)
    except Exception as exc:  # noqa: BLE001
        return f"（Pray 处理失败: {exc}）"


@mcp.tool
def search_documents(query: str, top_k: int = 3) -> str:
    """检索本地文档知识库（含用户上传的文档）。

    Args:
        query: 检索词/问题
        top_k: 返回片段数（默认 3）
    """
    from tools import search_documents as _tool

    try:
        return _tool.invoke({"query": query, "top_k": max(1, min(int(top_k), 10))})
    except Exception as exc:  # noqa: BLE001
        return f"检索失败: {exc}"


@mcp.tool
def web_search(query: str) -> str:
    """联网搜索实时信息（新闻/天气/事实核验等）。

    Args:
        query: 搜索关键词
    """
    from tools import web_search as _tool

    try:
        return _tool.invoke({"query": query})
    except Exception as exc:  # noqa: BLE001
        return f"搜索失败: {exc}"


@mcp.tool
def get_current_time() -> str:
    """获取当前本地日期时间（含星期/时区）。"""
    from tools import get_current_time as _tool

    return _tool.invoke({})


@mcp.tool
def list_memory() -> str:
    """列出 Pray 对用户的全局记忆（跨会话长期信息，如称呼/身份/偏好）。"""
    items = memory_mod.list_memory()
    if not items:
        return "（暂无全局记忆）"
    return "\n".join(f"- {m['key']}：{m['value']}（更新于 {m['updated_at']}）" for m in items)


@mcp.tool
def remember(key: str, value: str) -> str:
    """记录一条关于用户的长期信息（跨会话保留，同 key 覆盖旧值）。

    当用户透露稳定信息（称呼/身份/语言偏好/正在做的事等）时调用。
    只记长期有用的画像事实，不记一次性内容。

    Args:
        key: 记忆键（如 称呼 / 身份 / 语言偏好）
        value: 记忆内容（一句话）
    """
    try:
        r = memory_mod.remember(key, value)
        return f"已记住：{r['key']} = {r['value']}"
    except ValueError as exc:
        return f"记忆失败: {exc}"


@mcp.tool
def forget(key: str) -> str:
    """删除一条全局记忆（用户要求遗忘时调用）。

    Args:
        key: 要删除的记忆键（与 remember 时一致）
    """
    return f"已遗忘：{key}" if memory_mod.forget(key) else f"记忆里没有 {key}"


def main():
    parser = argparse.ArgumentParser(description="Pray MCP Server")
    parser.add_argument(
        "--http", action="store_true",
        help="以 Streamable HTTP 模式运行（默认 stdio，供 Claude Desktop 等使用）",
    )
    args = parser.parse_args()
    print("[Pray-MCP] 工具就绪：ask / search_documents / web_search / 记忆管理", flush=True)
    if args.http:
        mcp.run(transport="http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
