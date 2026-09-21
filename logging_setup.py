"""日志与安全审计。

修复的问题：此前全项目**没有一行 `logging`**，13 个模块全靠 `print`，
且 `server.Handler.log_message` 被直接 `pass` 掉 —— 所有 HTTP 访问日志被静音。

对一个"能执行任意命令"的服务来说，这是**审计能力的真空**：
谁在什么时候让模型跑了什么命令、批准记录是什么、哪个会话超时了，
事后一条痕迹都查不到。

本模块提供两套输出：

- `get_logger(name)`  —— 常规应用日志（控制台 + `logs/app.log` 轮转）
- `audit(event, **kw)` —— **安全审计日志**，独立文件 `logs/audit.log`，
  每行一条结构化 JSON，便于事后检索与统计

审计字段里的敏感值（api_key / token / secret / password）会被自动脱敏，
避免"为了可观测反而把密钥写进日志"。
"""
import json
import logging
import logging.handlers
import os
import threading
from datetime import datetime
from pathlib import Path

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5

_SENSITIVE_HINTS = ("key", "token", "secret", "password", "passwd", "authorization")

_lock = threading.Lock()


def _configure(log_dir: Path, level: int, console: bool) -> None:
    """按给定参数**重建**全部 handler（先摘旧的，再挂新的）。

    为什么要"先摘再挂"而不是"没有才挂"：原实现用的是
    `if not audit_logger.handlers: addHandler(...)`，于是 **handler 一旦存在就再也
    换不掉目录**。后果有两个，都是真实踩到的：

    - `setup_logging(log_dir=X)` 传了 X 也不生效：`_ensure_configured()` 已经先按
      默认目录挂好了 handler，后面的 `if not handlers` 直接跳过 —— 用户指定日志目录
      （部署时指向挂载卷）**静默失效**，日志看起来一切正常，只是写到了别处。
    - 日志目录变成"进程内首次调用时"的那个：谁先触发日志谁定调，
      配置结果依赖执行顺序。

    现在改成"以参数为准、幂等重建"，日志去向完全由调用方决定。
    """
    for name in ("pray", "pray.audit"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应阻断重配置
                pass
            logger.removeHandler(handler)

    app_logger = logging.getLogger("pray")
    app_logger.setLevel(level)
    app_logger.addHandler(
        _rotating(log_dir / "app.log", level, logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
    )
    if console:
        stream = logging.StreamHandler()
        stream.setLevel(level)
        stream.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
        app_logger.addHandler(stream)

    # 审计日志务必独立成文件，且不受控制台开关影响
    audit_logger = logging.getLogger("pray.audit")
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    audit_logger.addHandler(
        _rotating(log_dir / "audit.log", logging.INFO, _JsonLineFormatter())
    )


def _resolve_level(level=None) -> int:
    return getattr(logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO)


def _current_output_dir() -> Path | None:
    """handler **实际**正在写入的目录（从文件 handler 的 baseFilename 反推）。

    为什么要看 reality 而不是记一个"我配置过了"的标记：`logging` 的 logger 是
    **进程级全局对象**，会被模块重导入、重复 setup、外部库改 propagate 等方式扰动 ——
    模块级布尔/路径变量很容易与真实状态脱节，于是出现"以为配好了、其实 handler
    挂在别处（或根本没挂）"的静默错配。直接读 handler 的落点，
    "要不要重配"就变成了可验证的事实判断。
    """
    for handler in logging.getLogger("pray.audit").handlers:
        base = getattr(handler, "baseFilename", None)
        if base:
            return Path(base).parent
    return None


def _ensure_configured() -> None:
    """惰性自举：**仅在完全没有配置过**时，按当前日志目录配置。

    刻意不在"落点与默认目录不一致"时重配。因为那会把显式配置悄悄改回去：
    `setup_logging(log_dir=X)` 是调用方的明确意图（部署时把日志指向挂载卷、
    测试指向沙箱），如果 `audit()` 每次都把它拉回 `LOG_DIR`，
    就等于"入口参数永远被环境变量覆盖" —— 本质上是同一个 bug 换了个方向。

    所以职责划分是：
      - `setup_logging()`：**显式**配置，以参数为准，永远生效（幂等重建）。
      - `_ensure_configured()`：**兜底**自举，只在没有任何 handler 时按默认目录配置。

    目录随环境变量变化的场景（测试隔离）由 `reset_state()` 摘掉 handler 来触发重建，
    而不是靠这里猜。
    """
    if _current_output_dir() is not None:
        return
    with _lock:
        if _current_output_dir() is not None:
            return
        _configure(_default_log_dir(), _resolve_level(), console=False)


def setup_logging(log_dir=None, level=None, console: bool = True) -> Path:
    """配置应用日志。入口（CLI / 服务 / 桌面端 / MCP）启动时调用一次。

    返回实际使用的日志目录，便于启动信息里直接告诉用户"日志在哪"。
    调用方传入的 `log_dir` / `level` / `console` 一律**以本次参数为准**（幂等重建）。
    """
    resolved = Path(log_dir) if log_dir else _default_log_dir()
    with _lock:
        _configure(resolved, _resolve_level(level), console)
    return resolved


def _default_log_dir() -> Path:
    """日志目录：环境变量 LOG_DIR 优先，默认仓库下 logs/。"""
    env = os.getenv("LOG_DIR", "").strip()
    if env:
        return Path(env)
    try:
        from config import BASE_DIR
        return Path(BASE_DIR) / "logs"
    except Exception:  # noqa: BLE001 - config 不可用时退回当前目录
        return Path.cwd() / "logs"


def _mask(value) -> str:
    """脱敏：保留前 3 位与后 2 位，其余以 * 代替（够确认身份，不足以复用）。"""
    text = str(value)
    if len(text) <= 6:
        return "***"
    return f"{text[:3]}***{text[-2:]}"


def _sanitize(fields: dict) -> dict:
    """把疑似敏感字段的值替换为掩码；嵌套 dict 递归处理。"""
    out = {}
    for k, v in fields.items():
        low = str(k).lower()
        if any(hint in low for hint in _SENSITIVE_HINTS):
            out[k] = _mask(v) if v else ""
        elif isinstance(v, dict):
            out[k] = _sanitize(v)
        else:
            out[k] = v
    return out


class _JsonLineFormatter(logging.Formatter):
    """审计日志格式：一行一条 JSON（便于 grep / jq / 导入分析）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds")}
        extra = getattr(record, "audit_payload", None)
        if isinstance(extra, dict):
            payload.update(extra)
        else:  # 兜底：非 audit() 写入的记录
            payload.update({"event": record.getMessage()})
        return json.dumps(payload, ensure_ascii=False, default=str)


def _rotating(path: Path, level: int, formatter: logging.Formatter) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(path), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


# 说明：`_configure` / `_resolve_level` / `_current_output_dir` / `_ensure_configured`
# / `setup_logging` 定义在本文件上半部分（紧接 `_lock` 之后）。放在那里是刻意的：
# 它们是这个模块的**契约入口**，应该先读到；而 `_default_log_dir` / `_rotating` /
# `_JsonLineFormatter` 是它们的实现依赖，放在下面就近。
# 之前这里残留过一份旧实现（用 `_configured` 布尔开关 + "没有才挂 handler"），
# 因为定义在后面会**静默覆盖**新实现 —— 同名函数后定义者胜出，
# 这正是 tests/test_metatest.py 里"同名用例会被静默覆盖"那条规则针对的同一类坑。


def get_logger(name: str) -> logging.Logger:
    """取模块 logger（统一挂在 pray.* 命名空间下，便于整体调级）。"""
    _ensure_configured()
    short = name.split(".")[-1] if name.startswith("pray.") else name
    return logging.getLogger(f"pray.{short}")


def audit(event: str, **fields) -> None:
    """写一条安全审计记录（结构化、脱敏、独立文件）。

    典型用法::

        audit("run_command", command_hash=h, high_risk=True, approved=False,
              blocked=False, exit_code=0, duration_ms=12, thread_id=tid)
        audit("approval_granted", command_hash=h)
    """
    _ensure_configured()
    payload = {"event": event}
    payload.update(_sanitize(fields))
    logging.getLogger("pray.audit").info("", extra={"audit_payload": payload})


def reset_state() -> None:
    """关闭并移除全部 handler（测试复位用；否则轮转文件句柄会跨用例泄漏）。

    摘掉 handler 之后，`_current_output_dir()` 会返回 None，
    下次 `audit()` / `get_logger()` 会自动按当前 `LOG_DIR` 重建 —— 无需额外标记位。
    """
    with _lock:
        for name in ("pray", "pray.audit"):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                try:
                    handler.close()
                except Exception:  # noqa: BLE001
                    pass
                logger.removeHandler(handler)
