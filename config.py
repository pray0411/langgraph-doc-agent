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

# 模型
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "offline")  # deepseek / openai / ollama / offline
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
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.05"))  # 检索最低相似度阈值

INDEX_FILE = INDEX_DIR / "index.json"
