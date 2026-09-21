# -*- coding: utf-8 -*-
"""日志与安全审计测试（logging_setup.py）。

为什么这个文件比它看起来重要：`logging_setup` 是"命令执行审计"的唯一出口。
本项目允许模型跑任意命令，而事后追责**只有审计日志这一条线索**。所以：

1. **审计必须真的落盘**，且一行一条 JSON（否则没法 grep / jq）。
2. **敏感值必须被脱敏**：为了可观测而把 API Key 明文写进日志，
   是把"可观测性收益"换成"凭据泄漏风险"，得不偿失。
3. **审计文件与普通日志分离**：把审计混在 app.log 里，等于让它随轮转被冲掉。

这些都是"看起来实现了、其实没生效"的高发区，因此逐条断言。
"""
import json
import logging
from pathlib import Path


def _audit_path() -> Path:
    """当前用例的审计日志路径（LOG_DIR 由 conftest 隔离到沙箱）。"""
    import os

    return Path(os.environ["LOG_DIR"]) / "audit.log"


def _read_audit_records() -> list[dict]:
    """读出审计日志里的全部 JSON 行。"""
    path = _audit_path()
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


# ---------- 脱敏 ----------

def test_mask_keeps_identity_but_not_secret():
    """脱敏结果要"够确认身份、不足以复用"：短值全掩，长值留首尾。"""
    from logging_setup import _mask

    assert _mask("sk-1234567890abcdef") == "sk-***ef"
    assert _mask("12345") == "***"          # <= 6 位：整段掩掉
    assert _mask("123456") == "***"         # 边界值同样是整段掩掉


def test_sanitize_masks_sensitive_field_names():
    """字段名含 key / token / secret / password 的必须脱敏。"""
    from logging_setup import _sanitize

    out = _sanitize({
        "api_key": "sk-abcdef1234567890",
        "X-API-Token": "supersecrettoken",
        "password": "hunter2hunter2",
        "thread_id": "abc123",       # 非敏感，必须原样保留
        "exit_code": 0,
    })

    assert out["api_key"] == "sk-***90"
    assert out["X-API-Token"] == "sup***en"
    assert out["password"] == "hun***r2"
    assert out["thread_id"] == "abc123", "非敏感字段被误伤，会破坏排障能力"
    assert out["exit_code"] == 0


def test_sanitize_handles_nested_dict_and_empty_values():
    """嵌套 dict 要递归脱敏；空值不应变成 'None' 字符串。"""
    from logging_setup import _sanitize

    out = _sanitize({
        "request": {"headers": {"authorization": "Bearer abcdefghijklmn"}},
        "api_key": "",
    })

    assert out["request"]["headers"]["authorization"].startswith("Bea")
    assert "***" in out["request"]["headers"]["authorization"]
    assert out["api_key"] == "", "空值应保持为空串，而不是被 str() 成 'None'"


# ---------- 审计落盘 ----------

def test_audit_writes_one_json_line_per_event():
    """每条 audit() 写一行可解析的 JSON，且事件名与字段都在。"""
    from logging_setup import audit

    audit("run_command", command_hash="deadbeef", high_risk=True, exit_code=0)

    records = _read_audit_records()
    assert len(records) == 1, f"应恰好一条审计记录，实际 {len(records)}"
    rec = records[0]
    assert rec["event"] == "run_command"
    assert rec["command_hash"] == "deadbeef"
    assert rec["high_risk"] is True
    assert rec["exit_code"] == 0
    assert "ts" in rec, "每条审计记录都要有时间戳，否则无法排序/追责"


def test_audit_masks_credentials_before_writing_to_disk():
    """**最关键的一条**：脱敏必须发生在写盘之前。

    如果只在打印时脱敏、写文件时用原始值，那"日志脱敏"就只是观感上的，
    密钥仍然安静地躺在 logs/audit.log 里 —— 而这正是最容易被忽略的泄漏面。
    这里直接读**文件内容**断言密钥不存在。
    """
    from logging_setup import audit

    secret = "sk-live-abcdefghijklmnop"
    audit("gateway_config", api_key=secret, base_url="https://api.example.com/v1")

    raw = _audit_path().read_text(encoding="utf-8")
    assert secret not in raw, "审计日志里出现了明文密钥"

    rec = _read_audit_records()[0]
    assert rec["api_key"] != secret
    assert "***" in rec["api_key"]
    assert rec["base_url"] == "https://api.example.com/v1", "非敏感字段应保持可读"


def test_audit_does_not_leak_into_app_log():
    """审计必须独立成文件：混进 app.log 会让它随轮转被冲掉。"""
    import os

    from logging_setup import audit, get_logger

    audit("approval_granted", command_hash="cafe")
    get_logger("sometool").info("普通应用日志")

    app_log = Path(os.environ["LOG_DIR"]) / "app.log"
    assert app_log.exists(), "app.log 应被创建"
    app_text = app_log.read_text(encoding="utf-8")
    assert "approval_granted" not in app_text, "审计事件不应写进应用日志"
    assert "普通应用日志" in app_text


