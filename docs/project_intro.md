# Pray：LangGraph 通用 AI Agent 项目说明

## 项目简介

**Pray** 是一个基于 LangGraph 构建的通用 AI Agent。用户可以用自然语言提问，Agent 由模型自主决策调用工具完成回答：文档问答（RAG）、联网搜索（天气/新闻）、普通对话。

## 技术架构

采用 LangGraph 官方最主流的 **ReAct / Tool-calling Agent** 模式（`create_react_agent`），模型在循环内自主决定：

1. 文档问题 → 调用 `search_documents` 工具，检索本地文档索引
2. 实时问题 → 调用 `web_search` / `get_weather` 工具
3. 普通对话 → 直接回答，不调工具
4. 复杂任务 → 连续调用多个工具（思考 → 行动 → 观察 → 再思考）

**多轮记忆**：通过 LangGraph SQLite checkpointer 按 `thread_id` 持久化会话，重启服务不丢失。

每次问答会输出结构化反思 JSON：工具调用统计取自模型的结构化输出（`AIMessage.tool_calls`），
并检查回答是否复用了工具结果内容（grounded 检查）。

## 使用的技术栈

- Python 3.10+：项目主要开发语言。
- LangGraph：`create_react_agent` 通用 Agent 编排 + SQLite checkpointer 多轮记忆。
- 检索：本地 **jieba 分词 + BM25** 与 **sentence-transformers 语义向量** 的 RRF 混合检索，
  无需外部向量数据库；语义模型不可用时自动回退纯 BM25。
- 模型：支持 DeepSeek、OpenAI 及多家 OpenAI 兼容服务商，也可接本地 Ollama。
- 服务：Python 标准库 `http.server` 提供网页问答界面，零第三方依赖。

## 部署与使用

1. 安装依赖：pip install -r requirements.txt
2. 构建索引：python main.py build
3. 命令行提问：python main.py ask "问题"
4. 启动网页：python main.py web，浏览器访问 http://127.0.0.1:8000

## 配置说明

通过 .env 文件配置模型提供商（LLM_PROVIDER）、API Key、模型名称、检索参数（TOP_K、MIN_SCORE、EMBEDDING_MODEL）、
会话记忆位置（MEMORY_DB）、API Token 鉴权（API_TOKEN）等。

## 目录结构

- graph.py：create_react_agent 通用 Agent、checkpointer 记忆与反思逻辑
- retriever.py：文档加载、切分、jieba 分词、BM25 + 语义向量混合索引与检索
- tools.py：Agent 工具集（文档检索 / 联网搜索 / 天气）
- server.py：网页问答服务（并发安全、超时、API Token 鉴权）
- main.py：命令行入口
- config.py：全局配置（运行时 provider 动态切换）
- legacy/：V1 历史存档（graph_v1.py / llm.py），不参与运行
- docs/：文档目录，放入此目录的文件会被索引
