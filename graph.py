"""通用 Agent：基于 LangGraph create_react_agent 构建（ReAct 模式）。

核心思想：不再用规则判断"该走哪条路"，而是把工具交给模型，
由模型自主决定：
  - 文档问题 -> 调用 search_documents
  - 实时问题 -> 调用 web_search / get_weather
  - 普通对话 -> 直接回答
  - 需要多步 -> 连续调用多个工具（思考 -> 行动 -> 观察 -> 再思考）

这是 LangGraph 最主流的 Agent 架构（ReAct / Tool-calling Agent）。

V2 说明：架构从 V1 的"规则路由 + 人工确认 + 反思"演进为 ReAct 通用 Agent，
人工确认与反思改由模型在循环内自然处理（工具结果即观察）。对外仍保留
confirmed 参数与 reflection 字段以兼容调用方。
"""
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from config import DEEPSEEK_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROVIDER
from tools import get_weather, search_documents, web_search


class AgentResult(TypedDict, total=False):
    answer: str
    messages: list[str]
    reflection: str


class _OfflineTools:
    """离线模式：不调用模型/网络，用本地检索给出可运行的演示结果。

    定位：演示 RAG 检索链路（文档检索 -> 返回片段），而非模拟完整对话。
    与 ReAct Agent 保持一致的 invoke(messages) 接口。

    行为：
    - 文档相关问题 -> 返回检索结果片段（含相似度）
    - 检索不到 -> 提示换一种与文档相关的问法
    - 明显闲聊/非文档问题 -> 说明离线模式的能力边界
    """

    def invoke(self, state: dict) -> dict:
        question = ""
        for m in state.get("messages", []):
            if m.get("role") == "user":
                question = m.get("content", "")
                break

        from config import TOP_K
        from retriever import search as _search

        # 离线模式用更高的阈值，避免"1+1"这种无关问题误命中文档
        hits = _search(question, top_k=TOP_K, min_score=0.1)
        if hits:
            # 文档相关问题：返回检索到的片段
            parts = [
                f"[离线演示] 以下为本地文档检索结果（未调用模型）：",
                f"命中 {len(hits)} 个片段，最高相似度 {hits[0]['score']:.3f}",
            ]
            for i, h in enumerate(hits, 1):
                parts.append(f"\n[{i}] 来源: {h['source']}\n{h['chunk'][:400]}")
            answer = "\n".join(parts)
        else:
            # 无检索结果：区分原因，给出更有用的提示
            answer = (
                "[离线演示] 离线模式只能在本地文档知识库中检索相关内容。\n"
                "当前问题在文档中未检索到匹配内容，请尝试：\n"
                "1. 问与项目文档相关的问题（如：这个项目用什么技术栈？）\n"
                "2. 或切换到在线模式（页面顶部按钮）获得完整能力（对话/天气/联网搜索）"
            )
        return {"messages": [{"role": "assistant", "content": answer}]}


def build_agent(mode: str | None = None):
    """构建通用 Agent。离线模式返回本地检索实现，在线模式用 ReAct Agent。

    mode: 可选，显式指定模式（deepseek/openai/ollama/offline）；不传则用配置默认值。
    支持运行时动态切换（如网页端按钮切换）。
    """
    provider = (mode or LLM_PROVIDER).lower()
    if provider == "offline":
        return _OfflineTools()

    from config import OLLAMA_BASE_URL, OLLAMA_MODEL

    if provider == "ollama":
        # 本地 Ollama 模型：OpenAI 兼容接口，无需 API Key
        model = ChatOpenAI(
            model=OLLAMA_MODEL,
            api_key="ollama",  # Ollama 忽略 Key，占位即可
            base_url=OLLAMA_BASE_URL,
            temperature=0.3,
        )
        return create_react_agent(model=model, tools=[search_documents, web_search, get_weather])

    api_key = DEEPSEEK_API_KEY if provider == "deepseek" else None
    base_url = LLM_BASE_URL or ("https://api.deepseek.com/v1" if provider == "deepseek" else None)
    model = ChatOpenAI(
        model=LLM_MODEL,
        api_key=api_key,
        base_url=base_url,
        temperature=0.3,
    )
    return create_react_agent(model=model, tools=[search_documents, web_search, get_weather])


def ask(
    question: str,
    confirmed: bool = True,
    show_log: bool = False,
    mode: str | None = None,
) -> tuple[str, AgentResult]:
    """对外问答入口：执行一次 Agent 推理，返回 (回答, 结果详情)。

    confirmed: 保留的兼容参数。V2 的 ReAct 循环中，模型自主决定是否继续，
    不再有独立的人工确认节点。
    mode: 可选，动态指定运行模式（deepseek/openai/offline），不传用配置默认。
    """
    agent = build_agent(mode)
    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    messages = result.get("messages", [])
    if not messages:
        return "", {"answer": "", "messages": [], "reflection": ""}

    # 兼容 dict 与 AIMessage 两种消息类型
    last = messages[-1]
    answer = last.get("content", "") if isinstance(last, dict) else getattr(last, "content", "")
    answer = str(answer)

    # 提取过程日志
    log = []
    tool_calls = []
    for m in messages:
        role = m.get("role", "") if isinstance(m, dict) else getattr(m, "type", "")
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if role in ("ai", "assistant") and content:
            log.append(f"[模型] {str(content)[:120]}")
        elif role == "tool":
            log.append(f"[工具调用] {str(content)[:120]}")
            tool_calls.append(str(content))

    # 反思记录：基于实际执行过程生成
    reflection = _build_reflection(question, answer, tool_calls, mode or LLM_PROVIDER)

    if show_log:
        for line in log:
            print(line)
    return answer, {"answer": answer, "messages": log, "reflection": reflection}


def _build_reflection(question: str, answer: str, tool_calls: list[str], mode: str) -> str:
    """生成反思记录：检查回答是否基于检索/搜索内容（grounded）。

    统计本次问答实际调用了哪些工具、回答是否与工具结果相关，
    输出结构化的反思 JSON，供前端展示与后续优化。
    """
    import json as _json

    used_doc_search = any("来源" in c or "文档" in c for c in tool_calls)
    used_web_search = any("链接" in c for c in tool_calls)
    used_weather = any("°C" in c or "天气" in c for c in tool_calls)

    # grounded 检查：回答是否包含工具返回的关键内容
    grounded = False
    for c in tool_calls:
        # 取工具结果中的关键片段（去掉链接等噪声），看是否出现在回答里
        sample = c[:200].replace("\n", " ")
        if len(sample) > 20 and any(word in answer for word in sample.split()[:8]):
            grounded = True
            break

    reflection = {
        "问题": question,
        "运行模式": mode,
        "工具调用数": len(tool_calls),
        "使用文档检索": used_doc_search,
        "使用联网搜索": used_web_search,
        "使用天气查询": used_weather,
        "回答是否基于工具结果": grounded,
        "回答长度": len(answer),
        "说明": "基于实际执行过程的自动反思（ReAct 循环内模型自主决策）",
    }
    return _json.dumps(reflection, ensure_ascii=False, indent=2)