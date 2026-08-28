"""通用 Agent：基于 LangGraph create_react_agent 构建（ReAct 模式）。

核心思想：不再用规则判断"该走哪条路"，而是把工具交给模型，
由模型自主决定：
  - 文档问题 -> 调用 search_documents
  - 实时问题 -> 调用 web_search / get_weather
  - 普通对话 -> 直接回答
  - 需要多步 -> 连续调用多个工具（思考 -> 行动 -> 观察 -> 再思考）

这是 LangGraph 最主流的 Agent 架构（ReAct / Tool-calling Agent）。

V2 说明：架构从 V1 的"规则路由 + 人工确认 + 反思"演进为 ReAct 通用 Agent，
人工确认与反思改由模型在循环内自然处理（工具结果即观察）。对外保留 reflection
字段供前端展示过程信息。
"""
import threading
from typing import TypedDict

from langchain_openai import ChatOpenAI
# 注：LangGraph V1.0 起 create_react_agent 弃用并计划迁移到 langchain.agents.create_agent，
# 但当前 langgraph 1.2.10 中 langgraph.prebuilt 路径仍可用，且无需额外安装 langchain 包。
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import create_react_agent

from config import LLM_MODEL, LLM_PROVIDER, MEMORY_DB
from tools import get_weather, search_documents, web_search


class AgentResult(TypedDict, total=False):
    answer: str
    messages: list[str]
    reflection: str


# 全局记忆存储：SQLite checkpointer（按 thread_id 持久化多轮会话，重启不丢）。
# 模块级单例：web 服务多线程共享同一份会话历史。
# 加锁防并发竞态：ThreadingHTTPServer 多线程首次请求时可能同时初始化。
_memory = None
_memory_lock = threading.Lock()


def get_memory() -> SqliteSaver:
    """获取全局 SQLite checkpointer（懒加载，进程内单例，线程安全）。"""
    global _memory
    if _memory is None:
        with _memory_lock:
            if _memory is None:
                import sqlite3
                from pathlib import Path

                db_path = Path(MEMORY_DB)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(db_path), check_same_thread=False)
                _memory = SqliteSaver(conn)
    return _memory


# agent 构建缓存：按 provider 缓存，避免每次提问重建（create_react_agent 有开销）。
# 缓存键 = (provider, 运行时配置版本号)——网页端换 Key/模型后版本号变化，自动重建。
_agent_cache: dict[tuple[str, int], object] = {}
_agent_cache_lock = threading.Lock()


def build_agent(mode: str | None = None, memory: SqliteSaver | None = None):
    """构建通用 Agent（ReAct 模式 + checkpointer 多轮记忆），按 provider 缓存。

    mode: 可选，显式指定模式（deepseek/openai/qwen/zhipu/moonshot/ollama）。
    支持运行时动态切换（如网页端按钮切换）。
    memory: 可选，checkpointer 实例；默认使用全局 SQLite 记忆（thread_id 会话）。
    传入自定义 memory 时不走缓存（测试/特殊用途）。
    """
    provider = (mode or LLM_PROVIDER).lower()

    from config import get_runtime_config_version

    if memory is None:
        version = get_runtime_config_version()
        cache_key = (provider, version)
        with _agent_cache_lock:
            cached = _agent_cache.get(cache_key)
        if cached is not None:
            return cached

    from config import OLLAMA_BASE_URL, OLLAMA_MODEL

    if provider == "ollama":
        # 本地 Ollama 模型：OpenAI 兼容接口，无需 API Key
        model = ChatOpenAI(
            model=OLLAMA_MODEL,
            api_key="ollama",  # Ollama 忽略 Key，占位即可
            base_url=OLLAMA_BASE_URL,
            temperature=0.3,
        )
    else:
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

    agent = create_react_agent(
        model=model,
        tools=[search_documents, web_search, get_weather],
        checkpointer=memory or get_memory(),
    )

    if memory is None:
        with _agent_cache_lock:
            _agent_cache[cache_key] = agent
    return agent


def ask(
    question: str,
    show_log: bool = False,
    mode: str | None = None,
    history: list[dict] | None = None,
    thread_id: str | None = None,
) -> tuple[str, AgentResult]:
    """对外问答入口：执行一次 Agent 推理，返回 (回答, 结果详情)。

    thread_id: 会话标识。传入时走 checkpointer 持久化多轮记忆——
      相同 thread_id 的多次调用共享上下文，无需前端回传 history。
      不传则退化为单轮（每次独立，不记忆）。
    mode: 可选，动态指定运行模式（deepseek/openai/ollama）。
    history: 可选，兼容参数。传了 thread_id 时建议忽略（checkpointer 已含历史）。
    """
    agent = build_agent(mode)

    # 组装消息：历史 + 当前问题（无 thread_id 时才需要手动拼历史）
    messages_in = []
    if history and not thread_id:
        messages_in.extend(history)
    messages_in.append({"role": "user", "content": question})

    invoke_config = None
    if thread_id:
        invoke_config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke({"messages": messages_in}, config=invoke_config)
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

    来源：AIMessage.tool_calls 元数据（name/args 由模型结构化输出，可靠）。
    返回: [{"name": str, "args": dict|str, "result": str}, ...]
    """
    calls: list[dict] = []
    # 第一遍：AIMessage.tool_calls 元数据（真实来源）
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