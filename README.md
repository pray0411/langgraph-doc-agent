# Pray：通用 AI Agent（LangGraph ReAct）

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建的**通用 AI Agent**——**Pray**，能够回答任何问题：文档问答、联网搜索实时信息（天气/新闻）、普通对话，全部由**模型自主决策**调用工具完成。

> 从"专用文档问答 Agent"升级而来。核心变化：不再用规则判断"该走哪条路"，而是把工具交给模型，由模型自主决定何时调用什么工具（ReAct / Tool-calling 架构，LangGraph 最主流的 Agent 模式）。

[![CI](https://github.com/pray0411/langgraph-doc-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/pray0411/langgraph-doc-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **badge 是可验证的**：点进去能看到最近的运行结果、覆盖率报告与失败详情（CI 的
> artifact 里有 `junit/*.xml`、`coverage.xml`、`report/report.html`）。
> CI 在 **Windows + Linux × Python 3.10/3.12/3.13** 六个组合上跑，且**不提供任何
> API Key、不提供 `.env`** —— 用来证明"测试通过"这句话在别人的机器上同样成立。


## 核心能力

| 能力 | 工具 | 示例 |
|---|---|---|
| 📄 文档问答（RAG） | `search_documents` | "项目的核心架构是什么？" |
| 🌤️ 实时天气 | `get_weather` | "今天北京的天气怎么样？" |
| 🔍 联网搜索 | `web_search` | "最近有什么 AI 新闻？" |
| 💾 代码落盘 | `write_file` | "写一个猜数字游戏"（AI 主动落盘） |
| ▶️ 命令执行 | `run_command` | "运行 calculator.py 验证"（写→跑→修闭环） |
| 🌐 打开浏览器 | `open_in_browser` | "做个扫雷游戏"（自动生成 HTML 并打开） |
| 📖 网页抓取 | `fetch_url` | "锐评这个 GitHub 项目"（白名单只读抓取 README/元数据） |
| 💬 普通对话 | （直答） | "你好，你是谁？" |

> **网页抓取**：`fetch_url` 用于读取 GitHub 仓库真实内容（锐评/分析项目场景）。
> 只读安全设计：仅允许 `github.com` / `api.github.com` / `raw.githubusercontent.com`
> 三个域名，仅 GET、不执行任何代码，重定向逐跳校验白名单，响应大小上限。
> 仓库根 URL 自动抓 README；元数据走 `api.github.com/repos/<owner>/<repo>` JSON。

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

## 全局记忆（跨会话，profile 表）

**分层记忆架构**：会话记忆（checkpointer，thread 内短期）+ **全局记忆** +
system prompt 约束（规则）。

- 全局记忆存用户**长期稳定信息**（称呼/身份/语言偏好/项目背景），跨会话保留，
  与会话记忆分离存储（`data/global_memory.sqlite` 的 `profile` 表，key 去重覆盖）
- 模型在对话中**主动 remember**（透露稳定个人信息时）与 **forget**（用户要求遗忘时）
- 注入方式：每次构建 agent 时把记忆拼进 system prompt；记忆版本号纳入 agent
  缓存 key，remember/forget 后自动重建——无需每次对话重建
- 管控：`GET /api/memory` 查看、`DELETE /api/memory?key=<键>` 遗忘
- 设计取舍：本项目记忆是"精确事实"而非"模糊回忆"，结构化 profile 表比向量检索
  更省更准；向量式长期记忆（Mem0 类）适合海量非结构化场景，暂不需要

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
├── graph.py         # ★ 核心：langchain create_agent 通用 Agent + 反思逻辑 + checkpointer 会话记忆 + 全局记忆注入
├── memory.py        # 全局记忆（SQLite profile 表，跨会话长期记忆，模型可 remember/forget）
├── tools.py         # 工具集：search_documents / web_search / get_weather / write_file / run_command / open_in_browser / fetch_url
├── retriever.py     # jieba+BM25 + embedding 语义的 RRF 混合检索
├── server.py        # 网页服务（并发安全、请求超时、API Token 鉴权）
├── mcp_server.py    # MCP Server：把 Pray 暴露给 Claude Desktop 等 MCP 客户端
├── desktop.py       # 桌面端：pywebview 内嵌系统 WebView 加载本地 UI
├── runterm.py       # 交互终端会话（子进程管理：启动/输入/输出/停止）
├── main.py          # 命令行入口
├── config.py        # 配置（运行时 provider 动态切换、记忆/检索/鉴权配置）
├── approvals.py     # 高危命令审批登记（一次性消费 + 5 分钟 TTL）
├── logging_setup.py # 结构化日志 + 安全审计日志（凭据脱敏）
├── legacy/          # V1 历史存档（graph_v1.py / llm.py），不参与运行
├── tests/           # 测试（含元测试 / 契约 / 集成 / 评估 / E2E / 性能分层）
├── VERSION          # 版本号单一来源（config.APP_VERSION、pyproject 均引用它）
├── pyproject.toml   # 统一配置：pytest / coverage / ruff / mutmut
├── Dockerfile       # 隔离运行环境（非 root 用户 + 健康检查）
├── .github/workflows/  # CI（六组合矩阵 + lint + 漏洞扫描）/ Release（打 tag 出包）
├── CHANGELOG.md     # 迭代记录
├── LICENSE          # MIT
├── start.bat        # 前台启动脚本
├── start-background.bat  # 后台静默启动（不会自动注册开机自启；如需自启请自行把
│                         #   start-background.bat 快捷方式放入「启动」文件夹）
├── stop.bat         # 停止服务
├── requirements.txt / requirements-dev.txt / requirements-mcp.txt / requirements-desktop.txt
├── .env.example
├── docs/            # 文档知识库
├── static/          # 网页前端
├── index/           # 检索索引
├── data/            # 会话记忆（memory.sqlite，运行时生成）
└── models/          # embedding 模型缓存（首次运行下载）
```

> `legacy/` 中的 `graph_v1.py`（StateGraph 版本）与 `llm.py`（V1 模型封装）仅作学习参考，
> 不参与任何运行路径，也不要在新代码中 import 它们（见 `legacy/README.md`）。

## MCP 接入（把 Pray 暴露给 Claude Desktop 等客户端）

Pray 可作为一个 **MCP Server** 运行，让任意 MCP 客户端直接调用它的能力：

```bash
pip install -r requirements-mcp.txt   # 安装 fastmcp
python -X utf8 mcp_server.py          # stdio 模式（Claude Desktop 用）
python -X utf8 mcp_server.py --http   # Streamable HTTP 模式（MCP Inspector 调试）
```

暴露的工具：
- **整包**：`ask(question, new_thread=False)` —— 一次调用走 Pray 完整 Agent
  （自动检索/联网/调工具/多轮记忆），客户端把它当"一个会干活的助手"
- **拆件**：`search_documents` / `web_search` / `get_current_time` /
  `list_memory` / `remember` / `forget`

**安全边界**：不暴露 `run_command`/`write_file`/`fetch_url` 等执行类工具——MCP
客户端没有 Pray 前端的高危确认闸，本 server 只开放"只读检索 + 问答 + 记忆"面。

接入 Claude Desktop（`claude_desktop_config.json`）：
```json
{
  "mcpServers": {
    "pray": {
      "command": "C:\\Users\\<你>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["-X", "utf8", "D:\\路径\\langgraph-doc-agent\\mcp_server.py"]
    }
  }
}
```
（Windows 建议写 python.exe 绝对路径；模型 Key 等配置仍走项目 `.env`。）

## 桌面端（pywebview）

```bash
pip install pywebview
python -X utf8 desktop.py            # 启动桌面窗口（端口自动选空闲）
python -X utf8 desktop.py --check    # 无窗口自检（起服务→请求首页→退出）
```

实现：后台线程运行与网页版相同的服务，pywebview 用**系统 WebView**
（Windows = Edge WebView2，Win10/11 自带）加载页面——无 Electron/Chromium 体积，
前端零改动（同源访问，CSRF 校验天然通过）。关窗即关服务退出。

打包 exe：`pip install pyinstaller && pyinstaller Pray.spec`。
`Pray.spec` 默认排除 torch/sentence-transformers（桌面版检索自动回退纯 BM25），
体积 ~200MB；如需语义检索删掉对应 excludes 再打（体积 >1GB，首次要下模型）。

## 测试与质量体系

```bash
# 安装（运行时 + 开发依赖分开，部署镜像不带测试工具链）
pip install -r requirements.txt -r requirements-dev.txt

# 全量（含覆盖率门禁：低于 70% 直接失败）
python -X utf8 -m pytest tests/ --cov=. --cov-report=term-missing

# 快速迭代（跳过偏慢的性能基线）
python -X utf8 -m pytest tests/ -m "not slow"

# 检索质量评估（recall@k / MRR，指标跌破阈值即失败）
python -X utf8 -m pytest tests/ -m eval -q -s

# 前端 E2E（需先 pip install playwright && python -m playwright install chromium）
python -X utf8 -m pytest tests/e2e -m e2e -q

# 性能基线（P50/P95 并显示实测数字）
python -X utf8 -m pytest tests/ -m slow -q -s
# 在已知机器上重新生成基线（提交 perf_baseline.json 后，门禁变为"不超过基线 1.5×"）
PERF_WRITE_BASELINE=1 python -X utf8 -m pytest tests/test_perf.py -m slow -q -s

# 句柄泄漏排查（ResourceWarning 默认可见但不致命，需要严查时用这条）
python -X utf8 -m pytest tests/ -W error::ResourceWarning

# 依赖漏洞扫描
pip-audit -r requirements.txt --desc
```

### 测试分层

| 层 | 位置 | 说明 |
|---|---|---|
| 元测试 | `tests/test_metatest.py` | **测试套件自检**：同名用例、环境密封性、自证式用例、版本号单一来源、CI 配置有效性 —— 用测试守护测试本身 |
| 单元 | `tests/test_core.py` | 纯函数与模块级行为（检索、记忆、反思、成本估算） |
| 契约 | `tests/test_server_writes.py` | 真实 HTTP 打写接口：上传/删除/改名/重建索引/审批/命令执行 |
| 集成 | `tests/test_agent_loop.py` | **本地假 OpenAI 兼容服务**驱动真实 ReAct 工具循环（不联网、不花钱、确定性） |
| 多 provider | `tests/test_provider_contract.py` | 6 个 provider 的配置解析 + 请求构造契约（不真调 API） |
| 质量评估 | `tests/test_retrieval_eval.py` | recall@3 / MRR 带阈值门禁 |
| 前端 E2E | `tests/e2e/` | Playwright 金路径 + XSS 注入 + 畸形 Markdown |
| 性能基线 | `tests/test_perf.py` | 检索 P95 / SSE 首字 / 并发检索 |
| CLI | `tests/test_cli.py` | 命令行入口与参数校验 |
| 日志审计 | `tests/test_logging_audit.py` | 结构化日志 + 凭据脱敏审计 |

### 这套测试怎么做到"密封"

测试结果**不依赖开发者本机环境**，靠三层封堵（都在 `tests/conftest.py`）：

1. **`.env` 层**：会话级把 `dotenv.load_dotenv()` 换成空操作。原因：`config.py` 在
   模块级 `load_dotenv()`，任何一次模块重导入都会把本机 `.env` 的**真实 Key** 灌回
   `os.environ`，"删环境变量"式的隔离会被击穿 —— 本地全绿、CI 必红，而且本地那次
   全绿是真去调了付费接口。
2. **代理层**：把 `urllib.request.getproxies` 与 httpx 的 `get_environment_proxies`
   全部置空。原因：Windows 上 `getproxies()` 会**回退读注册表**里的系统代理，
   装了代理软件的机器上连"指向 127.0.0.1 的本地假模型服务"都会被绕出去拿到 502，
   而失败信息看起来完全像产品缺陷。
3. **网络层**：包装 `socket.connect`，只放行回环地址；需要真实外呼的用例必须显式
   加 `@pytest.mark.allow_network`（该标记由 `_network_marker_gate` 真正读取）。
   副作用目录（docs/index/generated/data/logs）全部隔离到临时目录。

### 覆盖率：现状与**已知缺口**（主动披露）

当前实测（`pytest --cov=. --cov-branch`）**总覆盖率约 80%**，门禁卡在 70%。但更需要
说清楚的是**哪些地方没有被覆盖**，而不是一个总数：

| 模块 | 覆盖率 | 缺口说明 |
|---|---|---|
| `server.py` | ~67% | 读接口与写接口主体已覆盖；**未覆盖**：会话导出（`/api/sessions/{id}/export`）、配置热更新 `/api/config` 的完整校验分支、`/api/mode` 的 ollama 可用性探测 |
| `graph.py` | ~87% | `_build_sources`/`_grounded`/`_estimate_cost` 等纯函数覆盖充分；**未覆盖**：部分流式异常分支与 recursion limit 边界 |
| `tools.py` | ~78% | `fetch_url` 白名单/重定向/超限分支、`write_file` 的部分 OSError 分支 |
| `mcp_server.py` | ~75% | stdio/HTTP 双模式的进程级启动未覆盖（需真起 MCP 客户端） |
| `retriever.py` | ~87% | jieba 缺失时的 bigram 降级、语义编码器异常路径 |
| `static/index.html` | 由 E2E 覆盖 | 1582 行自写渲染器；E2E 覆盖金路径 + XSS/畸形输入，**未覆盖**：代码块执行、设置面板、上传 UI |
| `desktop.py` | 0%（omitted） | pywebview 窗口需真实 GUI，故排除在统计外 |

> 之所以把缺口写出来：报一个 80% 却不说缺在哪，等于让对方自己去发现这些洞 ——
> 那时信任就崩了。**覆盖率回答"代码有没有被执行"，变异测试才回答"测试能不能抓住
> bug"**，因此本项目额外配了 `[tool.mutmut]`（对 `approvals.py` / `retriever.py`
> 做变异测试），作为对"覆盖率虚高"的正面回应。

### 已知限制与路线图

- [ ] **前端 E2E 首跑需人工确认一次**：用例与独立 CI job 已就位，但选择器依赖当前
  前端 DOM（`#question` / `#askBtn` / `.answer-body` / `.sources .source-card`）。
  首次在 CI 跑通后建议固定下来；改动前端结构时记得同步。
- [ ] **命令执行未做真沙箱**（C7）：目前是"黑名单启发式 + 高危确认 + 超时强杀 +
  目录边界"，不是隔离。计划：子进程 CPU/内存/进程数限制，高危命令走一次性 Docker
  容器，可选关闭子进程网络。
- [ ] **工具生态扩展**（C8）：表格读写（CSV/Excel）、Python 沙箱执行、正文提取式
  抓取、cross-encoder rerank（直接用 C1 的指标衡量收益）。
- [ ] **会话管理**（C5）：导出目前支持 markdown/json，待补重命名与游标分页。
- [ ] **OpenAPI + 属性测试**（B8）：给 `/api/*` 补 OpenAPI 定义，用 `schemathesis`
  自动生成畸形请求打接口。

## License

MIT
