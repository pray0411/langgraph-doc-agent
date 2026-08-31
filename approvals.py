"""高危命令用户批准登记。

问题背景：此前 run_command 的 confirmed=True 是模型自填参数，
后端无独立验证，模型可以绕过用户确认直接执行高危命令。

设计：前端弹窗确认后，调用 /api/approve 登记「命令哈希 → 批准记录」；
run_command 执行高危命令时，confirmed=True 必须命中已登记的命令哈希，
且一次性消费（用后即删），防止模型用同一批准重复执行任意命令。

仅内存存储（进程内有效），重启后清空——符合"每次高危操作都要用户确认"的语义。
"""
import hashlib
import threading
import time

_approvals: dict[str, float] = {}  # command_hash -> approved_at
_lock = threading.Lock()
_APPROVAL_TTL = 300  # 秒：批准 5 分钟内有效


def _hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def approve(command: str) -> bool:
    """登记一条命令的批准（由前端用户确认后调用）。"""
    with _lock:
        _approvals[_hash(command)] = time.time()
    return True


def is_approved(command: str) -> bool:
    """检查命令是否已被用户批准；命中则消费（用后即删）。"""
    key = _hash(command)
    with _lock:
        rec = _approvals.get(key)
        if rec is None:
            return False
        # 过期检查
        if time.time() - rec > _APPROVAL_TTL:
            del _approvals[key]
            return False
        # 一次性消费：批准只对"这一条命令"有效一次
        del _approvals[key]
        return True


def clear() -> None:
    with _lock:
        _approvals.clear()
