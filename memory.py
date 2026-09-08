"""全局记忆：跨会话的用户画像与长期事实（SQLite profile 表）。

记忆分层（面试/架构叙事）：
- 会话记忆：LangGraph checkpointer（MEMORY_DB），按 thread_id 保留对话上下文，
  属于"本次及近期会话"的短期记忆
- 全局记忆：本模块（GLOBAL_MEMORY_DB），存用户长期稳定信息（称呼、身份、
  语言偏好、项目事实等），跨会话永久保留
- 注入：build_agent 构建时把全局记忆拼进 system prompt（见 build_prompt_section），
  模型每次回答都"带着"这些信息；记忆变化（remember/forget）使版本号 +1，
  触发 agent 缓存失效并重建（无需每次对话重建）

为什么用结构化表而不是向量检索：
- 本项目记忆是"精确事实"（用户说了什么），不是"模糊语义回忆"；
  按 key 去重的 profile 表天然满足"记住最新值、不重复"，
  O(1) 注入成本，无需 embedding 模型与相似度调参
- 向量检索式长期记忆（如 Mem0 类方案）适合海量非结构化回忆，本项目暂不需要

线程安全：web 服务多线程共享；所有操作持模块锁 + 每次开短连接
（低频操作，连接成本可忽略，避免长连接跨线程问题）。
"""
import sqlite3
import threading
import time
from pathlib import Path

from config import GLOBAL_MEMORY_DB

_lock = threading.Lock()
_version = 0  # 记忆变更计数：纳入 agent 缓存 key，remember/forget 后自动重建

# 单条记忆长度上限（防模型写入超大文本撑爆 system prompt）
_MAX_KEY_LEN = 60
_MAX_VALUE_LEN = 500
# 注入 system prompt 的记忆条目上限（防 prompt 过长）
_MAX_INJECT = 30


def _conn() -> sqlite3.Connection:
    """打开 profile 表连接（不存在则建库建表）。"""
    db = Path(GLOBAL_MEMORY_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile (
            key        TEXT PRIMARY KEY,     -- 记忆键（同一主题覆盖更新）
            value      TEXT NOT NULL,        -- 记忆内容
            updated_at TEXT NOT NULL         -- 更新时间（ISO 本地时间）
        )
        """
    )
    return conn


def get_version() -> int:
    """记忆版本号：作为 agent 缓存 key 的一部分，变了即重建。"""
    return _version


def remember(key: str, value: str) -> dict:
    """写入/覆盖一条全局记忆（同 key 只保留最新值）。"""
    global _version
    key = (key or "").strip()
    value = (value or "").strip()
    if not key:
        raise ValueError("记忆 key 不能为空")
    if len(key) > _MAX_KEY_LEN:
        raise ValueError(f"记忆 key 过长（上限 {_MAX_KEY_LEN} 字符）")
    if not value:
        raise ValueError("记忆内容不能为空")
    if len(value) > _MAX_VALUE_LEN:
        raise ValueError(f"记忆内容过长（上限 {_MAX_VALUE_LEN} 字符）")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO profile (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, ts),
            )
            conn.commit()
        finally:
            conn.close()
        _version += 1
    return {"key": key, "value": value, "updated_at": ts}


def forget(key: str) -> bool:
    """删除一条全局记忆；返回是否命中。"""
    global _version
    key = (key or "").strip()
    if not key:
        return False
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("DELETE FROM profile WHERE key = ?", (key,))
            conn.commit()
            deleted = cur.rowcount > 0
        finally:
            conn.close()
        if deleted:
            _version += 1
    return deleted


def clear() -> None:
    """清空全部记忆（测试/重置用）。"""
    global _version
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM profile")
            conn.commit()
        finally:
            conn.close()
        _version += 1


def list_memory() -> list[dict]:
    """列出全部记忆（最新更新在前）。"""
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM profile ORDER BY updated_at DESC, key"
            ).fetchall()
        finally:
            conn.close()
    return [{"key": k, "value": v, "updated_at": t} for k, v, t in rows]


def build_prompt_section() -> str:
    """生成注入 system prompt 的全局记忆段；无记忆返回空串。"""
    items = list_memory()[:_MAX_INJECT]
    if not items:
        return ""
    lines = [
        "## 全局记忆（对用户的长期记忆，跨会话保留）",
        "以下是你从历史对话中记住的、关于用户的稳定信息。回答时自然运用：",
        "- 若与用户当前说法冲突，以当前说法为准，并调用 remember 更新记忆",
        "- 这些信息属于用户画像，回答时自然运用即可，不要向用户复述记忆元信息",
        "",
    ]
    for it in items:
        lines.append(f"- {it['key']}：{it['value']}")
    return "\n".join(lines)
