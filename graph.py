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
import re
import threading
import uuid
from typing import TypedDict

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI
# 注：LangGraph V1.0 起 create_react_agent 弃用并计划迁移到 langchain.agents.create_agent，
# 但当前 langgraph 1.2.10 中 langgraph.prebuilt 路径仍可用，且无需额外安装 langchain 包。
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import create_react_agent

from config import LLM_MODEL, LLM_PROVIDER, MEMORY_DB
from prompts import SYSTEM_PROMPT
from tools import get_weather, open_in_browser, run_command, search_documents, web_search, write_file


class AgentResult(TypedDict, total=False):
    answer: str
    messages: list[str]
    reflection: str
    sources: list[dict]  # 工具调用来源（文档片段/网页链接）


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
# 缓存值 = agent；usage 回调 handler 挂在 agent.__usage_handler 上随缓存保存。
_agent_cache: dict[tuple[str, int], object] = {}
_agent_cache_lock = threading.Lock()


class _UsageCapture(BaseCallbackHandler):
    """LangChain 回调：在 on_llm_end 捕获 token 用量，按会话（thread_id）累计。

    LangGraph 的 stream 模式会剥离 response_metadata 中的 usage（updates 流
    与 checkpoint 都不含），但 on_llm_end 回调能拿到完整 llm_output——这是
    流式路径下获取 token 用量的唯一可靠途径。

    由于 agent 按 provider 缓存、多线程共享同一 handler，用 thread_id 区分
    会话：每次调用的 usage 累加到对应会话，避免跨请求串值；单次读后即取
    （ask/ask_stream 结束时通过 _usage_of 读取该会话的累计值）。
    """

    def __init__(self):
        self._by_thread: dict[str, dict] = {}
        self._lock = threading.Lock()
        # 当前线程正在服务的 thread_id（ask/ask_stream 调用前设置，
        # 回调在同一线程执行，可直接读取；比从 callback metadata 取更可靠）
        self._thread_local = threading.local()

    def set_current_thread(self, thread_id: str) -> None:
        self._thread_local.thread_id = thread_id

    def on_llm_end(self, response, **kwargs) -> None:  # noqa: N802
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if not (isinstance(usage, dict) and usage.get("total_tokens")):
            return
        thread_id = getattr(self._thread_local, "thread_id", "default")
        with self._lock:
            acc = self._by_thread.setdefault(thread_id, {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            })
            acc["prompt_tokens"] += usage.get("prompt_tokens", 0)
            acc["completion_tokens"] += usage.get("completion_tokens", 0)
            acc["total_tokens"] += usage.get("total_tokens", 0)

    def get(self, thread_id: str) -> dict | None:
        with self._lock:
            acc = self._by_thread.get(thread_id or "default")
            if acc is None:
                return None
            return dict(acc)


def _usage_of(agent, thread_id: str = "") -> dict | None:
    """从 agent 上取指定会话的累计 token 用量。"""
    handler = getattr(agent, "__usage_handler", None)
    return handler.get(thread_id) if handler else None


