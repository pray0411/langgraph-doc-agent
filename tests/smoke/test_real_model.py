"""真模型冒烟：用真实 API 跑通三条主链路。

与 `tests/` 下其余用例的根本区别：那些用**本地假 OpenAI 兼容服务**保证确定性与
零成本；这里真的调用外部模型。目的不是覆盖率，而是发现假服务无法暴露的问题：
prompt 与真实模型行为的偏差、工具 schema 被服务商拒绝、流式分片格式差异、
provider 侧的限流与超时——外部评审第 5 条指出项目此前**完全没有这一层**，
于是"287 条用例全绿"只证明了管道连通性。

运行：

    DEEPSEEK_API_KEY=sk-xxx python -X utf8 -m pytest tests/smoke -m smoke -q -s

CI：`.github/workflows/smoke.yml`（定时 + 手动触发；未配置 secret 时整体跳过）。
默认的 `addopts` 已带 `-m "not smoke"`，因此日常全量测试不会触发真实外呼与费用。
"""
import os

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.allow_network,  # conftest 默认封网，这里显式放行
    pytest.mark.slow,
]


def _require_key() -> None:
    """双重闸门后才允许真实外呼；任一不满足即跳过（而不是失败）。

    为什么要 `PRAY_SMOKE=1` 这道显式开关，而不只看有没有 Key：
    `config.py` 在模块导入期执行 `load_dotenv()`，本机 `.env` 里的真实 Key 会
    进入 `os.environ`（项目自身的密封性测试记录过这条路径）。若只判断"存在
    Key 就跑"，开发者在本机随手跑一次全量测试就会真实调用付费接口 —— 冒烟
    必须是**被显式要求**才发生的行为。

    CI：`.github/workflows/smoke.yml` 里设置 `PRAY_SMOKE=1` 与密钥 secret；
    未配置 secret 时该 workflow 整体跳过。
    """
    if os.getenv("PRAY_SMOKE") != "1":
        pytest.skip("未显式启用真模型冒烟（需 PRAY_SMOKE=1），跳过以避免意外外呼与费用")
    if not (os.getenv("DEEPSEEK_API_KEY") or "").strip():
        pytest.skip("未配置 DEEPSEEK_API_KEY，跳过真模型冒烟")


def _ask(question: str):
    from graph import ask

    # 每条链路用独立 thread，避免相互污染会话记忆
    return ask(question, thread_id=f"smoke-{abs(hash(question)) % 10**8}")


def test_real_model_plain_answer():
    """链路 1：纯问答（不调用工具）——验证最基础的一次真实往返。"""
    _require_key()
    answer, result = _ask("只回答一个数字：1+1 等于几？")
    assert answer.strip(), "真模型未返回内容"
    assert "2" in answer, f"回答异常: {answer[:200]}"


def test_real_model_tool_call():
    """链路 2：模型自主调用工具——验证工具 schema 被真实模型接受并能执行。"""
    _require_key()
    answer, result = _ask("现在几点了？请调用工具查询后告诉我具体时间。")
    assert answer.strip(), "真模型未返回内容"
    reflection = result.get("reflection") or ""
    assert "get_current_time" in reflection, (
        f"模型没有调用时间工具（工具 schema 或 prompt 可能与真实模型不匹配）: {reflection[:300]}"
    )


def test_real_model_retrieval_chain():
    """链路 3：文档检索链路——验证检索 + 生成端到端可用（需索引）。"""
    _require_key()
    from retriever import build_index

    try:
        build_index()  # 已有索引时复用，不强制重建
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"索引不可用，跳过检索链路: {exc}")

    answer, result = _ask("根据项目文档，Pray 用的是什么 Agent 框架或模式？")
    assert answer.strip(), "真模型未返回内容"
    reflection = result.get("reflection") or ""
    assert "search_documents" in reflection, (
        f"模型没有调用文档检索工具: {reflection[:300]}"
    )
