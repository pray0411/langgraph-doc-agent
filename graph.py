"""通用 Agent：基于 LangGraph create_react_agent 构建（ReAct 模式）。

核心思想：不再用规则判断"该走哪条路"，而是把工具交给模型，
由模型自主决定：
  - 文档问题 -> 调用 search_documents
  - 实时问题 -> 调用 web_search / get_weather
  - 普通对话 -> 直接回答
  - 需要多步 -> 连续调用多个工具（思考 -> 行动 -> 观察 -> 再思考）

这是 LangGraph 最主流的 Agent 架构（ReAct / Tool-calling Agent）。
"""
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from config import DEEPSEEK_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROVIDER
from tools import get_weather, search_documents, web_search


class AgentResult(TypedDict, total=False):
    answer: str
    messages: list[str]


class _OfflineTools:
    """离线模式：不调用模型，直接尝试文档检索，返回结果。"""

    def invoke(self, question: str):
        from retriever import search as _search

        hits = _search(question, top_k=2)
        if hits:
            return f"[离线演示] 文档检索到相关片段：\n{hits[0]['chunk'][:500]}"
        return (
            "[离线演示] 当前为离线模式（LLM_PROVIDER=offline），无法联网或调用模型。"
            "请配置 DeepSeek/OpenAI API Key 获得完整能力。"
        )


def build_agent():
    """构建通用 Agent。离线模式返回简化实现，在线模式用 ReAct Agent。"""
    provider = LLM_PROVIDER.lower()
    if provider == "offline":
        return _OfflineTools()

    api_key = DEEPSEEK_API_KEY if provider == "deepseek" else None
    base_url = LLM_BASE_URL or ("https://api.deepseek.com/v1" if provider == "deepseek" else None)
    model = ChatOpenAI(
        model=LLM_MODEL,
        api_key=api_key,
        base_url=base_url,
        temperature=0.3,
    )
    return create_react_agent(model=model, tools=[search_documents, web_search, get_weather])


def ask(question: str, confirmed: bool = True, show_log: bool = False) -> tuple[str, AgentResult]:
    """对外问答入口：执行一次 Agent 推理，返回 (回答, 结果详情)。"""
    agent = build_agent()
    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    messages = result.get("messages", [])
    answer = messages[-1].content if messages else ""

    # 提取过程日志（仅显示 AI/工具消息摘要）
    log = []
    for m in messages:
        role = getattr(m, "type", "")
        content = getattr(m, "content", "")
        if role == "ai" and content:
            log.append(f"[模型] {content[:120]}")
        elif role == "tool":
            log.append(f"[工具调用] {content[:120]}")

    if show_log:
        for line in log:
            print(line)
    return str(answer), {"answer": str(answer), "messages": log}
