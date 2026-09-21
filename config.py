"""全局配置：从环境变量读取，未配置时使用默认值。"""
import os
import re
import sys
import threading as _threading
from pathlib import Path

# ---------- 运行形态：源码运行 vs 打包（PyInstaller）运行 ----------
#
# 为什么必须区分：打包后 `__file__` 指向**临时解包目录**（`sys._MEIPASS`），
# 而用户双击 exe 时的合理预期是"数据就在 exe 旁边"：
#   - 可写数据（生成的文件 / 索引 / 会话与全局记忆 / 日志）若落在 _MEIPASS，
#     进程退出后随临时目录一起消失 —— 表现为"生成的文件找不到""记忆不保留"
#   - 只读资源（VERSION、static/ 前端）随包分发，仍从解包目录读
_FROZEN = bool(getattr(sys, "frozen", False))
_RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BASE_DIR = Path(sys.executable).resolve().parent if _FROZEN else Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    if _FROZEN:
        # 打包运行时优先读 exe 同目录的 .env（用户把 API Key 写在 exe 旁边）；
        # load_dotenv 默认不覆盖已存在的环境变量，显式路径优先符合直觉。
        load_dotenv(BASE_DIR / ".env")
    load_dotenv()
except ImportError:  # dotenv 未安装时静默跳过
    pass

# 版本号**单一来源**：仓库根目录的 VERSION 文件。
# 为什么要有这个文件：此前 `desktop.py:APP_VERSION = "1.1.0"` 与 Release tag
# 是两处独立维护的值，`pyproject.toml` 里还有第三个（0.9.0）—— 三者随时可能
# 不一致，而"当前是什么版本"这种问题不该有歧义。现在统一为：VERSION 文件 →
# config.APP_VERSION → desktop 读取；pyproject.toml 也动态引用它。
# 打包运行时 VERSION 随包放在解包目录（见 Pray.spec 的 datas）。
def _read_version() -> str:
    for base in (_RESOURCE_DIR, BASE_DIR):
        try:
            text = (base / "VERSION").read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:  # 打包/裁剪环境里文件缺失时不应让 import 失败
            continue
    return "0.0.0"


APP_VERSION = _read_version()

# 文档与索引目录
DOCS_DIR = Path(os.getenv("DOCS_DIR", BASE_DIR / "docs"))
INDEX_DIR = Path(os.getenv("INDEX_DIR", BASE_DIR / "index"))

# Agent 可写文件的根目录（write_file 工具的安全边界）：
# 只允许在此目录内创建/修改文件，路径逃逸（../）会被拒绝
WRITE_DIR = Path(os.getenv("WRITE_DIR", str(BASE_DIR / "generated")))

# 上传文档目录：放在 WRITE_DIR 之内（同样受路径边界保护）。
# 独立成配置项的原因：上传文档既要在 http 层落盘，又要在检索层重建索引，
# 两边必须指向同一个目录，否则"文件在、检索不到"。
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", str(WRITE_DIR / "uploads")))


# ---------- 平台无关的路径安全检查 ----------
#
# 为什么不能只靠 `Path.is_absolute()` + `is_relative_to()`：
# 这两个判据都是**平台相关**的，于是同一份代码在两个平台上的安全边界不一致。
# CI 的 ubuntu 矩阵把它抓了出来（Windows 三个组合全绿、ubuntu 三个组合各挂 2 条）：
#
#   - `C:/Windows/system32/x.txt`：Linux 上不是绝对路径 → 被拼成
#     `<WRITE_DIR>/C:/Windows/system32/x.txt`，`is_relative_to` 判定"在目录内" → 放行，
#     在 WRITE_DIR 下留下一个怪名字文件；Windows 上盘符路径会替换 base → 正确拒绝。
#   - `..\escape.py`：Linux 上反斜杠只是普通文件名字符，不构成".."段 → 放行；
#     Windows 上正常识别为穿越 → 拒绝。
#
# 结论：判据必须只看**字符串形态**，不依赖运行平台 —— 任何平台的绝对路径写法、
# 任何分隔符下的 `..` / `~` 段，一律拒绝。代价是 Linux 上个别"文件名里带反斜杠"
# 的合法路径会被误伤（极少见）；相比之下"边界随平台漂移"是更糟的问题。
_UNC_PATH_RE = re.compile(r"^(\\\\|//)")
_ABS_POSIX_RE = re.compile(r"^/")
_ABS_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")  # C:\ / C:/ / C:foo
_PATH_SEP_RE = re.compile(r"[\\/]+")


