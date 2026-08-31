"""交互式终端会话管理：启动子进程、收发 stdin/stdout、终止。

前端通过轮询 API 使用：
- start(command) -> session_id
- send_input(session_id, text)
- poll(session_id) -> 新输出增量
- stop(session_id)

安全：与 run_command 共用黑名单；交互式运行由用户主动点击触发（视为已确认），
不做高危确认（用户就在终端前）。进程只在 WRITE_DIR 内启动。
"""
import os
import queue
import re
import subprocess
import threading
import uuid
from pathlib import Path

from config import WRITE_DIR

# 复用 run_command 的黑名单（从 tools 导入避免重复定义）
from tools import _BLOCKED_PATTERNS

# session: {session_id: {"proc", "out_q", "poll_offset", "done"}}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

_MAX_OUTPUT_BUFFER = 100_000  # 单会话输出缓冲上限（防内存膨胀）


def _blocked(command: str) -> str | None:
    """命中黑名单返回拦截原因，否则 None。"""
    for pat in _BLOCKED_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return f"⛔ 已拦截：命令包含破坏性操作（{pat}）"
    return None


def start(command: str) -> dict:
    """启动一个交互式子进程，返回 {session_id} 或 {error}。"""
    blocked = _blocked(command)
    if blocked:
        return {"error": blocked}

    cwd = str(Path(WRITE_DIR).resolve())
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    try:
        proc = subprocess.Popen(
            command, cwd=cwd, shell=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
            bufsize=1,  # 行缓冲，输出及时可见
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"启动失败: {exc}"}

    session_id = uuid.uuid4().hex
    out_q: queue.Queue = queue.Queue()
    done = threading.Event()

    def _reader():
        """读 stdout 直到 EOF，放入队列。"""
        try:
            for line in iter(proc.stdout.readline, ""):
                out_q.put(line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            done.set()
            out_q.put(None)  # EOF 哨兵

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    with _sessions_lock:
        _sessions[session_id] = {
            "proc": proc,
            "out_q": out_q,
            "offset": 0,
            "done": done,
            "buffer": [],
        }
    return {"session_id": session_id}


def send_input(session_id: str, text: str) -> dict:
    """向进程 stdin 写入一行输入。"""
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if not sess:
        return {"error": "会话不存在"}
    proc = sess["proc"]
    if proc.poll() is not None:
        return {"error": "进程已结束"}
    try:
        proc.stdin.write(text + "\n")
        proc.stdin.flush()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"写入失败: {exc}"}


def poll(session_id: str) -> dict:
    """返回自上次 poll 以来的新输出增量。"""
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if not sess:
        return {"error": "会话不存在"}

    new_lines = []
    while True:
        try:
            line = sess["out_q"].get_nowait()
        except queue.Empty:
            break
        if line is None:
            break
        new_lines.append(line)
        sess["buffer"].append(line)
    # 缓冲上限保护
    if len(sess["buffer"]) > _MAX_OUTPUT_BUFFER:
        overflow = len(sess["buffer"]) - _MAX_OUTPUT_BUFFER
        del sess["buffer"][:overflow]

    return {
        "lines": new_lines,
        "running": sess["proc"].poll() is None,
        "exit_code": sess["proc"].poll(),
    }


def stop(session_id: str) -> dict:
    """终止进程并清理会话。"""
    with _sessions_lock:
        sess = _sessions.pop(session_id, None)
    if not sess:
        return {"ok": True}
    proc = sess["proc"]
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


def list_active() -> int:
    with _sessions_lock:
        return len(_sessions)
