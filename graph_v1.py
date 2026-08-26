"""LangGraph 智能文档问答 Agent 状态图。

流程：用户提问
  -> route_query（判断是否文档相关）
  -> retrieve（工具调用：TF-IDF 检索 top_k）
  -> human_review（人工确认检索结果，human-in-the-loop）
  -> generate（模型结合检索上下文生成回答）
  -> reflect（反思：回答是否基于文档，可修正）

节点与边的设计对应 LangGraph 的 StateGraph + conditional edges。
"""
import json
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from llm import build_llm
from retriever import search

# ---------------- 状态定义 ----------------

class AgentState(TypedDict, total=False):
    question: str
    intent: str
    retrieved: list[dict]
    confirmed: bool
    answer: str
    reflection: str
    messages: list[str]  # 供演示使用的过程日志


# ---------------- 节点 ----------------

def route_query(state: AgentState) -> AgentState:
    """意图路由：判断问题是否与文档内容相关。"""
    question = state["question"]
    hits = search(question, top_k=1)
    intent = "doc_qa" if hits and hits[0]["score"] > 0.01 else "general"
    return {"intent": intent, "messages": [f"[路由] 识别意图: {intent}"]}


def retrieve(state: AgentState) -> AgentState:
    """工具调用节点：从文档索引检索相关内容。"""
    hits = search(state["question"], top_k=3)
    state["retrieved"] = hits
    state["messages"] = state.get("messages", []) + [
        f"[检索] 召回 {len(hits)} 个片段，top1 相似度 {hits[0]['score']:.3f}" if hits else "[检索] 未命中"
    ]
    return state


def human_review(state: AgentState) -> AgentState:
    """人工确认节点（human-in-the-loop）。

    演示模式（confirmed 未提供或为 True）直接放行；
    生产可接入人工审批接口，只有确认后才进入生成。
    """
    confirmed = state.get("confirmed", True)
    state["messages"] = state.get("messages", []) + [
        f"[人工确认] {'通过' if confirmed else '拒绝'}"
    ]
    return state


def generate(state: AgentState) -> AgentState:
    """生成节点：把检索结果组装进提示词，调用模型回答。"""
    question = state["question"]
    hits = state.get("retrieved", [])

    if not hits:
        answer = "没有在文档中找到相关内容，请换一种问法，或补充文档后再试。"
    else:
        context = "\n\n".join(f"【来自 {h['source']}】\n{h['chunk']}" for h in hits[:3])
        system = "你是一个严谨的文档问答助手。请只依据提供的文档内容回答；"
        system += "如果文档不足以回答问题，明确说明'文档中没有提到'。回答使用中文，简洁准确。"
        prompt = f"文档内容：\n{context}\n\n问题：{question}\n\n请回答："
        llm = build_llm()
        answer = llm.generate(prompt, system=system)

    state["answer"] = answer
    state["messages"] = state.get("messages", []) + [f"[生成] 回答完成（{len(answer)} 字）"]
    return state


def reflect(state: AgentState) -> AgentState:
    """反思节点：评估回答质量与来源，输出反思记录。"""
    question = state["question"]
    answer = state.get("answer", "")
    hits = state.get("retrieved", [])
    grounded = any(_contains_any(answer, h["chunk"]) for h in hits) if hits else False
    reflection = {
        "问题": question,
        "是否基于文档": grounded,
        "引用片段数": len(hits),
        "回答长度": len(answer),
    }
    state["reflection"] = json.dumps(reflection, ensure_ascii=False, indent=2)
    state["messages"] = state.get("messages", []) + [f"[反思] {reflection['是否基于文档']}"]
    return state


def _contains_any(answer: str, chunk: str) -> bool:
    """简单判断回答是否与某片段存在重叠词（粗粒度 grounded 检查）。"""
    if not answer or not chunk:
        return False
    words = set(w for w in chunk.replace("\n", " ").split() if len(w) > 1)
    return sum(1 for w in words if w in answer) >= 2


def should_continue(state: AgentState) -> Literal["generate", "retrieve", "end"]:
    """条件边：按意图与确认状态决定流向。"""
    if state["intent"] == "general":
        return "end"
    if not state.get("confirmed", True):
        return "end"
    return "generate"


# ---------------- 图构建 ----------------

def build_graph():
    g = StateGraph(AgentState)
    g.add_node("route", route_query)
    g.add_node("retrieve", retrieve)
    g.add_node("human", human_review)
    g.add_node("generate", generate)
    g.add_node("reflect", reflect)

    g.add_edge(START, "route")
    g.add_conditional_edges("route", should_continue, {"generate": "generate", "retrieve": "retrieve", "end": END})
    g.add_edge("retrieve", "human")
    g.add_conditional_edges("human", should_continue, {"generate": "generate", "end": END})
    g.add_edge("generate", "reflect")
    g.add_edge("reflect", END)
    return g.compile()


def ask(question: str, confirmed: bool = True, show_log: bool = False) -> str:
    """对外问答入口：构建图并执行一次完整推理，返回回答文本。"""
    app = build_graph()
    result = app.invoke({"question": question, "confirmed": confirmed})
    if show_log:
        for m in result.get("messages", []):
            print(m)
    return result.get("answer", ""), result