def unsafe_path_reason(path: str, *, allow_empty: bool = False) -> str | None:
    """检查路径是否越出 WRITE_DIR 语义边界。返回拒绝原因；None 表示通过。

    `allow_empty`：空串表示"根目录"的调用方（如 `list_files` 列根目录）传 True。
    "路径是不是空的"属于**调用方该回答的问题**（写文件不允许空、列目录允许空），
    不是"是否越界"的问题 —— 两者混在一起会让 `list_files("")` 被误拒。

    调用方仍应叠加 `is_relative_to` 做最终确认（符号链接等仍需 realpath 判定），
    本函数负责堵住"平台差异导致的判据失效"。
    """
    if path is None:
        return None if allow_empty else "路径为空"
    raw = str(path).strip()
    if not raw:
        return None if allow_empty else "路径为空"
    if _UNC_PATH_RE.match(raw):
        return f"不接受 UNC/网络路径：{path}"
    if _ABS_POSIX_RE.match(raw):
        return f"不接受绝对路径：{path}"
    if _ABS_WIN_DRIVE_RE.match(raw):
        return f"不接受盘符绝对路径：{path}"
    for seg in _PATH_SEP_RE.split(raw):
        if seg in ("..", "~"):
            return f"路径含越界段 {seg}：{path}"
    return None


# 多轮会话记忆存储（SQLite checkpointer）
MEMORY_DB = os.getenv("MEMORY_DB", str(BASE_DIR / "data" / "memory.sqlite"))

# 全局记忆存储（SQLite profile 表）：跨会话的用户画像/长期事实。
# 与会话记忆分离存储：会话记忆归 LangGraph checkpointer 管理（thread 内短期），
# 全局记忆归业务层（长期，随每次对话注入 system prompt）。
GLOBAL_MEMORY_DB = os.getenv("GLOBAL_MEMORY_DB", str(BASE_DIR / "data" / "global_memory.sqlite"))

# 本地 API token（安全加固，可选）：
# 设置后，/ask 与 /api/config 等会消耗额度/写入配置的接口要求请求头
# 携带 `X-API-Token: <token>`；未设置则保持开放（仅本机使用）。
API_TOKEN = os.getenv("API_TOKEN", "")

# 模型
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")  # deepseek / openai / qwen / zhipu / moonshot / ollama
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")  # 可选，自定义 OpenAI 兼容地址

# 本地模型（Ollama）：免费离线对话，需安装 Ollama 并拉取模型
# 例：OLLAMA_BASE_URL=http://localhost:11434/v1, OLLAMA_MODEL=qwen2.5:7b
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# 联网搜索（双引擎：配了 Bocha 用博查中文搜索，否则退回 DuckDuckGo）
BOCHA_API_KEY = os.getenv("BOCHA_API_KEY", "")

def _warn_bad_env(name: str, raw: str, default, why: str = "不是合法数值") -> None:
    """提示某个环境变量取值不可用并已回退（走 stderr，避免依赖日志模块）。

    `why` 让"格式错误"与"关系约束不满足"（如 CHUNK_OVERLAP >= CHUNK_SIZE）
    都能给出说得清的原因 —— 回退时不说清为什么，用户会以为配置生效了。
    """
    import sys

    print(
        f"[config] 环境变量 {name}={raw!r} {why}，已回退为 {default!r}",
        file=sys.stderr,
    )


def _env_int(name: str, default: int) -> int:
    """读整数环境变量；非法值回退默认并告警，**不让进程在 import 期崩掉**。

    修的问题（外部评审指出）：原实现是 `int(os.getenv("TOP_K", "3"))` 这种写法，
    `.env` 里写错一个字符（`TOP_K=3o`）就会在 import 阶段抛 ValueError——
    程序连启动都到不了，错误信息还只有一个裸堆栈。而"配置写错"恰恰是
    最可预期的一类输入错误，应该被友好地兜住，而不是让整个服务起不来。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        _warn_bad_env(name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    """读浮点环境变量；语义同 `_env_int`。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        _warn_bad_env(name, raw, default)
        return default


# 检索
CHUNK_SIZE = _env_int("CHUNK_SIZE", 500)
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 100)

# 不变式：1 <= CHUNK_SIZE 且 0 <= CHUNK_OVERLAP < CHUNK_SIZE。
# 为什么必须硬校验而不是只在文档里写一句（外部评审第 6 条）：切片步长是
# `CHUNK_SIZE - CHUNK_OVERLAP`，一旦 overlap >= size，步长就 <= 0 ——
# 轻则切片不前进/陷入死循环，重则同一段文本被反复拼进片段、索引体积爆炸。
# 这两个值来自环境变量，最常见的误写正是 `CHUNK_OVERLAP=500`（与 size 相等），
# 所以约束必须落在代码里、并且回退时说明原因。
if CHUNK_SIZE < 1:
    _warn_bad_env("CHUNK_SIZE", str(CHUNK_SIZE), 500, why="必须为正整数")
    CHUNK_SIZE = 500
