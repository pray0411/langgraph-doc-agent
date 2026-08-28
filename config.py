"""全局配置：从环境变量读取，未配置时使用默认值。"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv 未安装时静默跳过
    pass

BASE_DIR = Path(__file__).resolve().parent

# 文档与索引目录
DOCS_DIR = Path(os.getenv("DOCS_DIR", BASE_DIR / "docs"))
INDEX_DIR = Path(os.getenv("INDEX_DIR", BASE_DIR / "index"))

# 多轮会话记忆存储（SQLite checkpointer）
MEMORY_DB = os.getenv("MEMORY_DB", str(BASE_DIR / "data" / "memory.sqlite"))

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

# 检索
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "100"))
TOP_K = int(os.getenv("TOP_K", "3"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.0"))  # 检索最低分数阈值（混合检索融合分）

# 语义检索模型（sentence-transformers，本地运行无需 API Key）
# 未配置/加载失败时自动回退纯 BM25 检索
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

INDEX_FILE = INDEX_DIR / "index.json"

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
import threading as _threading

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


def get_provider_config(provider: str) -> dict:
    """获取 provider 的有效配置（优先运行时设置，回退预设默认）。"""
    provider = provider.lower()
    # 运行时设置优先（持锁读取，避免读到写一半的 dict）
    with _runtime_config_lock:
        if provider in _runtime_provider_config:
            return dict(_runtime_provider_config[provider])

    # 回退到预设默认（从环境变量读 Key）
    preset = PROVIDER_PRESETS.get(provider)
    if not preset:
        return {"api_key": "", "base_url": "", "model": ""}
    env_key = os.getenv(preset["api_key_env"], "")
    return {
        "api_key": env_key,
        "base_url": preset["default_base_url"],
        "model": preset["default_model"],
    }
