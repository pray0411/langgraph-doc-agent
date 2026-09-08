"""Pray MCP 客户端演示：用标准 MCP SDK 连接 mcp_server（stdio），支持交互问答。

不需要 Claude 账号/任何外部客户端——直接证明 Pray 的 MCP server 协议层正常，
并可当面试演示（"任何 MCP 客户端都能连 Pray"）。

用法:
    python -X utf8 mcp_demo.py

交互命令:
    <直接输入>      提问（走 pray 的 ask 工具，含多轮记忆）
    /new            开启全新会话
    /mem            查看全局记忆
    /tools          重新列出工具
    /exit           退出
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-X", "utf8", str(REPO / "mcp_server.py")],
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("=" * 56)
            print("Pray MCP 客户端演示（stdio 连接成功）")
            print(f"已注册工具 {len(names)} 个: {', '.join(names)}")
            print("=" * 56)
            print("直接输入问题提问；/new 新会话 /mem 看记忆 /tools 列工具 /exit 退出\n")

            while True:
                try:
                    q = input("你 > ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n再见。")
                    return
                if not q:
                    continue
                if q == "/exit":
                    print("再见。")
                    return
                if q == "/new":
                    r = await session.call_tool("ask", {"question": "开始新会话", "new_thread": True})
                    print("Pray > 已开启新会话。\n")
                    continue
                if q == "/mem":
                    r = await session.call_tool("list_memory", {})
                    print("Pray > " + _text(r) + "\n")
                    continue
                if q == "/tools":
                    print(f"工具: {', '.join(names)}\n")
                    continue
                if q.startswith("/"):
                    print(f"未知命令: {q}（可用 /new /mem /tools /exit）\n")
                    continue

                r = await session.call_tool("ask", {"question": q})
                print("Pray > " + _text(r) + "\n")


def _text(result) -> str:
    if getattr(result, "is_error", False):
        return f"[工具错误] {result.content}"
    parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    return (parts[0] if parts else "(空)").strip()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
