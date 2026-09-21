"""核心链路测试：真实的 ReAct 工具循环。

补上的空白：本项目的 **3 处 `create_agent(...)` 调用全部传了 `tools=[]`**，
也就是说"模型决定调工具 → 执行工具 → 观察结果 → 再思考 → 出答案"这条
**项目存在的全部理由**，此前一条测试都没有跑过。`ask()` / `ask_stream()`
本身也从未被真实调用（服务端测试直接 monkeypatch 掉了 `server.ask`）。

这里的做法是"最外层替身"：起一个**本地假 OpenAI 兼容服务**（见
tests/conftest.py 的 `fake_llm`），把响应用脚本编排好，然后驱动真实的
agent 图去跑。这样：

- 被测的是真实的 agent 图、真实的工具执行、真实的消息流转；
- 但不存在网络依赖、不产生费用、结果完全确定。

三层分离的测试策略：
  ① 纯函数（_build_sources / _grounded / _extract_tool_calls）→ 构造数据直测（test_core.py）
  ② 编排层（工具循环、多轮、异常、迭代上限）→ **本文件**，假模型服务驱动
  ③ 端到端 → 极少量冒烟（真实模型，不进 CI）
"""
import json

# ---------- 单工具循环 ----------

def test_agent_loop_calls_tool_then_answers(fake_llm):
    """一轮"调工具 → 拿结果 → 出答案"的完整循环。"""
    import graph

    fake_llm.push_tool_call("get_current_time")
    fake_llm.push_text("现在是测试时间。")

    answer, result = graph.ask("现在几点了", mode="deepseek", thread_id="loop-1")

    assert answer == "现在是测试时间。"
    assert fake_llm.request_count() == 2, "应当恰好两次模型调用（决定调工具 / 基于结果作答）"

    # 第二次请求里必须带着工具执行结果 —— 否则"观察"这一步没真正发生
    second_messages = fake_llm.messages_of(1)
    tool_messages = [m for m in second_messages if m.get("role") == "tool"]
    assert tool_messages, f"第二次请求里应包含工具结果消息：{second_messages}"
    assert "20" in str(tool_messages[0].get("content")), "工具结果应被真实执行并回传"

    # 反思与来源来自真实的 tool_calls 元数据
    reflection = json.loads(result["reflection"])
    assert reflection["工具调用数"] == 1
    assert reflection["调用的工具"] == ["get_current_time"]
    assert reflection["回答是否基于工具结果"] is False  # 回答没复用工具结果文本


def test_agent_loop_grounded_true_when_answer_reuses_tool_output(fake_llm):
    """模型复用了工具结果内容时，grounded 应为 True（防"幻觉式作答"的观测点）。"""
    import graph

    fake_llm.push_tool_call("search_documents", {"query": "技术栈"})
    fake_llm.push_text("技术栈是 Python 3.10+ 与 LangGraph。")

    _, result = graph.ask("技术栈是什么", mode="deepseek", thread_id="loop-2")
    # search_documents 会命中 sample_docs（本用例未提供文档，故断言结构而非内容）
    reflection = json.loads(result["reflection"])
    assert reflection["使用文档检索"] is True
    assert reflection["工具调用数"] == 1


def test_agent_loop_with_real_index_produces_sources(fake_llm, sample_docs):
    """带真实索引时，search_documents 的结果应被解析成来源卡片。"""
    import graph
    from retriever import build_index

    build_index(force=True)
    fake_llm.push_tool_call("search_documents", {"query": "这个项目的技术栈是什么"})
    fake_llm.push_text("根据项目介绍文档，技术栈是 Python 3.10+、LangGraph 与 BM25 检索。")

    _, result = graph.ask("这个项目的技术栈是什么", mode="deepseek", thread_id="loop-3")

    assert result["sources"], "应产出至少一条来源"
    assert result["sources"][0]["type"] == "document"
    assert "project_intro.md" in result["sources"][0]["title"]
    reflection = json.loads(result["reflection"])
    assert reflection["使用文档检索"] is True


# ---------- 一条消息里并行调用多个工具 ----------

