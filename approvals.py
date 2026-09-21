"""高危命令用户批准登记。

问题背景：此前 run_command 的 confirmed=True 是模型自填参数，
后端无独立验证，模型可以绕过用户确认直接执行高危命令。

设计：前端弹窗确认后，调用 /api/approve 登记「命令哈希 → 批准记录」；
run_command 执行高危命令时，confirmed=True 必须命中已登记的命令哈希，
且一次性消费（用后即删），防止模型用同一批准重复执行任意命令。

仅内存存储（进程内有效），重启后清空——符合"每次高危操作都要用户确认"的语义。

关于"常数时间比较"的说明（一次复查修正）：本模块用 dict 查找而非字符串
`==`，查找键是命令的 SHA-256 摘要。摘要是**由命令内容推导出来的，不是秘密**
（秘密是命令本身，而命令必须由用户在前端确认过）。因此这里不存在
"靠比较耗时逐字节猜哈希"的时间侧信道，`hmac.compare_digest` 并不适用也
没有收益——真正需要常数时间比较的是"拿用户输入的 token 与保存的 token 比对"，
那种地方必须用 compare_digest（存在于 server 的 token 校验场景）。
"""
import hashlib
import threading
import time

from logging_setup import audit, get_logger

logger = get_logger(__name__)

_approvals: dict[str, float] = {}  # command_hash -> approved_at
_lock = threading.Lock()
_APPROVAL_TTL = 300  # 秒：批准 5 分钟内有效


def _hash(command: str) -> str:
    """命令摘要（带固定域前缀，避免与其他地方对同一字符串的裸 SHA-256 混用）。"""
    return hashlib.sha256(b"pray-approval:" + command.encode("utf-8")).hexdigest()


def approve(command: str) -> bool:
    """登记一条命令的批准（由前端用户确认后调用）。"""
    key = _hash(command)
    with _lock:
        _approvals[key] = time.time()
    logger.info("登记高危命令批准 hash=%s", key[:12])
    audit("approval_granted", command_hash=key[:16], command_preview=command[:120],
          ttl_s=_APPROVAL_TTL)
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
            logger.info("批准已过期 hash=%s", key[:12])
            audit("approval_expired", command_hash=key[:16], ttl_s=_APPROVAL_TTL)
            return False
        # 一次性消费：批准只对"这一条命令"有效一次
        del _approvals[key]
    audit("approval_consumed", command_hash=key[:16], command_preview=command[:120])
    return True


def clear() -> None:
    with _lock:
        _approvals.clear()


def reset_state() -> None:
    """重置批准表（测试复位用）。"""
    clear()


def pending_count() -> int:
    """仍有效（未过期、未消费）的批准条数（观测/自检用）。"""
    now = time.time()
    with _lock:
        return sum(1 for ts in _approvals.values() if now - ts <= _APPROVAL_TTL)
