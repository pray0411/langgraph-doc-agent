"""交互式终端会话管理：启动子进程、收发 stdin/stdout、终止。

前端通过轮询 API 使用：
- start(command) -> session_id
- send_input(session_id, text)
- poll(session_id) -> 新输出增量
- stop(session_id)

安全：与 run_command 共用黑名单；交互式运行由用户主动点击触发（视为已确认），
不做高危确认（用户就在终端前）。进程只在 WRITE_DIR 内启动。

关于配置读取方式（一次修复）：原实现在模块顶部 `from config import WRITE_DIR`
—— 那是**导入时快照**。`config` 是运行时可变的（测试会 monkeypatch，用户可通过
`.env` 改），快照意味着"改配置不生效"，而且会让测试结果依赖导入顺序：
某个用例先把 WRITE_DIR 指到临时目录，之后所有已导入 runterm 的用例都跟着变。
现在改为在调用点导入（`from config import WRITE_DIR` 写在函数体内），
每次执行都从 sys.modules 取当前模块，配置改动即时生效。
"""
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from logging_setup import audit, get_logger

logger = get_logger(__name__)

# session: {session_id: {"proc", "out_q", "offset", "done", "buffer", "last_active"}}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

_MAX_OUTPUT_BUFFER = 100_000  # 单会话输出缓冲上限（防内存膨胀）


def _load_patterns() -> tuple[list[str], list[str]]:
    """取命令黑名单与高危名单（调用点导入，避免导入时快照）。"""
    from tools import _BLOCKED_PATTERNS, _HIGH_RISK_PATTERNS

    return _BLOCKED_PATTERNS, _HIGH_RISK_PATTERNS


def _blocked(command: str) -> str | None:
    """命中黑名单返回拦截原因，否则 None。"""
    blocked_patterns, _ = _load_patterns()
    for pat in blocked_patterns:
        if re.search(pat, command, re.IGNORECASE):
            return f"⛔ 已拦截：命令包含破坏性操作（{pat}）"
    return None


def start(command: str) -> dict:
    """启动一个交互式子进程，返回 {session_id} 或 {error}。"""
    from config import WRITE_DIR

    blocked = _blocked(command)
    if blocked:
        audit("runterm_start", outcome="blocked", command_preview=command[:120])
        return {"error": blocked}
    # 高危命令（删除/移动/安装包/联网下载等）须用户批准（approvals 登记）
    _, high_risk_patterns = _load_patterns()
    if any(re.search(p, command, re.IGNORECASE) for p in high_risk_patterns):
        from approvals import is_approved

        if not is_approved(command):
            audit("runterm_start", outcome="need_confirm", command_preview=command[:120])
            return {"error": "NEED_CONFIRM 高危命令需要用户确认后重试"}

    cwd = Path(WRITE_DIR).resolve()
    # 目录不存在时让 Popen 抛 WinError 267（"目录名称无效"）是一类很难读懂的失败；
    # 这里主动补齐，把错误面收敛掉。
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"error": f"工作目录不可用: {exc}"}

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    started = time.time()
    try:
        proc = subprocess.Popen(
            command, cwd=str(cwd), shell=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
            bufsize=1,  # 行缓冲，输出及时可见
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("终端会话启动失败：%s", command[:120])
        audit("runterm_start", outcome="spawn_error", command_preview=command[:120],
              error=str(exc)[:200])
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
            logger.exception("终端会话读取失败 session=%s", session_id)
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
            "last_active": time.time(),
        }
    audit("runterm_start", outcome="started", session_id=session_id, pid=proc.pid,
          command_preview=command[:120],
          duration_ms=int((time.time() - started) * 1000))
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
    sess["last_active"] = time.time()
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
    sess["last_active"] = time.time()

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
    """终止进程并清理会话。

    修复的资源泄漏：原实现只 kill 子进程，**从不关闭 stdin/stdout 管道、也不
    `wait()` 回收**。后果是每开一次交互终端就泄漏 2 个文件描述符，直到 GC 才释放；
    长跑的服务端会持续抬高句柄数，测试里则表现为
    `ResourceWarning: unclosed file <_io.TextIOWrapper name=12 ...>`。
    这里显式关闭管道并回收进程，把"靠 GC 兜底"改成"确定性释放"。
    """
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
        logger.exception("终止终端会话失败 session=%s", session_id)

    # 关闭管道（读线程可能正阻塞在 readline，会被解锁并抛异常后退出）
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:  # noqa: BLE001 - 关闭失败不应影响上层
            pass
    # 回收僵尸进程（Windows 上 taskkill 之后仍需 wait 才能释放句柄）
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass

    audit("runterm_stop", session_id=session_id, pid=getattr(proc, "pid", None),
          exit_code=proc.poll())
    return {"ok": True}


def reset_state() -> None:
    """终止所有会话（测试复位用）。

    不清理会在用例之间留下活着的子进程：既是资源泄漏，
    也会让"当前活跃会话数"这类断言随执行顺序变化。
    """
    with _sessions_lock:
        ids = list(_sessions)
    for sid in ids:
        stop(sid)


def list_active() -> int:
    with _sessions_lock:
        return len(_sessions)


# 闲置超时：会话超过该时长无轮询即自动终止（防内存泄漏）
_STALE_TIMEOUT = 600  # 秒（10 分钟）


def sweep_stale() -> int:
    """清理闲置超时的会话（进程终止 + 记录移除），返回清理数量。"""
    now = time.time()
    stale_ids = []
    with _sessions_lock:
        for sid, sess in _sessions.items():
            if now - sess.get("last_active", now) > _STALE_TIMEOUT:
                stale_ids.append(sid)
    for sid in stale_ids:
        stop(sid)
    return len(stale_ids)