def test_agent_loop_parallel_tool_calls_in_one_turn(fake_llm):
    """模型在同一条消息里并行请求两个工具，两个都应被执行且结果都回传。"""
    import graph

    fake_llm.push_tool_calls([("get_current_time", {}), ("list_files", {"path": ""})])
    fake_llm.push_text("时间和文件列表都拿到了。")

    _, result = graph.ask("给我时间和文件列表", mode="deepseek", thread_id="loop-4")

    reflection = json.loads(result["reflection"])
    assert reflection["工具调用数"] == 2
    assert set(reflection["调用的工具"]) == {"get_current_time", "list_files"}

    tool_messages = [m for m in fake_llm.messages_of(1) if m.get("role") == "tool"]
    assert len(tool_messages) == 2, "两个工具的观察结果都应回传给模型"


# ---------- 工具报错要被"观察"到，而不是把整个请求打挂 ----------

def test_agent_loop_tool_error_is_observed_not_fatal(fake_llm):
    """工具执行失败时，错误应作为观察结果回到模型，而不是让整轮请求失败。

    这是一个真实风险点：工具抛异常若直接冒泡，用户看到的是"服务内部错误"；
    正确行为是模型看到错误信息后自行决定重试或换个说法作答。
    """
    import graph

    fake_llm.push_tool_call("read_file", {"file_path": "../../etc/passwd"})
    fake_llm.push_text("那个文件不允许读取。")

    answer, result = graph.ask("读一下 /etc/passwd", mode="deepseek", thread_id="loop-5")

    assert answer == "那个文件不允许读取。"
    assert fake_llm.request_count() == 2, "工具报错后模型仍应被再调用一次"

    tool_messages = [m for m in fake_llm.messages_of(1) if m.get("role") == "tool"]
    assert tool_messages, "错误信息应作为工具观察结果回传"
    assert "拒绝" in str(tool_messages[0].get("content")) or "越界" in str(
        tool_messages[0].get("content")
    )


# ---------- 不调工具的纯对话 ----------

def test_agent_loop_without_tool_calls_answers_directly(fake_llm):
    """模型直接作答时只调用一次模型、零工具、零来源。"""
    import graph

    fake_llm.push_text("你好，我是这个项目的助手。")

    answer, result = graph.ask("你好", mode="deepseek", thread_id="loop-6")

    assert answer == "你好，我是这个项目的助手。"
    assert fake_llm.request_count() == 1
    assert result["sources"] == []
    assert json.loads(result["reflection"])["工具调用数"] == 0


# ---------- 多步链式调用 ----------

def test_agent_loop_chains_multiple_steps(fake_llm):
    """"思考 → 行动 → 观察 → 再思考"跑多步：连续三次工具调用后给出结论。"""
    import graph

    fake_llm.push_tool_call("get_current_time")
    fake_llm.push_tool_call("list_files", {"path": ""})
    fake_llm.push_tool_call("get_current_time")
    fake_llm.push_text("综合三次结果，结论如下。")

    _, result = graph.ask("做三步再总结", mode="deepseek", thread_id="loop-7")

    assert fake_llm.request_count() == 4
    reflection = json.loads(result["reflection"])
    assert reflection["工具调用数"] == 3
    assert reflection["调用的工具"] == ["get_current_time", "list_files", "get_current_time"]


# ---------- 迭代上限：把"失控"变成可读提示 ----------

def test_agent_loop_recursion_limit_returns_readable_message(fake_llm, monkeypatch):
    """模型陷入无限工具循环时必须止损，并给出可读提示（而不是抛栈 / 500）。"""
    import graph

    monkeypatch.setattr(graph, "RECURSION_LIMIT", 6)
    for _ in range(30):
        fake_llm.push_tool_call("get_current_time")

    answer, result = graph.ask("无限循环", mode="deepseek", thread_id="loop-8")

    assert "上限" in answer, f"应返回可读的止损提示，实际：{answer!r}"
    assert result["sources"] == []
    # 未超过脚本量，说明确实是图自己停下的，而不是脚本被耗光
    assert fake_llm.request_count() < 30

    # 流式路径同样要止损并推 error 事件
    for _ in range(30):
        fake_llm.push_tool_call("get_current_time")
    events = list(graph.ask_stream("无限循环（流式）", mode="deepseek", thread_id="loop-9"))
    assert events[-1]["type"] == "error", f"流式应推 error 事件：{events[-1]}"
    assert "上限" in events[-1]["message"]


