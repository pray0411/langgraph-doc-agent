# LangGraph 智能文档问答 Agent

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建的智能文档问答 Agent。用自然语言对本地文档集合提问，Agent 通过 **意图路由 → 向量检索 → 人工确认 → 模型生成 → 回答反思** 的状态图流程给出基于文档的回答。

> 这是一个从零搭建的 Agent 工程练手项目：状态图编排、RAG 检索、human-in-the-loop、可插拔模型、CLI + Web 双入口。

## 核心特性

- 🧠 **LangGraph 状态图编排**：5 个节点 + 条件边，完整展示 Agent 工作流
- 📚 **RAG 检索**：本地 TF-IDF 稀疏向量 + 余弦相似度，零外部依赖，离线可运行
- 🤖 **可插拔模型**：DeepSeek / OpenAI 兼容 API / 离线演示模式，一键切换
- 🧑‍💻 **human-in-the-loop**：检索后人工确认节点，体现工程可靠性设计
- 🔍 **回答反思**：输出 grounded 检查与质量反思记录
- 🌐 **双入口**：命令行问答 + 网页问答（纯标准库，零第三方前端依赖）

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

## 配置模型（可选）

默认**离线模式**，无需任何 API Key 即可完整演示。想接真实大模型，复制 `.env.example` 为 `.env` 并填写：

```ini
# DeepSeek（国内直连，推荐练手）
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxx
LLM_MODEL=deepseek-chat

# 或 OpenAI
# LLM_PROVIDER=openai
# OPENAI_API_KEY=sk-xxxx

# 或离线演示
# LLM_PROVIDER=offline
```

> ⚠️ `.env` 已加入 `.gitignore`，API Key 不会进入版本库。
python main.py build

# 3a. 命令行提问
python main.py ask "这个项目的核心架构是什么？"

# 3b. 或启动网页问答
python main.py web          # 浏览器访问 http://127.0.0.1:8000
python main.py web --port 9000
```

## 配置模型（可选）

默认**离线模式**，无需任何 API Key 即可完整演示。想接真实大模型，复制 `.env.example` 为 `.env` 并填写：

```ini
# DeepSeek（国内直连，推荐练手）
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxx
LLM_MODEL=deepseek-chat

# 或 OpenAI
# LLM_PROVIDER=openai
# OPENAI_API_KEY=sk-xxxx

# 或离线演示
# LLM_PROVIDER=offline
```

## 工作流程

```
用户提问
   │
   ▼
┌─────────────┐   文档相关?    ┌─────────────┐
│ route_query │──────────────▶│  retrieve   │  TF-IDF 检索 top-K
└─────────────┘  否           └─────────────┘
   │ 结束                           │
                                    ▼
                              ┌─────────────┐   确认通过?
                              │ human_review│──────────────┐
                              └─────────────┘   否         │
                                    │ 是                    │
                                    ▼                       │
                              ┌─────────────┐               │
                              │  generate   │               │
                              └─────────────┘               │
                                    │                       │
                                    ▼                       │
                              ┌─────────────┐               │
                              │   reflect   │               │
                              └─────────────┘               │
                                    │                       │
                                    ▼                       ▼
                                 回答                   结束
```

## 目录结构

```
langgraph-doc-agent/
├── config.py        # 全局配置（环境变量）
├── retriever.py     # 文档加载、切分、TF-IDF 索引与检索
├── llm.py           # 模型封装（DeepSeek / OpenAI / 离线）
├── graph.py         # LangGraph 状态图定义与节点逻辑 ★ 核心
├── server.py        # 网页问答服务（http.server）
├── main.py          # 命令行入口
├── requirements.txt
├── .env.example
├── docs/            # 文档目录：放进去的文件会被索引
│   └── project_intro.md
├── static/          # 网页前端
│   └── index.html
└── index/           # 构建生成的索引（自动创建）
```

## 实现要点（面试可讲）

### 状态设计
`AgentState` 用 `TypedDict` 定义，节点间通过 state 传递 `question / intent / retrieved / confirmed / answer / reflection / messages`，体现 LangGraph 的"状态即数据流"设计。

### 条件边
- `route` 之后：`doc_qa` → retrieve，`general` → 结束
- `human_review` 之后：确认通过 → generate，拒绝 → 结束

### human-in-the-loop
`confirmed` 字段控制是否放行生成节点。演示默认放行，生产可改为异步人工审批接口。

### grounded 反思
`reflect` 节点用检索片段与回答的重叠词粗粒度判断"是否基于文档"，输出反思记录，可扩展为更严谨的引用校验。

## 常见问题

**Q: 检索效果一般？**
A: 本地 TF-IDF 是词面匹配，对同义词不敏感。可升级为 embedding 向量检索（如 BGE/OpenAI embedding）或 BM25 混合检索。

**Q: 如何加文档？**
A: 把 `.md/.txt/.py/.rst/.html` 文件放进 `docs/`（支持子目录），重新 `python main.py build` 即可。

**Q: 模型调用报错？**
A: 检查 `.env` 中 `LLM_PROVIDER` 与 Key 是否正确；网络受限时改用 `LLM_PROVIDER=offline`。

## 后续扩展方向

- [ ] embedding 向量检索（FAISS / Chroma / 开源 BGE 模型）
- [ ] 多轮对话记忆（LangGraph checkpointer）
- [ ] 流式输出（SSE）
- [ ] 多文档引用标注与溯源
- [ ] 知识库增量更新（watchdog 监控 docs 目录）
- [ ] 接入 MCP 工具，扩展 Agent 能力

## License

MIT
