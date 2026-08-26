# -*- coding: utf-8 -*-
"""内置规则回答引擎：离线模式下无需模型即可回答的常见问题。

覆盖：数学计算、时间/日期、问候、身份介绍、能力介绍、礼貌用语。
设计目标：让离线模式能直接回答"简单问题"，不依赖任何模型/网络。

入口: try_rule_answer(question) -> str | None
- 能回答: 返回答案字符串
- 不能回答: 返回 None（交给上层继续处理）
"""
import ast
import datetime
import operator
import re

# 安全求值用的运算符映射
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(expr: str):
    """只允许数字和四则运算的安全求值。"""
    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.BinOp):
            return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            return _OPS[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("unsupported")
    return _eval(ast.parse(expr, mode="eval").body)


def _try_math(q: str) -> str | None:
    """尝试数学计算。"""
    # 中文字符映射 + 常见问法归一
    expr = q
    expr = expr.replace("×", "*").replace("x", "*").replace("X", "*")
    expr = expr.replace("÷", "/")
    expr = expr.replace("加", "+").replace("减", "-").replace("乘", "*").replace("除", "/")
    expr = expr.replace("等于", "=").replace("？", "").replace("?", "").replace("？", "")
    # "等于几/等于多少/是多少/是几" -> "="，取 = 左边
    expr = re.sub(r"等于(几|多少|什么)|是多少|是几", "=", expr)
    if "=" in expr:
        expr = expr.split("=")[0]
    # 只保留数字和运算符
    math_expr = re.sub(r"[^0-9+\-*/().\s]", "", expr).strip()
    # 必须有数字、有运算符、且是合法表达式
    if not math_expr or not re.search(r"\d", math_expr):
        return None
    if not re.fullmatch(r"[\d+\-*/().\s]+", math_expr):
        return None
    if not any(op in math_expr for op in ["+", "-", "*", "/", "(", ")"]):
        return None
    try:
        result = _safe_eval(math_expr)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        elif isinstance(result, float):
            result = round(result, 6)
        return f"{math_expr} = {result}"
    except Exception:  # noqa: BLE001
        return None


def _try_time(q: str) -> str | None:
    """尝试时间/日期回答。"""
    now = datetime.datetime.now()
    if re.search(r"现在.*(几点|时间)|什么时间|几点钟|几点了", q):
        return f"现在是 {now:%H:%M}（本地时间）"
    if re.search(r"今天.*(几号|日期|星期)|今天日期|什么日子|今天是", q):
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        return f"今天是 {now:%Y} 年 {now:%m} 月 {now:%d} 日，星期{weekdays[now.weekday()]}"
    if re.search(r"今年|哪一年|年份", q) and "出生" not in q:
        return f"今年是 {now:%Y} 年"
    return None


def _try_greeting(q: str) -> str | None:
    """尝试问候/身份/能力/礼貌用语。"""
    # 问候
    if re.fullmatch(r"(你好|您好|hi|hello|嗨|哈喽|在吗|早上好|下午好|晚上好)[!！。.~～]*", q):
        return (
            "你好！我是这个项目的 AI 助手。当前处于离线模式，可以回答文档相关问题、"
            "简单数学计算和时间日期等。切换到在线模式可获得完整能力（天气、联网搜索、广泛对话）。"
        )
    # 身份
    if re.search(r"(你是谁|你叫什么|自我介绍|介绍一下你)", q) and "项目" not in q:
        return (
            "我是基于 LangGraph 构建的通用 AI Agent，支持文档问答（RAG）、天气查询、"
            "联网搜索和对话。当前为离线模式，仅内置了基础回答能力；切换到在线模式（DeepSeek）"
            "可获得完整功能。"
        )
    # 能力介绍
    if re.search(r"(你能做什么|你会什么|有什么功能|能力)", q):
        return (
            "我的能力：\n"
            "1. 文档问答：检索项目文档知识库回答\n"
            "2. 实时天气：查询任意城市天气\n"
            "3. 联网搜索：搜索最新信息（新闻等）\n"
            "4. 普通对话：聊天、问答\n"
            "（离线模式下：文档检索 + 简单问题；在线模式：全部能力）"
        )
    # 礼貌
    if re.search(r"^(谢谢|感谢|多谢|thank)", q):
        return "不客气！很高兴能帮到你。如果需要更多帮助，随时问我。"
    if re.search(r"^(再见|拜拜|bye)", q):
        return "再见！期待下次为你服务。"
    return None


def try_rule_answer(question: str) -> str | None:
    """尝试用规则回答。能回答返回答案，否则返回 None。"""
    q = question.strip().lower()

    for handler in (_try_math, _try_time, _try_greeting):
        answer = handler(q)
        if answer:
            return answer
    return None