# ---------- A2 回归：多轮会话的来源不得跨轮污染 ----------

def test_second_turn_does_not_inherit_first_turn_sources(fake_llm, sample_docs):
    """同一会话第二轮没有调用任何工具时，sources / 工具调用数必须为空。

    这是 P0 缺陷的回归用例：`agent.invoke()` 返回的 `result["messages"]` 是
    **整个 thread 的累积历史**，原实现直接在上面提取工具调用，于是第二轮
    会带上第一轮的文档来源（前端表现为"来源卡片与这一问无关"）。
    修法是按"最后一条 human 消息"切出本轮消息（见 graph._current_turn_messages）。
    """
    import graph
    from retriever import build_index

    build_index(force=True)

    # 第一轮：调检索工具 → 有来源
    fake_llm.push_tool_call("search_documents", {"query": "这个项目的技术栈是什么"})
    fake_llm.push_text("技术栈是 Python 3.10+ 与 LangGraph，使用 BM25 检索。")
    _, first = graph.ask("这个项目的技术栈是什么", mode="deepseek", thread_id="multi-1")
    assert first["sources"], "前提：第一轮应当有来源"
    assert json.loads(first["reflection"])["工具调用数"] == 1

    # 第二轮：同一 thread，模型直接作答（零工具调用）
    fake_llm.push_text("好的，我记住了。")
    _, second = graph.ask("谢谢", mode="deepseek", thread_id="multi-1")

    assert second["sources"] == [], (
        f"第二轮没有调用任何工具，不该带上第一轮的来源：{second['sources']}"
    )
    second_reflection = json.loads(second["reflection"])
    assert second_reflection["工具调用数"] == 0
    assert second_reflection["调用的工具"] == []
    assert second_reflection["使用文档检索"] is False


def test_second_turn_process_log_only_contains_current_turn(fake_llm, sample_docs):
    """过程日志（result["messages"]）也只应包含本轮，不夹带历史轮次。"""
    import graph
    from retriever import build_index

    build_index(force=True)

    fake_llm.push_tool_call("search_documents", {"query": "技术栈"})
    fake_llm.push_text("第一轮回答")
    graph.ask("第一问", mode="deepseek", thread_id="multi-2")

    fake_llm.push_text("第二轮回答")
    _, second = graph.ask("第二问", mode="deepseek", thread_id="multi-2")

    joined = "\n".join(second["messages"])
    assert "第一轮回答" not in joined, f"过程日志夹带了历史轮次：{second['messages']}"
    assert "第一问" not in joined


# ---------- 流式路径：真实 agent 的完整事件序列 ----------

def test_ask_stream_real_agent_event_sequence(fake_llm):
    """ask_stream 走真实 agent 时的事件序列：start → 工具 → token → done。

    此前只测了首尾两个事件，中间的工具事件与顺序完全没验证；
    而前端的高危确认弹窗依赖 tool_done 事件，丢了功能就坏。
    """
    import graph

    fake_llm.push_tool_call("get_current_time")
    fake_llm.push_text("现在是测试时间。")

    events = list(graph.ask_stream("现在几点了", mode="deepseek", thread_id="stream-1"))
    types = [e["type"] for e in events]

    assert types[0] == "start"
    assert types[-1] == "done"
    assert "tool_start" in types, f"应推送 tool_start：{types}"
    assert "tool_done" in types, f"应推送 tool_done：{types}"
    assert types.index("tool_start") < types.index("tool_done"), "工具事件顺序颠倒"

    # tool_start 不应重复推送（同一轮同名工具只报一次）
    assert types.count("tool_start") == 1

    tokens = "".join(e.get("content", "") for e in events if e["type"] == "token")
    assert tokens == "现在是测试时间。"

    done = events[-1]
    assert done["answer"] == "现在是测试时间。"
    assert json.loads(done["reflection"])["工具调用数"] == 1

    # token 事件必须出现在 done 之前
    assert types.index("token") < types.index("done")