if CHUNK_OVERLAP < 0 or CHUNK_OVERLAP >= CHUNK_SIZE:
    _fixed_overlap = max(1, CHUNK_SIZE // 5)
    _warn_bad_env(
        "CHUNK_OVERLAP", str(CHUNK_OVERLAP), _fixed_overlap,
        why=f"必须满足 0 <= overlap < CHUNK_SIZE({CHUNK_SIZE})",
    )
    CHUNK_OVERLAP = _fixed_overlap

TOP_K = _env_int("TOP_K", 3)

# 检索最低融合分阈值（RRF）。
#
# 为什么默认不再是 0.0（外部评审第 3 条）：RRF 分恒为正，`if score > 0.0` 等于
# **没有任何过滤**——只要查询词与片段有任何共现就入库，短查询下几乎必然命中，
# 于是"我把不相关的片段塞给了模型"这件事在代码层面完全不可见。
# 取值依据：单通道 rank r 的贡献是 1/(60+r)（rank 0 ≈ 0.0167，rank 30 ≈ 0.0111，
# rank 60 ≈ 0.0083）。默认 0.01 保留"单通道排名前 40 左右"及"双通道命中"的结果，
# 滤掉长尾弱命中；需要更严或更松时通过环境变量调整。
MIN_SCORE = _env_float("MIN_SCORE", 0.01)

# 低置信阈值：最高融合分低于此值时，检索结果会被标注为"相关度较低"，
# 并提示模型在无法据此作答时如实说明，而不是硬凑一个答案。
# 0.02 大致对应"只有单通道、且排名不靠前"的命中（双通道 rank0 ≈ 0.033）。
RETRIEVAL_LOW_CONFIDENCE = _env_float("RETRIEVAL_LOW_CONFIDENCE", 0.02)

# 语义检索模型（sentence-transformers，本地运行无需 API Key）
# 未配置/加载失败时自动回退纯 BM25 检索
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

INDEX_FILE = INDEX_DIR / "index.json"

# ===== 成本估算（可选）=====
# 各服务商每百万 token 单价（人民币，近似公开定价；估算展示用，非账单）
# 结构: {provider: {"input": float, "output": float}}
PROVIDER_PRICES = {
    "deepseek": {"input": 2.0, "output": 8.0},        # deepseek-chat
    "openai": {"input": 1.2, "output": 4.8},          # gpt-4o-mini 近似
    "qwen": {"input": 0.5, "output": 2.0},            # qwen-plus 近似
    "zhipu": {"input": 0.6, "output": 2.0},           # glm-4-flash 近似
    "moonshot": {"input": 12.0, "output": 12.0},      # moonshot-v1-8k 近似
}

# ===== 服务商预设（OpenAI 兼容接口）=====
# 每个服务商: {default_base_url, default_model, api_key_env}
PROVIDER_PRESETS = {
    "deepseek": {
        "default_base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    "qwen": {  # 阿里通义千问
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "api_key_env": "QWEN_API_KEY",
    },
    "zhipu": {  # 智谱 GLM
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "api_key_env": "ZHIPU_API_KEY",
    },
    "moonshot": {  # Kimi
        "default_base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "api_key_env": "MOONSHOT_API_KEY",
    },
}

# 运行时 API 配置（网页端可动态更换，不写入文件）
# 结构: {provider: {"api_key": str, "base_url": str, "model": str}}
# 加锁保护：网页端写（/api/config）与 Agent 读（构建模型时）并发，dict 读写非原子
# （`import threading as _threading` 已提到文件顶部：模块级导入放在代码中间
#  会触发 ruff E402，也让"这个文件的依赖"变得不好一眼看清。）
_runtime_provider_config: dict = {}
_runtime_config_lock = _threading.Lock()
# 配置版本号：每次运行时配置变更 +1，供外部缓存失效（如 agent 按 mode 缓存）
_runtime_config_version = 0


def get_runtime_config_version() -> int:
    """返回运行时配置版本号（agent 缓存失效用）。"""
    with _runtime_config_lock:
        return _runtime_config_version


def set_runtime_provider_config(provider: str, api_key: str, base_url: str = "", model: str = ""):
    """设置某 provider 的运行时配置（网页端更换 Key 用）。"""
    global _runtime_config_version
    with _runtime_config_lock:
        _runtime_provider_config[provider.lower()] = {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
        }
        _runtime_config_version += 1


def reset_state() -> None:
    """清空运行时配置（测试复位用）。

    运行时配置是进程级可变状态：不复位就会跨用例渗透，
    表现为"某个用例改了 Key，后面所有用例都跟着变"。
    """
    global _runtime_config_version
    with _runtime_config_lock:
        _runtime_provider_config.clear()
        _runtime_config_version += 1


def get_provider_config(provider: str) -> dict:
    """获取 provider 的有效配置（优先运行时设置，回退预设默认）。

    返回值额外带 `source` 字段（"runtime" / "preset" / "unknown"），
    供调用方判断 base_url 是**用户显式指定**的还是**预设默认值** ——
    这是 `LLM_BASE_URL` 能生效的前提：预设默认值不该遮蔽环境变量。
    """
    provider = provider.lower()
    # 运行时设置优先（持锁读取，避免读到写一半的 dict）
    with _runtime_config_lock:
        if provider in _runtime_provider_config:
            return {**_runtime_provider_config[provider], "source": "runtime"}

    # 回退到预设默认（从环境变量读 Key）
    preset = PROVIDER_PRESETS.get(provider)
    if not preset:
        return {"api_key": "", "base_url": "", "model": "", "source": "unknown"}
    env_key = os.getenv(preset["api_key_env"], "")
    return {
        "api_key": env_key,
        "base_url": preset["default_base_url"],
        "model": preset["default_model"],
        "source": "preset",
    }
