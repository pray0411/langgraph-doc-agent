# -*- coding: utf-8 -*-
"""多服务商配置契约测试。

## 为什么值得单独写一个文件

项目支持 6 个 OpenAI 兼容服务商（deepseek / openai / qwen / zhipu / moonshot / ollama），
但此前的测试**只覆盖了 DeepSeek 一条路径**。这意味着另外 5 个预设的
base_url、默认模型名、API Key 读取的环境变量名，全部没有任何检查 ——
写错一个字母不会有任何测试变红，只会在用户切到那个服务商时以一个
看不懂的连接错误暴露出来。

这类测试的成本极低（不发请求、不需要真 Key）：只断言"配置解析出来的东西
长什么样"，以及"交给模型客户端的是不是这些值"。它防的是**配置改坏**。

## 做法

不真调 API。通过替换 `graph.create_agent` 捕获真正传给模型的参数，
然后断言 base_url / model / api_key 与预期一致。
"""
from urllib.parse import urlsplit

import pytest

# provider -> (API Key 环境变量名, 预期 base_url, 预期默认模型)
EXPECTED_PRESETS = {
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    "qwen": ("QWEN_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "zhipu": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "moonshot": ("MOONSHOT_API_KEY", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
}


def _capture_model(monkeypatch, provider: str):
    """构建 agent 并捕获传给模型客户端的实际参数。"""
    import graph

    captured: dict = {}

    def _fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()  # 只需要拿到参数，不需要真的可用

    monkeypatch.setattr(graph, "create_agent", _fake_create_agent)
    graph.build_agent(provider)

    model = captured.get("model")
    assert model is not None, "create_agent 没有收到 model 参数"
    base_url = getattr(model, "openai_api_base", None) or str(getattr(model, "base_url", ""))
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None)
    return base_url, model_name, model


# ---------- 预设完整性 ----------

def test_every_declared_preset_has_expected_contract():
    """预设表与测试里的期望表必须一一对应（新增服务商必须同步补契约）。

    这条防的是"加了新服务商但忘了写测试，于是它永远没人管"。
    契约表本身就是文档：看着它就知道每个服务商该长什么样。
    """
    from config import PROVIDER_PRESETS

    assert set(PROVIDER_PRESETS) == set(EXPECTED_PRESETS), (
        "PROVIDER_PRESETS 与契约期望不一致；新增/删除服务商时必须同步更新本文件。\n"
        f"仅代码侧有：{sorted(set(PROVIDER_PRESETS) - set(EXPECTED_PRESETS))}\n"
        f"仅测试侧有：{sorted(set(EXPECTED_PRESETS) - set(PROVIDER_PRESETS))}"
    )


@pytest.mark.parametrize("provider", sorted(EXPECTED_PRESETS))
def test_preset_base_url_and_default_model(provider):
    """每个预设的 base_url 与默认模型名必须与契约一致，且是合法 https 地址。"""
    from config import PROVIDER_PRESETS

    key_env, expected_url, expected_model = EXPECTED_PRESETS[provider]
    preset = PROVIDER_PRESETS[provider]

    assert preset["default_base_url"] == expected_url, (
        f"{provider} 的 base_url 变了：{preset['default_base_url']} != {expected_url}"
    )
    assert preset["default_model"] == expected_model, (
        f"{provider} 的默认模型变了：{preset['default_model']} != {expected_model}"
    )
    assert preset["api_key_env"] == key_env, (
        f"{provider} 读取的 Key 环境变量名变了：{preset['api_key_env']} != {key_env}"
    )

    parts = urlsplit(expected_url)
    assert parts.scheme == "https", f"{provider} 的 base_url 必须走 https"
    assert parts.hostname, f"{provider} 的 base_url 缺少主机名"


@pytest.mark.parametrize("provider", sorted(EXPECTED_PRESETS))
def test_provider_config_reports_preset_source(monkeypatch, provider):
    """未做运行时配置时，来源必须标记为 preset（这是 base_url 优先级的判据）。"""
    from config import get_provider_config

    key_env, expected_url, _ = EXPECTED_PRESETS[provider]
    monkeypatch.setenv(key_env, "sk-preset-key")

    pcfg = get_provider_config(provider)

    assert pcfg["source"] == "preset"
    assert pcfg["base_url"] == expected_url
    assert pcfg["api_key"] == "sk-preset-key", (
        f"{provider} 应从 {key_env} 读取 Key；读到的是 {pcfg['api_key']!r}"
    )


@pytest.mark.parametrize("provider", sorted(EXPECTED_PRESETS))
def test_model_client_receives_preset_base_url(monkeypatch, provider):
    """端到端一点：真正交给 ChatOpenAI 的 base_url 必须是该预设的地址。

    只测 `get_provider_config` 是不够的 —— 配置解析对了，但构建模型时用错字段
    或漏传参数，一样会打错地方。这里断言的是"最终交给客户端的值"。
    """
    key_env, expected_url, expected_model = EXPECTED_PRESETS[provider]
    monkeypatch.setenv(key_env, "sk-preset-key")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    base_url, model_name, _ = _capture_model(monkeypatch, provider)

    assert base_url == expected_url, f"{provider} 的模型 base_url 不对：{base_url}"
    assert model_name == expected_model, f"{provider} 的模型名不对：{model_name}"


