"""Agent 工具集：通过 LangChain @tool 定义，供 ReAct Agent 自主调用。

- search_documents: 检索本地文档索引（RAG）
- web_search: 联网搜索实时信息（天气/新闻/事实）
- get_weather: 查询指定城市实时天气
"""
from langchain_core.tools import tool

from config import BOCHA_API_KEY, TOP_K
from retriever import search as _search_docs


def _bocha_search(query: str, max_results: int = 5) -> list[dict]:
    """博查搜索（国内，中文质量高，Agent 专用）。"""
    import json as _json
    import urllib.request

    payload = _json.dumps(
        {"query": query, "freshness": "noLimit", "summary": True, "count": max_results}
    ).encode()
    req = urllib.request.Request(
        "https://api.bochaai.com/v1/web-search",
        data=payload,
        headers={
            "Authorization": f"Bearer {BOCHA_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=15)
    data = _json.loads(resp.read().decode("utf-8"))
    # 博查响应结构: {code, data: {webPages: {value: [...]}}}
    values = data.get("data", {}).get("webPages", {}).get("value", []) or []
    results = []
    for v in values:
        results.append(
            {
                "title": v.get("name", ""),
                "body": v.get("summary") or v.get("snippet", ""),
                "href": v.get("url", ""),
            }
        )
    return results

def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """DuckDuckGo 搜索（英文较好，中文一般）。"""
    from duckduckgo_search import DDGS

    with DDGS(timeout=10) as ddgs:
        raw = list(ddgs.text(query, region="cn-zh", max_results=max_results))
    if not raw:
        with DDGS(timeout=10) as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
    results = []
    for r in raw:
        results.append(
            {"title": r.get("title", ""), "body": r.get("body", ""), "href": r.get("href", "")}
        )
    return results


@tool
def search_documents(query: str) -> str:
    """在本地文档知识库中检索与 query 相关的内容。

    当用户问题涉及项目文档、技术架构、部署配置、README 内容时调用。
    返回检索到的文档片段（含来源），按相关度从高到低排列。
    """
    try:
        hits = _search_docs(query, top_k=TOP_K)
    except Exception as exc:  # noqa: BLE001
        return f"文档检索失败: {exc}"
    if not hits:
        return "没有在文档知识库中找到相关内容。"
    # 带编号的来源列表，指示模型回答时引用来源编号
    parts = ["以下为检索到的文档片段（回答时请用 [1][2]... 标注来源）："]
    for i, h in enumerate(hits, 1):
        parts.append(f"[{i}] 来源: {h['source']} | 相关度: {h['score']:.3f}\n{h['chunk']}")
    return "\n\n".join(parts)


@tool
def web_search(query: str) -> str:
    """联网搜索实时信息（天气、新闻、最新事件、事实查询等）。

    当用户问题需要当前时间/实时数据/最新信息，或本地文档无法回答时调用。
    返回搜索结果的标题与摘要。自动选择搜索引擎：配置了 Bocha 博查 API Key
    时优先用博查（中文搜索质量高），否则退回 DuckDuckGo。
    """
    results = []
    errors = []
    # 引擎1: 博查（配置了 Key 才用）
    if BOCHA_API_KEY:
        try:
            results = _bocha_search(query)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"博查: {exc}")
    # 引擎2: DuckDuckGo（博查失败或无 Key 时兜底）
    if not results:
        try:
            results = _ddg_search(query)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"DuckDuckGo: {exc}")
    if not results:
        detail = "；".join(errors) if errors else "无结果"
        return f"没有搜索到相关信息。（{detail}）"
    parts = ["以下为搜索到的网页结果（回答时请引用对应编号的链接作为来源）："]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        parts.append(f"[{i}] {title}\n   摘要: {body}\n   链接: {href}")
    return "\n".join(parts)


@tool
def get_weather(city: str) -> str:
    """查询指定城市的当前天气。

    当用户问某个城市的天气、气温、是否下雨等情况时调用。
    城市使用中文或拼音均可，例如 '北京' / 'beijing' / '上海'。
    返回天气描述、温度、风力等信息。
    """
    import urllib.parse
    import urllib.request

    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&lang=zh"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.read().decode("utf-8", errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        return f"天气查询失败: {exc}（请确认城市名，或稍后重试）"
