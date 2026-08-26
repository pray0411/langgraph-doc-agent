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
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "offline")  # deepseek / openai / offline
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")  # 可选，自定义 OpenAI 兼容地址

# 联网搜索（双引擎：配了 Bocha 用博查中文搜索，否则退回 DuckDuckGo）
BOCHA_API_KEY = os.getenv("BOCHA_API_KEY", "")

# 检索
EMBEDDING_MODE = os.getenv("EMBEDDING_MODE", "local")  # local / remote
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "100"))
TOP_K = int(os.getenv("TOP_K", "3"))

INDEX_FILE = INDEX_DIR / "index.json"
