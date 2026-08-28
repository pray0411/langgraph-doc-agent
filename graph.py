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
    """离线模式：不调用模型/网络，用本地检索 + 内置规则回答。

    定位：零依赖可运行的演示模式。
    - 简单问题（数学/时间/问候/身份）-> 内置规则引擎直接回答
    - 文档相关问题 -> 返回检索结果片段（含相似度）
    - 其他 -> 提示离线能力边界，引导切换模式
    """

    def invoke(self, state: dict) -> dict:
        question = ""
        for m in state.get("messages", []):
            if m.get("role") == "user":
                question = m.get("content", "")
                break

        # 1. 先用内置规则引擎回答简单问题（数学/时间/问候等，无需模型）
        from rule_engine import try_rule_answer

        rule_answer = try_rule_answer(question)
        if rule_answer:
            answer = f"[离线演示] {rule_answer}"
            return {"messages": [{"role": "assistant", "content": answer}]}

        # 2. 规则答不了，尝试文档检索
        from config import TOP_K
        from retriever import search as _search

        # 离线模式用稍高的阈值，避免"1+1"这种无关问题误命中文档
        hits = _search(question, top_k=TOP_K, min_score=0.05)
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
            # 无检索结果：说明离线能力边界
            answer = (
                "[离线演示] 离线模式可以回答：\n"
                "1. 简单问题：数学计算、时间日期、问候\n"
                "2. 文档相关问题：如'这个项目用什么技术栈？'\n"
                "当前问题不在上述范围，请换一种问法，或切换到在线模式获得完整能力。"
            )
        return {"messages": [{"role": "assistant", "content": answer}]}

