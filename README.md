# Pray：通用 AI Agent（LangGraph ReAct）

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建的**通用 AI Agent**——**Pray**，能够回答任何问题：文档问答、联网搜索实时信息（天气/新闻）、普通对话，全部由**模型自主决策**调用工具完成。

> 从"专用文档问答 Agent"升级而来。核心变化：不再用规则判断"该走哪条路"，而是把工具交给模型，由模型自主决定何时调用什么工具（ReAct / Tool-calling 架构，LangGraph 最主流的 Agent 模式）。

## 核心能力

| 能力 | 工具 | 示例 |
|---|---|---|
| 📄 文档问答（RAG） | `search_documents` | "项目的核心架构是什么？" |
| 🌤️ 实时天气 | `get_weather` | "今天北京的天气怎么样？" |
| 🔍 联网搜索 | `web_search` | "最近有什么 AI 新闻？" |
| 💾 代码落盘 | `write_file` | "写一个猜数字游戏"（AI 主动落盘） |
| ▶️ 命令执行 | `run_command` | "运行 calculator.py 验证"（写→跑→修闭环） |
| 🌐 打开浏览器 | `open_in_browser` | "做个扫雷游戏"（自动生成 HTML 并打开） |
| 💬 普通对话 | （直答） | "你好，你是谁？" |

> **代码落盘**：AI 写代码类任务时**主动**调用 `write_file` 落盘到 `generated/`
> 目录（`WRITE_DIR` 可配置）。安全边界：只允许写入该目录内，`../` 逃逸与
> 绝对路径会被拒绝，父目录自动创建。
>
> **命令执行**：AI 写完代码后**主动运行验证**（`python xxx.py`）。安全防护：
> - 破坏性命令（`rm -rf /`、`format`、`shutdown` 等）**黑名单拦截**
> - 高危命令（删除/移动/安装包/联网下载等）**需前端确认**后才执行
>   （工具返回 NEED_CONFIRM → 界面弹窗 → 确认后续问）
> - 只在 `generated/` 目录内执行；30 秒超时强杀；输出截断 2000 字符
>
> ⚠️ **安全边界说明（请务必阅读）**：黑名单是**尽力而为的关键词启发式**，不是
> 沙箱隔离——`python -c "import os; os.remove(...)"` 这类写法可以绕过黑名单，
> 但会被"高危命令需确认"的门槛拦住（`remove` 命中高危规则）。**真正的人为护栏是
> 确认弹窗与超时强杀，而非黑名单本身**。因此**不要**在共享/生产/含敏感数据的环境
> 直接对外暴露本服务：请运行在专用隔离环境（Docker/虚拟机），以普通用户而非 root
> 运行，并配合 `API_TOKEN` 鉴权与网络访问控制。本项目的命令执行设计定位是
> **单机个人开发辅助**，不是安全边界。

> **🖥 交互终端**：代码块「▶ 运行」支持所有可执行语言——HTML 在 iframe 中运行，
> Python/JS/Shell 在**交互终端**中运行（真实 stdin/stdout：程序输出实时显示，
> 你在输入框打字即可操作程序）。终端弹窗由 `/api/run/start|input|output|stop`
> 驱动（子进程 + 轮询），复用黑名单安全校验，关闭弹窗自动终止进程。

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
│          ReAct Agent（langchain create_agent）│
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

文档问答底层是**本地混合检索**（无需外部向量数据库），**定位适合中小型文档集**（数百个片段以内）——检索为内存全量打分 + 结果缓存，文档量极大时建议换向量数据库：

