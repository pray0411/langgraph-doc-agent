# Pray

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 的本地 ReAct Agent。做法是把一组工具交给模型，由模型自主决定何时调用什么，覆盖文档问答（RAG）、联网搜索、代码落盘与运行验证、会话记忆与跨会话长期记忆。

[![CI](https://github.com/pray0411/langgraph-doc-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/pray0411/langgraph-doc-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 适用范围

本项目在**已建索引的文档范围**内问答、按需联网获取实时信息、以及"写代码 → 落盘 → 运行 → 读报错 → 修改"的本地闭环这几条路径上经过实测。它不是"什么都能问"的通用助手：索引外的问题会答不上来，长链路多步规划与需要真沙箱隔离的执行型任务均超出当前设计范围。

仓库名 `langgraph-doc-agent` 来自最初定位（专用文档问答），后演进为通用 Agent，应用名为 **Pray**；保留旧名以免外部链接失效。

## 特性

- **Agent 内核**：LangChain `create_agent`（ReAct / tool-calling），13 个工具由模型自主编排
- **三层记忆**：checkpointer 会话记忆、SQLite `profile` 全局记忆、系统提示约束
- **本地混合检索**：jieba + BM25 与语义向量的 RRF 融合，无需外部向量数据库；语义模型缺失时自动降级为纯 BM25，并可查询当前实际模式
- **四种交付形态**：Web（SSE 流式）、CLI、MCP Server、桌面应用（pywebview）
- **默认拒绝的命令安全模型**：非只读命令一律需用户确认，确认走服务端签发 nonce 的两阶段流程
- **工程质量**：CI 覆盖 Windows / Linux × Python 3.10/3.12/3.13 六组合，287 条用例、检索质量门禁、覆盖率门禁、依赖漏洞扫描

## 架构

```
                     交付形态
        Web (SSE)   CLI   MCP Server   Desktop
             └───────┴──────┬──────┴───────┘
                            ▼
              ┌──────────────────────────┐
              │  Agent (create_agent)    │
              │  ReAct：思考 → 调工具 →   │
              │  观察 → 再思考 …          │
              └───────┬──────────┬───────┘
                      ▼          ▼
              ┌───────────┐  ┌──────────────┐
              │  工具层   │  │   记忆层     │
              │  13 tools │  │ 会话 + 全局  │
              └─────┬─────┘  └──────────────┘
                    ▼
      混合检索 / 联网 / 文件 / 命令 / 抓取
```

## 快速开始

```bash
# 1. 安装依赖（运行时可另加开发依赖：-r requirements-dev.txt）
pip install -r requirements.txt

# 2. 配置模型：复制 .env.example 为 .env，填 LLM_PROVIDER 与对应 API Key
#    LLM_PROVIDER=deepseek
#    DEEPSEEK_API_KEY=sk-xxx

# 3. 构建文档索引（文档问答用，可选）
python main.py build

# 4a. 网页版（推荐）
python -X utf8 main.py web          # http://127.0.0.1:8000

# 4b. 命令行
python -X utf8 main.py ask "这个项目的技术栈是什么？"
```

修改 Python 代码后需重启服务才会生效。Windows 控制台中文乱码时统一使用 `python -X utf8 main.py ...`。

## 工具

注册给模型的全部 13 个工具（`grep -c '^@tool' tools.py` 与 `graph.py` 的工具列表一致，`tests/test_security_model.py` 有对应断言）：

| 工具 | 说明 | 安全约束 |
|---|---|---|
| `search_documents` | 本地文档知识库检索（含用户上传文档） | 只读 |
| `web_search` | 联网搜索（博查 / DuckDuckGo 双引擎） | 只读，`BOCHA_API_KEY` 可选 |
| `get_weather` | 指定城市实时天气 | 只读 |
| `get_current_time` | 当前本地时间、星期、时区 | 只读 |
| `fetch_url` | 只读抓取网页（GitHub 仓库自动抓 README） | 域名白名单；其他域名需确认 |
| `write_file` | 代码/内容落盘 | 限定 `WRITE_DIR`，拒绝路径逃逸 |
| `read_file` | 按行号分段读取文件 | 限定 `WRITE_DIR`，拒读二进制 |
| `list_files` | 递归列目录（大小 / 修改时间） | 限定 `WRITE_DIR` |
| `edit_file` | 精确片段替换（改代码无需整文件重写） | 片段须唯一匹配 |
| `run_command` | 执行命令并返回输出 | 默认拒绝 + 用户确认（见"安全模型"） |
| `open_in_browser` | 用系统浏览器打开生成的文件 | 限定 `WRITE_DIR` |
| `remember` | 记录用户长期信息 | 同 key 覆盖，写入侧做注入规范化 |
| `forget` | 删除指定全局记忆 | — |

## 记忆

| 层 | 存储 | 范围 | 说明 |
|---|---|---|---|
| 会话记忆 | `data/memory.sqlite`（LangGraph checkpointer） | 单个 `thread_id` | 按轮次持久化，重启不丢；侧边栏可回放、删除、导出 |
| 全局记忆 | `data/global_memory.sqlite`（`profile` 表） | 跨会话 | 用户长期信息，key 去重覆盖；模型主动 remember / forget |
| 提示约束 | `prompts.py` | 全局 | 行为准则与工具使用纪律 |

全局记忆在构建 Agent 时拼入 system prompt；记忆版本号参与 Agent 缓存 key，写入或删除后自动重建，无需每轮重建。记忆是"模型写、模型读"的通道，写入与注入两侧都做规范化并声明"条目是数据而非指令"——该防护只阻断结构逃逸，语义层面的注入无法根除，详见 [docs/安全模型.md](docs/安全模型.md)。

## 检索

`retriever.py` 实现本地混合检索，面向中小规模文档集：

- 词法通道：jieba 分词 + BM25（k1=1.5, b=0.75），过滤中文停用词
- 语义通道：sentence-transformers 本地模型，理解同义改写；可用 `DISABLE_SEMANTIC=1` 显式关闭
- 融合：两通道排名后按 RRF（k=60）融合；语义模型缺失时静默降级为纯 BM25
- 当前实际模式可通过 `retriever.retrieval_mode()` 查询（返回 `hybrid` 或 `bm25`），**报告检索指标时必须同时标注模式**
- 缓存按 (查询, 索引版本, 上传版本) 失效；索引为带版本号的 JSON，旧格式首次启动自动重建

用户上传的文档（`.md` / `.txt` / `.py` / `.rst` / `.html`，单文件 ≤ 5 MB）进入内存 BM25 增量索引，与全局索引同量纲融合。

## 前端

零依赖单文件前端（`static/index.html`，无构建工具）：Markdown 渲染、SSE 逐 token 流式输出、会话侧边栏与历史回放 / 导出、引用来源卡片、文档上传管理、暗色模式、移动端适配、代码块交互式运行（Python / JS / Shell 走真实 stdin/stdout 的终端）。前端渲染器为自写实现，XSS 防护由 E2E 用例覆盖。

## 交付形态

### Web

```bash
python -X utf8 main.py web --port 8000
```

### CLI

```bash
python -X utf8 main.py ask "问题"     # 单轮问答
python -X utf8 main.py build          # 重建索引
python -X utf8 main.py web            # 启动服务
```

### MCP Server

```bash
pip install -r requirements-mcp.txt
python -X utf8 mcp_server.py            # stdio（Claude Desktop 等）
python -X utf8 mcp_server.py --http     # Streamable HTTP（调试）
python -X utf8 mcp_demo.py              # 内置演示客户端，无需第三方账号
```

暴露 7 个工具：整包 `ask(question, new_thread)`，以及 `search_documents` / `web_search` / `get_current_time` / `list_memory` / `remember` / `forget`。**不暴露执行类工具**——MCP 客户端没有本机的确认闸，服务面收敛为只读检索、问答与记忆。

接入 Claude Desktop：把 `claude_desktop_config.example.json` 中的路径改为本机 Python 与 `mcp_server.py` 的绝对路径，保存为 `%APPDATA%\Claude\claude_desktop_config.json`（macOS 为 `~/Library/Application Support/Claude/claude_desktop_config.json`）。

### 桌面应用

分为**直接使用**与**从源码打包**两条路径。

**直接使用（推荐给最终用户）**：从 [Releases](https://github.com/pray0411/langgraph-doc-agent/releases) 下载 `Pray-<版本>-win64.zip`，解压后双击 `Pray.exe`。目标电脑**无需安装 Python**。首次使用需在同目录放置 `.env` 填写 API Key（同目录的 `.env.example` 可直接改名使用）；生成的代码、会话记忆、索引与日志都保存在 exe 同级目录，整个过程是绿色便携的。

自检与排查：

```bash
Pray.exe --check                  # 输出页面状态、版本、provider、API Key 是否已配置、数据目录
Pray.exe --no-update-check        # 跳过启动时的版本检查
```

**从源码运行 / 打包**：

```bash
pip install -r requirements-desktop.txt
python -X utf8 desktop.py             # 独立窗口（端口自动选空闲）
python -X utf8 desktop.py --check     # 无窗口自检

build_exe.bat                         # 一键打包（Windows；等价于下面两条命令）
pip install pyinstaller
pyinstaller Pray.spec --noconfirm --clean
```

后台线程运行与网页版相同的服务，pywebview 使用系统 WebView（Windows 为 Edge WebView2），不引入 Chromium/Electron 体积。启动时异步检查 GitHub Releases，有新版本弹窗提示（不自动替换自身）。`Pray.spec` 默认排除 torch / sentence-transformers，产物约 70 MB（压缩包）/ 解压后约 200 MB，检索自动回退纯 BM25。

打包后的路径语义：**只读资源**（`VERSION`、`static/`）随包分发、从解包目录读取；**可写数据**（`generated/`、`data/`、`index/`、`logs/`）与 `.env` 一律锚定到 exe 所在目录（`config.BASE_DIR` 在 frozen 模式下指向 exe 同级目录），否则数据会落在 PyInstaller 的临时目录里、进程退出即丢失。

发布新版本：把 `VERSION` 与 `CHANGELOG.md` 更新后打 tag，在 GitHub Release 上传该 zip（exe 体积大，不进入仓库，`dist/` 已在 `.gitignore` 中）。

> 仅想从源码 zip 快速跑起来（不打包 exe）：解压后双击 `启动.bat`，脚本会依次检查 Python、首次自动安装依赖、创建 `.env` 模板，然后启动桌面窗口（缺 pywebview 时回退网页版）。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web 界面 |
| GET | `/health` | 健康检查 |
| GET | `/api/mode` | 当前模式与可用模式 |
| GET | `/api/sessions?q=&limit=&offset=` | 会话列表（支持搜索与分页） |
| GET | `/api/sessions/{id}/messages` | 会话历史消息 |
| GET | `/api/sessions/{id}/export?format=md\|json` | 导出会话 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| GET | `/api/memory` | 全局记忆列表 |
| DELETE | `/api/memory?key={键}` | 删除一条全局记忆 |
| GET | `/api/uploads` | 已上传文档列表 |
| POST | `/api/upload` | 上传文档 |
| POST | `/api/uploads/rename` \| `/api/uploads/reindex` | 重命名 / 重建检索索引 |
| POST | `/ask` | 非流式问答 |
| POST | `/ask/stream` | SSE 流式问答 |
| POST | `/api/mode` \| `/api/config` \| `/api/config/test` | 切换模式 / 更新服务商配置 / 连通性自检 |
| POST | `/api/run/prepare` | 命令分级并签发 nonce（高危命令第一段） |
| POST | `/api/confirm` | 用户确认换一次性批准（第二段） |
| POST | `/api/run/start` \| `/input` \| `/stop` \| `/write` | 交互终端：启动 / 输入 / 停止 / 写临时文件 |
| GET | `/api/run/output?session_id=` | 轮询终端输出 |
| GET | `/api/open?file=` | 用系统程序打开文件 |

## 配置

`.env` 支持的环境变量（默认值见 `config.py`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `deepseek` | `deepseek` / `openai` / `qwen` / `zhipu` / `moonshot` / `ollama` |
| `DEEPSEEK_API_KEY`、`OPENAI_API_KEY` | 空 | 服务商密钥（亦可在网页端设置面板运行时填写） |
| `LLM_MODEL`、`LLM_BASE_URL` | `deepseek-chat`、空 | 模型名与自定义 OpenAI 兼容网关 |
| `OLLAMA_BASE_URL`、`OLLAMA_MODEL` | `http://localhost:11434/v1`、`qwen2.5:7b` | 本地模型 |
| `BOCHA_API_KEY` | 空 | 博查搜索密钥；缺省时退回 DuckDuckGo |
| `API_TOKEN` | 空 | 配置后写操作接口需 `X-API-Token`；绑定非回环地址时强制生成 |
| `ALLOWED_ORIGINS` | 空 | 额外允许的来源（自定义域名 / 反向代理场景） |
| `WRITE_DIR`、`UPLOADS_DIR` | `generated/`、`generated/uploads` | 文件写入与上传目录 |
| `MEMORY_DB`、`GLOBAL_MEMORY_DB` | `data/*.sqlite` | 会话记忆、全局记忆 |
| `DOCS_DIR`、`INDEX_DIR` | `docs/`、`index/` | 文档库与索引目录 |
| `CHUNK_SIZE`、`CHUNK_OVERLAP` | `500`、`100` | 切分参数（导入期校验 overlap < size） |
| `TOP_K`、`MIN_SCORE` | `3`、`0.0` | 检索条数与最低融合分 |
| `EMBEDDING_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | 语义检索模型 |
| `DISABLE_SEMANTIC` | 空 | 置 1 时关闭语义通道（评估与 CI 用） |

## 安全模型

服务默认只监听 `127.0.0.1`，定位是单机个人工具。以下为三道防护与已知残留面。

### 命令执行：默认拒绝的分级判定

| 层级 | 范围 | 行为 |
|---|---|---|
| 第 1 层 | 破坏性操作（`rm -rf /`、`format`、`shutdown`、`mkfs` 等） | 直接拒绝，无确认入口 |
| 第 2 层 | 其余命令，含 `python xxx.py`、`python -c`、`powershell`、`node`、`bash` 等任意可执行写法 | 需用户确认 |
| 第 3 层 | 只读白名单（`dir`/`ls`/`type`/`cat`/`findstr`/`where`/`pwd`/`echo` 等） | 免确认，但须同时不含 shell 元字符、不含危险选项、参数不越出可读边界 |

设计取向是**默认拒绝**：新增解释器或未预料的执行方式默认落到"需要确认"，失误方向保守。相比关键词黑名单，它堵住了"`write_file` 写脚本 → `run_command` 跑脚本"这条零确认的任意代码执行路径。

确认流程为两阶段：工具层通过 `approvals.mint()` 生成 nonce → 结构化 SSE 事件 `need_confirm` 推送 `{nonce, command, reason}` → 前端展示完整命令 → 用户确认后回传 `/api/confirm` 换一次性批准（与命令哈希绑定，用后即删，5 分钟 TTL）。这样授权不是"前端调接口自助登记"，且保证展示的命令与被批准的命令一致。

### 鉴权与来源校验

`_origin_ok()` 按顺序判定带副作用的请求（`/api/open` 这类有副作用的 GET 亦包含在内）：

1. 携带正确 `X-API-Token` → 放行（浏览器不会自动附带该头，CSRF 模型不适用）
2. `Host` 不属于服务端认定的本地身份（回环地址、本机名、本机网卡 IP、`ALLOWED_ORIGINS`）→ 拒绝
3. 有 `Origin` → 其 hostname 须在同一本地身份集合内
4. 无 `Origin` → 检查 `Sec-Fetch-Site`，显式 `cross-site` 则拒绝

判据是"与服务端自己算出的本地身份集合比对"，而非常见的 `Origin == Host`——后者两侧都由请求方提供，可被同时伪造。未配置 `API_TOKEN` 且绑定非回环地址时，服务会自动生成随机 Token 并打印到控制台，避免以"无鉴权 + 对外可达"启动。

### 边界声明

上述三层均为**应用层判定，不是沙箱**。被批准执行的代码在操作系统看来就是普通子进程，可读写该用户有权限访问的一切。真正的人为护栏是确认流程、目录边界与超时强杀，而非分类器本身。**不要将本服务暴露给不受信方**；如需多人使用，应运行在容器或虚拟机中、以非 root 用户启动，并配合鉴权与网络访问控制。

已知残留面：缺少 `Origin` 与 `Sec-Fetch-Site` 的请求会被放行（为兼容 `curl` 等非浏览器客户端）；`/ask` 超时返回 504 后模型调用仍在后台继续，无法取消；子进程的 CPU / 内存限额与一次性容器尚未实现。完整论证、残留面清单与设计取舍见 [docs/安全模型.md](docs/安全模型.md)。

## 测试与质量

```bash
pip install -r requirements.txt -r requirements-dev.txt

python -X utf8 -m pytest tests/ --cov=. --cov-report=term-missing   # 全量 + 覆盖率门禁
python -X utf8 -m pytest tests/ -m "not slow"                        # 跳过性能基线
python -X utf8 -m pytest tests/ -m eval -q -s                        # 检索质量（recall@k / MRR）
python -X utf8 -m pytest tests/e2e -m e2e -q                         # 前端 E2E（需 Playwright）
python -X utf8 -m pytest tests/ -m slow -q -s                        # 性能基线 P50/P95
pip-audit -r requirements.txt --desc                                  # 依赖漏洞扫描
```

| 层 | 位置 | 说明 |
|---|---|---|
| 元测试 | `tests/test_metatest.py` | 测试套件自检：同名用例、环境密封性、版本号单一来源、CI 配置有效性 |
| 安全模型 | `tests/test_security_model.py` | 把安全承诺写成断言：命令分级表、写→跑必须被拦、DNS rebinding、SSRF 收敛、工具面一致性 |
| 单元 | `tests/test_core.py` | 检索、记忆、反思、成本估算、命令分级等纯函数与模块行为 |
| 契约 | `tests/test_server_writes.py` | 真实 HTTP 打写接口：上传 / 删除 / 重命名 / 重建索引 / prepare→confirm / 命令执行 |
| 集成 | `tests/test_agent_loop.py` | 本地假 OpenAI 兼容服务驱动真实 ReAct 循环（不联网、确定性） |
| 多 provider | `tests/test_provider_contract.py` | 6 个 provider 的配置解析与请求构造契约 |
| 质量评估 | `tests/test_retrieval_eval.py` | recall@3 / MRR 带阈值门禁，固定纯 BM25 并标注模式 |
| 前端 E2E | `tests/e2e/` | Playwright：主路径 + XSS 注入 + 畸形 Markdown（独立 CI job） |
| 性能基线 | `tests/test_perf.py` | 检索 P95、SSE 首字、并发检索 |
| CLI / 日志 | `tests/test_cli.py`、`tests/test_logging_audit.py` | 命令行入口；结构化日志与凭据脱敏 |

测试套件通过三层隔离做到不依赖开发者本机环境：屏蔽 `.env` 加载、清空系统代理、包装 `socket.connect` 只放行回环地址（真实外呼须显式标记）。当前 287 条用例、总覆盖率 81%，门禁 70%；**各模块的覆盖率缺口在 [docs/工程质量.md](docs/工程质量.md) 中逐项披露**（含未覆盖的具体分支），另有 `[tool.mutmut]` 对 `approvals.py` / `retriever.py` 做变异测试，用于检验"测试能否抓住 bug"。

CI 覆盖 Windows / Linux × Python 3.10/3.12/3.13 六组合，不提供任何 API Key 与 `.env`。首次上 CI 时八个 job 中有七个失败，暴露的问题（可选依赖未装导致 `mcp_server` 导入失败、`requirements-mcp.txt` 依赖冲突、lint 从未运行、E2E 抓到前端 XSS）记录在 [docs/工程质量.md](docs/工程质量.md)。

## 目录结构

```
.
├── graph.py           # Agent 构建、ask / ask_stream、反思与来源、记忆注入
├── memory.py          # 全局记忆（SQLite profile 表）
├── tools.py           # 13 个工具 + classify_command 默认拒绝分级
├── retriever.py       # 混合检索与上传文档增量索引、retrieval_mode()
├── prompts.py         # 系统提示词
├── server.py          # Web 服务：路由、鉴权与来源校验、SSE、两阶段授权
├── approvals.py       # 两阶段审批（nonce + 一次性消费 + TTL）
├── runterm.py         # 交互终端会话（会话数 / 有界队列 / 缓冲上限）
├── logging_setup.py   # 结构化日志与安全审计（凭据脱敏）
├── mcp_server.py      # MCP Server
├── mcp_demo.py        # MCP 演示客户端
├── desktop.py         # 桌面端（pywebview）与更新检查
├── Pray.spec          # PyInstaller 打包配置
├── main.py            # 命令行入口
├── config.py          # 配置与环境变量解析
├── static/            # 前端（单文件 index.html）
├── docs/              # 文档知识库与设计说明
├── assets/            # 应用图标
├── tests/             # 测试（单元 / 契约 / 集成 / 评估 / E2E / 性能）
├── legacy/            # V1 历史归档，不参与运行
├── VERSION            # 版本号单一来源
├── pyproject.toml     # pytest / coverage / ruff / mutmut 配置
├── Dockerfile         # 隔离运行环境（非 root + 健康检查）
├── .github/workflows/ # CI（六组合矩阵 + lint + 漏洞扫描）、Release
├── CHANGELOG.md
└── index/  data/  models/  generated/    # 运行时生成
```

## 已知限制与路线图

- **命令执行未做真沙箱**：默认拒绝分级 + 用户确认 + 超时强杀 + 目录边界仍属应用层判定；计划补子进程 CPU/内存限额与一次性容器
- **`/ask` 超时无法取消后台调用**：超时返回 504 后线程继续运行并消耗额度，需 asyncio 或子进程隔离
- **全局状态竞争**：`set_mode` / `set_runtime_provider_config` 直接修改模块级状态，计划改为请求级配置快照
- **PDF / DOCX 解析**：目前仅支持文本格式文档
- **混合检索指标未进 CI**：质量门禁固定在纯 BM25（跨机器可比），混合通道指标缺少回归门禁
- **`write_file` 直接覆盖**：无版本快照与覆盖前提示
- **前端**：建议接入 DOMPurify 二次净化，单文件体积偏大待拆分
- **工具扩展**：表格读写（CSV/Excel）、cross-encoder rerank（落地时须给出纯 BM25 / 混合 / 加重排三组指标）
- **真模型夜间冒烟**：现有集成测试均使用本地假服务，缺少定期用真模型跑主链路的 job

## License

MIT
