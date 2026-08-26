"""Agent 工具集：通过 LangChain @tool 定义，供 ReAct Agent 自主调用。

- search_documents: 检索本地文档索引（RAG）
- web_search: 联网搜索实时信息（天气/新闻/事实）
- get_weather: 查询指定城市实时天气
"""
from langchain_core.tools import tool

from retriever import search as _search_docs


@tool
def search_documents(query: str) -> str:
    """在本地文档知识库中检索与 query 相关的内容。

    当用户问题涉及项目文档、技术架构、部署配置、README 内容时调用。
    返回检索到的文档片段（含来源），按相关度从高到低排列。
    """
    try:
        hits = _search_docs(query, top_k=3)
    except Exception as exc:  # noqa: BLE001
        return f"文档检索失败: {exc}"
    if not hits:
        return "没有在文档知识库中找到相关内容。"
    parts = []
    for h in hits:
        parts.append(f"【来源: {h['source']} | 相关度: {h['score']:.3f}】\n{h['chunk']}")
    return "\n\n".join(parts)


@tool
def web_search(query: str) -> str:
    """联网搜索实时信息（天气、新闻、最新事件、事实查询等）。

    当用户问题需要当前时间/实时数据/最新信息，或本地文档无法回答时调用。
    返回搜索结果的标题与摘要。
    """
    try:
        from duckduckgo_search import DDGS

        with DDGS(timeout=10) as ddgs:
            results = list(ddgs.text(query, region="cn-zh", max_results=5))
    except Exception as exc:  # noqa: BLE001
        return f"联网搜索失败: {exc}（可稍后重试或换个问法）"
    if not results:
        return "没有搜索到相关信息。"
    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        parts.append(f"{i}. {title}\n   {body}\n   链接: {href}")
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
