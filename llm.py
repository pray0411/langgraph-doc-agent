"""模型封装：支持 DeepSeek / OpenAI / Ollama(本地) / 离线检索四种模式。

统一接口 generate(prompt, system) -> str。
- deepseek/openai: 走 OpenAI 兼容 API（需 Key）
- ollama: 走本地 Ollama 的 OpenAI 兼容接口（免费离线，需装 Ollama）
- offline: 不调模型，仅本地文档检索演示
"""
from config import (
    DEEPSEEK_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OPENAI_API_KEY,
)


class LLMError(RuntimeError):
    pass


class _OfflineLLM:
    """离线检索演示：不调用外部 API。

    从 prompt 中提取「文档内容」段落返回，模拟一个基于文档的回答，
    让无 API Key 的环境也能完整跑通并验证 RAG 链路。
    """

    def generate(self, prompt: str, system: str = "") -> str:
        marker = "文档内容：\n"
        if marker in prompt:
            content = prompt.split(marker, 1)[1]
            content = content.split("\n\n问题：", 1)[0]
            return f"[离线演示] 以下为基于文档检索的原始内容：\n\n{content.strip()}"
        return "[离线演示] 未检索到文档内容，请先构建索引。"


class _OpenAICompatLLM:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.model = model
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError("未安装 openai 包，请先运行: pip install -r requirements.txt") from exc
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)

    def generate(self, prompt: str, system: str = "") -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system or "你是一个智能助手。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"模型调用失败: {exc}") from exc


class _OllamaLLM(_OpenAICompatLLM):
    """本地 Ollama 模型：走 OpenAI 兼容接口（http://localhost:11434/v1）。

    Ollama 提供 OpenAI 兼容端点，无需 API Key（任意占位即可）。
    """

    def __init__(self, base_url: str = OLLAMA_BASE_URL, model: str = OLLAMA_MODEL):
        super().__init__(api_key="ollama", base_url=base_url, model=model)


def build_llm() -> _OfflineLLM | _OpenAICompatLLM:
    """根据 LLM_PROVIDER 构建模型实例。"""
    provider = LLM_PROVIDER.lower()
    if provider == "offline":
        return _OfflineLLM()
    if provider == "ollama":
        # Ollama 本地模型：免费离线对话；未安装时给出清晰提示
        return _OllamaLLM()
    if provider == "deepseek":
        if not DEEPSEEK_API_KEY:
            raise LLMError("未配置 DEEPSEEK_API_KEY，请在 .env 中填写或使用 LLM_PROVIDER=offline")
        return _OpenAICompatLLM(DEEPSEEK_API_KEY, LLM_BASE_URL or "https://api.deepseek.com/v1", LLM_MODEL)
    if provider == "openai":
        if not OPENAI_API_KEY:
            raise LLMError("未配置 OPENAI_API_KEY，请在 .env 中填写或使用 LLM_PROVIDER=offline")
        return _OpenAICompatLLM(OPENAI_API_KEY, LLM_BASE_URL or "https://api.openai.com/v1", LLM_MODEL)
    raise LLMError(f"未知的 LLM_PROVIDER: {provider}")