- **语义通道**：[sentence-transformers](https://github.com/UKPLab/sentence-transformers) 本地模型（默认
  `paraphrase-multilingual-MiniLM-L12-v2`，首次使用自动下载到 `models/`，无需 API Key），
  理解同义改写（如"架构"与"分层设计"）
- **词法通道**：中文 [jieba](https://github.com/fxsjy/jieba) 分词 + BM25（k1=1.5, b=0.75），过滤停用词
- **融合**：两通道各自排名后 **RRF（Reciprocal Rank Fusion, k=60）** 融合
- **降级**：embedding 模型不可用（未安装/加载失败）时自动回退纯 BM25，功能不中断
- **缓存**：检索结果按 (查询, 索引版本) 内存缓存，索引重建自动失效
- **索引**：JSON 文件带版本号（V3），旧格式首次启动自动重建（`python main.py build` 可强制重建）

> ⚠️ **首次文档问答会稍慢**：语义模型首次加载需要下载权重（约 470MB，下载后缓存到 `models/`）；
> 网络不可用时自动回退纯 BM25，不影响使用。

## 多轮记忆（checkpointer）

**真正的服务端会话记忆**：基于 LangGraph SQLite checkpointer（`data/memory.sqlite`），
按 `thread_id` 持久化每轮对话，**重启服务不丢失**。

- 前端"新对话"按钮生成新 `thread_id`，同一会话内模型能记住上下文
- 会话侧边栏可切换/删除历史会话；点击会话**回放完整历史消息**
  （`GET /api/sessions`、`GET /api/sessions/{id}/messages`、`DELETE /api/sessions/{id}`）
- 命令行 `python -X utf8 main.py ask "问题"` 为单轮（不传 thread_id）

## 前端界面

零依赖单文件前端（`static/index.html`，无构建工具）：

- **Markdown 渲染**：回答支持标题/列表/代码块（含复制按钮）/表格/引用/链接，自写轻量渲染器
- **流式输出**：`POST /ask/stream`（SSE）逐 token 推送回答，工具调用时显示"正在调用工具"状态
- **会话侧边栏**：左侧列出历史会话（标题=首条消息，按时间倒序），点击**回放完整历史**、✕ 删除、新对话
- **引用来源卡片**：回答下方展示工具调用来源（文档片段 / 网页链接，可点击）
- **暗色模式**：跟随系统或手动切换（localStorage 记忆）
- **消息操作**：hover 显示复制按钮
- **移动端适配**：窄屏侧边栏自动收起（☰ 展开）

## Token 用量与成本

每次回答的"过程详情"面板展示 Token 用量与成本估算（基于各服务商公开单价，见
`config.py` 的 `PROVIDER_PRICES`，可自行调整）：

- **精确用量**：非流式 `/ask` 路径经 LangChain `on_llm_end` 回调获取
- **流式估算**：`/ask/stream` 路径下 **DeepSeek 的流式响应不返回 usage**（服务商限制），
  自动按回答文本长度估算并标注"（估算）"
- 成本估算仅供参考，非账单

## 安全（API Token，可选）

`.env` 配置 `API_TOKEN=xxx` 后，`/ask`、`/ask/stream`、`/api/mode`、`/api/config`、
`/api/sessions` 均要求请求头 `X-API-Token: xxx`，防止本机端口被局域网/他人滥用
（防止盗用 API 额度）。未配置时保持零配置开放（默认绑定 127.0.0.1）。
前端在 ⚙ 设置面板填入 Token 后存入浏览器 localStorage。

> **CSRF 防护**：所有带副作用的请求（POST/DELETE/PUT/PATCH）都会校验 `Origin`
> 请求头，仅允许本机 `127.0.0.1` / `localhost` 来源（非浏览器客户端无 Origin 时放行），
> 防止恶意网页借浏览器跨站调用本服务。

> ⚠️ **超时语义说明**：`/ask` 超时（默认 60 秒）后立即返回 504，但**模型调用无法被
> 取消**——请求仍在后台线程继续执行并消耗额度。这是 Python 线程模型的限制，如需严格
> 取消请改用 asyncio 或子进程隔离。

## 反思（reflection）

每次问答输出一条结构化反思 JSON（前端展示 / 供调优）：

- **工具调用统计**：取自 `AIMessage.tool_calls` 元数据（模型的**结构化输出**），而不是在工具返回的格式化文本里搜关键词——旧版按 "来源"/"文档" 字样猜测是否用了检索，等于让工具自证清白，已废弃
- **grounded 检查**：取工具结果中最长的连续字符片段（≥10 字符），检查其是否出现在最终回答里；回答确实复用了工具内容才判 True，避免"仅引用格式噪声"的假阳性

## 目录结构

```
langgraph-doc-agent/
├── graph.py         # ★ 核心：langchain create_agent 通用 Agent + 反思逻辑 + checkpointer 记忆
├── tools.py         # 工具集：search_documents / web_search / get_weather / write_file / run_command / open_in_browser
├── retriever.py     # jieba+BM25 + embedding 语义的 RRF 混合检索
├── server.py        # 网页服务（并发安全、请求超时、API Token 鉴权）
├── runterm.py       # 交互终端会话（子进程管理：启动/输入/输出/停止）
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
真实工具调用提取与 grounded 检查、来源提取（文档/网页）、
会话列表与删除、历史回放（含 sources/reflection 生成）、流式事件结构、
Token 用量提取与成本估算、真实 HTTP 契约（thread_id/401）。
测试不依赖真实网络/仓库文件系统（索引与记忆隔离到临时目录）。

## 后续扩展

- [ ] 更多工具：日历、邮件、数据库查询
- [ ] 接入 MCP 生态

## License

MIT
