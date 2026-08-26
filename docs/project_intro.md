# LangGraph 智能文档问答 Agent 项目说明

## 项目简介

本项目是一个基于 LangGraph 构建的智能文档问答 Agent。用户可以用自然语言对指定文档集合提问，Agent 会先判断问题意图，再从本地文档索引中检索相关片段，最后由大语言模型结合检索内容生成回答。整个过程通过 LangGraph 状态图编排，支持人工确认（human-in-the-loop）与回答反思。

## 技术架构

项目采用分层设计，核心是 LangGraph 的 StateGraph 状态机：

1. 路由节点（route_query）：判断用户问题是否与文档内容相关，相关则进入检索流程，不相关则直接结束或走通用问答。
2. 检索节点（retrieve）：将问题向量化，与本地 TF-IDF 索引中的片段计算余弦相似度，召回 Top-K 相关片段。
3. 人工确认节点（human_review）：支持 human-in-the-loop，可人工确认检索结果是否可用。
4. 生成节点（generate）：把召回片段组装成上下文提示词，调用大语言模型生成回答。
5. 反思节点（reflect）：检查回答是否基于文档内容，输出反思记录，为后续优化提供依据。

## 使用的技术栈

- Python 3.10+：项目主要开发语言。
- LangGraph：基于状态图的工作流编排框架，定义节点、边与条件跳转。
- 检索：本地 TF-IDF 稀疏向量 + 余弦相似度，无需外部向量数据库，离线可运行。
- 模型：支持 DeepSeek、OpenAI 兼容 API，以及无 API Key 的离线演示模式。
- 服务：Python 标准库 http.server 提供网页问答界面，零第三方依赖。

## 部署与使用

1. 安装依赖：pip install -r requirements.txt
2. 构建索引：python main.py build
3. 命令行提问：python main.py ask "问题"
4. 启动网页：python main.py web，浏览器访问 http://127.0.0.1:8000

## 配置说明

通过 .env 文件配置模型提供商（LLM_PROVIDER）、API Key、模型名称、检索参数（TOP_K 等）。默认使用离线模式，无需任何 API Key 即可完整演示全流程。

## 目录结构

- graph.py：LangGraph 状态图定义与节点逻辑
- retriever.py：文档加载、切分、TF-IDF 索引与检索
- llm.py：模型封装（DeepSeek / OpenAI / 离线）
- server.py：网页问答服务
- main.py：命令行入口
- config.py：全局配置
- docs/：文档目录，放入此目录的文件会被索引
