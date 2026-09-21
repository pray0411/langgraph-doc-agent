"""交互式终端会话管理：启动子进程、收发 stdin/stdout、终止。

前端通过轮询 API 使用：
- start(command) -> session_id
- send_input(session_id, text)
- poll(session_id) -> 新输出增量
- stop(session_id)

安全：与 run_command **共用同一个分类器**（`tools.classify_command`，默认拒绝），
不再自己维护一份必然漂移的名单。前端 ▶ 触发**不等于已获批准**：高危命令要求
用户在看到完整命令后确认 —— 服务端签发 nonce（/api/run/prepare），用户确认后
经 /api/confirm 换取一次性批准，`start()` 仍会显式校验批准记录。
进程只在 WRITE_DIR 内启动。

关于配置读取方式（一次修复）：原实现在模块顶部 `from config import WRITE_DIR`
—— 那是**导入时快照**。`config` 是运行时可变的（测试会 monkeypatch，用户可通过
`.env` 改），快照意味着"改配置不生效"，而且会让测试结果依赖导入顺序：
某个用例先把 WRITE_DIR 指到临时目录，之后所有已导入 runterm 的用例都跟着变。
现在改为在调用点导入（`from config import WRITE_DIR` 写在函数体内），
每次执行都从 sys.modules 取当前模块，配置改动即时生效。
"""
import os
import queue
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

# 资源上限（外部评审指出：原实现会话数、输出队列、单行长度三者皆无上限，可被打满）
_MAX_SESSIONS = 8             # 并发会话数
_MAX_QUEUE = 2_000            # 单会话待取输出行数上限（有界队列，防堆积）
_MAX_LINE_CHARS = 8_192       # 单行读取上限（防一行超长输出吃满内存）
_MAX_OUTPUT_BUFFER = 100_000  # 单会话已读缓冲上限（防内存膨胀）


def _classify(command: str) -> tuple[str, str]:
    """取命令分级（调用点导入，避免导入时快照）。

    修复的问题：原实现自己维护一套黑名单/高危判定，与 `tools.run_command`
    是**两份会各自漂移的副本**。外部评审已指出 tools 侧的判定漏掉
    `python xxx.py`；如果这里继续留一份副本，同样的问题要修两遍、
    且必然有一次会忘。现在两条路径共用同一个分类器。
    """
    from tools import classify_command

    return classify_command(command)


def classify(command: str) -> tuple[str, str]:
    """公开的分级入口（/api/run/prepare 使用）。

    对外暴露而不是让 server 直接 import tools：保持"终端与 run_command 用同一套
    判定"这件事只有一处实现，避免 server 又长出第三份规则。
    """
    return _classify(command)


def start(command: str) -> dict:
    """启动一个交互式子进程，返回 {session_id} 或 {error}。"""
    from config import WRITE_DIR

    level, reason = _classify(command)
    if level == "blocked":
        audit("runterm_start", outcome="blocked", command_preview=command[:120], detail=reason)
        return {"error": f"⛔ 已拦截：命令{reason}"}
    if level == "high":
        # 交互终端由前端 ▶ 触发，前端会先经 /api/run/prepare 拿到**服务端签发**
        # 的 nonce，用户看到完整命令并确认后由 /api/confirm 换取一次性批准。
        # 这里仍显式校验批准记录，不因"用户点过按钮"就放行任意命令——
        # 用户点的是一段模型生成的代码，不等于审查过这条命令。
        # （旧实现的前端在打开终端时会**自动**调自助登记接口把命令登记成已批准，
        #  于是这道校验在真实使用路径上永远为真地通过 —— 外部评审 P0-1。）
        from approvals import is_approved

        if not is_approved(command):
            audit("runterm_start", outcome="need_confirm", command_preview=command[:120],
                  detail=reason)
            return {"error": f"NEED_CONFIRM {reason}，需要用户确认后重试"}

    # 会话数上限：每个会话都是一个真实进程 + 读线程 + 队列。没有上限时，
    # 反复调用 /api/run/start 就能把进程/句柄/内存打满（外部评审指出）。
    with _sessions_lock:
        alive = len(_sessions)
    if alive >= _MAX_SESSIONS:
        audit("runterm_start", outcome="too_many_sessions", active=alive)
        return {"error": f"并发终端会话已达上限（{_MAX_SESSIONS}），请先关闭已有会话"}

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
    # 有界队列（外部评审指出：`_MAX_QUEUE` 此前只定义、未接线 ——
    # 队列仍是 `queue.Queue()` 无界，前端一旦停止轮询或读得慢，
    # 读线程就会把子进程产出全部堆进内存直到 OOM）。
    # 满了丢**最旧**一行：保留最新输出（用户看到的是进度尾部），
    # 丢弃行数累计到 dropped[0]，由 poll() 上报，前端可提示"输出过快已省略"。
    out_q: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE)
    dropped = [0]
    done = threading.Event()

    def _reader():
        """读 stdout 直到 EOF，放入有界队列；队满丢最旧并计数。"""
        try:
            for line in iter(proc.stdout.readline, ""):
                # 单行上限（同一处未接线的常量）：`print("x" * 10**8)` 这类
                # **无换行**的超长输出会让 readline 一次性读进整行，
                # 单行就能吃掉内存。超限则截断并显式标注，而不是静默。
                if len(line) > _MAX_LINE_CHARS:
                    line = line[:_MAX_LINE_CHARS] + f"…（本行超 {_MAX_LINE_CHARS} 字符已截断）\n"
                while True:
                    try:
                        out_q.put_nowait(line)
                        break
                    except queue.Full:
                        try:
                            out_q.get_nowait()  # 腾一格：丢最旧
                        except queue.Empty:
                            pass
                        dropped[0] += 1
                        if dropped[0] > _MAX_QUEUE:
                            break  # 极端情况下避免死循环（消费端完全不取）
        except Exception:  # noqa: BLE001
            logger.exception("终端会话读取失败 session=%s", session_id)
        finally:
            done.set()
            # EOF 哨兵必须能落队，否则 poll() 永远看不到"已结束"：
            # 队满时先腾格再放，仍失败则放弃（此时消费端确实没在取）。
            while True:
                try:
                    out_q.put_nowait(None)
                    break
                except queue.Full:
                    try:
                        out_q.get_nowait()
                    except queue.Empty:
                        break

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    with _sessions_lock:
        _sessions[session_id] = {
            "proc": proc,
            "out_q": out_q,
            "offset": 0,
            "done": done,
            "buffer": [],
            "dropped": dropped,
            "dropped_reported": 0,
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

    # 上报"本次 poll 期间因队列满被丢弃的行数"（增量，不重复计数）。
    # 有界队列是**静默丢数据**的设计，必须让调用方知道丢过东西，
    # 否则前端会以为"输出就这么多"，把不完整当成完整。
    total_dropped = sess.get("dropped", [0])[0]
    reported = sess.get("dropped_reported", 0)
    new_dropped = max(0, total_dropped - reported)
    sess["dropped_reported"] = total_dropped

    return {
        "lines": new_lines,
        "running": sess["proc"].poll() is None,
        "exit_code": sess["proc"].poll(),
        "dropped": new_dropped,
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
