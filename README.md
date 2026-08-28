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

> 💡 **离线模式**：不配置任何 API Key（`LLM_PROVIDER=offline`）时，Agent 会用
> 本地文档检索给出演示回答，适合无 Key 环境验证链路；配置 DeepSeek/OpenAI
> Key 后获得完整能力（联网搜索、天气、智能对话）。

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

## 检索：jieba + BM25

文档问答底层是**本地稀疏检索**（无需外部向量数据库，离线可跑）：

- **分词**：中文用 [jieba](https://github.com/fxsjy/jieba)（未安装时自动降级为单字+双字 bigram），过滤中文停用词
- **排序**：BM25（k1=1.5, b=0.75），比旧版 TF-IDF + 余弦相似度对长文档更公平
- **索引**：JSON 文件带版本号，旧格式索引首次启动自动重建（`python main.py build` 可强制重建）
- **阈值**：与查询无任何共现词的片段分数 ≤ 0，默认 `MIN_SCORE=0.0` 直接过滤

## 反思（reflection）

每次问答输出一条结构化反思 JSON（前端展示 / 供调优）：

- **工具调用统计**：取自 `AIMessage.tool_calls` 元数据（模型的**结构化输出**），而不是在工具返回的格式化文本里搜关键词——旧版按 "来源"/"文档" 字样猜测是否用了检索，等于让工具自证清白，已废弃
- **grounded 检查**：取工具结果中最长的连续字符片段（≥10 字符），检查其是否出现在最终回答里；回答确实复用了工具内容才判 True，避免"仅引用格式噪声"的假阳性

## 目录结构

```
langgraph-doc-agent/
├── graph.py         # ★ 核心：create_react_agent 通用 Agent + 反思逻辑
├── tools.py         # 工具集：search_documents / web_search / get_weather
├── retriever.py     # jieba 分词 + BM25 检索（文档问答底层）
├── rule_engine.py   # 离线模式内置规则回答（数学/时间/问候）
├── server.py        # 网页服务（含并发安全与请求超时）
├── main.py          # 命令行入口
├── config.py        # 配置（含运行时 provider 动态切换，线程安全）
├── legacy/          # V1 历史存档（graph_v1.py / llm.py），不参与运行
├── requirements.txt
├── .env.example
├── docs/            # 文档知识库
├── static/          # 网页前端
└── index/           # 检索索引
```

> `legacy/` 中的 `graph_v1.py`（StateGraph 版本）与 `llm.py`（V1 模型封装）仅作学习参考，
> 不参与任何运行路径，也不要在新代码中 import 它们（见 `legacy/README.md`）。

## 测试

```bash
python -m pytest tests/ -v
```

测试覆盖：BM25 检索（相关/无关/阈值/索引升级）、离线模式、并发配置读写、
工具降级（网络故障时不崩溃）、真实工具调用提取与 grounded 检查、服务端超时语义。
测试不依赖真实网络/时间/仓库文件系统（索引隔离到临时目录）。

## 后续扩展

- [ ] 多轮对话记忆（checkpointer）
- [ ] 流式输出（SSE）
- [ ] 更多工具：日历、邮件、数据库查询
- [ ] 接入 MCP 生态
- [ ] 检索升级为 embedding 向量库

## License

MIT