def _estimate_tokens_from_text(text: str) -> int:
    """粗略估算文本 token 数（流式模式拿不到精确用量时的降级）。

    中文约 1.5 字/token，英文/数字约 4 字符/token（近似，仅供展示）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, int(cjk / 1.5 + other / 4))


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

    usage_handler = _UsageCapture()

    if provider == "ollama":
        # 本地 Ollama 模型：OpenAI 兼容接口，无需 API Key
        model = ChatOpenAI(
            model=OLLAMA_MODEL,
            api_key="ollama",  # Ollama 忽略 Key，占位即可
            base_url=OLLAMA_BASE_URL,
            temperature=0.3,
            callbacks=[usage_handler],
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
            callbacks=[usage_handler],
        )

    agent = create_react_agent(
        model=model,
        tools=[search_documents, web_search, get_weather, write_file, run_command, open_in_browser],
        checkpointer=memory or get_memory(),
        # 系统提示：行为准则集中管理在 prompts.py（与工具 docstring 协同）
        prompt=SYSTEM_PROMPT,
    )
    # 挂 usage 回调供 _usage_of 读取（随 agent 缓存一起保存）
    agent.__usage_handler = usage_handler  # type: ignore[attr-defined]

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

    # 标记当前线程的会话（usage 回调按此累计，防跨请求串值）
    handler = getattr(agent, "__usage_handler", None)
    if handler is not None:
        handler.set_current_thread(thread_id or "default")

    # 组装消息：历史 + 当前问题（无 thread_id 时才需要手动拼历史）
    messages_in = []
    if history and not thread_id:
        messages_in.extend(history)
    messages_in.append({"role": "user", "content": question})

    # checkpointer 强制要求 thread_id：无会话标识时用一次性临时 id（用完删除，
    # 保持"单轮不记忆"语义且不污染会话列表）
    temp_thread = None
    if not thread_id:
        temp_thread = f"temp-{uuid.uuid4().hex}"
        thread_id = temp_thread
    result = agent.invoke(
        {"messages": messages_in},
        config={"configurable": {"thread_id": thread_id}},
    )
    if temp_thread:
        delete_session(temp_thread)
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
    usage = _usage_of(agent, thread_id)
    return answer, {
        "answer": answer,
        "messages": log,
        "reflection": reflection,
        "sources": _build_sources(tool_calls),
        "usage": usage,
        "cost": _estimate_cost(mode or LLM_PROVIDER, usage),
    }


def ask_stream(
    question: str,
    mode: str | None = None,
    thread_id: str | None = None,
):
    """流式问答入口：生成器，逐 token yield 事件，供 SSE 推送。

    事件类型：
      {"type": "start"}                        — 开始
      {"type": "token", "content": str}        — 回答文本增量
      {"type": "tool_start", "name": str}      — 工具开始执行
      {"type": "tool_done", "result": str}     — 工具返回（前 200 字符）
      {"type": "done", "answer, reflection, sources"} — 完成（含最终完整信息）
      {"type": "error", "message": str}        — 出错

    说明：LangGraph 双流模式（messages + updates）——messages 提供 token 级增量
    供前端渲染；updates 提供每步的完整消息（工具调用/反思用）。
    token 用量经 on_llm_end 回调（_UsageCapture）捕获——LangGraph 的流与
    checkpoint 都会剥离 usage 元数据，回调是流式路径下获取用量的唯一途径。
    """
    agent = build_agent(mode)

    messages_in = [{"role": "user", "content": question}]
    # checkpointer 强制要求 thread_id：无会话标识时用一次性临时 id（用完删除）
    temp_thread = None
    if not thread_id:
        temp_thread = f"temp-{uuid.uuid4().hex}"
        thread_id = temp_thread
    config = {"configurable": {"thread_id": thread_id}}

    # 标记当前线程的会话（usage 回调按此累计，防跨请求串值）
    handler = getattr(agent, "__usage_handler", None)
    if handler is not None:
        handler.set_current_thread(thread_id)

    yield {"type": "start", "mode": mode or LLM_PROVIDER}

    text_parts: list[str] = []
    seen_tools: set[str] = set()
    all_messages: list = []  # 从 updates 收集的完整消息（用于 usage/tool_calls）
    try:
        for item in agent.stream(
            {"messages": messages_in}, config=config, stream_mode=["messages", "updates"]
        ):
            if isinstance(item, tuple):
                mode_name, inner = item
                if mode_name == "messages":
                    msg_chunk, metadata = inner
                    node = (metadata or {}).get("langgraph_node", "")
                    if node == "agent":
                        content = getattr(msg_chunk, "content", None)
                        if isinstance(content, str) and content:
                            text_parts.append(content)
                            yield {"type": "token", "content": content}
                        # 工具调用开始（tool_call_chunks 出现在 agent 的 AIMessage 增量里）
                        for tcc in getattr(msg_chunk, "tool_call_chunks", None) or []:
                            name = tcc.get("name")
                            if name and name not in seen_tools:
                                seen_tools.add(name)
                                yield {"type": "tool_start", "name": name}
                    elif node == "tools":
                        # 工具结果消息（ToolMessage）——回传摘要供前端展示
                        content = getattr(msg_chunk, "content", None)
                        if isinstance(content, str) and content:
                            yield {"type": "tool_done", "result": content[:200]}
                else:
                    # updates 流：{node: {"messages": [...]}}
                    for payload in (inner or {}).values():
                        if isinstance(payload, dict):
                            all_messages.extend(payload.get("messages", []) or [])
            else:
                # 兼容：单独 updates 输出
                for payload in (item or {}).values():
                    if isinstance(payload, dict):
                        all_messages.extend(payload.get("messages", []) or [])
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": str(exc)}
        if temp_thread:
            delete_session(temp_thread)
        return

    answer = "".join(text_parts)
    tool_calls = _extract_tool_calls(all_messages)
    reflection = _build_reflection(question, answer, tool_calls, mode or LLM_PROVIDER)
    # 用量：优先回调精确值（非流式 invoke 路径）；流式路径 deepseek 不返回 usage，
    # 降级为按文本长度估算并标注 estimated
    usage = _usage_of(agent, thread_id)
    estimated = False
    if usage is None and answer:
        total = _estimate_tokens_from_text(answer)
        usage = {"prompt_tokens": None, "completion_tokens": total, "total_tokens": total}
        estimated = True
    if temp_thread:
        delete_session(temp_thread)
    yield {
        "type": "done",
        "answer": answer,
        "reflection": reflection,
        "sources": _build_sources(tool_calls),
        "usage": usage,
        "cost": _estimate_cost(mode or LLM_PROVIDER, usage),
        "usage_estimated": estimated,
    }


def _build_sources(tool_calls: list[dict]) -> list[dict]:
    """从工具调用结果中提取来源（文档片段 / 网页链接），供前端引用卡片展示。

    返回: [{"type": "document"|"web", "title": str, "preview"/"url": str, ...}, ...]
    """
    sources: list[dict] = []
    for call in tool_calls:
        name = call.get("name")
        result = str(call.get("result", ""))
        if name == "search_documents":
            # 结果格式: "[1] 来源: <path> | 相关度: 0.5\n<chunk>"
            for line in result.split("\n"):
                if "来源:" in line:
                    title = line.split("来源:", 1)[1].split("|", 1)[0].strip()
                    sources.append({
                        "type": "document",
                        "title": title,
                        "preview": result[:300],
                    })
                    break
        elif name == "web_search":
            # 结果格式: "[1] <title>\n   摘要: ...\n   链接: <url>"
            blocks = re.split(r"\[\d+\]", result)
            for block in blocks[1:]:
                lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
                title = lines[0] if lines else ""
                url = ""
                for ln in lines:
                    if ln.startswith("链接:"):
                        url = ln.split("链接:", 1)[1].strip()
                        break
                if url:
                    sources.append({"type": "web", "title": title, "url": url})
    # 去重：web 按 URL、document 按 title；最多 5 条
    seen = set()
    deduped = []
    for s in sources:
        if s["type"] == "web":
            key = ("web", s.get("url", ""))
        else:
            key = ("doc", s.get("title", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    return deduped[:5]


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


# ---------- Token 用量与成本 ----------

def _extract_usage(messages: list) -> dict | None:
    """从消息序列中提取最近一次模型调用的 token 用量。

    来源：AIMessage.response_metadata 中的 usage（OpenAI 标准）或
    token_usage（DeepSeek 返回的实际字段名）。
    返回: {"prompt_tokens", "completion_tokens", "total_tokens"} 或 None。
    """
    usage = None
    for m in messages:
        if isinstance(m, dict):
            meta = m.get("response_metadata", {}) or {}
        else:
            meta = getattr(m, "response_metadata", None) or {}
        u = meta.get("usage") or meta.get("token_usage") or {}
        if isinstance(u, dict) and u.get("total_tokens"):
            usage = {
                "prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "total_tokens": u.get("total_tokens", 0),
            }
    return usage


def _estimate_cost(provider: str, usage: dict | None) -> dict | None:
    """按服务商单价估算本次调用成本（人民币，仅供展示）。

    返回: {"input": 元, "output": 元, "total": 元, "currency": "¥"} 或 None（未知单价）。
    """
    if not usage:
        return None
    from config import PROVIDER_PRICES

    price = PROVIDER_PRICES.get((provider or "").lower())
    if not price:
        return None
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    input_cost = (prompt / 1_000_000) * price["input"]
    output_cost = (completion / 1_000_000) * price["output"]
    return {
        "input": round(input_cost, 6),
        "output": round(output_cost, 6),
        "total": round(input_cost + output_cost, 6),
        "currency": "¥",
    }


# ---------- 会话管理（从 checkpointer 读取/删除） ----------

def _message_content(m) -> str:
    """兼容 dict 与 BaseMessage 提取文本内容。"""
    if isinstance(m, dict):
        content = m.get("content", "")
        return str(content) if isinstance(content, str) else ""
    return str(getattr(m, "content", "") or "")


def _message_type(m) -> str:
    if isinstance(m, dict):
        return m.get("type", "") or m.get("role", "")
    return getattr(m, "type", "")


def list_sessions(limit: int = 50) -> list[dict]:
    """列出最近会话（按最新活动排序）。

    从 checkpointer 读取每个 thread 的最新 checkpoint，
    用第一条用户消息作标题、checkpoint 的 ts 字段作更新时间。
    返回: [{"thread_id", "title", "updated_at", "message_count"}, ...]
    """
    saver = get_memory()
    # list(None) 返回全部 thread 的所有历史 checkpoint，顺序为最新在前；
    # 用 setdefault 保留每个 thread 的第一条（即最新）checkpoint
    latest: dict[str, object] = {}
    for t in saver.list(None):
        thread_id = (t.config.get("configurable") or {}).get("thread_id", "")
        if not thread_id:
            continue
        latest.setdefault(thread_id, t)

    sessions = []
    for thread_id, t in latest.items():
        cp = t.checkpoint or {}
        cv = cp.get("channel_values", {})
        messages = cv.get("messages", []) or []
        title = "(空会话)"
        for m in messages:
            if _message_type(m) in ("human", "user") and _message_content(m).strip():
                title = _message_content(m).strip()[:40]
                break
        # ts 是 ISO 时间戳（如 2026-08-28T18:09:37.123456+00:00）
        updated_at = str(cp.get("ts", ""))[:19]
        sessions.append({
            "thread_id": thread_id,
            "title": title,
            "updated_at": updated_at,
            "message_count": len(messages),
        })

    # 按更新时间倒序（ISO 时间戳字符串可比较）
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions[:limit]


def get_session_messages(thread_id: str, limit: int = 100) -> list[dict]:
    """读取指定会话的历史消息（供前端回放）。

    从 checkpointer 取该 thread 的最新 checkpoint，提取 human/ai 消息
    （跳过 tool 消息）。中间态 AI 消息（仅 tool_calls 无文本）的工具名
    合并进下一条有文本的 AI 消息；并基于每轮的工具调用结果生成
    sources（来源卡片）与 reflection（过程详情）供前端回放展示。
    返回: [{"role": "user"|"assistant", "content", "tools", "sources"?, "reflection"?}, ...]
    """
    saver = get_memory()
    # 该 thread 的最新 checkpoint（list 返回最新在前）
    latest = None
    for t in saver.list(None):
        tid = (t.config.get("configurable") or {}).get("thread_id", "")
        if tid == thread_id:
            latest = t
            break
    if latest is None:
        return []

    def _tools_of(m) -> list[dict]:
        """提取 AI 消息的 tool_calls（name+args），兼容 dict 与 BaseMessage。"""
        if isinstance(m, dict):
            tcs = m.get("tool_calls", []) or []
        else:
            tcs = getattr(m, "tool_calls", None) or []
        return [
            {"name": tc.get("name", ""), "args": tc.get("args", {})}
            for tc in tcs
            if isinstance(tc, dict) and tc.get("name")
        ]

    messages = ((latest.checkpoint or {}).get("channel_values", {}) or {}).get("messages", []) or []
    # 会话时间：checkpoint 的 ts（ISO 时间戳），历史消息用它近似每条消息的时间
    session_time = str((latest.checkpoint or {}).get("ts", ""))
    result: list[dict] = []
    pending_tools: list[dict] = []      # 待并入下一条 AI 消息的工具调用
    pending_results: list[str] = []     # 工具结果文本（按顺序对应 pending_tools）
    current_question = ""

    for m in messages:
        mtype = _message_type(m)
        content = _message_content(m).strip()
        if mtype in ("human", "user"):
            if content:
                current_question = content
                pending_tools = []
                pending_results = []
                result.append({"role": "user", "content": content, "tools": [], "time": session_time})
        elif mtype in ("ai", "assistant"):
            tools = _tools_of(m)
            if content:
                # 有文本：合并待并入的工具调用，生成 sources/reflection
                all_tools = pending_tools + tools
                # 工具结果回填（pending_results 对应 pending_tools 部分）
                for call, res in zip(pending_tools, pending_results):
                    call["result"] = res
                sources = _build_sources(all_tools) if all_tools else []
                # 历史回放无精确 usage（checkpoint 不含），按文本长度估算并标注
                estimated_total = _estimate_tokens_from_text(content)
                usage = {
                    "prompt_tokens": None,
                    "completion_tokens": estimated_total,
                    "total_tokens": estimated_total,
                    "estimated": True,
                }
                result.append({
                    "role": "assistant",
                    "content": content,
                    "tools": [t["name"] for t in all_tools],
                    "sources": sources,
                    # 无工具调用也生成 reflection，保证前端"过程详情"面板始终存在
                    "reflection": _build_reflection(current_question, content, all_tools, "history"),
                    "usage": usage,
                    "cost": _estimate_cost("deepseek", usage),
                    "time": session_time,
                })
                pending_tools = []
                pending_results = []
            elif tools:
                # 仅调用工具无文本的中间消息：挂起，等真正回答合并
                pending_tools = pending_tools + tools
        elif mtype == "tool":
            # 工具结果消息：记录文本（对应最近的 pending_tools）
            pending_results.append(content)
        # 其他类型跳过
    return result[-limit:]


def delete_session(thread_id: str) -> bool:
    """删除指定会话（checkpointer 的 thread）。"""
    saver = get_memory()
    saver.delete_thread(thread_id)  # SqliteSaver.delete_thread 直接收 thread_id 字符串
    return True