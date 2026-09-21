# 变更日志

本文件记录项目的**关键变更**，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

### Fixed（缺陷修复）

- **测试套件密封性（P0）**：`tests/conftest.py` 新增 `_hermetic_dotenv`，测试期间把
  `dotenv.load_dotenv` 变为空操作。此前 `sys.modules.pop("config")` 触发的模块重导入会
  重新执行 `load_dotenv()`，把开发者本机 `.env` 里的真实 Key 灌回环境，导致隔离 fixture
  被击穿、用例真的去调用付费接口 —— 表现为"本机全绿、任何第三方 clone 后必红"。
- **测试套件代理污染（P0）**：新增 `_hermetic_proxy`。`urllib.request.getproxies()` 在
  Windows 上会**回退读取注册表**里的系统代理，httpx 的 `get_environment_proxies()` 又建立在
  它之上 —— 因此"清空 `os.environ` 里的代理变量"完全无效：即使环境变量为空，请求仍会被
  发给系统代理。本机装有 7890 这类代理工具时，连指向 `127.0.0.1` 的本地假模型服务都会被
  绕出去并拿到 `502`。现在从**代理解析层**堵死（同时覆盖 `urllib` / `httpx`）。
- **多轮会话来源跨轮污染（P0）**：`graph._current_turn_messages()` 按"最后一条 human 消息"
  切出本轮消息。此前 `result["messages"]` 是整个 thread 的累积历史，第二轮未调用任何工具
  也会显示上一轮的文档来源与 `工具调用数=1`。
- **上传文档重启即失效（P0）**：`retriever.ensure_uploaded_index()` 从磁盘 `uploads/` 目录
  重建内存索引。此前上传索引是纯内存的，且只有 HTTP 上传接口会写它，重启后文件仍在盘上、
  列表也还列得出来，但 `chunks: 0` 且永远检索不到。
- **`LLM_BASE_URL` 是死代码**：`graph._resolve_base_url()` 修正优先级。此前
  `pcfg.get("base_url") or LLM_BASE_URL or ...` 中，6 个预设 provider 的 `base_url`
  永远非空，`or LLM_BASE_URL` 永远轮不到 —— 文档承诺的 one-api / vLLM / 自建网关
  实际不可用，且无任何报错。
- **迭代上限不可读**：`ask()` / `ask_stream()` 捕获 `GraphRecursionError` 并转为可读提示
  （流式路径推送 `error` 事件），不再把异常栈丢给用户。
- **命令执行输出混流**：`tools.run_command` 拆分 stdout / stderr，出错时能分辨来源。
- **高危命令黑名单缺陷**：`rd /s` 规则补词边界（原 `rd\s+/s` 缺 `\b`，会误伤 `forward /s`），
  并删除重复的 `\brmdir\b` 规则。
- **检索缓存抖动**：`_search_cache` 由"满即全清"改为 **LRU 逐条淘汰**，避免高并发下
  周期性把热门查询一次性丢光、下一轮全部重算。
- **会话列表全量扫描**：`list_sessions()` 利用 `SqliteSaver.list` 的
  `ORDER BY checkpoint_id DESC` 契约提前收敛（收集到 `limit` 个 thread 即停），
  复杂度从 O(全部历史) 降到 O(limit 附近)；`get_session_messages()` 改用
  `saver.get_tuple(config)` 替代全量 `list(None)` 线性查找。
- **脆弱写法**：token 用量回调不再挂私有属性（`agent.__usage_handler`）+ `threading.local`，
  改为每次请求新建 handler 并经 LangGraph 原生 `config={"callbacks": [...]}` 注入，
  `threading.local` 整套删除。
- **测试用例问题**：删除 1 条被同名函数静默覆盖的用例；重写 2 条"名不副实"的用例
  （`test_ask_timeout_*` 与 `test_run_command_high_risk_*` 此前未真正触达被测代码）。

### Added（新增）

- **`logging_setup.py`**：统一日志配置 + **安全审计日志**（JSON 行）。命令执行、审批、
  超时、迭代上限等敏感动作各打结构化一条（命令哈希、是否命中高危、批准与否），
  访问日志恢复输出并对 `X-API-Token` / 请求体 `api_key` 脱敏。
- **`tests/conftest.py`**：可复用的**本地假 OpenAI 兼容服务**（`fake_llm` fixture），
  响应可脚本化编排 —— 在**不联网、不花钱、结果确定**的前提下驱动真实的 ReAct 工具循环。
- **`tests/test_agent_loop.py`**：核心链路测试。此前 3 处 `create_agent(...)` 全部传
  `tools=[]`，"模型调工具 → 观察 → 再思考 → 出答案"从未被测试过。现覆盖：单工具循环、
  并行多工具、工具报错被观察而非致命、纯对话不调工具、多步链式、迭代上限止损、
  两轮会话不污染（A2 回归）、`ask_stream` 完整事件序列。
- **`tests/test_metatest.py`**：测试套件自检。AST 级守护：同名用例、定义未被收集、
  `.env` 未被中和、真实 Key 泄漏、base_url 指向非本机、网络守卫是否有效、
  自证式用例、测试目录落在仓库内、`.gitignore` 覆盖运行期产物、代理未被中和。