@pytest.mark.parametrize("provider", sorted(EXPECTED_PRESETS))
def test_preset_api_key_is_forwarded_to_client(monkeypatch, provider):
    """API Key 必须被真正传给模型客户端（配了却没用上是最难查的一类问题）。"""
    key_env, _, _ = EXPECTED_PRESETS[provider]
    monkeypatch.setenv(key_env, "sk-contract-key-123")

    _, _, model = _capture_model(monkeypatch, provider)

    secret = getattr(model, "openai_api_key", None)
    if hasattr(secret, "get_secret_value"):
        secret = secret.get_secret_value()
    assert secret == "sk-contract-key-123", (
        f"{provider} 没有把 {key_env} 的值传给客户端（实际 {secret!r}）"
    )


# ---------- LLM_BASE_URL 优先级（A6 的契约） ----------

@pytest.mark.parametrize("provider", sorted(EXPECTED_PRESETS))
def test_llm_base_url_overrides_preset(monkeypatch, provider):
    """环境变量 `LLM_BASE_URL` 必须能覆盖**任何**预设的默认地址。

    这是 README 承诺的"自定义 OpenAI 兼容网关（one-api / vLLM）"能生效的前提。
    原实现里 `or LLM_BASE_URL` 排在预设默认值之后，因此是**死代码** ——
    变量设了也不生效、且没有任何报错。这条按服务商逐一守住它。
    """
    key_env, _, _ = EXPECTED_PRESETS[provider]
    monkeypatch.setenv(key_env, "sk-preset-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9999/v1")

    base_url, _, _ = _capture_model(monkeypatch, provider)

    assert base_url == "http://127.0.0.1:9999/v1", (
        f"{provider} 下 LLM_BASE_URL 没有生效（仍是 {base_url}）"
    )


def test_runtime_config_beats_env_var(monkeypatch):
    """网页端本次运行手填的地址必须优先于环境变量（最具体的赢）。"""
    import config
    from graph import _resolve_base_url

    pcfg = {"base_url": "http://127.0.0.1:1111/v1", "source": "runtime"}
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:2222/v1")
    assert _resolve_base_url(pcfg) == "http://127.0.0.1:1111/v1"

    # 运行时设置了 provider 但没填地址 → 回落到环境变量
    empty_runtime = {"base_url": "", "source": "runtime"}
    assert _resolve_base_url(empty_runtime) == "http://127.0.0.1:2222/v1"

    # 没有任何显式配置 → 用预设默认值
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    preset = {"base_url": "https://api.deepseek.com/v1", "source": "preset"}
    assert _resolve_base_url(preset) == "https://api.deepseek.com/v1"

    # 未知服务商且无环境变量 → 兜底地址，不能返回空串（空 base_url 会打到默认域名）
    assert _resolve_base_url({"base_url": "", "source": "unknown"}) == "https://api.deepseek.com/v1"
    del config


def test_unknown_provider_is_reported_not_guessed():
    """未知服务商必须显式标记为 unknown，而不是静默套用某个预设。"""
    from config import get_provider_config

    pcfg = get_provider_config("no-such-provider")

    assert pcfg["source"] == "unknown"
    assert pcfg["api_key"] == ""
    assert pcfg["base_url"] == ""


def test_runtime_config_overrides_preset(monkeypatch):
    """运行时配置（网页端换 Key/模型）生效，且来源标记为 runtime。"""
    import config

    config.set_runtime_provider_config(
        "deepseek", api_key="sk-runtime", base_url="http://127.0.0.1:3333/v1", model="my-model"
    )
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    pcfg = config.get_provider_config("deepseek")
    assert pcfg["source"] == "runtime"
    assert pcfg["api_key"] == "sk-runtime"
    assert pcfg["model"] == "my-model"

    base_url, model_name, _ = _capture_model(monkeypatch, "deepseek")
    assert base_url == "http://127.0.0.1:3333/v1"
    assert model_name == "my-model"


def test_runtime_config_version_bumps_to_invalidate_agent_cache():
    """运行时配置变更必须让版本号自增 —— 这是 agent 缓存失效的唯一信号。

    不失效的后果：用户在网页端换了 Key，界面提示成功，但后续对话**仍在用旧 Key**
    打旧地址。这类"配置生效了但行为没变"的问题极难自查。
    """
    import config

    before = config.get_runtime_config_version()
    config.set_runtime_provider_config("openai", api_key="sk-new")
    assert config.get_runtime_config_version() > before


def test_ollama_path_uses_local_base_url(monkeypatch):
    """ollama 走本地地址且不需要真实 Key（占位即可）。

    这条单独写是因为 ollama 在 `build_agent` 里是与在线分支**分开**的一段代码，
    前面的参数化测不到它。
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")

    base_url, model_name, _ = _capture_model(monkeypatch, "ollama")

    assert base_url == "http://127.0.0.1:11434/v1"
    assert model_name == "qwen2.5:7b"
