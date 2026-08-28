# Pray：通用 AI Agent（LangGraph ReAct）

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建的**通用 AI Agent**——**Pray**，能够回答任何问题：文档问答、联网搜索实时信息（天气/新闻）、普通对话，全部由**模型自主决策**调用工具完成。

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

> ⚠️ **修改代码后记得重启网页服务**：Python 服务启动时会把模块加载进内存，
> 改了 `tools.py` / `graph.py` 等代码后，必须停止旧服务（Ctrl+C 或结束进程）
> 再重新 `python -X utf8 main.py web` 启动，新代码才生效。

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

## 检索：BM25 + 语义向量（混合检索）

文档问答底层是**本地混合检索**（无需外部向量数据库）：

- **语义通道**：[sentence-transformers](https://github.com/UKPLab/sentence-transformers) 本地模型（默认
  `paraphrase-multilingual-MiniLM-L12-v2`，首次运行自动下载到 `models/`，无需 API Key），
  理解同义改写（如"架构"与"分层设计"）
- **词法通道**：中文 [jieba](https://github.com/fxsjy/jieba) 分词 + BM25（k1=1.5, b=0.75），过滤停用词
- **融合**：两通道各自排名后 **RRF（Reciprocal Rank Fusion, k=60）** 融合
- **降级**：embedding 模型不可用（未安装/加载失败）时自动回退纯 BM25，功能不中断
- **索引**：JSON 文件带版本号（V3），旧格式首次启动自动重建（`python main.py build` 可强制重建）

## 多轮记忆（checkpointer）

**真正的服务端会话记忆**：基于 LangGraph SQLite checkpointer（`data/memory.sqlite`），
按 `thread_id` 持久化每轮对话，**重启服务不丢失**。

- 前端"新对话"按钮生成新 `thread_id`，同一会话内模型能记住上下文
- `/ask` 响应回传 `thread_id`，浏览器刷新后继续同一会话
- 命令行 `python -X utf8 main.py ask "问题"` 为单轮（不传 thread_id）

## 安全（API Token，可选）

`.env` 配置 `API_TOKEN=xxx` 后，`/ask`、`/api/mode`、`/api/config` 要求请求头
`X-API-Token: xxx`，防止本机端口被局域网/他人滥用（防止盗用 API 额度）。
未配置时保持零配置开放（默认绑定 127.0.0.1）。前端在 ⚙ 设置面板填入 Token 后
存入浏览器 localStorage。

## 反思（reflection）

每次问答输出一条结构化反思 JSON（前端展示 / 供调优）：

- **工具调用统计**：取自 `AIMessage.tool_calls` 元数据（模型的**结构化输出**），而不是在工具返回的格式化文本里搜关键词——旧版按 "来源"/"文档" 字样猜测是否用了检索，等于让工具自证清白，已废弃
- **grounded 检查**：取工具结果中最长的连续字符片段（≥10 字符），检查其是否出现在最终回答里；回答确实复用了工具内容才判 True，避免"仅引用格式噪声"的假阳性

## 目录结构

```
langgraph-doc-agent/
├── graph.py         # ★ 核心：create_react_agent 通用 Agent + 反思逻辑 + checkpointer 记忆
├── tools.py         # 工具集：search_documents / web_search / get_weather
├── retriever.py     # jieba+BM25 + embedding 语义的 RRF 混合检索
├── server.py        # 网页服务（并发安全、请求超时、API Token 鉴权）
├── main.py          # 命令行入口
├── config.py        # 配置（运行时 provider 动态切换、记忆/检索/鉴权配置）
├── legacy/          # V1 历史存档（graph_v1.py / llm.py），不参与运行
├── start.bat        # 前台启动脚本
├── start-background.bat  # 后台静默启动（开机自启用）
├── stop.bat         # 停止服务
├── requirements.txt
├── .env.example
├── docs/            # 文档知识库
├── static/          # 网页前端
├── index/           # 检索索引
├── data/            # 会话记忆（memory.sqlite，运行时生成）
└── models/          # embedding 模型缓存（首次运行下载）
```

> `legacy/` 中的 `graph_v1.py`（StateGraph 版本）与 `llm.py`（V1 模型封装）仅作学习参考，
> 不参与任何运行路径，也不要在新代码中 import 它们（见 `legacy/README.md`）。

## 测试

```bash
python -m pytest tests/ -v
```

测试覆盖：混合检索（语义通道/BM25 回退/无关拒答）、checkpointer 多轮记忆、
API Token 鉴权、并发配置读写、工具降级（网络故障时不崩溃）、
真实工具调用提取与 grounded 检查、服务端超时语义。
测试不依赖真实网络/仓库文件系统（索引与记忆隔离到临时目录）。

## 后续扩展

- [ ] 流式输出（SSE）
- [ ] 更多工具：日历、邮件、数据库查询
- [ ] 接入 MCP 生态
- [ ] 前端会话切换（多会话列表）

## License

MIT