- **`tests/eval/`**：检索质量评估集（recall@k / MRR / 命中率）与阈值门禁。
- **`tests/test_retrieval_eval.py`**、**`tests/test_provider_contract.py`**（多 provider 契约）、
  **`tests/test_server_writes.py`**（`server.py` 写接口）。
- **审计与可观测接口**：`/api/audit`（审计日志尾部）、`/dashboard`（可观测面板）、
  `/api/sessions/<id>/export`（会话导出）、`/api/uploads/rename`、`/api/uploads/reindex`、
  `/api/config/test`（网关连通性自检）、`GET /api/sessions` 支持 `q` 搜索与分页。
- **工程基建**：`LICENSE`（MIT）、`CHANGELOG.md`、`Dockerfile` + `.dockerignore`、
  `pyproject.toml`（pytest / coverage / ruff 统一配置）、`requirements-dev.txt`、
  GitHub Actions CI（Windows + Linux × Python 3.10/3.12/3.13 矩阵）、`dependabot.yml`。
- **`tests/test_perf.py`（性能与并发基线）**：检索 P50/P95 延迟、检索缓存命中不变量、
  并发检索一致性与耗时、SSE 首字延迟。门禁双轨 —— 无基线时用宽松的绝对上限，
  在已知机器上生成 `perf_baseline.json` 后自动切换为"不超过基线 1.5×"。
- **`tests/e2e/`（前端 E2E，Playwright）**：金路径（提问 → SSE 流式渲染 → 来源卡片）、
  新对话清屏、**XSS 注入**（`<img onerror>` / `<script>` / `<svg onload>` / `javascript:` 伪协议）、
  畸形 Markdown 不崩溃。未安装 Playwright 时整目录 skip，CI 有独立 job 安装 Chromium。
- **测试报告与可追溯（C10）**：CI 产出 `junit/*.xml` + `coverage.xml` + `report/report.html`
  并作为 artifact 上传；用例分层标记（`unit` / `integration` / `eval` / `e2e` / `slow` /
  `allow_network`）配合 `--strict-markers`（拼错的标记直接报错，不会静默不生效）。
- **发布自动化（C12）**：`.github/workflows/release.yml` —— 打 tag 自动用 `Pray.spec`
  构建 Windows exe 并创建 Release（草稿态）；发布前**校验 tag 与 `VERSION` 文件一致**，
  避免"包里外版本不一致"。
- **变异测试配置（B8）**：`[tool.mutmut]`（针对 `approvals.py` / `retriever.py`），
  CI 提供 `workflow_dispatch` 触发的非阻断 signal job。
- **审计与可观测接口**：`/api/audit`（审计日志尾部）、`/dashboard`（可观测面板）、
  `/api/sessions/<id>/export`（会话导出）、`/api/uploads/rename`、`/api/uploads/reindex`、
  `/api/config/test`（网关连通性自检）、`GET /api/sessions` 支持 `q` 搜索与分页。

### Changed（调整）

- `requirements.txt` 移除 `pytest`（归入 `requirements-dev.txt`）；
  `requirements-mcp.txt` 补上缺失的 `mcp`（`mcp_demo.py` 依赖但未声明）；
  `duckduckgo-search` 改名后的兼容导入（优先 `ddgs`，回退旧名）。
- `.gitignore` 移除"个人面试准备材料"等本地字样，补齐 `logs/`、覆盖率产物、`.tmp`、
  `report/`（pytest-html 报告目录）。
- **版本号收敛为单一来源**：新增 `VERSION` 文件 → `config.APP_VERSION`；
  `desktop.py` 不再硬编码（原为 `"1.1.0"`），`pyproject.toml` 也改为动态引用
  （原另写 `0.9.0`）。此前三处各写一个，随时可能不一致。元测试守护这条不变量。
- **README 重写"测试"章节**：补上**可复现命令块**、分层表格、三层密封性说明，
  并**主动披露覆盖率缺口**（写清 `server.py` / `graph.py` / `tools.py` / 前端分别缺在哪），
  而不是只报一个总数。已知限制与路线图（真沙箱 C7、工具生态 C8、会话管理 C5、
  OpenAPI 属性测试 B8）同样写在 README 里。
- 测试用例全部改为"干净环境可复现"：无 `.env`、无真实网络、无代理、无付费调用。

### Fixed（补记）

- **`setup_logging(log_dir=...)` 被静默忽略**：新增 `tests/test_logging_audit.py` 时
  发现的**真实缺陷** —— 原实现用 `_configured` 布尔位做"只配置一次"，导致日志目录一旦
  被隐式初始化，之后显式传入的 `log_dir` 完全不生效。现改为幂等重建
  （`_configure` / `_resolve_level` / `_current_output_dir` / `_ensure_configured` /
  `reset_state`），并补回归用例。这正是"先写测试、再发现产品缺陷"的一次实例。

---

## [0.9.0] - 初始公开版本

- LangGraph ReAct 通用 Agent（`create_agent`），工具自主调用。
- 混合检索 RAG：jieba + BM25 + sentence-transformers 语义向量 RRF 融合，语义不可用自动回退 BM25。
- Web 界面（SSE 流式）、交互终端、命令执行审批、会话持久化（SQLite checkpointer）、
  全局长期记忆、MCP Server / Desktop 打包。
