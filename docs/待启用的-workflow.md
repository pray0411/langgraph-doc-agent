# 待启用的两个 workflow

本文件是**临时交接说明**，可以删除。

`deps-installable` CI job 与真模型冒烟 workflow 已经写好、通过 YAML 校验，但推送时被 GitHub 拒绝：

```
refusing to allow a Personal Access Token to create or update workflow
`.github/workflows/ci.yml` without `workflow` scope
```

当前使用的 Personal Access Token 缺少 **`workflow`** scope（GitHub 对 `.github/workflows/` 下文件的保护策略）。
代码本身不需要改动，只差把这两个文件放上去——**它们已经在本仓库的工作区里了**：

| 文件 | 状态 | 位置 |
|---|---|---|
| `.github/workflows/ci.yml` | 已修改（新增 `deps-installable` job） | 本机工作区已就绪，未提交 |
| `.github/workflows/smoke.yml` | 新建（真模型冒烟） | 本机工作区已就绪，未提交 |

---

## 方式 A：给 Token 加 `workflow` scope（推荐，一次到位）

1. 打开 <https://github.com/settings/tokens>
2. 找到正在使用的 token → **Edit**
3. 勾选 **`workflow`** → **Update token**
4. 本地执行（改动已在工作区）：

```powershell
cd D:\dsh工作区\langgraph-doc-agent
git add .github/workflows
git commit -m "ci: 依赖可安装性门禁 + 真模型夜间冒烟 workflow"
git push origin main
```

## 方式 B：在 GitHub 网页上手动创建（不需要改 Token）

用浏览器登录态操作，不受 PAT scope 限制。两个文件都在本机工作区，**直接打开复制内容**即可：

1. **编辑** <https://github.com/pray0411/langgraph-doc-agent/edit/main/.github/workflows/ci.yml>
   打开本机 `D:\dsh工作区\langgraph-doc-agent\.github\workflows\ci.yml`，
   把文件**末尾**从 `# ---------- 依赖可安装性 ----------` 开始到文件结束的内容，追加到网页编辑器末尾 → Commit。

2. **新建** <https://github.com/pray0411/langgraph-doc-agent/new/main/.github/workflows>
   文件名填 `smoke.yml`，把本机 `D:\dsh工作区\langgraph-doc-agent\.github\workflows\smoke.yml`
   的**全部内容**粘贴进去 → Commit。

3. （可选）让冒烟真正跑起来：`Settings → Secrets and variables → Actions → New repository secret`，
   名字 `DEEPSEEK_API_KEY`，值为你的密钥。**不填也行**——workflow 会整体跳过并保持绿色。

## 本地等价能力（不依赖 workflow，现在就能用）

```powershell
cd D:\dsh工作区\langgraph-doc-agent

# 1) 依赖可安装性：四个 requirements 是否自洽（CI 的 deps-installable job 就是这个）
foreach ($f in "requirements.txt","requirements-dev.txt","requirements-mcp.txt","requirements-desktop.txt") {
    Write-Host "== $f =="; python -m pip install --dry-run -r $f
}

# 2) 真模型冒烟：三个主链路（纯问答 / 工具调用 / 检索）
$env:PRAY_SMOKE="1"; $env:DEEPSEEK_API_KEY="sk-xxx"
python -X utf8 -m pytest tests/smoke -m smoke -q -s
```

> 冒烟用例需要 `PRAY_SMOKE=1` 这道显式开关：`config.py` 在导入期执行 `load_dotenv()`，
> 本机 `.env` 里的真实 Key 会进入环境变量，只靠"检测到 Key 就跑"会让人在本地随手跑全量测试时产生真实费用。

---

## 这两个文件做了什么

**`ci.yml` 的 `deps-installable` job**：要求四个 requirements 文件都能被 pip 解析。
来自一次真实事故——`requirements-mcp.txt` 曾因 `mcp==1.22.0` 与 `fastmcp==4.0.3` 依赖链冲突而
**根本装不上**，而本机因为早已装好相关包毫无察觉，「照文档装依赖」从来没人验证过。

**`smoke.yml`**：三条主链路（纯问答 / 工具调用 / 检索）跑真实模型，每日定时 + 手动触发。
主 CI 的全部用例都跑在本地假 OpenAI 服务上（确定性、零成本），但它证明不了"真实模型能按预期使用这些工具"。
未配置 secret 时整体跳过，不阻断主流程。