def build_agent(mode: str | None = None):
    """构建通用 Agent。离线模式返回本地检索实现，在线模式用 ReAct Agent。

    mode: 可选，显式指定模式（deepseek/openai/qwen/zhipu/moonshot/ollama/offline）。
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

    # 在线模式：从 provider 配置统一获取（网页端可动态更换 Key）
    from config import get_provider_config
    pcfg = get_provider_config(provider)
    api_key = pcfg.get("api_key") or "empty-key-placeholder"
    base_url = pcfg.get("base_url") or "https://api.deepseek.com/v1"
    model_name = pcfg.get("model") or LLM_MODEL

    model = ChatOpenAI(
        model=model_name,
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
    history: list[dict] | None = None,
) -> tuple[str, AgentResult]:
    """对外问答入口：执行一次 Agent 推理，返回 (回答, 结果详情)。

    confirmed: 保留的兼容参数。V2 的 ReAct 循环中，模型自主决定是否继续。
    mode: 可选，动态指定运行模式（deepseek/openai/ollama/offline）。
    history: 可选，多轮对话历史 [{"role": "user"/"assistant", "content": str}, ...]。
    """
    agent = build_agent(mode)

    # 组装消息：历史 + 当前问题（多轮对话记忆）
    messages_in = []
    if history:
        messages_in.extend(history)
    messages_in.append({"role": "user", "content": question})

    result = agent.invoke({"messages": messages_in})
    messages = result.get("messages", [])
    if not messages:
        return "", {"answer": "", "messages": [], "reflection": ""}

    # 兼容 dict 与 AIMessage 两种消息类型
    last = messages[-1]
    answer = last.get("content", "") if isinstance(last, dict) else getattr(last, "content", "")
    answer = str(answer)

    # 提取过程日志与真实工具调用（基于 AIMessage.tool_calls 元数据，而非字符串猜测）
    log = []
    tool_calls = _extract_tool_calls(messages)
    for m in messages:
        role = m.get("role", "") if isinstance(m, dict) else getattr(m, "type", "")
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if role in ("ai", "assistant") and content:
            log.append(f"[模型] {str(content)[:120]}")
        elif role == "tool":
            log.append(f"[工具调用] {str(content)[:120]}")

    # 反思记录：基于实际执行过程生成
    reflection = _build_reflection(question, answer, tool_calls, mode or LLM_PROVIDER)

    if show_log:
        for line in log:
            print(line)
    return answer, {"answer": answer, "messages": log, "reflection": reflection}


def _extract_tool_calls(messages: list) -> list[dict]:
    """从消息序列中提取真实的工具调用记录。

    优先使用 AIMessage.tool_calls 元数据（name/args 由模型结构化输出，可靠）；
    对纯 dict 消息（离线模式）回退为解析 tool 角色消息的文本内容。
    返回: [{"name": str, "args": dict|str, "result": str}, ...]
    """
    calls: list[dict] = []
    # 第一遍：AIMessage.tool_calls 元数据（在线模式的真实来源）
    for m in messages:
        tcs = getattr(m, "tool_calls", None)
        if not tcs:
            continue
        for tc in tcs:
            name = tc.get("name", "")
            args = tc.get("args", {})
            calls.append({"name": name, "args": args, "result": ""})

    # 第二遍：把 tool 角色的结果文本按顺序回填到对应调用
    results: list[str] = []
    for m in messages:
        role = m.get("role", "") if isinstance(m, dict) else getattr(m, "type", "")
        if role == "tool":
            content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
            results.append(str(content))
    for call, result in zip(calls, results):
        call["result"] = result

    # 离线模式（纯 dict 消息，无 tool_calls）：从 tool 消息文本回退解析
    if not calls:
        for m in messages:
            role = m.get("role", "") if isinstance(m, dict) else getattr(m, "type", "")
            content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
            if role == "tool":
                calls.append({"name": "search_documents", "args": {}, "result": str(content)})
    return calls


def _compact(text: str) -> str:
    """去掉空白后的小写文本，用于字符级连续片段匹配。"""
    return "".join(str(text).lower().split())


def _grounded(answer: str, tool_calls: list[dict]) -> bool:
    """检查回答是否基于工具结果（grounded）。

    方法：取每个工具结果中最长的连续字符片段（去空白），看是否出现在回答里。
    片段长度阈值 MIN_GROUND_FRAGMENT（10 字符）排除了"来源"/"文档"这类
    工具自己格式文本里的噪声词——只要回答确实复用了工具结果的内容就会命中。
    没有工具调用时返回 False（纯对话，不存在 grounded 概念）。
    """
    MIN_GROUND_FRAGMENT = 10
    answer_c = _compact(answer)
    for call in tool_calls:
        result_c = _compact(call.get("result", ""))
        if len(result_c) < MIN_GROUND_FRAGMENT:
            continue
        # 从结果中取最长的连续片段，滑动检测是否在回答中出现
        for start in range(len(result_c) - MIN_GROUND_FRAGMENT + 1):
            frag = result_c[start : start + MIN_GROUND_FRAGMENT]
            if frag in answer_c:
                return True
    return False


def _build_reflection(question: str, answer: str, tool_calls: list[dict], mode: str) -> str:
    """生成反思记录：统计真实工具调用，并检查回答是否基于工具结果（grounded）。

    与 V1 不同：工具调用来自 AIMessage.tool_calls 元数据（模型结构化输出），
    而非在工具返回的格式化文本里搜关键词——那是自我实现的预言。
    """
    import json as _json

    names = [c["name"] for c in tool_calls if c.get("name")]
    used_doc_search = any(n == "search_documents" for n in names)
    used_web_search = any(n == "web_search" for n in names)
    used_weather = any(n == "get_weather" for n in names)

    reflection = {
        "问题": question,
        "运行模式": mode,
        "工具调用数": len(tool_calls),
        "调用的工具": names,
        "使用文档检索": used_doc_search,
        "使用联网搜索": used_web_search,
        "使用天气查询": used_weather,
        "回答是否基于工具结果": _grounded(answer, tool_calls),
        "回答长度": len(answer),
        "说明": "工具调用取自 AIMessage.tool_calls 元数据；grounded 基于回答对工具结果连续片段的复用检查",
    }
    return _json.dumps(reflection, ensure_ascii=False, indent=2)