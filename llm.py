"""模型封装：支持 DeepSeek / OpenAI / 离线演示三种模式。

统一接口 generate(prompt, system) -> str。
- deepseek/openai: 走 OpenAI 兼容 API
- offline: 不调 API，直接把提示词返回（用于无 Key 演示与测试）
"""
from config import DEEPSEEK_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROVIDER, OPENAI_API_KEY


class LLMError(RuntimeError):
    pass


class _OfflineLLM:
    """离线演示模型：不调用外部 API，原样返回输入，保证无 Key 也能跑通全流程。"""

    def generate(self, prompt: str, system: str = "") -> str:
        return prompt


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


def build_llm() -> _OfflineLLM | _OpenAICompatLLM:
    """根据 LLM_PROVIDER 构建模型实例。"""
    provider = LLM_PROVIDER.lower()
    if provider == "offline":
        return _OfflineLLM()
    if provider == "deepseek":
        if not DEEPSEEK_API_KEY:
            raise LLMError("未配置 DEEPSEEK_API_KEY，请在 .env 中填写或使用 LLM_PROVIDER=offline")
        return _OpenAICompatLLM(DEEPSEEK_API_KEY, LLM_BASE_URL or "https://api.deepseek.com/v1", LLM_MODEL)
    if provider == "openai":
        if not OPENAI_API_KEY:
            raise LLMError("未配置 OPENAI_API_KEY，请在 .env 中填写或使用 LLM_PROVIDER=offline")
        return _OpenAICompatLLM(OPENAI_API_KEY, LLM_BASE_URL or "https://api.openai.com/v1", LLM_MODEL)
    raise LLMError(f"未知的 LLM_PROVIDER: {provider}")