def test_audit_logger_does_not_propagate():
    """审计 logger 不能向上传播，否则会被父级 handler 重复输出。"""
    from logging_setup import audit  # noqa: F401 - 触发配置

    assert logging.getLogger("pray.audit").propagate is False


# ---------- 配置与命名空间 ----------

def test_setup_logging_returns_and_creates_log_dir(tmp_path):
    """setup_logging 要返回日志目录并真的创建它（启动信息会把它告诉用户）。"""
    from logging_setup import setup_logging

    target = tmp_path / "custom-logs"
    resolved = setup_logging(log_dir=target, console=False)

    assert Path(resolved) == target
    assert target.is_dir(), "日志目录应被创建"
    assert (target / "audit.log").exists(), "审计文件必须存在"


def test_setup_logging_honours_log_dir_after_implicit_config(tmp_path):
    """回归用例：已经按默认目录自举过之后，再指定 log_dir 必须真的换过去。

    这条测的是一个**真实缺陷**（写这条用例时才发现）：
    原实现里 `_ensure_configured()` 会先按默认目录挂好 handler，而 `setup_logging`
    用的是 `if not audit_logger.handlers: addHandler(...)` —— handler 已存在就直接
    跳过，于是**传入的 log_dir 被静默忽略**。日志照常输出、程序不报错，
    只是写到了另一个目录：部署时把日志指向挂载卷会失效，排查故障时才发现。

    这里刻意先触发一次隐式配置（`get_logger`），再显式指定目录，
    最后断言**审计记录真的落在新目录**，而不是只看返回值。
    """
    from logging_setup import audit, get_logger, setup_logging

    get_logger("some.module")          # ① 触发默认目录的隐式配置
    target = tmp_path / "relocated-logs"
    setup_logging(log_dir=target, console=False)   # ② 显式换目录

    audit("after_relocation", note="应落在新目录")

    assert (target / "audit.log").exists(), "指定的 log_dir 没有生效（handler 未重建）"
    records = [
        json.loads(line)
        for line in (target / "audit.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r["event"] == "after_relocation" for r in records), (
        "审计记录没有写进新目录 —— log_dir 被静默忽略了"
    )


def test_get_logger_uses_pray_namespace():
    """所有模块 logger 统一挂在 pray.* 下，便于整体调级。"""
    from logging_setup import get_logger

    assert get_logger("tools").name == "pray.tools"
    assert get_logger("pray.graph").name == "pray.graph"


def test_reset_state_removes_handlers():
    """reset_state 必须摘掉 handler：否则轮转文件句柄会跨用例泄漏。"""
    from logging_setup import audit, get_logger, reset_state

    audit("before_reset")          # 触发配置
    assert logging.getLogger("pray").handlers, "配置后应有 handler"

    reset_state()

    assert logging.getLogger("pray").handlers == []
    assert logging.getLogger("pray.audit").handlers == []
    # 复位后再次使用应能自举，而不是静默丢日志
    audit("after_reset")
    assert any(r["event"] == "after_reset" for r in _read_audit_records())


# ---------- JSON 格式器兜底 ----------

def test_json_formatter_falls_back_for_plain_records():
    """非 audit() 写入的记录也要能格式化成合法 JSON（不能抛异常）。"""
    from logging_setup import _JsonLineFormatter

    record = logging.LogRecord(
        name="pray.audit", level=logging.INFO, pathname=__file__, lineno=1,
        msg="裸消息", args=(), exc_info=None,
    )
    payload = json.loads(_JsonLineFormatter().format(record))

    assert payload["event"] == "裸消息"
    assert "ts" in payload


def test_json_formatter_serializes_non_json_native_values():
    """字段里有非 JSON 原生类型（如 set / 自定义对象）时不能崩。"""
    from logging_setup import _JsonLineFormatter

    record = logging.LogRecord(
        name="pray.audit", level=logging.INFO, pathname=__file__, lineno=1,
        msg="", args=(), exc_info=None,
    )
    record.audit_payload = {"event": "weird", "tags": {"a", "b"}}
    payload = json.loads(_JsonLineFormatter().format(record))

    assert payload["event"] == "weird"
    assert isinstance(payload["tags"], str), "非原生类型应被 default=str 降级为字符串"


def test_default_log_dir_prefers_env_var(monkeypatch, tmp_path):
    """LOG_DIR 环境变量优先（部署时要把日志写到挂载卷上）。"""
    from logging_setup import _default_log_dir

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "env-logs"))
    assert _default_log_dir() == tmp_path / "env-logs"


def test_default_log_dir_falls_back_without_env(monkeypatch):
    """未设 LOG_DIR 时应回退到仓库 logs/，而不是抛异常。"""
    from logging_setup import _default_log_dir

    monkeypatch.delenv("LOG_DIR", raising=False)
    assert _default_log_dir().name == "logs"
