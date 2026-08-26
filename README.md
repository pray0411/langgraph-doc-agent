# 通用 AI Agent（LangGraph ReAct）

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建的**通用 AI Agent**，能够回答任何问题：文档问答、联网搜索实时信息（天气/新闻）、普通对话，全部由**模型自主决策**调用工具完成。

> 从"专用文档问答 Agent"升级而来。核心变化：不再用规则判断"该走哪条路"，而是把工具交给模型，由模型自主决定何时调用什么工具（ReAct / Tool-calling 架构，LangGraph 最主流的 Agent 模式）。

## 核心能力

| 能力 | 工具 | 示例 |
|---|---|---|
| 📄 文档问答（RAG） | `search_documents` | "项目的核心架构是什么？" |
| 🌤️ 实时天气 | `get_weather` | "今天北京的天气怎么样？" |
| 🔍 联网搜索 | `web_search` | "最近有什么 AI 新闻？" |
| 💬 普通对话 | （直答） | "你好，你是谁？" |

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置模型（.env）
#    LLM_PROVIDER=deepseek
#    DEEPSEEK_API_KEY=sk-xxx

# 3. 构建文档索引（可选，用于文档问答）
python main.py build

# 4a. 命令行问答
python -X utf8 main.py ask "今天北京的天气怎么样？"
python -X utf8 main.py ask "这个项目的技术栈是什么？"

# 4b. 网页问答（推荐）
python -X utf8 main.py web
# 浏览器打开 http://127.0.0.1:8000
```

> Windows 命令行中文乱码时，请使用 `python -X utf8 main.py ...`（网页端无此问题）。

## 架构：ReAct 模式

```
用户提问
   │
   ▼
┌─────────────────────────────────────────┐
│          ReAct Agent（create_react_agent）│
│                                         │
│  模型自主循环：                          │
│    思考(Reason) → 行动(Act/调工具)      │
│    → 观察(Observe) → 再思考 → ...      │
│                                         │
│  工具集：                               │
│    search_documents  (本地文档 RAG)     │
│    get_weather        (实时天气)        │
│    web_search         (联网搜索)        │
└─────────────────────────────────────────┘
   │
   ▼
  最终回答
```

**关键点**：模型根据问题内容自主决定：
- 问天气 → 调 `get_weather`
- 问文档 → 调 `search_documents`
- 问实时信息 → 调 `web_search`
- 普通聊天 → 直接回答，不调工具
- 复杂任务 → 连续调用多个工具

## 目录结构

```
langgraph-doc-agent/
├── graph.py         # ★ 核心：create_react_agent 通用 Agent
├── graph_v1.py      # V1 版：规则路由的专用文档问答（留档对比）
├── tools.py         # 工具集：search_documents / web_search / get_weather
├── retriever.py     # TF-IDF 检索（文档问答底层）
├── llm.py           # 模型封装（V1 遗留，保留兼容）
├── server.py        # 网页服务
├── main.py          # 命令行入口
├── config.py        # 配置
├── requirements.txt
├── .env.example
├── docs/            # 文档知识库
├── static/          # 网页前端
└── index/           # 检索索引
```

## 实现要点（面试可讲）

### 1. ReAct 架构
用 `create_react_agent(model, tools)` 构建，模型在循环中自主决定：
`思考 → 调用工具 → 观察结果 → 再思考`，直到给出最终回答。

### 2. 工具封装
用 `@tool` 装饰器定义工具（名称、描述、参数 schema 自动生成），模型通过函数调用来"使用"工具。工具描述写得越清楚，模型越会正确选择。

### 3. 模型自主 vs 规则路由
- **V1（规则路由）**：用相似度阈值判断走哪条路，硬编码、不灵活
- **V2（模型自主）**：模型根据语义自己选工具，能处理任何问题，可扩展

### 4. 升级过程（真实迭代）
从 V1 到 V2 的升级：新增 `tools.py`、用 `create_react_agent` 重构、修复若干集成问题（import、编码、缩进）。这个"踩坑→解决"过程是真实的工程经验，commit 历史完整保留。

## 常见问题

**Q: 天气查询用的什么？**
A: `get_weather` 调用 wttr.in 公开接口，无需 API Key，返回城市实时天气。

**Q: 联网搜索用的什么？**
A: DuckDuckGo Search（`duckduckgo_search` 包），加了 `region='cn-zh'` 适配中文搜索。若网络不通可换 SerpAPI/Bocha 等。

**Q: 如何加更多能力？**
A: 在 `tools.py` 里用 `@tool` 新增一个函数，然后在 `graph.py` 的 `tools=[...]` 里注册即可。

## 后续扩展

- [ ] 多轮对话记忆（checkpointer）
- [ ] 流式输出（SSE）
- [ ] 更多工具：日历、邮件、数据库查询
- [ ] 接入 MCP 生态
- [ ] 检索升级为 embedding 向量库

## License

MIT
